"""Every failure mode terrapy raises, and what to do with each.

Run: python examples/04_errors.py

Each block below triggers one exception on purpose, except the lock case,
which needs a second process holding the lock. Nothing here leaves state
behind.
"""

from __future__ import annotations

from _sandbox import BROKEN_TF, sandbox

from terrapy import (
    BinaryNotFoundError,
    ExecutionError,
    InitializationError,
    LockError,
    Terrapy,
    TerrapyTimeoutError,
    UnsupportedFeatureError,
    UsageError,
    ValidationError,
)


def missing_binary() -> None:
    """Raised by the constructor, before anything runs."""
    try:
        Terrapy(".", binary_path="terraform-that-does-not-exist")
    except BinaryNotFoundError as exc:
        print(f"BinaryNotFoundError: {exc}")
        print(f"  searched: {exc.searched}")


def not_initialized(workdir: str) -> None:
    """Any command that needs a `.terraform` directory and does not find one.

    terrapy classifies on the message as well as the exit code, so a backend
    error from `plan` is raised as InitializationError.
    """
    tf = Terrapy(workdir)
    try:
        tf.plan()
    except InitializationError as exc:
        print(f"InitializationError: exit {exc.exit_code}")
        print(f"  {exc.stderr.strip().splitlines()[0] if exc.stderr.strip() else exc}")


def invalid_config(workdir: str) -> None:
    """`validate` exits 1 on an invalid config. terrapy parses the report first,
    so the diagnostics are returned as structured data."""
    tf = Terrapy(workdir)
    tf.init()
    try:
        tf.validate()
    except ValidationError as exc:
        print(f"ValidationError: {exc.result.error_count} error(s)")
        for diag in exc.result.diagnostics:
            where = f"{diag.range.filename}:{diag.range.start.line}" if diag.range else "?"
            print(f"  [{diag.severity.value}] {where} {diag.summary}")
            if diag.detail:
                print(f"      {diag.detail}")


def bad_arguments(tf: Terrapy) -> None:
    """UsageError reports an invalid call rather than a command failure. The
    second case needs applied state."""
    try:
        # No saved plan and no auto-approve: the CLI would block on a prompt
        # it cannot show in -json mode, so terrapy refuses up front.
        tf.apply(auto_approve=False)
    except UsageError as exc:
        print(f"UsageError: {exc}")

    try:
        tf.output("no-such-output")
    except UsageError as exc:
        print(f"UsageError: {exc}")


def timed_out(workdir: str) -> None:
    """A timeout cancels the run and reports what had been collected."""
    tf = Terrapy(workdir)
    try:
        tf.run_raw("version", timeout=0.001)
    except TerrapyTimeoutError as exc:
        print(f"TerrapyTimeoutError after {exc.timeout}s: {' '.join(exc.command)}")


def old_binary(workdir: str) -> None:
    """A flag the installed binary does not have. The CLI writes `flag
    provided but not defined: -x` to stderr and terrapy names the flag. This
    is what an unsupported feature looks like on a too-old binary."""
    tf = Terrapy(workdir)
    try:
        tf.run_raw("plan", "-not-a-real-flag")
    except UnsupportedFeatureError as exc:
        print(f"UnsupportedFeatureError: {exc.feature}")
    except ExecutionError as exc:
        # Older CLI versions word this differently.
        print(f"ExecutionError: exit {exc.exit_code}")


def locked_state(workdir: str) -> None:
    """Not triggered here: a second process must hold the lock. This shows the
    handler, including the fields parsed from the lock message."""
    tf = Terrapy(workdir)
    try:
        tf.apply()
        print("no lock held, apply went through")
    except LockError as exc:
        info = exc.lock_info or {}
        print(f"locked by {info.get('Who')} since {info.get('Created')}")
        if exc.lock_id:
            tf.force_unlock(exc.lock_id)
    except ExecutionError as exc:
        # Everything that is not a lock, a missing init, or a missing flag.
        print(f"ExecutionError: exit {exc.exit_code}, {len(exc.diagnostics)} diagnostic(s)")


def main() -> int:
    missing_binary()

    with sandbox() as workdir:
        not_initialized(str(workdir))
        timed_out(str(workdir))
        old_binary(str(workdir))

        tf = Terrapy(workdir, timeout=600)
        tf.init()
        tf.apply()
        bad_arguments(tf)
        locked_state(str(workdir))
        tf.destroy()

    with sandbox(BROKEN_TF) as workdir:
        invalid_config(str(workdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
