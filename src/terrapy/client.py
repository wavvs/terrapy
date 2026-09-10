"""The synchronous public API: `Terrapy` and the grouped `StateCommands`
and `WorkspaceCommands` helpers.

A client instance holds an immutable `RunConfig` and an injectable `Runner`.
The `Runner` is the real `SyncRunner` by default, or a `FakeRunner` in tests.
Every method builds a `RunSpec` with `command.py`, hands it to the runner, and
turns the outcome into a typed result or a specific exception. Only
`_run_plain`, `_run_checked`, and `_run_collecting` touch the runner, so every
public method is argv construction plus result parsing and nothing else.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, TypedDict, overload

from . import command, discovery
from ._run_sync import Run, SyncRunner
from .config import RunConfig, RunSpec
from .events import (
    ApplyCompleteEvent,
    ApplyErroredEvent,
    ApplyProgressEvent,
    ApplyStartEvent,
    ChangeSummaryEvent,
    Event,
    OutputsEvent,
)
from .exceptions import (
    ExecutionError,
    InitializationError,
    LockError,
    TerrapyError,
    UnsupportedFeatureError,
    UsageError,
    ValidationError,
)
from .models import (
    ApplyResult,
    ChangeSummary,
    CommandResult,
    Diagnostic,
    FmtResult,
    InitResult,
    OutputValue,
    Plan,
    PlanResult,
    ResourceProgress,
    State,
    StateResource,
    ValidateResult,
    VersionInfo,
)
from .runner import Runner

if TYPE_CHECKING:
    from .config import EventCallback, StrPath

_log = logging.getLogger("terrapy")

_workspace_override: ContextVar[str | None] = ContextVar(
    "terrapy_workspace_override", default=None
)

_LOCK_FIELD_RE = re.compile(
    r"^\s*(ID|Path|Operation|Who|Version|Created|Info):\s*(.*)$", re.MULTILINE
)
_LOCK_MARKERS = ("Error acquiring the state lock", "Lock Info:", "state blob is already locked")
_INIT_HINT_MARKERS = (
    "terraform init",
    "tofu init",
    "has not been initialized",
    "Backend initialization required",
)

_UNSUPPORTED_FEATURE_RE = re.compile(r"flag provided but not defined:\s*(-\S+)")


def _looks_like_lock_error(text: str) -> bool:
    return any(marker in text for marker in _LOCK_MARKERS)


def _parse_lock_info(text: str) -> tuple[str | None, dict[str, str]]:
    info: dict[str, str] = {}
    for m in _LOCK_FIELD_RE.finditer(text):
        info[m.group(1)] = m.group(2).strip()
    return info.get("ID"), info


def _diagnostics_text(base: CommandResult) -> str:
    parts = [base.stderr]
    parts.extend(f"{d.summary} {d.detail}" for d in base.diagnostics)
    return "\n".join(parts)


def _load_json_object(stdout: str) -> dict[str, object]:
    """Parse command stdout as a JSON object, tolerating empty output and a
    non-object top level (both yield `{}`). Text that is not JSON at all raises
    `TerrapyError` rather than returning an empty payload. Every JSON-returning
    method parses its stdout here."""
    if not stdout.strip():
        return {}
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise TerrapyError(f"could not parse command output as JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _nonempty_lines(stdout: str) -> list[str]:
    """The non-blank lines of stdout, for line-oriented (non-JSON) commands."""
    return [line for line in stdout.splitlines() if line.strip()]


class EventRouter:
    """Per-type event subscription: `router.on("diagnostic", handler)`, then
    pass `router.dispatch` as `on_event`."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventCallback]] = {}

    def on(self, event_type: str, handler: EventCallback) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event: Event) -> None:
        for handler in self._handlers.get(event.type, ()):
            handler(event)


def _build_env(
    *, inherit_env: bool, automation: bool, input_enabled: bool, user_env: Mapping[str, str] | None
) -> dict[str, str]:
    env: dict[str, str] = dict(os.environ) if inherit_env else {}
    if automation:
        env["TF_IN_AUTOMATION"] = "1"
        env["CHECKPOINT_DISABLE"] = "1"
    if not input_enabled:
        env["TF_INPUT"] = "false"
    if user_env:
        env.update(user_env)
    return env


def _extract_change_summary(events: Sequence[Event]) -> ChangeSummary:
    # The last summary, not the first: `apply` plans before it applies, so it
    # emits a change_summary with operation="plan" ahead of the applied tally.
    return next(
        (ev.summary for ev in reversed(events) if isinstance(ev, ChangeSummaryEvent)),
        ChangeSummary(),
    )


def _extract_outputs(events: Sequence[Event]) -> dict[str, OutputValue]:
    return next(
        (dict(ev.outputs) for ev in reversed(events) if isinstance(ev, OutputsEvent)), {}
    )


def _extract_resource_progress(events: Sequence[Event]) -> tuple[ResourceProgress, ...]:
    progress: dict[str, ResourceProgress] = {}
    hook_types = (ApplyStartEvent, ApplyProgressEvent, ApplyCompleteEvent, ApplyErroredEvent)
    for ev in events:
        if isinstance(ev, hook_types):
            progress[ev.address] = ResourceProgress(
                address=ev.address,
                action=ev.action,
                id_key=ev.id_key,
                id_value=ev.id_value,
                elapsed_seconds=ev.elapsed_seconds,
                errored=isinstance(ev, ApplyErroredEvent),
            )
    return tuple(progress.values())


class _BaseResultKwargs(TypedDict):
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    diagnostics: tuple[Diagnostic, ...]


def _base_result_kwargs(base: CommandResult) -> _BaseResultKwargs:
    """The `CommandResult` fields every typed result carries, so each builder
    lists only its own extra fields."""
    return {
        "command": base.command,
        "exit_code": base.exit_code,
        "stdout": base.stdout,
        "stderr": base.stderr,
        "duration": base.duration,
        "diagnostics": base.diagnostics,
    }


def _build_apply_result(base: CommandResult, events: tuple[Event, ...]) -> ApplyResult:
    """The shared `apply`/`destroy` result assembly, identical for both."""
    return ApplyResult(
        **_base_result_kwargs(base),
        change_summary=_extract_change_summary(events),
        outputs=_extract_outputs(events),
        resources=_extract_resource_progress(events),
        events=events,
    )


def _build_init_result(
    base: CommandResult, events: tuple[Event, ...], *, upgraded: bool, reconfigured: bool
) -> InitResult:
    return InitResult(
        **_base_result_kwargs(base),
        upgraded=upgraded,
        reconfigured=reconfigured,
        events=events,
    )


def _raise_command_failure(base: CommandResult, spec: RunSpec, *, kind: str) -> None:
    """Map a failed invocation to the most specific exception."""
    text = _diagnostics_text(base)
    m = _UNSUPPORTED_FEATURE_RE.search(text)
    if m:
        raise UnsupportedFeatureError(
            f"unsupported flag: {m.group(1)}",
            feature=m.group(1),
        )
    if _looks_like_lock_error(text):
        lock_id, lock_info = _parse_lock_info(text)
        raise LockError(
            "state lock could not be acquired",
            command=base.command,
            exit_code=base.exit_code,
            stdout=base.stdout,
            stderr=base.stderr,
            diagnostics=base.diagnostics,
            lock_id=lock_id,
            lock_info=lock_info or None,
        )
    if kind == "init" or any(marker in text for marker in _INIT_HINT_MARKERS):
        raise InitializationError(
            f"{kind} failed",
            command=base.command,
            exit_code=base.exit_code,
            stdout=base.stdout,
            stderr=base.stderr,
            diagnostics=base.diagnostics,
        )
    raise ExecutionError(
        f"{kind} exited with code {base.exit_code}",
        command=base.command,
        exit_code=base.exit_code,
        stdout=base.stdout,
        stderr=base.stderr,
        diagnostics=base.diagnostics,
    )


class Terrapy:
    """A client bound to one working directory and one resolved binary.

    Immutable after construction (all inputs freeze into `RunConfig`), so an
    instance is safe to share and call concurrently across threads. Concurrent
    calls contend over the working directory and the backend state, which
    Terraform and OpenTofu guard with their own state lock. They do not contend
    over anything this object holds.
    """

    def __init__(
        self,
        working_dir: StrPath | None = None,
        *,
        binary_path: StrPath | None = None,
        env: Mapping[str, str] | None = None,
        inherit_env: bool = True,
        automation: bool = True,
        input: bool = False,
        no_color: bool = True,
        parallelism: int | None = None,
        use_chdir: bool = False,
        timeout: float | None = None,
        inactivity_timeout: float | None = None,
        logger: logging.Logger | None = None,
        runner: Runner | None = None,
    ) -> None:
        self._logger = logger or _log

        resolved_binary = discovery.find_binary(binary_path)

        resolved_env = _build_env(
            inherit_env=inherit_env, automation=automation, input_enabled=input, user_env=env
        )
        resolved_working_dir = os.fspath(working_dir) if working_dir is not None else os.getcwd()

        self._config = RunConfig(
            working_dir=resolved_working_dir,
            binary_path=resolved_binary,
            env=resolved_env,
            automation=automation,
            input=input,
            no_color=no_color,
            parallelism=parallelism,
            use_chdir=use_chdir,
            timeout=timeout,
            inactivity_timeout=inactivity_timeout,
        )
        self._runner: Runner = runner if runner is not None else SyncRunner()
        self._version_lock = threading.Lock()
        self._version_cache: VersionInfo | None = None

        self.state = StateCommands(self)
        self.workspace = WorkspaceCommands(self)

    # -- construction helpers ---------------------------------------------

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(working_dir={self._config.working_dir!r}, "
            f"binary_path={self._config.binary_path!r})"
        )

    def __enter__(self) -> Terrapy:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    @property
    def working_dir(self) -> str:
        return self._config.working_dir

    @property
    def binary_path(self) -> str:
        return self._config.binary_path

    def _spec(self, builder: Callable[..., RunSpec], *args: object, **kwargs: object) -> RunSpec:
        spec = builder(self._config, *args, **kwargs)
        workspace = _workspace_override.get()
        if workspace:
            spec = dataclasses.replace(spec, env={**spec.env, "TF_WORKSPACE": workspace})
        return spec

    # -- low-level execution ------------------------------------------------

    def _run_plain(self, spec: RunSpec) -> CommandResult:
        with self._runner.start(spec) as run:
            for _ in run:
                pass
            return run.result()

    def _run_checked(self, spec: RunSpec, *, kind: str) -> CommandResult:
        base = self._run_plain(spec)
        if base.exit_code not in spec.success_exit_codes:
            self._raise_for_failure(base, spec, kind=kind)
        return base

    def _run_collecting(
        self, spec: RunSpec, *, on_event: EventCallback | None = None
    ) -> tuple[CommandResult, tuple[Event, ...]]:
        events: list[Event] = []
        with self._runner.start(spec) as run:
            for ev in run:
                events.append(ev)
                if on_event is not None:
                    on_event(ev)
            base = run.result()
        return base, tuple(events)

    def _raise_for_failure(self, base: CommandResult, spec: RunSpec, *, kind: str) -> None:
        _raise_command_failure(base, spec, kind=kind)

    # -- version ------------------------------------------------------------

    def version(self) -> VersionInfo:
        # Held across the probe, so two threads cannot both spawn `version
        # -json`. Routed through the runner so the client's env/cwd apply and
        # an injected runner can intercept it.
        with self._version_lock:
            if self._version_cache is None:
                base = self._run_checked(self._spec(command.build_version), kind="version")
                self._version_cache = VersionInfo.from_dict(_load_json_object(base.stdout))
            return self._version_cache

    # -- lifecycle: init / validate / fmt -----------------------------------

    def init(
        self,
        *,
        backend: bool = True,
        backend_config: Mapping[str, object] | Sequence[str] | StrPath | None = None,
        reconfigure: bool = False,
        migrate_state: bool = False,
        upgrade: bool = False,
        get: bool = True,
        plugin_dirs: Sequence[StrPath] | None = None,
        lockfile: str | None = None,
        from_module: StrPath | None = None,
        on_event: EventCallback | None = None,
        timeout: float | None = None,
    ) -> InitResult:
        spec = self._spec(
            command.build_init,
            backend=backend,
            backend_config=backend_config,
            reconfigure=reconfigure,
            migrate_state=migrate_state,
            upgrade=upgrade,
            get=get,
            plugin_dirs=plugin_dirs,
            lockfile=lockfile,
            from_module=from_module,
            json_mode=True,
            timeout=timeout,
        )
        base, events = self._run_collecting(spec, on_event=on_event)
        if base.exit_code not in spec.success_exit_codes:
            self._raise_for_failure(base, spec, kind="init")
        return _build_init_result(base, events, upgraded=upgrade, reconfigured=reconfigure)

    def validate(self) -> ValidateResult:
        # The runner accepts exit 1 so the -json report is parsed before the
        # raise; that parsed report is what the exception carries. Any other
        # non-zero exit means validate itself failed, and must not come back
        # reporting `valid=True` off an empty payload.
        spec = self._spec(command.build_validate)
        base = self._run_checked(spec, kind="validate")
        result = ValidateResult.from_dict(_load_json_object(base.stdout))
        if not result.valid:
            raise ValidationError(
                f"configuration is invalid: {result.error_count} error(s), "
                f"{result.warning_count} warning(s)",
                result=result,
                command=base.command,
                exit_code=base.exit_code,
                stdout=base.stdout,
                stderr=base.stderr,
                diagnostics=result.diagnostics,
            )
        return result


    def fmt(
        self,
        paths: Sequence[StrPath] | None = None,
        *,
        check: bool = False,
        write: bool = True,
        diff: bool = False,
        recursive: bool = False,
    ) -> FmtResult:
        spec = self._spec(
            command.build_fmt, paths, check=check, write=write, diff=diff, recursive=recursive
        )
        base = self._run_checked(spec, kind="fmt")
        return FmtResult(
            **_base_result_kwargs(base), changed_files=tuple(_nonempty_lines(base.stdout))
        )

    # -- lifecycle: plan / apply / destroy ----------------------------------

    def plan(
        self,
        *,
        out: StrPath | None = None,
        destroy: bool = False,
        refresh_only: bool = False,
        refresh: bool = True,
        var: Mapping[str, object] | None = None,
        var_files: Sequence[StrPath] | None = None,
        targets: Sequence[str] | None = None,
        replace: Sequence[str] | None = None,
        parallelism: int | None = None,
        on_event: EventCallback | None = None,
    ) -> PlanResult:
        spec = self._spec(
            command.build_plan,
            out=out,
            destroy=destroy,
            refresh_only=refresh_only,
            refresh=refresh,
            var=var,
            var_files=var_files,
            targets=targets,
            replace=replace,
            parallelism=parallelism,
            json_mode=True,
        )
        base, events = self._run_collecting(spec, on_event=on_event)
        if base.exit_code not in spec.success_exit_codes:
            self._raise_for_failure(base, spec, kind="plan")

        kwargs: dict[str, object] = dict(
            **_base_result_kwargs(base),
            has_changes=base.exit_code == 2,
            change_summary=_extract_change_summary(events),
            events=events,
        )
        if out is not None:
            plan_obj = self.show_plan(out)
            kwargs.update(
                plan_file=os.fspath(out),
                plan=plan_obj,
                resource_changes=plan_obj.resource_changes,
                output_changes=plan_obj.output_changes,
            )
        return PlanResult(**kwargs)  # type: ignore[arg-type]

    def apply(
        self,
        plan_file: StrPath | None = None,
        *,
        auto_approve: bool = True,
        var: Mapping[str, object] | None = None,
        var_files: Sequence[StrPath] | None = None,
        targets: Sequence[str] | None = None,
        replace: Sequence[str] | None = None,
        refresh_only: bool = False,
        parallelism: int | None = None,
        on_event: EventCallback | None = None,
    ) -> ApplyResult:
        spec = self._spec(
            command.build_apply,
            plan_file,
            auto_approve=auto_approve,
            var=var,
            var_files=var_files,
            targets=targets,
            replace=replace,
            refresh_only=refresh_only,
            parallelism=parallelism,
            json_mode=True,
        )
        base, events = self._run_collecting(spec, on_event=on_event)
        if base.exit_code not in spec.success_exit_codes:
            self._raise_for_failure(base, spec, kind="apply")
        return _build_apply_result(base, events)

    def destroy(
        self,
        *,
        auto_approve: bool = True,
        var: Mapping[str, object] | None = None,
        var_files: Sequence[StrPath] | None = None,
        targets: Sequence[str] | None = None,
        parallelism: int | None = None,
        on_event: EventCallback | None = None,
    ) -> ApplyResult:
        spec = self._spec(
            command.build_destroy,
            auto_approve=auto_approve,
            var=var,
            var_files=var_files,
            targets=targets,
            parallelism=parallelism,
            json_mode=True,
        )
        base, events = self._run_collecting(spec, on_event=on_event)
        if base.exit_code not in spec.success_exit_codes:
            self._raise_for_failure(base, spec, kind="destroy")
        return _build_apply_result(base, events)

    # -- outputs / show -------------------------------------------------

    @overload
    def output(self) -> dict[str, OutputValue]: ...
    @overload
    def output(self, name: str) -> OutputValue: ...

    def output(self, name: str | None = None) -> dict[str, OutputValue] | OutputValue:
        # Always the map form. `output -json NAME` prints the bare value, with
        # no type/sensitive metadata, so a named lookup filters the full map
        # rather than asking for one output. Same subprocess either way.
        spec = self._spec(command.build_output, None)
        base = self._run_checked(spec, kind="output")
        outputs = {
            k: OutputValue.from_dict(v)
            for k, v in _load_json_object(base.stdout).items()
            if isinstance(v, Mapping)
        }
        if name is None:
            return outputs
        if name not in outputs:
            raise UsageError(f"no output named {name!r}")
        return outputs[name]

    def show_plan(self, path: StrPath) -> Plan:
        spec = self._spec(command.build_show, path)
        base = self._run_checked(spec, kind="show")
        return Plan.from_dict(_load_json_object(base.stdout))

    def show_state(self) -> State:
        spec = self._spec(command.build_show, None)
        base = self._run_checked(spec, kind="show")
        return State.from_dict(_load_json_object(base.stdout))

    def force_unlock(self, lock_id: str, *, force: bool = True) -> CommandResult:
        spec = self._spec(command.build_force_unlock, lock_id, force=force)
        return self._run_checked(spec, kind="force-unlock")

    # -- escape hatch ---------------------------------------------------

    def run_raw(
        self, *args: str, timeout: float | None = None, success_exit_codes: Sequence[int] = (0,)
    ) -> CommandResult:
        spec = self._spec(
            command.build_raw, *args, timeout=timeout, success_exit_codes=success_exit_codes
        )
        return self._run_checked(spec, kind="run_raw")

    # -- streaming primitives ---------------------------------------------

    def _stream(self, builder: Callable[..., RunSpec], **kwargs: object) -> Run:
        """Start a run and hand back the live `Run` without consuming it. Only
        the four typed `*_stream` methods below reach this, so the untyped
        `**kwargs` hop is never part of the public surface."""
        return self._runner.start(self._spec(builder, json_mode=True, **kwargs))

    def plan_stream(
        self,
        *,
        out: StrPath | None = None,
        destroy: bool = False,
        refresh_only: bool = False,
        refresh: bool = True,
        var: Mapping[str, object] | None = None,
        var_files: Sequence[StrPath] | None = None,
        targets: Sequence[str] | None = None,
        replace: Sequence[str] | None = None,
        parallelism: int | None = None,
        timeout: float | None = None,
    ) -> Run:
        return self._stream(
            command.build_plan,
            out=out,
            destroy=destroy,
            refresh_only=refresh_only,
            refresh=refresh,
            var=var,
            var_files=var_files,
            targets=targets,
            replace=replace,
            parallelism=parallelism,
            timeout=timeout,
        )

    def apply_stream(
        self,
        plan_file: StrPath | None = None,
        *,
        auto_approve: bool = True,
        var: Mapping[str, object] | None = None,
        var_files: Sequence[StrPath] | None = None,
        targets: Sequence[str] | None = None,
        replace: Sequence[str] | None = None,
        refresh_only: bool = False,
        parallelism: int | None = None,
        timeout: float | None = None,
    ) -> Run:
        return self._stream(
            command.build_apply,
            plan_file=plan_file,
            auto_approve=auto_approve,
            var=var,
            var_files=var_files,
            targets=targets,
            replace=replace,
            refresh_only=refresh_only,
            parallelism=parallelism,
            timeout=timeout,
        )

    def init_stream(
        self,
        *,
        backend: bool = True,
        backend_config: Mapping[str, object] | Sequence[str] | StrPath | None = None,
        reconfigure: bool = False,
        migrate_state: bool = False,
        upgrade: bool = False,
        get: bool = True,
        plugin_dirs: Sequence[StrPath] | None = None,
        lockfile: str | None = None,
        from_module: StrPath | None = None,
        timeout: float | None = None,
    ) -> Run:
        return self._stream(
            command.build_init,
            backend=backend,
            backend_config=backend_config,
            reconfigure=reconfigure,
            migrate_state=migrate_state,
            upgrade=upgrade,
            get=get,
            plugin_dirs=plugin_dirs,
            lockfile=lockfile,
            from_module=from_module,
            timeout=timeout,
        )

    def destroy_stream(
        self,
        *,
        auto_approve: bool = True,
        var: Mapping[str, object] | None = None,
        var_files: Sequence[StrPath] | None = None,
        targets: Sequence[str] | None = None,
        parallelism: int | None = None,
        timeout: float | None = None,
    ) -> Run:
        return self._stream(
            command.build_destroy,
            auto_approve=auto_approve,
            var=var,
            var_files=var_files,
            targets=targets,
            parallelism=parallelism,
            timeout=timeout,
        )


class StateCommands:
    """`client.state.<method>`: thin wrappers over `terraform state <sub>`."""

    def __init__(self, client: Terrapy) -> None:
        self._client = client

    def list(self, *, addresses: Sequence[str] | None = None) -> list[str]:
        spec = self._client._spec(command.build_state_list, addresses)
        base = self._client._run_checked(spec, kind="state")
        return _nonempty_lines(base.stdout)

    def show(self, address: str) -> StateResource:
        state = self.pull()
        for resource in state.resources:
            if resource.address == address:
                return resource
        raise UsageError(f"resource {address!r} was not found in state")

    def mv(self, source: str, destination: str, *, dry_run: bool = False) -> CommandResult:
        spec = self._client._spec(command.build_state_mv, source, destination, dry_run=dry_run)
        return self._client._run_checked(spec, kind="state")

    def rm(self, *addresses: str, dry_run: bool = False) -> CommandResult:
        spec = self._client._spec(command.build_state_rm, addresses, dry_run=dry_run)
        return self._client._run_checked(spec, kind="state")

    def pull(self) -> State:
        spec = self._client._spec(command.build_state_pull)
        base = self._client._run_checked(spec, kind="state")
        return State.from_dict(_load_json_object(base.stdout))

    def push(self, state_file: StrPath, *, force: bool = False) -> CommandResult:
        spec = self._client._spec(command.build_state_push, state_file, force=force)
        return self._client._run_checked(spec, kind="state")

    def replace_provider(self, from_: str, to: str) -> CommandResult:
        spec = self._client._spec(command.build_state_replace_provider, from_, to)
        return self._client._run_checked(spec, kind="state")


class WorkspaceCommands:
    """`client.workspace.<method>`: thin wrappers over `terraform workspace <sub>`."""

    def __init__(self, client: Terrapy) -> None:
        self._client = client

    def list(self) -> list[str]:
        spec = self._client._spec(command.build_workspace_list)
        base = self._client._run_checked(spec, kind="workspace")
        names = []
        for line in base.stdout.splitlines():
            stripped = line.strip().lstrip("*").strip()
            if stripped:
                names.append(stripped)
        return names

    def show(self) -> str:
        spec = self._client._spec(command.build_workspace_show)
        base = self._client._run_checked(spec, kind="workspace")
        return base.stdout.strip()

    def new(self, name: str) -> CommandResult:
        spec = self._client._spec(command.build_workspace_new, name)
        return self._client._run_checked(spec, kind="workspace")

    def select(self, name: str, *, or_create: bool = False) -> CommandResult:
        spec = self._client._spec(command.build_workspace_select, name, or_create=or_create)
        return self._client._run_checked(spec, kind="workspace")

    def delete(self, name: str, *, force: bool = False) -> CommandResult:
        spec = self._client._spec(command.build_workspace_delete, name, force=force)
        return self._client._run_checked(spec, kind="workspace")

    @contextmanager
    def use(self, name: str) -> Iterator[None]:
        """Scoped, thread-safe workspace switch via `TF_WORKSPACE`, not
        `workspace select` (which mutates on-disk state and would race
        across concurrent callers)."""
        token = _workspace_override.set(name)
        try:
            yield
        finally:
            _workspace_override.reset(token)


__all__ = [
    "Terrapy",
    "StateCommands",
    "WorkspaceCommands",
    "EventRouter",
]
