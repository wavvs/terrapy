"""Tolerant dict getters. Every model's `from_dict` uses them.

Terraform and OpenTofu add fields in new releases, and omit fields that do not
apply to a message. Each getter here returns the caller's default for a missing
key or a value of the wrong type, rather than raising, so a `from_dict` built
only from these getters never fails on a real payload it does not yet fully
recognize.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

_log = logging.getLogger("terrapy")

_T = TypeVar("_T")


def get_str(d: Mapping[str, object], key: str, default: str | None = None) -> str | None:
    v = d.get(key, default)
    return v if isinstance(v, str) else default


def get_int(d: Mapping[str, object], key: str, default: int | None = None) -> int | None:
    v = d.get(key, default)
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return default
    return default


def get_float(d: Mapping[str, object], key: str, default: float | None = None) -> float | None:
    v = d.get(key, default)
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return default
    return default


def get_bool(d: Mapping[str, object], key: str, default: bool | None = None) -> bool | None:
    v = d.get(key, default)
    return v if isinstance(v, bool) else default


def get_dict(
    d: Mapping[str, object], key: str, default: Mapping[str, object] | None = None
) -> Mapping[str, object] | None:
    v = d.get(key, default)
    return v if isinstance(v, Mapping) else default


def get_list(
    d: Mapping[str, object], key: str, default: Sequence[object] | None = None
) -> Sequence[object] | None:
    v = d.get(key, default)
    if isinstance(v, str):
        return default
    return v if isinstance(v, Sequence) else default


def get_nested(
    d: Mapping[str, object], key: str, from_dict: Callable[[Mapping[str, object]], _T]
) -> _T | None:
    v = get_dict(d, key)
    if v is None:
        return None
    return from_dict(v)


def get_tuple_of(
    d: Mapping[str, object], key: str, from_dict: Callable[[Mapping[str, object]], _T]
) -> tuple[_T, ...]:
    items = get_list(d, key, ())
    assert items is not None
    result: list[_T] = []
    for item in items:
        if isinstance(item, Mapping):
            result.append(from_dict(item))
        else:
            _log.debug("%s: skipping non-mapping element %r", key, item)
    return tuple(result)


def get_str_tuple(d: Mapping[str, object], key: str) -> tuple[str, ...]:
    items = get_list(d, key, ())
    assert items is not None
    return tuple(item for item in items if isinstance(item, str))


def log_unknown_keys(model_name: str, d: Mapping[str, object], known_keys: frozenset[str]) -> None:
    unknown = set(d.keys()) - known_keys
    if unknown:
        _log.debug("%s: unknown keys in payload: %s", model_name, sorted(unknown))
