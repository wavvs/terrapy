"""The sync driver against a real child process (streaming, cancellation,
timeout, and deadlock avoidance from a big stderr write with nobody reading
it), plus the pure `runner.py` helpers those paths depend on."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from terrapy._run_sync import SyncRunner
from terrapy.config import RunSpec
from terrapy.events import MalformedLineEvent
from terrapy.exceptions import (
    BinaryNotFoundError,
    TerrapyError,
    TerrapyTimeoutError,
    UsageError,
)
from terrapy.runner import (
    STDERR_DONE,
    STDOUT_DONE,
    StderrRingBuffer,
    escalate_after,
)

FAKE_CHILD = str(Path(__file__).parent / "fixtures" / "fake_child.py")


def make_spec(*args: str, **kwargs: object) -> RunSpec:
    return RunSpec(
        argv=(sys.executable, FAKE_CHILD, *args),
        cwd=os.getcwd(),
        env=dict(os.environ),
        **kwargs,  # type: ignore[arg-type]
    )


def test_streams_events_in_order() -> None:
    runner = SyncRunner()
    with runner.start(make_spec("normal")) as run:
        events = list(run)
    assert run.returncode == 0
    messages = [e.message for e in events[:5]]
    assert messages == [f"line {i}" for i in range(5)]
    assert events[-1].type == "change_summary"


def test_result_available_after_completion() -> None:
    runner = SyncRunner()
    with runner.start(make_spec("normal")) as run:
        list(run)
        result = run.result()
    assert result.exit_code == 0
    assert "line 0" in result.stdout


def test_result_raises_before_completion() -> None:
    runner = SyncRunner()
    run = runner.start(make_spec("normal"))
    try:
        with pytest.raises(UsageError):
            run.result()
    finally:
        with run:
            list(run)


def test_exit_code_propagates() -> None:
    runner = SyncRunner()
    with runner.start(make_spec("exit_code", "7")) as run:
        list(run)
    assert run.returncode == 7


def test_big_stderr_does_not_deadlock() -> None:
    """Regression test for the deadlock-avoidance invariant: stdout and
    stderr must both drain concurrently even though nothing consumes
    stderr through a callback."""
    runner = SyncRunner()
    started = time.monotonic()
    with runner.start(make_spec("big_stderr")) as run:
        events = list(run)
    elapsed = time.monotonic() - started
    assert elapsed < 15, "big stderr write appears to have deadlocked the run"
    assert run.returncode == 0
    assert len(run.stderr_text()) > 0
    assert "E" in run.result().stderr
    assert events[-1].message == "done"


def test_on_stderr_callback_receives_text() -> None:
    runner = SyncRunner()
    chunks: list[str] = []
    spec = make_spec("big_stderr", on_stderr=chunks.append)
    with runner.start(spec) as run:
        list(run)
    assert "".join(chunks).count("E") >= 256 * 1024


def test_malformed_line_does_not_kill_the_run() -> None:
    runner = SyncRunner()
    with runner.start(make_spec("malformed")) as run:
        events = list(run)
    assert run.returncode == 0
    assert isinstance(events[0], MalformedLineEvent)
    assert events[1].message == "recovered"


def test_timeout_cancels_and_raises() -> None:
    runner = SyncRunner()
    spec = make_spec("hang", timeout=1.0)
    started = time.monotonic()
    with pytest.raises(TerrapyTimeoutError) as excinfo:
        with runner.start(spec) as run:
            for _ in run:
                pass
    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert "before hang" in excinfo.value.stdout


def test_break_out_of_loop_still_cleans_up() -> None:
    """Breaking out of `for ev in run:` early must not hang __exit__ (the
    child process must still be reaped and threads joined)."""
    runner = SyncRunner()
    started = time.monotonic()
    with runner.start(make_spec("hang")) as run:
        for _ in run:
            break
    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert run.returncode is not None


def test_explicit_cancel_escalates_on_a_stubborn_child() -> None:
    """`hang` ignores the graceful signal, so a second cancel must escalate to
    a hard kill rather than wait forever for a clean exit."""
    runner = SyncRunner()
    started = time.monotonic()
    with runner.start(make_spec("hang")) as run:
        for _ev in run:
            run.cancel(grace=0.1)
            run.cancel(grace=0.1)
            break
    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert run.returncode is not None


def test_binary_not_found_raises() -> None:
    runner = SyncRunner()
    spec = RunSpec(argv=("this-binary-does-not-exist-xyz",), cwd=os.getcwd(), env=dict(os.environ))
    with pytest.raises(BinaryNotFoundError):
        runner.start(spec)


def test_oversized_stdout_line_raises_instead_of_returning_empty() -> None:
    """An over-long line is discarded by the assembler and cannot be recovered,
    so result() must raise rather than return an empty payload that callers
    would parse as `{}`."""
    runner = SyncRunner(max_line_bytes=64)
    with runner.start(make_spec("bigline")) as run:
        list(run)
        with pytest.raises(TerrapyError, match="exceeded 64 bytes"):
            run.result()


def test_wait_drains_and_returns_exit_code() -> None:
    with SyncRunner().start(make_spec("exit_code", "3")) as run:
        assert run.wait() == 3
        # wait() consumed the stream, so iteration is already exhausted.
        assert list(run) == []
    assert run.returncode == 3


def test_on_stderr_callback_exception_does_not_kill_the_run() -> None:
    def boom(_text: str) -> None:
        raise RuntimeError("callback is broken")

    with SyncRunner().start(make_spec("big_stderr", on_stderr=boom)) as run:
        events = list(run)
    assert run.returncode == 0
    assert [e.message for e in events] == ["start", "done"]


def test_trailing_line_without_newline_is_still_emitted() -> None:
    with SyncRunner().start(make_spec("no_trailing_newline")) as run:
        events = list(run)
    assert [e.message for e in events] == ["last line"]


# -- runner.py helpers --------------------------------------------------------


def test_ring_buffer_keeps_everything_under_cap() -> None:
    buf = StderrRingBuffer(cap_bytes=100)
    buf.append("hello ")
    buf.append("")          # empty appends are ignored
    buf.append("world")
    assert buf.text() == "hello world"
    assert buf.truncated is False


def test_ring_buffer_evicts_oldest_and_keeps_the_tail() -> None:
    buf = StderrRingBuffer(cap_bytes=10)
    buf.append("aaaaa")
    buf.append("bbbbb")     # exactly at cap
    assert buf.truncated is False
    buf.append("ccccc")     # over cap: the oldest chunk goes
    assert buf.text() == "bbbbbccccc"
    assert buf.truncated is True


def test_ring_buffer_counts_bytes_not_characters() -> None:
    # Two 3-byte characters per chunk exceed a 10-byte cap even though len() is 2.
    buf = StderrRingBuffer(cap_bytes=10)
    buf.append("你好")
    buf.append("世界")
    assert buf.truncated is True
    assert buf.text() == "世界"


def test_sentinels_are_distinct_and_readable() -> None:
    assert STDOUT_DONE is not STDERR_DONE
    assert repr(STDOUT_DONE) == "<STDOUT_DONE>"
    assert repr(STDERR_DONE) == "<STDERR_DONE>"


@pytest.mark.parametrize("sent_at,grace,now,expected", [
    (None, 30.0, 1_000_000.0, False),   # nothing sent yet: never escalate
    (100.0, 30.0, 129.9, False),        # inside the grace window
    (100.0, 30.0, 130.0, True),         # exactly at the boundary
    (100.0, 30.0, 500.0, True),
    (100.0, 0.0, 100.0, True),          # zero grace escalates immediately
])
def test_escalate_after(sent_at, grace, now, expected) -> None:
    assert escalate_after(sent_at, grace, now) is expected
