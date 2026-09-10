"""The threaded synchronous driver: `SyncRunner` and the `Run` handle.

Each invocation uses two background threads and the caller's thread:

1. A stdout reader thread, feeding raw chunks through `LineAssembler`, turning
   each complete line into an `Event` with `events.event_from_line`, and
   putting it on a bounded `queue.Queue`. The bound is the backpressure: a
   slow consumer stalls this thread, which stalls the read, which stalls the
   child.
2. A stderr reader thread, which must never block. It drains stderr into a
   bounded `StderrRingBuffer` and calls `on_stderr` (if set) from this thread,
   off the bounded queue, so stderr keeps draining even when the consumer
   never reads it. Both pipes have to drain at all times or the child blocks
   on whichever fills up first.
3. The calling thread, consuming the queue through `Run`: it iterates the
   events and drives cancellation.

When a reader thread reaches the end of its pipe it puts a marker on the
queue (`STDOUT_DONE`, `STDERR_DONE`). `proc.wait()` is called only after both
markers arrive, so the exit status is collected once both pipes are known to
be closed instead of after a guessed delay. Cancelling or timing out ends the
wait as well. Pipe file descriptors are closed in a `finally` block, so
cleanup still runs when a consumer leaves a `for ev in run:` loop early.
"""

from __future__ import annotations

import codecs
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager

from ._signals import kill_hard, popen_kwargs, send_graceful
from .config import RunSpec
from .events import (
    DEFAULT_MAX_LINE_BYTES,
    AssembledLine,
    DiagnosticEvent,
    Event,
    LineAssembler,
    event_from_line,
)
from .exceptions import BinaryNotFoundError, TerrapyError, TerrapyTimeoutError, UsageError
from .models import CommandResult, Diagnostic
from .runner import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_QUEUE_MAXSIZE,
    DEFAULT_STDERR_TAIL_BYTES,
    STDERR_DONE,
    STDOUT_DONE,
    StderrRingBuffer,
    escalate_after,
)

_log = logging.getLogger("terrapy.runner")

_POLL_INTERVAL = 0.5
_READ_CHUNK_SIZE = 65536
_MIN_WAIT = 0.05


class Run(AbstractContextManager["Run"], Iterator[Event]):
    """A handle to one running invocation: an iterator of `Event` and a
    context manager guaranteeing cleanup.

    Use as `with client.plan_stream(...) as run: for ev in run: ...`. The
    context manager's `__exit__` is the single place cleanup happens: it
    cancels the process if it is still running, drains any events the consumer
    did not (so the background threads are never left blocked on a full queue),
    and waits for the process to exit. A consumer can therefore leave the loop
    early, by `break` or by an exception, without stranding the process or the
    threads.
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        spec: RunSpec,
        *,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        stderr_cap_bytes: int = DEFAULT_STDERR_TAIL_BYTES,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self.pid = proc.pid
        self.args = list(spec.argv)
        self.returncode: int | None = None

        self._proc = proc
        self._spec = spec
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_maxsize)
        self._stderr_ring = StderrRingBuffer(cap_bytes=stderr_cap_bytes)
        self._max_line_bytes = max_line_bytes

        self._stdout_lines: list[str] = []
        self._stdout_truncated = False
        self._diagnostics: list[Diagnostic] = []

        self._done_stdout = False
        self._done_stderr = False
        self._finished = False

        self._start_time = time.monotonic()
        self._last_event_at = self._start_time
        self._duration = 0.0

        self._timeout = spec.timeout
        self._inactivity_timeout = spec.inactivity_timeout
        self._timed_out = False

        self._cancel_lock = threading.Lock()
        self._cancel_requested = False
        self._graceful_sent_at: float | None = None
        self._grace = DEFAULT_GRACE_SECONDS
        self._hard_sent = False

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name=f"terrapy-stdout-{proc.pid}", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name=f"terrapy-stderr-{proc.pid}", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    # -- reader threads -------------------------------------------------

    def _read_stdout(self) -> None:
        assembler = LineAssembler(max_line_bytes=self._max_line_bytes)
        try:
            stream = self._proc.stdout
            assert stream is not None
            while True:
                try:
                    chunk = stream.read(_READ_CHUNK_SIZE)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                for assembled in assembler.feed(chunk):
                    self._handle_stdout_line(assembled)
            trailing = assembler.flush()
            if trailing is not None:
                self._handle_stdout_line(trailing)
        finally:
            try:
                if self._proc.stdout is not None:
                    self._proc.stdout.close()
            except OSError:
                pass
            self._queue.put(STDOUT_DONE)

    def _handle_stdout_line(self, assembled: AssembledLine) -> None:
        event = event_from_line(assembled)
        if assembled.truncated:
            # The assembler discards the over-long text, so it cannot be
            # recovered here. Record it and raise from result(): dropping it
            # instead would return an empty payload that looks like success.
            self._stdout_truncated = True
        else:
            self._stdout_lines.append(assembled.text)
        if isinstance(event, DiagnosticEvent):
            self._diagnostics.append(event.diagnostic)
        self._queue.put(event)  # blocks here: this is the backpressure point

    def _read_stderr(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            stream = self._proc.stderr
            assert stream is not None
            while True:
                try:
                    chunk = stream.read(_READ_CHUNK_SIZE)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    self._stderr_ring.append(text)
                    if self._spec.on_stderr is not None:
                        try:
                            self._spec.on_stderr(text)
                        except Exception:
                            _log.exception("on_stderr callback raised")
        finally:
            try:
                if self._proc.stderr is not None:
                    self._proc.stderr.close()
            except OSError:
                pass
            self._queue.put(STDERR_DONE)

    # -- iteration --------------------------------------------------------

    def __iter__(self) -> Run:
        return self

    def __next__(self) -> Event:
        while True:
            if self._done_stdout and self._done_stderr:
                self._finish()
                if self._timed_out:
                    raise self._build_timeout_error()
                raise StopIteration
            # Checked every pass, not only on an idle tick: a run that keeps
            # emitting events never raises queue.Empty, so a deadline tested
            # only in that branch would never fire.
            self._on_wait_timeout()
            wait_timeout = self._next_wait_timeout()
            try:
                item = self._queue.get(timeout=wait_timeout)
            except queue.Empty:
                self._on_wait_timeout()
                continue
            if item is STDOUT_DONE:
                self._done_stdout = True
                continue
            if item is STDERR_DONE:
                self._done_stderr = True
                continue
            self._last_event_at = time.monotonic()
            assert isinstance(item, Event)
            return item

    def _next_wait_timeout(self) -> float:
        if self._timeout is None and self._inactivity_timeout is None:
            return _POLL_INTERVAL
        candidates = [_POLL_INTERVAL]
        now = time.monotonic()
        if self._timeout is not None:
            candidates.append(max(0.0, self._start_time + self._timeout - now))
        if self._inactivity_timeout is not None:
            candidates.append(max(0.0, self._last_event_at + self._inactivity_timeout - now))
        return max(_MIN_WAIT, min(candidates))

    def _on_wait_timeout(self) -> None:
        now = time.monotonic()
        if not self._timed_out:
            if self._timeout is not None and now - self._start_time >= self._timeout:
                self._timed_out = True
                self.cancel()
                return
            if (
                self._inactivity_timeout is not None
                and now - self._last_event_at >= self._inactivity_timeout
            ):
                self._timed_out = True
                self.cancel()
                return
        self._service_cancel()

    def _build_timeout_error(self) -> TerrapyTimeoutError:
        elapsed = self._timeout if self._timeout is not None else self._inactivity_timeout
        return TerrapyTimeoutError(
            f"invocation exceeded its timeout after {elapsed}s and was terminated",
            command=self._spec.argv,
            timeout=elapsed or 0.0,
            stdout=self._stdout_text(),
            stderr=self.stderr_text(),
            diagnostics=tuple(self._diagnostics),
        )

    # -- cancellation -------------------------------------------------------

    def cancel(self, *, grace: float | None = None) -> None:
        """Idempotent: the first call sends the graceful signal; later calls
        (including ones made implicitly by timeout handling or `__exit__`)
        escalate to a hard kill once `grace` seconds have passed since the
        graceful signal was sent."""
        with self._cancel_lock:
            if not self._cancel_requested:
                self._cancel_requested = True
                self._grace = grace if grace is not None else DEFAULT_GRACE_SECONDS
                send_graceful(self._proc)
                self._graceful_sent_at = time.monotonic()
                return
            if grace is not None:
                self._grace = grace
            self._escalate_if_due()

    def _service_cancel(self) -> None:
        if self._cancel_requested:
            with self._cancel_lock:
                self._escalate_if_due()

    def _escalate_if_due(self) -> None:
        if self._hard_sent:
            return
        if escalate_after(self._graceful_sent_at, self._grace, time.monotonic()):
            kill_hard(self._proc)
            self._hard_sent = True

    # -- completion -----------------------------------------------------

    def wait(self, timeout: float | None = None) -> int:
        """Drain remaining events (without returning them) and return the
        exit code. `timeout` here is local to this call, independent of any
        `timeout`/`inactivity_timeout` configured on the invocation itself."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() >= deadline and self._proc.poll() is None:
                self.cancel()
                raise TerrapyTimeoutError(
                    f"wait() timed out after {timeout}s",
                    command=self._spec.argv,
                    timeout=timeout or 0.0,
                    stdout=self._stdout_text(),
                    stderr=self.stderr_text(),
                    diagnostics=tuple(self._diagnostics),
                )
            try:
                next(self)
            except StopIteration:
                break
        assert self.returncode is not None
        return self.returncode

    def stderr_text(self) -> str:
        """The bounded tail of decoded stderr text collected so far."""
        return self._stderr_ring.text()

    def _stdout_text(self) -> str:
        return "\n".join(self._stdout_lines)

    def result(self) -> CommandResult:
        """The generic result, valid only once the run has completed (raises
        `UsageError` otherwise). The client builds the typed subclasses from
        this plus the events it collected while iterating."""
        if not self._finished:
            raise UsageError("Run.result() is only available after the run has completed")
        if self._stdout_truncated:
            raise TerrapyError(
                f"a stdout line exceeded {self._max_line_bytes} bytes and was discarded; "
                f"{' '.join(self._spec.argv)} produced output this driver cannot buffer"
            )
        assert self.returncode is not None
        return CommandResult(
            command=tuple(self._spec.argv),
            exit_code=self.returncode,
            stdout=self._stdout_text(),
            stderr=self.stderr_text(),
            duration=self._duration,
            diagnostics=tuple(self._diagnostics),
        )

    # -- completion / cleanup ----------------------------------------------

    def _finish(self) -> None:
        if self._finished:
            return
        self._stdout_thread.join(timeout=30)
        self._stderr_thread.join(timeout=30)
        try:
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_hard(self._proc)
            self._proc.wait()
        self._duration = time.monotonic() - self._start_time
        self.returncode = self._proc.returncode
        self._finished = True

    def _drain_until_done(self) -> None:
        while not (self._done_stdout and self._done_stderr):
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._service_cancel()
                continue
            if item is STDOUT_DONE:
                self._done_stdout = True
            elif item is STDERR_DONE:
                self._done_stderr = True

    def _wait_out_grace(self) -> None:
        if not self._cancel_requested or self._graceful_sent_at is None:
            return
        remaining = max(0.0, self._graceful_sent_at + self._grace - time.monotonic())
        try:
            self._proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
        with self._cancel_lock:
            self._escalate_if_due()

    def _close_pipes(self) -> None:
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self._proc.poll() is None:
                self.cancel()
            # Drain before waiting: the reader threads may be parked in
            # queue.put on a full queue, which stops them draining the pipe and
            # blocks the child mid-write, so it would never reach the signal.
            self._drain_until_done()
            self._wait_out_grace()
        finally:
            self._finish()
            self._close_pipes()


class SyncRunner:
    """The real `Runner`: launches a subprocess and returns a live `Run`."""

    def __init__(
        self,
        *,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        stderr_cap_bytes: int = DEFAULT_STDERR_TAIL_BYTES,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self._queue_maxsize = queue_maxsize
        self._stderr_cap_bytes = stderr_cap_bytes
        self._max_line_bytes = max_line_bytes

    def start(self, spec: RunSpec) -> Run:
        try:
            proc = subprocess.Popen(
                list(spec.argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=spec.cwd,
                env=dict(spec.env),
                bufsize=0,
                **popen_kwargs(),
            )
        except OSError as exc:
            raise BinaryNotFoundError(
                f"failed to execute {spec.argv[0]!r}: {exc}", searched=(spec.argv[0],)
            ) from exc
        return Run(
            proc,
            spec,
            queue_maxsize=self._queue_maxsize,
            stderr_cap_bytes=self._stderr_cap_bytes,
            max_line_bytes=self._max_line_bytes,
        )


__all__ = ["Run", "SyncRunner"]
