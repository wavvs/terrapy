"""Typed, immutable models for terrapy command results and their nested data.

Every `from_dict` is built only from the tolerant getters in `_parsing`, so a
payload from a newer CLI that carries unknown keys, omits keys, or types one
oddly still parses instead of raising. Each model keeps the payload it was
built from in `.raw`, so nothing terrapy does not yet model is lost.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import total_ordering
from typing import TYPE_CHECKING

from ._parsing import (
    get_bool,
    get_dict,
    get_int,
    get_list,
    get_nested,
    get_str,
    get_str_tuple,
    get_tuple_of,
    log_unknown_keys,
)
from .enums import Action, ChangeOperation, DiagnosticSeverity, ResourceMode

if TYPE_CHECKING:
    from .events import Event


# -- source positions, diagnostics, change summaries --------------------------


@dataclass(frozen=True, slots=True)
class Pos:
    """A single line/column/byte position in a source file."""

    line: int = 0
    column: int = 0
    byte: int = 0

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Pos:
        return cls(
            line=get_int(d, "line", 0) or 0,
            column=get_int(d, "column", 0) or 0,
            byte=get_int(d, "byte", 0) or 0,
        )


@dataclass(frozen=True, slots=True)
class Range:
    """A source range, as attached to a diagnostic."""

    filename: str = ""
    start: Pos = field(default_factory=Pos)
    end: Pos = field(default_factory=Pos)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Range:
        return cls(
            filename=get_str(d, "filename", "") or "",
            start=get_nested(d, "start", Pos.from_dict) or Pos(),
            end=get_nested(d, "end", Pos.from_dict) or Pos(),
        )


@dataclass(frozen=True, slots=True)
class Snippet:
    """The source-code context around a diagnostic's range, when available."""

    context: str | None = None
    code: str = ""
    start_line: int = 0
    highlight_start_offset: int = 0
    highlight_end_offset: int = 0
    values: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Snippet:
        values_raw = d.get("values")
        values: tuple[tuple[str, str], ...] = ()
        if isinstance(values_raw, Sequence) and not isinstance(values_raw, (str, bytes)):
            values = tuple(
                (str(item.get("traversal", "")), str(item.get("statement", "")))
                for item in values_raw
                if isinstance(item, Mapping)
            )
        elif isinstance(values_raw, Mapping):
            values = tuple((str(k), str(v)) for k, v in values_raw.items())
        return cls(
            context=get_str(d, "context"),
            code=get_str(d, "code", "") or "",
            start_line=get_int(d, "start_line", 0) or 0,
            highlight_start_offset=get_int(d, "highlight_start_offset", 0) or 0,
            highlight_end_offset=get_int(d, "highlight_end_offset", 0) or 0,
            values=values,
        )


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single diagnostic message (error or warning) from any `-json` command."""

    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    summary: str = ""
    detail: str = ""
    address: str | None = None
    range: Range | None = None
    snippet: Snippet | None = None
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = frozenset({"severity", "summary", "detail", "address", "range", "snippet"})

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Diagnostic:
        log_unknown_keys("Diagnostic", d, cls._KNOWN_KEYS)
        severity_raw = d.get("severity")
        severity = DiagnosticSeverity(severity_raw) if severity_raw else DiagnosticSeverity.ERROR
        return cls(
            severity=severity,
            summary=get_str(d, "summary", "") or "",
            detail=get_str(d, "detail", "") or "",
            address=get_str(d, "address"),
            range=get_nested(d, "range", Range.from_dict),
            snippet=get_nested(d, "snippet", Snippet.from_dict),
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    """The `add`/`change`/`remove`/`import` tally for a plan or apply."""

    add: int = 0
    change: int = 0
    remove: int = 0
    import_: int = 0
    operation: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ChangeSummary:
        return cls(
            add=get_int(d, "add", 0) or 0,
            change=get_int(d, "change", 0) or 0,
            remove=get_int(d, "remove", 0) or 0,
            import_=get_int(d, "import", 0) or 0,
            operation=get_str(d, "operation"),
        )

    def __repr__(self) -> str:
        base = f"+{self.add} ~{self.change} -{self.remove}"
        if self.import_:
            base += f" import={self.import_}"
        return f"ChangeSummary({base})"


# -- output values ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutputValue:
    """One entry from `output -json` (or the `outputs` field of a plan/state)."""

    value: object = None
    type: object = None
    sensitive: bool = False
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> OutputValue:
        return cls(
            value=d.get("value"),
            type=d.get("type"),
            sensitive=get_bool(d, "sensitive", False) or False,
            raw=d,
        )

    def __repr__(self) -> str:
        shown = "<sensitive>" if self.sensitive else self.value
        return f"OutputValue(value={shown!r}, type={self.type!r}, sensitive={self.sensitive})"


# -- the common result base ---------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandResult:
    """Fields present on the result of every terrapy command.

    Assembled by the client from the process outcome plus any diagnostics
    parsed from `-json` output; there is no `from_dict` for this base class
    because it is not itself a wire payload.
    """

    command: tuple[str, ...] = ()
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    diagnostics: tuple[Diagnostic, ...] = ()


# -- the static plan representation (`show -json <planfile>`) -----------------


def _any_sensitive(v: object) -> bool:
    """Whether an `after_sensitive` / `before_sensitive` marker signals any
    sensitivity. For a scalar output the marker is a plain `bool`; for a
    complex output it mirrors the shape of the value with `true` at each
    sensitive leaf (e.g. `{"password": true}` or `[false, true]`), so any
    `true` anywhere in the structure counts."""
    if isinstance(v, bool):
        return v
    if isinstance(v, Mapping):
        return any(_any_sensitive(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(_any_sensitive(x) for x in v)
    return False


@dataclass(frozen=True, slots=True)
class Change:
    """The before/after values and action list for one resource or output change."""

    actions: tuple[Action, ...] = ()
    before: object = None
    after: object = None
    after_unknown: object = None
    before_sensitive: object = None
    after_sensitive: object = None
    replace_paths: tuple[object, ...] = ()
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Change:
        actions_raw = get_list(d, "actions", ()) or ()
        actions = tuple(Action(a) for a in actions_raw if isinstance(a, str))
        replace_paths_raw = get_list(d, "replace_paths", ()) or ()
        return cls(
            actions=actions,
            before=d.get("before"),
            after=d.get("after"),
            after_unknown=d.get("after_unknown"),
            before_sensitive=d.get("before_sensitive"),
            after_sensitive=d.get("after_sensitive"),
            replace_paths=tuple(replace_paths_raw),
            raw=d,
        )

    @property
    def is_noop(self) -> bool:
        return self.actions in ((), (Action.NOOP,))

    @property
    def is_create(self) -> bool:
        return Action.CREATE in self.actions and Action.DELETE not in self.actions

    @property
    def is_read(self) -> bool:
        return self.actions == (Action.READ,)

    @property
    def is_update(self) -> bool:
        return self.actions == (Action.UPDATE,)

    @property
    def is_delete(self) -> bool:
        return Action.DELETE in self.actions and Action.CREATE not in self.actions

    @property
    def is_replace(self) -> bool:
        return Action.CREATE in self.actions and Action.DELETE in self.actions

    @property
    def is_forget(self) -> bool:
        return Action.FORGET in self.actions


@dataclass(frozen=True, slots=True)
class ResourceChange:
    """One entry from a plan's `resource_changes` list."""

    address: str = ""
    module_address: str = ""
    mode: ResourceMode = ResourceMode.MANAGED
    type: str = ""
    name: str = ""
    provider_name: str = ""
    change: Change = field(default_factory=Change)
    action_reason: str | None = None
    index: object = None
    deposed: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = frozenset({
        "address", "module_address", "mode", "type", "name", "provider_name",
        "change", "action_reason", "index", "deposed",
    })

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ResourceChange:
        log_unknown_keys("ResourceChange", d, cls._KNOWN_KEYS)
        mode_raw = get_str(d, "mode")
        mode = ResourceMode(mode_raw) if mode_raw else ResourceMode.MANAGED
        change_raw = get_dict(d, "change", {}) or {}
        return cls(
            address=get_str(d, "address", "") or "",
            module_address=get_str(d, "module_address", "") or "",
            mode=mode,
            type=get_str(d, "type", "") or "",
            name=get_str(d, "name", "") or "",
            provider_name=get_str(d, "provider_name", "") or "",
            change=Change.from_dict(change_raw),
            action_reason=get_str(d, "action_reason"),
            index=d.get("index"),
            deposed=get_str(d, "deposed"),
            raw=d,
        )

    def __repr__(self) -> str:
        actions = [a.value for a in self.change.actions]
        return f"ResourceChange(address={self.address!r}, actions={actions})"


@dataclass(frozen=True, slots=True)
class OutputChange:
    """One entry from a plan's `output_changes` mapping."""

    name: str = ""
    change: Change = field(default_factory=Change)
    sensitive: bool = False
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, name: str, d: Mapping[str, object]) -> OutputChange:
        return cls(
            name=name,
            change=Change.from_dict(d),
            sensitive=_any_sensitive(d.get("after_sensitive"))
            or _any_sensitive(d.get("before_sensitive")),
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """The full static plan representation from `show -json <planfile>`."""

    format_version: str = ""
    terraform_version: str = ""
    variables: Mapping[str, object] = field(default_factory=dict)
    planned_values: Mapping[str, object] = field(default_factory=dict)
    resource_changes: tuple[ResourceChange, ...] = ()
    output_changes: Mapping[str, OutputChange] = field(default_factory=dict)
    prior_state: Mapping[str, object] = field(default_factory=dict)
    configuration: Mapping[str, object] = field(default_factory=dict)
    relevant_attributes: tuple[object, ...] = ()
    checks: tuple[object, ...] = ()
    timestamp: str | None = None
    applyable: bool = False
    complete: bool = True
    errored: bool = False
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = frozenset({
        "format_version", "terraform_version", "variables", "planned_values",
        "resource_changes", "output_changes", "prior_state", "configuration",
        "relevant_attributes", "checks", "timestamp", "applyable", "complete", "errored",
    })

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Plan:
        log_unknown_keys("Plan", d, cls._KNOWN_KEYS)
        rc_raw = get_list(d, "resource_changes", ()) or ()
        resource_changes = tuple(
            ResourceChange.from_dict(rc) for rc in rc_raw if isinstance(rc, Mapping)
        )
        oc_raw = get_dict(d, "output_changes", {}) or {}
        output_changes = {
            name: OutputChange.from_dict(name, v)
            for name, v in oc_raw.items()
            if isinstance(v, Mapping)
        }
        return cls(
            format_version=get_str(d, "format_version", "") or "",
            terraform_version=get_str(d, "terraform_version", "") or "",
            variables=get_dict(d, "variables", {}) or {},
            planned_values=get_dict(d, "planned_values", {}) or {},
            resource_changes=resource_changes,
            output_changes=output_changes,
            prior_state=get_dict(d, "prior_state", {}) or {},
            configuration=get_dict(d, "configuration", {}) or {},
            relevant_attributes=tuple(get_list(d, "relevant_attributes", ()) or ()),
            checks=tuple(get_list(d, "checks", ()) or ()),
            timestamp=get_str(d, "timestamp"),
            applyable=get_bool(d, "applyable", False) or False,
            complete=bool(get_bool(d, "complete", True)),
            errored=get_bool(d, "errored", False) or False,
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanResult(CommandResult):
    """The typed result of `plan(...)` / `plan_stream(...)`."""

    has_changes: bool = False
    change_summary: ChangeSummary = field(default_factory=ChangeSummary)
    resource_changes: tuple[ResourceChange, ...] = ()
    output_changes: Mapping[str, OutputChange] = field(default_factory=dict)
    plan_file: str | None = None
    plan: Plan | None = None
    events: tuple[Event, ...] = field(default=(), repr=False, compare=False)

    def __repr__(self) -> str:
        s = self.change_summary
        return f"PlanResult(has_changes={self.has_changes}, +{s.add} ~{s.change} -{s.remove})"


# -- state, in both the `show -json` and `state pull` shapes -------------------
#
# Terraform and OpenTofu expose state in two JSON shapes, depending on how you
# obtained it:
#
# - `show -json` with no plan file returns the "state representation" format: a
#   `values.root_module` tree whose resources already carry a computed
#   `address`, with nested modules under `child_modules`.
# - `state pull` returns the raw state file format: a flat top-level `resources`
#   list, each entry holding one or more `instances` and no computed address, so
#   the address is built here from `module`, `type`, `name`, and the instance's
#   `index_key`.
#
# `State.from_dict` reads both, which is how `show_state()` and `state.pull()`
# share one model.


@dataclass(frozen=True, slots=True)
class StateResource:
    """One resource instance recorded in state."""

    address: str = ""
    mode: ResourceMode = ResourceMode.MANAGED
    type: str = ""
    name: str = ""
    provider_name: str = ""
    module_address: str = ""
    index: object = None
    schema_version: int = 0
    values: Mapping[str, object] = field(default_factory=dict)
    sensitive_values: Mapping[str, object] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"StateResource(address={self.address!r})"


def _resources_from_representation(module: Mapping[str, object]) -> list[StateResource]:
    """Flatten a `values.root_module` tree (state-representation format)."""
    out: list[StateResource] = []
    resources = get_list(module, "resources", ()) or ()
    for r in resources:
        if not isinstance(r, Mapping):
            continue
        mode_raw = get_str(r, "mode")
        mode = ResourceMode(mode_raw) if mode_raw else ResourceMode.MANAGED
        out.append(
            StateResource(
                address=get_str(r, "address", "") or "",
                mode=mode,
                type=get_str(r, "type", "") or "",
                name=get_str(r, "name", "") or "",
                provider_name=get_str(r, "provider_name", "") or "",
                module_address=get_str(module, "address", "") or "",
                index=r.get("index"),
                schema_version=get_int(r, "schema_version", 0) or 0,
                values=get_dict(r, "values", {}) or {},
                sensitive_values=get_dict(r, "sensitive_values", {}) or {},
                dependencies=get_str_tuple(r, "depends_on"),
                raw=r,
            )
        )
    for child in get_list(module, "child_modules", ()) or ():
        if isinstance(child, Mapping):
            out.extend(_resources_from_representation(child))
    return out


def _address_from_raw(
    module: str, mode: ResourceMode, type_: str, name: str, index_key: object
) -> str:
    prefix = f"{module}." if module and module != "root" else ""
    kind = "data." if mode is ResourceMode.DATA else ""
    base = f"{prefix}{kind}{type_}.{name}"
    if index_key is None:
        return base
    if isinstance(index_key, str):
        return f'{base}["{index_key}"]'
    return f"{base}[{index_key}]"


def _resources_from_raw_state(resources: Sequence[object]) -> list[StateResource]:
    """Expand a raw state file's top-level `resources` list (one entry per
    resource, each fanning out over its `instances`) into flat StateResources."""
    out: list[StateResource] = []
    for r in resources:
        if not isinstance(r, Mapping):
            continue
        mode_raw = get_str(r, "mode")
        mode = ResourceMode(mode_raw) if mode_raw else ResourceMode.MANAGED
        type_ = get_str(r, "type", "") or ""
        name = get_str(r, "name", "") or ""
        module = get_str(r, "module", "root") or "root"
        provider = get_str(r, "provider", "") or ""
        instances = get_list(r, "instances", ()) or ()
        if not instances:
            out.append(
                StateResource(
                    address=_address_from_raw(module, mode, type_, name, None),
                    mode=mode,
                    type=type_,
                    name=name,
                    provider_name=provider,
                    module_address="" if module == "root" else module,
                    raw=r,
                )
            )
            continue
        for inst in instances:
            if not isinstance(inst, Mapping):
                continue
            index_key = inst.get("index_key")
            out.append(
                StateResource(
                    address=_address_from_raw(module, mode, type_, name, index_key),
                    mode=mode,
                    type=type_,
                    name=name,
                    provider_name=provider,
                    module_address="" if module == "root" else module,
                    index=index_key,
                    schema_version=get_int(inst, "schema_version", 0) or 0,
                    values=get_dict(inst, "attributes", {}) or {},
                    dependencies=get_str_tuple(inst, "dependencies"),
                    raw=inst,
                )
            )
    return out


@dataclass(frozen=True, slots=True)
class State:
    """A snapshot of Terraform/OpenTofu state, from either `show -json` or
    `state pull`."""

    format_version: str = ""
    terraform_version: str = ""
    serial: int | None = None
    lineage: str | None = None
    outputs: Mapping[str, OutputValue] = field(default_factory=dict)
    resources: tuple[StateResource, ...] = ()
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> State:
        outputs_raw = get_dict(d, "outputs", {}) or {}
        outputs = {
            k: OutputValue.from_dict(v) for k, v in outputs_raw.items() if isinstance(v, Mapping)
        }

        values = get_dict(d, "values")
        if values is not None:
            root_module = get_dict(values, "root_module", {}) or {}
            resources = tuple(_resources_from_representation(root_module))
            values_outputs = get_dict(values, "outputs")
            if values_outputs:
                outputs = {
                    k: OutputValue.from_dict(v)
                    for k, v in values_outputs.items()
                    if isinstance(v, Mapping)
                }
            return cls(
                format_version=get_str(d, "format_version", "") or "",
                terraform_version=get_str(d, "terraform_version", "") or "",
                outputs=outputs,
                resources=resources,
                raw=d,
            )

        resources_raw = get_list(d, "resources", ()) or ()
        resources = tuple(_resources_from_raw_state(resources_raw))
        return cls(
            format_version=str(d.get("version", "")) if d.get("version") is not None else "",
            terraform_version=get_str(d, "terraform_version", "") or "",
            serial=get_int(d, "serial"),
            lineage=get_str(d, "lineage"),
            outputs=outputs,
            resources=resources,
            raw=d,
        )

    def __repr__(self) -> str:
        return f"State(resources={len(self.resources)}, outputs={len(self.outputs)})"


# -- validate -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidateResult:
    """Parsed `terraform validate -json` / `tofu validate -json` output."""

    valid: bool = True
    error_count: int = 0
    warning_count: int = 0
    diagnostics: tuple[Diagnostic, ...] = ()
    format_version: str = ""
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = frozenset({
        "valid", "error_count", "warning_count", "diagnostics", "format_version",
    })

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ValidateResult:
        log_unknown_keys("ValidateResult", d, cls._KNOWN_KEYS)
        diagnostics = get_tuple_of(d, "diagnostics", Diagnostic.from_dict)
        error_count = get_int(d, "error_count")
        if error_count is None:
            error_count = sum(1 for diag in diagnostics if diag.severity.value == "error")
        warning_count = get_int(d, "warning_count")
        if warning_count is None:
            warning_count = sum(1 for diag in diagnostics if diag.severity.value == "warning")
        valid = get_bool(d, "valid")
        if valid is None:
            valid = error_count == 0
        return cls(
            valid=valid,
            error_count=error_count,
            warning_count=warning_count,
            diagnostics=diagnostics,
            format_version=str(d.get("format_version", "")),
            raw=d,
        )

    def __repr__(self) -> str:
        return (
            f"ValidateResult(valid={self.valid}, errors={self.error_count}, "
            f"warnings={self.warning_count})"
        )


# -- version ------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


@total_ordering
@dataclass(frozen=True, slots=True)
class VersionTuple:
    """A parsed semantic version, comparable with `<`, `<=`, `>`, `>=`, `==`.

    Only major/minor/patch participate in ordering. A prerelease suffix is
    kept for display but does not affect comparisons (build metadata never
    does), which keeps a `>= (1, 6, 0)` capability check satisfied by
    `1.6.0-rc1` instead of failing on semver prerelease precedence.
    """

    major: int
    minor: int
    patch: int
    prerelease: str = ""
    build: str = ""

    @classmethod
    def parse(cls, text: str) -> VersionTuple:
        m = _SEMVER_RE.match(text.strip())
        if not m:
            return cls(0, 0, 0)
        return cls(
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            prerelease=m.group("pre") or "",
            build=m.group("build") or "",
        )

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionTuple):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        # This must agree with __eq__: equal versions hash equal. Defining it
        # in the class body also stops the frozen dataclass from generating a
        # __hash__ over all fields. That __hash__ would hash 1.6.0 and
        # 1.6.0-rc1 differently, even though they compare equal.
        return hash(self._key())

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, VersionTuple):
            return NotImplemented
        return self._key() < other._key()

    def __repr__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += f"-{self.prerelease}"
        return f"VersionTuple({s!r})"

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += f"-{self.prerelease}"
        if self.build:
            s += f"+{self.build}"
        return s


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Parsed `version -json` output for either Terraform or OpenTofu."""

    version: str
    version_tuple: VersionTuple
    platform: str = ""
    provider_selections: Mapping[str, str] = field(default_factory=dict)
    outdated: bool = False
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = frozenset({
        "terraform_version", "version", "platform", "provider_selections",
        "terraform_outdated", "outdated",
    })

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> VersionInfo:
        log_unknown_keys("VersionInfo", d, cls._KNOWN_KEYS)
        version = get_str(d, "terraform_version") or get_str(d, "version") or ""
        provider_selections = get_dict(d, "provider_selections", {}) or {}
        return cls(
            version=version,
            version_tuple=VersionTuple.parse(version),
            platform=get_str(d, "platform", "") or "",
            provider_selections={
                k: v for k, v in provider_selections.items() if isinstance(v, str)
            },
            outdated=bool(d.get("terraform_outdated", d.get("outdated", False))),
            raw=d,
        )

    def __repr__(self) -> str:
        return f"VersionInfo(version={self.version!r}, platform={self.platform!r})"


# -- apply / destroy / init / fmt results -------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceProgress:
    """Per-resource apply progress, correlated from `apply_start`/`apply_progress`/
    `apply_complete`/`apply_errored` streaming events by resource address."""

    address: str
    action: ChangeOperation = ChangeOperation.NOOP
    id_key: str | None = None
    id_value: str | None = None
    elapsed_seconds: float = 0.0
    errored: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyResult(CommandResult):
    """The typed result of `apply(...)` / `destroy(...)`."""

    change_summary: ChangeSummary = field(default_factory=ChangeSummary)
    outputs: Mapping[str, OutputValue] = field(default_factory=dict)
    resources: tuple[ResourceProgress, ...] = ()
    events: tuple[Event, ...] = field(default=(), repr=False, compare=False)

    def __repr__(self) -> str:
        s = self.change_summary
        return f"ApplyResult(+{s.add} ~{s.change} -{s.remove})"


@dataclass(frozen=True, slots=True, kw_only=True)
class InitResult(CommandResult):
    """The typed result of `init(...)`."""

    upgraded: bool = False
    reconfigured: bool = False
    events: tuple[Event, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class FmtResult(CommandResult):
    """The typed result of `fmt(...)`."""

    changed_files: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.changed_files


__all__ = [
    "Pos",
    "Range",
    "Snippet",
    "Diagnostic",
    "ChangeSummary",
    "OutputValue",
    "CommandResult",
    "Change",
    "ResourceChange",
    "OutputChange",
    "Plan",
    "PlanResult",
    "State",
    "StateResource",
    "ValidateResult",
    "VersionInfo",
    "VersionTuple",
    "ResourceProgress",
    "ApplyResult",
    "InitResult",
    "FmtResult",
]
