"""Client-level defaults (`RunConfig`) and one resolved invocation (`RunSpec`).

The client constructor freezes one `RunConfig` that never changes for the life
of the instance, so a client is safe to share across threads.
Each `command.py` builder then produces a fresh `RunSpec` per invocation,
holding everything the runner needs to start the child: the full argv, the
working directory, the environment, and the exit codes that count as success
for this one invocation. Most commands use `{0}`; `plan -detailed-exitcode`
uses `{0, 2}` and `fmt -check` uses `{0, 3}`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from .events import Event

StrPath: TypeAlias = "str | os.PathLike[str]"
EventCallback: TypeAlias = "Callable[[Event], None]"
StderrCallback: TypeAlias = "Callable[[str], None]"


@dataclass(frozen=True, slots=True, kw_only=True)
class RunConfig:
    """Immutable, resolved client defaults. One `RunConfig` per client instance."""

    working_dir: str
    binary_path: str
    env: Mapping[str, str]
    automation: bool = True
    input: bool = False
    no_color: bool = True
    parallelism: int | None = None
    use_chdir: bool = False
    timeout: float | None = None
    inactivity_timeout: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSpec:
    """One fully-resolved invocation, ready to hand to a `Runner`."""

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    success_exit_codes: frozenset[int] = frozenset({0})
    timeout: float | None = None
    inactivity_timeout: float | None = None
    json_mode: bool = False
    on_stderr: StderrCallback | None = field(default=None, repr=False, compare=False)


__all__ = ["RunConfig", "RunSpec", "StrPath", "EventCallback", "StderrCallback"]
