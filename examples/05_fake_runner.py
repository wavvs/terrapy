"""Test code that calls terrapy, with no Terraform installed.

Run: python examples/05_fake_runner.py

`Terrapy(runner=...)` accepts anything with `start(spec) -> Run`. With a fake
runner the client still builds the real argv, parses real `-json` lines and
applies the real exit-code rules, but no process is started. This runs in
milliseconds and needs no binary or cloud credentials in CI.

The client resolves `binary_path` in its constructor, before it looks at the
runner, so the path still has to exist. `sys.executable` always does.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from typing import cast

from terrapy import Event, Run, Terrapy
from terrapy.config import RunSpec
from terrapy.events import AssembledLine, event_from_line
from terrapy.models import CommandResult

# Three lines of real `plan -json` output.
PLAN_OUTPUT = [
    '{"@level":"info","@message":"Terraform 1.9.8","@module":"terraform.ui",'
    '"type":"version","terraform":"1.9.8","ui":"1.2"}',
    '{"@level":"info","@message":"local_file.note[0]: Plan to create",'
    '"@module":"terraform.ui","type":"planned_change",'
    '"change":{"resource":{"addr":"local_file.note[0]","resource_type":"local_file",'
    '"resource_name":"note"},"action":"create"}}',
    '{"@level":"info","@message":"Plan: 3 to add, 0 to change, 0 to destroy.",'
    '"@module":"terraform.ui","type":"change_summary",'
    '"changes":{"add":3,"change":0,"remove":0,"operation":"plan"}}',
]


class FakeRun:
    """A `Run`-shaped object over predefined lines. Context manager plus iterator,
    which is all the client uses."""

    def __init__(self, lines: Sequence[str], exit_code: int) -> None:
        self._events = [event_from_line(AssembledLine(text=line)) for line in lines]
        self._stdout = "\n".join(lines)
        self._exit_code = exit_code
        self.args: list[str] = []

    def __enter__(self) -> FakeRun:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def result(self) -> CommandResult:
        return CommandResult(
            command=tuple(self.args),
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr="",
            duration=0.01,
        )


class FakeRunner:
    """Replays one predefined response and records every spec it was handed."""

    def __init__(self, lines: Sequence[str], exit_code: int = 0) -> None:
        self.calls: list[RunSpec] = []
        self._lines = lines
        self._exit_code = exit_code

    def start(self, spec: RunSpec) -> Run:
        self.calls.append(spec)
        run = FakeRun(self._lines, self._exit_code)
        run.args = list(spec.argv)
        # The Runner protocol names the concrete Run class, so a fake needs a
        # cast. The client only iterates it and calls result().
        return cast(Run, run)


def main() -> int:
    # Exit 2 is what `plan -detailed-exitcode` returns when changes are pending.
    runner = FakeRunner(PLAN_OUTPUT, exit_code=2)
    tf = Terrapy(".", binary_path=sys.executable, runner=runner)

    plan = tf.plan(var={"file_count": 3}, targets=["local_file.note"])

    print(f"has_changes: {plan.has_changes}")
    print(f"summary:     {plan.change_summary}")
    print(f"events:      {[event.type for event in plan.events]}")
    print(f"argv:        {' '.join(runner.calls[-1].argv[1:])}")

    assert plan.has_changes, "exit 2 means changes are pending"
    assert plan.change_summary.add == 3
    assert "-detailed-exitcode" in runner.calls[-1].argv
    assert "-var" in runner.calls[-1].argv

    # Exit 1 with no diagnostics is a plain failure.
    failing = Terrapy(".", binary_path=sys.executable, runner=FakeRunner([], exit_code=1))
    try:
        failing.plan()
    except Exception as exc:  # noqa: BLE001 - printing the type is the point
        print(f"exit 1 raised: {type(exc).__name__}")

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
