"""The terrapy exception hierarchy.

Every exception that terrapy raises for an expected failure derives from
`TerrapyError`. A programming error, such as a `TypeError`, does not.
`ExecutionError` and its subclasses always carry the full context of the
failed invocation: `command`, `exit_code`, `stdout`, `stderr`, and
`diagnostics`, so a caller can build an error message without re-parsing
anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Diagnostic, ValidateResult


class TerrapyError(Exception):
    """Base class for every exception raised by terrapy."""


class BinaryNotFoundError(TerrapyError):
    """No `terraform` or `tofu` binary was found on PATH and no valid `binary_path` was given."""

    def __init__(self, message: str, *, searched: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.searched = tuple(searched)


class VersionDetectionError(TerrapyError):
    """`version -json` failed, timed out, or would not parse."""

    def __init__(self, message: str, *, binary_path: str, raw_output: str = "") -> None:
        super().__init__(message)
        self.binary_path = binary_path
        self.raw_output = raw_output


class UnsupportedFeatureError(TerrapyError):
    """A requested flag or feature is absent on the resolved binary."""

    def __init__(self, message: str, *, feature: str) -> None:
        super().__init__(message)
        self.feature = feature


class UsageError(TerrapyError):
    """An illegal argument combination was caught before the process was spawned."""


class TerrapyTimeoutError(TerrapyError):
    """An invocation exceeded its timeout (or inactivity timeout) and was killed."""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        timeout: float,
        stdout: str = "",
        stderr: str = "",
        diagnostics: Sequence[Diagnostic] = (),
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        self.diagnostics = tuple(diagnostics)


class ExecutionError(TerrapyError):
    """A command exited with a code not in its configured `success_exit_codes`.

    Carries everything needed to build a useful error message or programmatic
    response without re-parsing anything: the argv, exit code, captured stdout
    and stderr, and structured diagnostics (parsed from `-json` output when
    available).
    """

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        diagnostics: Sequence[Diagnostic] = (),
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.diagnostics = tuple(diagnostics)

    def __str__(self) -> str:
        base = super().__str__()
        cmd = " ".join(self.command)
        return f"{base} (exit {self.exit_code}): {cmd}"


class ValidationError(ExecutionError):
    """The configuration is invalid.

    Raised by `validate()` when the report comes back with errors. The parsed
    report is on `.result`, so a caller can read the diagnostics without
    re-running the command.
    """

    def __init__(self, message: str, *, result: ValidateResult, **kwargs: object) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.result = result


class LockError(ExecutionError):
    """The state lock could not be acquired (or released)."""

    def __init__(
        self,
        message: str,
        *,
        lock_id: str | None = None,
        lock_info: Mapping[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.lock_id = lock_id
        self.lock_info = lock_info


class InitializationError(ExecutionError):
    """`init` (or a backend it depends on) failed."""
