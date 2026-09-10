"""Shared test fixtures: `FakeRunner`, a `Runner` implementation that never
spawns a process, plus small JSON/JSONL fixture loaders.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from terrapy.config import RunSpec
from terrapy.events import AssembledLine, DiagnosticEvent, Event, event_from_line
from terrapy.models import CommandResult

FIXTURES_DIR = Path(__file__).parent / "unit" / "fixtures"


def load_json_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_jsonl_fixture(name: str) -> list[str]:
    text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


class FakeRun(AbstractContextManager["FakeRun"], Iterator[Event]):
    """A `Run`-shaped object backed by predefined data instead of a subprocess."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout_lines: Sequence[str] = (),
        stderr: str = "",
        duration: float = 0.01,
    ) -> None:
        self.pid = 4242
        self.args: list[str] = []
        self.returncode = exit_code
        self._events = [event_from_line(AssembledLine(text=line)) for line in stdout_lines]
        self._idx = 0
        self._stdout = "\n".join(stdout_lines)
        self._stderr = stderr
        self._duration = duration
        self._diagnostics = tuple(
            ev.diagnostic for ev in self._events if isinstance(ev, DiagnosticEvent)
        )
        self.cancel_calls: list[dict[str, object]] = []

    def __iter__(self) -> FakeRun:
        return self

    def __next__(self) -> Event:
        if self._idx >= len(self._events):
            raise StopIteration
        ev = self._events[self._idx]
        self._idx += 1
        return ev

    def __enter__(self) -> FakeRun:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cancel(self, *, grace: float | None = None) -> None:
        self.cancel_calls.append({"grace": grace})

    def wait(self, timeout: float | None = None) -> int:
        for _ in self:
            pass
        return self.returncode

    def stderr_text(self) -> str:
        return self._stderr

    def result(self) -> CommandResult:
        return CommandResult(
            command=tuple(self.args),
            exit_code=self.returncode,
            stdout=self._stdout,
            stderr=self._stderr,
            duration=self._duration,
            diagnostics=self._diagnostics,
        )


class FakeRunner:
    """Register predefined responses by argv prefix; `start()` records every
    `RunSpec` it was given so tests can assert on the built argv too."""

    def __init__(self) -> None:
        self.calls: list[RunSpec] = []
        self._responses: dict[tuple[str, ...], FakeRun] = {}
        self.default_response: FakeRun | None = None

    def register(self, argv_prefix: Sequence[str], run: FakeRun) -> None:
        self._responses[tuple(argv_prefix)] = run

    def start(self, spec: RunSpec) -> FakeRun:
        self.calls.append(spec)
        run = self._match(spec.argv) or self.default_response or FakeRun()
        run.args = list(spec.argv)
        run._idx = 0  # replay from the start; a registered run may be reused
        return run

    def _match(self, argv: Sequence[str]) -> FakeRun | None:
        best: FakeRun | None = None
        best_len = -1
        for prefix, run in self._responses.items():
            if tuple(argv[: len(prefix)]) == prefix and len(prefix) > best_len:
                best = run
                best_len = len(prefix)
        return best


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def json_fixture():
    return load_json_fixture


@pytest.fixture
def jsonl_fixture():
    return load_jsonl_fixture
