"""Enums used across terrapy.
"""

from __future__ import annotations

import logging
from enum import Enum

_log = logging.getLogger("terrapy")


class _TolerantStrEnum(str, Enum):
    """A str Enum that synthesizes a pass-through member for unknown values.

    Unknown values are logged once at DEBUG and returned as a member whose
    `.value` (and string identity) is the original wire string. `isinstance`,
    `==`, and `str()` all behave normally for these synthesized members.
    """

    @classmethod
    def _missing_(cls, value: object) -> _TolerantStrEnum | None:
        if not isinstance(value, str):
            return None
        _log.debug("%s: unknown wire value %r, passing through", cls.__name__, value)
        pseudo = str.__new__(cls, value)
        pseudo._name_ = value
        pseudo._value_ = value
        return pseudo


class Level(_TolerantStrEnum):
    """The `@level` field on a streamed JSON line."""

    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class MessageType(_TolerantStrEnum):
    """The `type` discriminator on a streamed JSON line."""

    VERSION = "version"
    LOG = "log"
    DIAGNOSTIC = "diagnostic"
    RESOURCE_DRIFT = "resource_drift"
    PLANNED_CHANGE = "planned_change"
    CHANGE_SUMMARY = "change_summary"
    OUTPUTS = "outputs"
    APPLY_START = "apply_start"
    APPLY_PROGRESS = "apply_progress"
    APPLY_COMPLETE = "apply_complete"
    APPLY_ERRORED = "apply_errored"
    PROVISION_START = "provision_start"
    PROVISION_PROGRESS = "provision_progress"
    PROVISION_COMPLETE = "provision_complete"
    PROVISION_ERRORED = "provision_errored"
    REFRESH_START = "refresh_start"
    REFRESH_COMPLETE = "refresh_complete"
    TEST_ABORT = "test_abort"


class DiagnosticSeverity(_TolerantStrEnum):
    """`@level` / diagnostic `severity` values ('error' or 'warning')."""

    ERROR = "error"
    WARNING = "warning"


class ChangeOperation(_TolerantStrEnum):
    """A resource change action.

    Used both as the `change.action` summary on a streaming event and as one
    element of a `change.actions` tuple in the static `show -json` plan
    representation, which spells a replace as `["delete", "create"]` rather
    than `"replace"`. Both spellings parse.
    """

    NOOP = "no-op"
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    REPLACE = "replace"
    FORGET = "forget"


Action = ChangeOperation


class ResourceMode(_TolerantStrEnum):
    """Whether an address refers to a managed resource or a data source."""

    MANAGED = "managed"
    DATA = "data"
