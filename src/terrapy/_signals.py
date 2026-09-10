"""Graceful and hard process termination, on every platform.

Terraform and OpenTofu shut down gracefully on the first interrupt: SIGINT on
POSIX, `CTRL_BREAK_EVENT` on Windows. That first signal has to reach the whole
process group (or tree) so the provider plugin subprocesses stop too, which is
why `popen_kwargs()` sets up the group or console at `Popen` time. There is no
way to do it afterwards. `send_graceful` and `kill_hard` work on any object
with `.pid` and `.send_signal`/`.kill`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any, Protocol


class _Signalable(Protocol):
    pid: int

    def send_signal(self, sig: int) -> None: ...
    def kill(self) -> None: ...


def popen_kwargs() -> dict[str, Any]:
    """Extra kwargs for `Popen` that isolate the child into its own process
    group (POSIX) or console process group (Windows), so a later signal reaches
    it without touching the calling process."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def send_graceful(proc: _Signalable) -> None:
    """Send the CLI's documented graceful-shutdown signal."""
    if sys.platform == "win32":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass


def kill_hard(proc: _Signalable) -> None:
    """Kill the process (and, where possible, its whole tree) immediately."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass


__all__ = ["popen_kwargs", "send_graceful", "kill_hard"]
