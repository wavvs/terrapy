"""The tolerant `_parsing` getters. Every model's `from_dict` leans on these
to survive a wire payload that disagrees with the documented schema, so the
wrong-type and missing-key paths are the ones that matter."""

from __future__ import annotations

import logging

import pytest

from terrapy._parsing import (
    get_bool,
    get_dict,
    get_float,
    get_int,
    get_list,
    get_nested,
    get_str,
    get_str_tuple,
    get_tuple_of,
    log_unknown_keys,
)


@pytest.mark.parametrize("payload,expected", [
    ({"k": "hi"}, "hi"),
    ({"k": 3}, "fallback"),      # wrong type falls back
    ({"k": None}, "fallback"),
    ({}, "fallback"),
])
def test_get_str(payload, expected) -> None:
    assert get_str(payload, "k", "fallback") == expected


def test_get_str_default_is_none() -> None:
    assert get_str({}, "k") is None


@pytest.mark.parametrize("payload,expected", [
    ({"k": 5}, 5),
    ({"k": "7"}, 7),             # numeric strings are coerced
    ({"k": "nope"}, -1),         # non-numeric strings fall back
    ({"k": True}, -1),           # bool is NOT an int here
    ({"k": None}, -1),
    ({}, -1),
])
def test_get_int(payload, expected) -> None:
    assert get_int(payload, "k", -1) == expected


@pytest.mark.parametrize("payload,expected", [
    ({"k": 1.5}, 1.5),
    ({"k": 2}, 2.0),
    ({"k": "3.5"}, 3.5),
    ({"k": "nope"}, -1.0),
    ({"k": True}, -1.0),
    ({}, -1.0),
])
def test_get_float(payload, expected) -> None:
    assert get_float(payload, "k", -1.0) == expected


@pytest.mark.parametrize("payload,expected", [
    ({"k": True}, True),
    ({"k": False}, False),
    ({"k": "yes"}, None),        # strings are not coerced to bool
    ({"k": 1}, None),
    ({}, None),
])
def test_get_bool(payload, expected) -> None:
    assert get_bool(payload, "k") is expected


def test_get_dict_and_list_reject_wrong_types() -> None:
    assert get_dict({"k": {"a": 1}}, "k") == {"a": 1}
    assert get_dict({"k": [1, 2]}, "k") is None
    assert get_dict({}, "k", {}) == {}

    assert get_list({"k": [1, 2]}, "k") == [1, 2]
    # a str is a Sequence but never the list we mean
    assert get_list({"k": "ab"}, "k", ()) == ()
    assert get_list({"k": {"a": 1}}, "k", ()) == ()


def test_get_nested() -> None:
    made = get_nested({"k": {"n": 1}}, "k", lambda d: d["n"])
    assert made == 1
    assert get_nested({"k": "not a mapping"}, "k", lambda d: d) is None
    assert get_nested({}, "k", lambda d: d) is None


def test_get_tuple_of_skips_non_mappings() -> None:
    payload = {"items": [{"n": 1}, "junk", 5, {"n": 2}]}
    assert get_tuple_of(payload, "items", lambda d: d["n"]) == (1, 2)
    assert get_tuple_of({}, "items", lambda d: d) == ()


def test_get_str_tuple_skips_non_strings() -> None:
    assert get_str_tuple({"k": ["a", 1, None, "b"]}, "k") == ("a", "b")
    assert get_str_tuple({}, "k") == ()


def test_log_unknown_keys_reports_only_the_extras(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="terrapy"):
        log_unknown_keys("Thing", {"known": 1, "surprise": 2}, frozenset({"known"}))
    assert "surprise" in caplog.text
    assert "known" not in caplog.text.replace("unknown", "")


def test_log_unknown_keys_silent_when_all_known(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="terrapy"):
        log_unknown_keys("Thing", {"known": 1}, frozenset({"known"}))
    assert caplog.text == ""
