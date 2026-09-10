"""Signal delivery and process-group isolation. Both platform branches are
exercised by faking `sys.platform`, because the POSIX path is unreachable on
Windows and vice versa, and a swallowed-exception bug here would otherwise
only show up as a hung cancel in production."""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

from terrapy import _signals


class FakeProc:
    """Records signals instead of delivering them."""

    def __init__(self, *, pid: int = 4242, raises: BaseException | None = None) -> None:
        self.pid = pid
        self.signals: list[int] = []
        self.killed = False
        self._raises = raises

    def send_signal(self, sig: int) -> None:
        if self._raises is not None:
            raise self._raises
        self.signals.append(sig)

    def kill(self) -> None:
        if self._raises is not None:
            raise self._raises
        self.killed = True


@pytest.fixture
def posix(monkeypatch):
    """Make the POSIX branch reachable on any host.

    Windows has neither `os.killpg`/`os.getpgid` nor `signal.SIGKILL`, and
    Python resolves `os.killpg` before evaluating its arguments, so all three
    must exist before the branch can run at all. Returns the recorded calls.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(_signals.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(_signals.os, "killpg",
                        lambda pgid, sig: seen.append((pgid, sig)), raising=False)
    monkeypatch.setattr(_signals.signal, "SIGKILL", getattr(signal, "SIGKILL", 9), raising=False)
    return seen


# -- popen_kwargs -------------------------------------------------------------


def test_popen_kwargs_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert _signals.popen_kwargs() == {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
    }


def test_popen_kwargs_posix(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _signals.popen_kwargs() == {"start_new_session": True}


# -- send_graceful ------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "CTRL_BREAK_EVENT"), reason="windows-only signal")
def test_send_graceful_windows_uses_ctrl_break(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    proc = FakeProc()
    _signals.send_graceful(proc)
    assert proc.signals == [signal.CTRL_BREAK_EVENT]


@pytest.mark.skipif(not hasattr(signal, "CTRL_BREAK_EVENT"), reason="windows-only signal")
def test_send_graceful_windows_swallows_dead_process(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    _signals.send_graceful(FakeProc(raises=ProcessLookupError()))  # must not raise


def test_send_graceful_posix_signals_the_group(posix) -> None:
    proc = FakeProc(pid=99)
    _signals.send_graceful(proc)
    assert posix == [(99, signal.SIGINT)]
    assert proc.signals == []  # group delivery succeeded, no direct fallback


def test_send_graceful_posix_falls_back_to_direct_signal(posix, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise PermissionError

    monkeypatch.setattr(_signals.os, "killpg", boom, raising=False)
    proc = FakeProc()
    _signals.send_graceful(proc)
    assert proc.signals == [signal.SIGINT]


def test_send_graceful_posix_swallows_everything(posix, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise ProcessLookupError

    monkeypatch.setattr(_signals.os, "getpgid", boom, raising=False)
    _signals.send_graceful(FakeProc(raises=ProcessLookupError()))  # must not raise


# -- kill_hard ----------------------------------------------------------------


def test_kill_hard_windows_taskkills_the_tree(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[list[str]] = []
    monkeypatch.setattr(_signals.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or None)
    proc = FakeProc(pid=77)
    _signals.kill_hard(proc)
    assert calls and calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]
    assert calls[0][4] == "77"
    assert proc.killed is True


def test_kill_hard_windows_survives_taskkill_failure(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*_a, **_k):
        raise OSError("taskkill missing")

    monkeypatch.setattr(_signals.subprocess, "run", boom)
    proc = FakeProc(pid=77)
    _signals.kill_hard(proc)
    assert proc.killed is True  # still falls through to proc.kill()


def test_kill_hard_posix_kills_the_group(posix) -> None:
    proc = FakeProc(pid=55)
    _signals.kill_hard(proc)
    assert posix == [(55, _signals.signal.SIGKILL)]
    assert proc.killed is False


def test_kill_hard_posix_falls_back_to_direct_kill(posix, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise ProcessLookupError

    monkeypatch.setattr(_signals.os, "killpg", boom, raising=False)
    proc = FakeProc()
    _signals.kill_hard(proc)
    assert proc.killed is True


def test_kill_hard_posix_swallows_everything(posix, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise ProcessLookupError

    monkeypatch.setattr(_signals.os, "getpgid", boom, raising=False)
    _signals.kill_hard(FakeProc(raises=ProcessLookupError()))  # must not raise
