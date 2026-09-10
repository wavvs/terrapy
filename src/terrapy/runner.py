"""The `Runner` protocol, and the stateless helpers the driver uses.

The client classes run commands only by calling `runner.start(spec)`, so any
object implementing this `Protocol` can take the place of `SyncRunner`.
`FakeRunner` in `tests/conftest.py` does exactly that, which is how the client
tests run without an installed `terraform` or `tofu` binary.

`StderrRingBuffer` holds the last `cap_bytes` of stderr. `escalate_after`
reports whether a graceful cancel has outlived its grace window. `STDOUT_DONE`
and `STDERR_DONE` are the values a reader thread puts on the queue when its
pipe reaches end of file. None of them holds state belonging to a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ._run_sync import Run
    from .config import RunSpec

DEFAULT_STDERR_TAIL_BYTES = 1024 * 1024
DEFAULT_GRACE_SECONDS = 30.0
DEFAULT_QUEUE_MAXSIZE = 10_000


@runtime_checkable
class Runner(Protocol):
    """What the client calls to execute one command.

    A real implementation spawns a child process and streams its output. The
    `FakeRunner` used in tests replays predefined or recorded output for a matched
    argv, with no subprocess involved.
    """

    def start(self, spec: RunSpec) -> Run: ...


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


STDOUT_DONE = _Sentinel("STDOUT_DONE")
STDERR_DONE = _Sentinel("STDERR_DONE")


class StderrRingBuffer:
    """The most recent `cap_bytes` of decoded stderr text.

    Stderr must always be drained so the child never blocks on a full pipe,
    even when nothing is consuming it (see `_run_sync.py`'s deadlock-avoidance
    note). Draining without consuming is safe here because memory stays bounded
    at `cap_bytes` no matter how much stderr the child writes: only the most
    recent tail is kept.
    """

    def __init__(self, cap_bytes: int = DEFAULT_STDERR_TAIL_BYTES) -> None:
        # Each chunk is stored with its byte size, so a trim subtracts a
        # known number instead of re-encoding the dropped chunk.
        self._chunks: list[tuple[str, int]] = []
        self._size = 0
        self._cap = cap_bytes
        self._truncated = False

    def append(self, text: str) -> None:
        if not text:
            return
        nbytes = len(text.encode("utf-8", errors="replace"))
        self._chunks.append((text, nbytes))
        self._size += nbytes
        self._trim()

    def _trim(self) -> None:
        while self._size > self._cap and self._chunks:
            _, dropped_size = self._chunks.pop(0)
            self._size -= dropped_size
            self._truncated = True

    def text(self) -> str:
        return "".join(text for text, _ in self._chunks)

    @property
    def truncated(self) -> bool:
        return self._truncated


def escalate_after(graceful_sent_at: float | None, grace: float, now: float) -> bool:
    """Whether the hard kill is now due: a graceful signal was sent and `grace`
    seconds have passed since."""
    return graceful_sent_at is not None and now >= graceful_sent_at + grace


__all__ = [
    "Runner",
    "STDOUT_DONE",
    "STDERR_DONE",
    "StderrRingBuffer",
    "escalate_after",
    "DEFAULT_STDERR_TAIL_BYTES",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_QUEUE_MAXSIZE",
]
