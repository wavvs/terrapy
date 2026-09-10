"""terrapy: a typed Python wrapper for the Terraform and OpenTofu CLIs.

Synchronous, standard library only. Events from `-json` output are yielded as
the process writes them.
"""

from __future__ import annotations

import logging

from ._run_sync import Run
from ._version import __version__
from .client import (
    EventRouter,
    StateCommands,
    Terrapy,
    WorkspaceCommands,
)
from .discovery import find_binary, get_version_info
from .enums import (
    Action,
    ChangeOperation,
    DiagnosticSeverity,
    Level,
    MessageType,
    ResourceMode,
)
from .events import (
    ApplyCompleteEvent,
    ApplyErroredEvent,
    ApplyProgressEvent,
    ApplyStartEvent,
    ChangeSummaryEvent,
    DiagnosticEvent,
    Event,
    HookEvent,
    LogEvent,
    MalformedLineEvent,
    OutputsEvent,
    PlannedChangeEvent,
    ProvisionCompleteEvent,
    ProvisionErroredEvent,
    ProvisionProgressEvent,
    ProvisionStartEvent,
    RefreshCompleteEvent,
    RefreshStartEvent,
    ResourceAddr,
    ResourceDriftEvent,
    TruncatedLineEvent,
    VersionEvent,
)
from .exceptions import (
    BinaryNotFoundError,
    ExecutionError,
    InitializationError,
    LockError,
    TerrapyError,
    TerrapyTimeoutError,
    UnsupportedFeatureError,
    UsageError,
    ValidationError,
    VersionDetectionError,
)
from .models import (
    ApplyResult,
    Change,
    ChangeSummary,
    CommandResult,
    Diagnostic,
    FmtResult,
    InitResult,
    OutputChange,
    OutputValue,
    Plan,
    PlanResult,
    Pos,
    Range,
    ResourceChange,
    ResourceProgress,
    Snippet,
    State,
    StateResource,
    ValidateResult,
    VersionInfo,
    VersionTuple,
)

logging.getLogger("terrapy").addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    "find_binary",
    "get_version_info",
    # clients
    "Terrapy",
    "StateCommands",
    "WorkspaceCommands",
    "EventRouter",
    "Run",
    # enums
    "Level",
    "MessageType",
    "DiagnosticSeverity",
    "ChangeOperation",
    "Action",
    "ResourceMode",
    # events
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
    # exceptions
    "TerrapyError",
    "BinaryNotFoundError",
    "VersionDetectionError",
    "UnsupportedFeatureError",
    "UsageError",
    "TerrapyTimeoutError",
    "ExecutionError",
    "ValidationError",
    "LockError",
    "InitializationError",
    # models
    "CommandResult",
    "PlanResult",
    "ApplyResult",
    "InitResult",
    "FmtResult",
    "ValidateResult",
    "Plan",
    "ResourceChange",
    "Change",
    "OutputChange",
    "State",
    "StateResource",
    "OutputValue",
    "Diagnostic",
    "Pos",
    "Range",
    "Snippet",
    "ChangeSummary",
    "ResourceProgress",
    "VersionInfo",
    "VersionTuple",
]
