"""The streaming event hierarchy and JSONL line parsing.

With `-json`, Terraform and OpenTofu write newline-delimited JSON to stdout.
Every line has a common envelope: `@level`, `@message`, `@module`,
`@timestamp`, and `type`. The format is documented at
https://developer.hashicorp.com/terraform/internals/machine-readable-ui.
`parse_event_line` decodes one line into a typed `Event` subclass, dispatching
on `type` through `EVENT_REGISTRY` and falling back to the base `Event` for an
unknown `type`. A line that is not valid JSON becomes a `MalformedLineEvent`.
All line and JSON parsing in the package happens here: the driver feeds raw
bytes to `LineAssembler`, and every assembled line is parsed by this module.
"""

from __future__ import annotations

import codecs
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypedDict

from ._parsing import get_dict, get_float, get_str
from .enums import ChangeOperation, Level, MessageType
from .models import ChangeSummary, Diagnostic, OutputValue

_log = logging.getLogger("terrapy")

DEFAULT_MAX_LINE_BYTES = 10 * 1024 * 1024


class _Envelope(TypedDict):
    level: Level
    message: str
    module: str
    timestamp: str
    type: str
    raw: Mapping[str, object]


def _envelope(d: Mapping[str, object]) -> _Envelope:
    """The common `@level`/`@message`/`@module`/`@timestamp`/`type`/`raw`
    fields every event carries, so no subclass re-lists them or builds a
    throwaway base `Event` to copy them off."""
    level_raw = get_str(d, "@level")
    return {
        "level": Level(level_raw) if level_raw else Level.INFO,
        "message": get_str(d, "@message", "") or "",
        "module": get_str(d, "@module", "") or "",
        "timestamp": get_str(d, "@timestamp", "") or "",
        "type": get_str(d, "type", "") or "",
        "raw": d,
    }


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for every streamed event; unrecognized `type` values still
    parse into this class with all data preserved in `.raw`."""

    level: Level = Level.INFO
    message: str = ""
    module: str = ""
    timestamp: str = ""
    type: str = ""
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Event:
        return cls(**_envelope(d))


@dataclass(frozen=True, slots=True)
class LogEvent(Event):
    """A plain `type=log` line, or a non-JSON / malformed line preserved as text."""


@dataclass(frozen=True, slots=True)
class MalformedLineEvent(LogEvent):
    """A line that could not be parsed as JSON at all. `.raw['text']` holds it."""


@dataclass(frozen=True, slots=True)
class TruncatedLineEvent(LogEvent):
    """A line exceeded `max_line_bytes` and was discarded up to the next newline."""


@dataclass(frozen=True, slots=True)
class VersionEvent(Event):
    """`type=version`: emitted once at the start of a run."""

    terraform_version: str = ""
    ui_version: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> VersionEvent:
        return cls(
            **_envelope(d),
            terraform_version=get_str(d, "terraform", "") or "",
            ui_version=get_str(d, "ui", "") or "",
        )


@dataclass(frozen=True, slots=True)
class DiagnosticEvent(Event):
    """`type=diagnostic`: an in-stream error or warning."""

    diagnostic: Diagnostic = field(default_factory=Diagnostic)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> DiagnosticEvent:
        diag_raw = get_dict(d, "diagnostic", {}) or {}
        return cls(**_envelope(d), diagnostic=Diagnostic.from_dict(diag_raw))


@dataclass(frozen=True, slots=True)
class ResourceAddr:
    """The `resource` object attached to change/hook events."""

    addr: str = ""
    module: str = ""
    resource: str = ""
    implied_provider: str = ""
    resource_type: str = ""
    resource_name: str = ""
    resource_key: object = None

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ResourceAddr:
        return cls(
            addr=get_str(d, "addr", "") or "",
            module=get_str(d, "module", "") or "",
            resource=get_str(d, "resource", "") or "",
            implied_provider=get_str(d, "implied_provider", "") or "",
            resource_type=get_str(d, "resource_type", "") or "",
            resource_name=get_str(d, "resource_name", "") or "",
            resource_key=d.get("resource_key"),
        )


class _ChangeFields(TypedDict):
    resource: ResourceAddr
    action: ChangeOperation
    reason: str


def _change_fields(d: Mapping[str, object]) -> _ChangeFields:
    """The `change.resource`/`change.action`/`change.reason` fields shared by
    `planned_change` and `resource_drift`."""
    change = get_dict(d, "change", {}) or {}
    resource_raw = get_dict(change, "resource", {}) or {}
    return {
        "resource": ResourceAddr.from_dict(resource_raw),
        "action": ChangeOperation(get_str(change, "action") or "no-op"),
        "reason": get_str(change, "reason", "") or "",
    }


@dataclass(frozen=True, slots=True)
class PlannedChangeEvent(Event):
    """`type=planned_change`: one resource's planned action, as it streams in."""

    resource: ResourceAddr = field(default_factory=ResourceAddr)
    action: ChangeOperation = ChangeOperation.NOOP
    reason: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> PlannedChangeEvent:
        return cls(**_envelope(d), **_change_fields(d))


@dataclass(frozen=True, slots=True)
class ResourceDriftEvent(PlannedChangeEvent):
    """`type=resource_drift`: a resource changed outside of Terraform/OpenTofu."""

    previous_run_action: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ResourceDriftEvent:
        change = get_dict(d, "change", {}) or {}
        return cls(
            **_envelope(d),
            **_change_fields(d),
            previous_run_action=get_str(change, "previous_run_action", "") or "",
        )


@dataclass(frozen=True, slots=True)
class ChangeSummaryEvent(Event):
    """`type=change_summary`: the final (or plan) tally of add/change/remove."""

    summary: ChangeSummary = field(default_factory=ChangeSummary)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ChangeSummaryEvent:
        changes_raw = get_dict(d, "changes", {}) or {}
        return cls(**_envelope(d), summary=ChangeSummary.from_dict(changes_raw))


@dataclass(frozen=True, slots=True)
class OutputsEvent(Event):
    """`type=outputs`: the final output values."""

    outputs: Mapping[str, OutputValue] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> OutputsEvent:
        outputs_raw = get_dict(d, "outputs", {}) or {}
        outputs = {
            k: OutputValue.from_dict(v) for k, v in outputs_raw.items() if isinstance(v, Mapping)
        }
        return cls(**_envelope(d), outputs=outputs)


class _HookFields(TypedDict):
    address: str
    resource: ResourceAddr
    action: ChangeOperation
    id_key: str | None
    id_value: str | None
    elapsed_seconds: float


def _hook_fields(d: Mapping[str, object]) -> _HookFields:
    """The `hook.*` fields shared by every apply/provision/refresh hook event."""
    hook = get_dict(d, "hook", {}) or {}
    resource = ResourceAddr.from_dict(get_dict(hook, "resource", {}) or {})
    return {
        "address": resource.addr,
        "resource": resource,
        "action": ChangeOperation(get_str(hook, "action") or "no-op"),
        "id_key": get_str(hook, "id_key"),
        "id_value": get_str(hook, "id_value"),
        "elapsed_seconds": get_float(hook, "elapsed_seconds", 0.0) or 0.0,
    }


@dataclass(frozen=True, slots=True)
class HookEvent(Event):
    """Base for the `apply_*`/`provision_*`/`refresh_*` per-resource hook events."""

    address: str = ""
    resource: ResourceAddr = field(default_factory=ResourceAddr)
    action: ChangeOperation = ChangeOperation.NOOP
    id_key: str | None = None
    id_value: str | None = None
    elapsed_seconds: float = 0.0

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> HookEvent:
        return cls(**_envelope(d), **_hook_fields(d))


@dataclass(frozen=True, slots=True)
class ApplyStartEvent(HookEvent):
    pass


@dataclass(frozen=True, slots=True)
class ApplyProgressEvent(HookEvent):
    pass


@dataclass(frozen=True, slots=True)
class ApplyCompleteEvent(HookEvent):
    pass


@dataclass(frozen=True, slots=True)
class ApplyErroredEvent(HookEvent):
    pass


@dataclass(frozen=True, slots=True)
class ProvisionStartEvent(HookEvent):
    provisioner: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> ProvisionStartEvent:
        hook = get_dict(d, "hook", {}) or {}
        return cls(
            **_envelope(d),
            **_hook_fields(d),
            provisioner=get_str(hook, "provisioner", "") or "",
        )


@dataclass(frozen=True, slots=True)
class ProvisionProgressEvent(ProvisionStartEvent):
    pass


@dataclass(frozen=True, slots=True)
class ProvisionCompleteEvent(ProvisionStartEvent):
    pass


@dataclass(frozen=True, slots=True)
class ProvisionErroredEvent(ProvisionStartEvent):
    pass


@dataclass(frozen=True, slots=True)
class RefreshStartEvent(HookEvent):
    pass


@dataclass(frozen=True, slots=True)
class RefreshCompleteEvent(HookEvent):
    pass


EVENT_REGISTRY: Mapping[str, Callable[[Mapping[str, object]], Event]] = {
    MessageType.VERSION.value: VersionEvent.from_dict,
    MessageType.LOG.value: LogEvent.from_dict,
    MessageType.DIAGNOSTIC.value: DiagnosticEvent.from_dict,
    MessageType.RESOURCE_DRIFT.value: ResourceDriftEvent.from_dict,
    MessageType.PLANNED_CHANGE.value: PlannedChangeEvent.from_dict,
    MessageType.CHANGE_SUMMARY.value: ChangeSummaryEvent.from_dict,
    MessageType.OUTPUTS.value: OutputsEvent.from_dict,
    MessageType.APPLY_START.value: ApplyStartEvent.from_dict,
    MessageType.APPLY_PROGRESS.value: ApplyProgressEvent.from_dict,
    MessageType.APPLY_COMPLETE.value: ApplyCompleteEvent.from_dict,
    MessageType.APPLY_ERRORED.value: ApplyErroredEvent.from_dict,
    MessageType.PROVISION_START.value: ProvisionStartEvent.from_dict,
    MessageType.PROVISION_PROGRESS.value: ProvisionProgressEvent.from_dict,
    MessageType.PROVISION_COMPLETE.value: ProvisionCompleteEvent.from_dict,
    MessageType.PROVISION_ERRORED.value: ProvisionErroredEvent.from_dict,
    MessageType.REFRESH_START.value: RefreshStartEvent.from_dict,
    MessageType.REFRESH_COMPLETE.value: RefreshCompleteEvent.from_dict,
}


def parse_event_line(line: str) -> Event:
    """Parse one complete JSONL line into a typed `Event`.

    A line that is not valid JSON, or whose top level is not a JSON object,
    becomes a `MalformedLineEvent` carrying the raw text. Malformed and
    unrecognized input never raises here, so one bad line does not end the
    run.
    """
    stripped = line.rstrip("\r\n")
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return MalformedLineEvent(message=stripped, type="", raw={"text": stripped})
    if not isinstance(payload, Mapping):
        return MalformedLineEvent(message=stripped, type="", raw={"text": stripped})
    msg_type = payload.get("type")
    parser = EVENT_REGISTRY.get(msg_type) if isinstance(msg_type, str) else None
    if parser is None:
        return Event.from_dict(payload)
    try:
        return parser(payload)
    except Exception:
        _log.debug("failed to parse %r event, falling back to base Event", msg_type, exc_info=True)
        return Event.from_dict(payload)


@dataclass(frozen=True, slots=True)
class AssembledLine:
    """One line produced by `LineAssembler`: either normal text, or a marker
    that the accumulated bytes for this line exceeded `max_line_bytes` and
    were discarded."""

    text: str
    truncated: bool = False


def event_from_line(assembled: AssembledLine) -> Event:
    """Convert one `LineAssembler` result into an `Event`."""
    if assembled.truncated:
        return TruncatedLineEvent(
            level=Level.WARN,
            message="line truncated: exceeded max_line_bytes",
            type="truncated_line",
        )
    return parse_event_line(assembled.text)


class LineAssembler:
    """Incrementally assembles complete `\\n`-terminated lines from a byte stream.

    Decodes UTF-8 incrementally (so multibyte characters split across reads
    are handled correctly), strips a trailing `\\r`, strips a leading BOM on
    the very first line, and enforces `max_line_bytes` so a pathological
    stream cannot grow the internal buffer without bound: as soon as the
    buffered (not yet newline-terminated) text exceeds the limit, it is
    discarded immediately and the next completed line is reported as
    `truncated=True` instead of carrying the (partial, discarded) text.
    """

    def __init__(
        self, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES, errors: str = "replace"
    ) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors=errors)
        self._buf = ""
        self._max_line_bytes = max_line_bytes
        self._discarding = False
        self._seen_first_line = False

    def feed(self, chunk: bytes) -> list[AssembledLine]:
        """Feed raw bytes; returns zero or more complete lines (without the newline)."""
        text = self._decoder.decode(chunk)
        if text:
            self._buf += text
        lines: list[AssembledLine] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if self._discarding:
                self._discarding = False
                lines.append(AssembledLine(text="", truncated=True))
                continue
            if self._exceeds_limit(line):
                lines.append(AssembledLine(text="", truncated=True))
                continue
            lines.append(AssembledLine(text=self._normalize(line)))
        if self._exceeds_limit(self._buf):
            self._buf = ""
            self._discarding = True
        return lines

    def _exceeds_limit(self, text: str) -> bool:
        # A str of N code points is at most 4*N UTF-8 bytes, so a character
        # count clears the common short line without encoding it. Only text
        # near the limit is encoded to get the exact byte length.
        if len(text) * 4 <= self._max_line_bytes:
            return False
        return len(text.encode("utf-8", errors="replace")) > self._max_line_bytes

    def _normalize(self, line: str) -> str:
        if line.endswith("\r"):
            line = line[:-1]
        if not self._seen_first_line:
            self._seen_first_line = True
            if line.startswith("﻿"):
                line = line[1:]
        return line

    def flush(self) -> AssembledLine | None:
        """Called at EOF: returns any trailing unterminated line, or None."""
        self._buf += self._decoder.decode(b"", final=True)
        if self._discarding:
            self._discarding = False
            self._buf = ""
            return AssembledLine(text="", truncated=True)
        if not self._buf:
            return None
        remainder = self._normalize(self._buf)
        self._buf = ""
        if not remainder:
            return None
        return AssembledLine(text=remainder)


__all__ = [
    "Event",
    "LogEvent",
    "MalformedLineEvent",
    "TruncatedLineEvent",
    "VersionEvent",
    "DiagnosticEvent",
    "ResourceAddr",
    "PlannedChangeEvent",
    "ResourceDriftEvent",
    "ChangeSummaryEvent",
    "OutputsEvent",
    "HookEvent",
    "ApplyStartEvent",
    "ApplyProgressEvent",
    "ApplyCompleteEvent",
    "ApplyErroredEvent",
    "ProvisionStartEvent",
    "ProvisionProgressEvent",
    "ProvisionCompleteEvent",
    "ProvisionErroredEvent",
    "RefreshStartEvent",
    "RefreshCompleteEvent",
    "EVENT_REGISTRY",
    "parse_event_line",
    "AssembledLine",
    "LineAssembler",
    "event_from_line",
]
