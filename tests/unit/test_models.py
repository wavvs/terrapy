"""`from_dict` against real fixtures, a "future" fixture with injected unknown
keys and enum values to lock in forward-compatibility, plus version ordering,
change classification, sensitivity detection and the reprs that redact."""

from __future__ import annotations

import pytest

from terrapy.enums import Action, DiagnosticSeverity
from terrapy.exceptions import ExecutionError, ValidationError
from terrapy.models import (
    Change,
    ChangeSummary,
    Diagnostic,
    OutputValue,
    Plan,
    Snippet,
    State,
    ValidateResult,
    VersionInfo,
    VersionTuple,
    _any_sensitive,
)


def test_version_info_from_dict(json_fixture) -> None:
    info = VersionInfo.from_dict(json_fixture("version_terraform.json"))
    assert info.version == "1.9.5"
    assert info.version_tuple == VersionTuple(1, 9, 5)
    assert info.platform == "linux_amd64"
    assert info.provider_selections["registry.terraform.io/hashicorp/aws"] == "5.60.0"
    assert info.outdated is False


@pytest.mark.parametrize("text,expected", [
    ("1.9.5", (1, 9, 5, "", "")),
    ("  1.9.5  ", (1, 9, 5, "", "")),
    ("1.6.0-rc1", (1, 6, 0, "rc1", "")),
    ("1.6.0+build.7", (1, 6, 0, "", "build.7")),
    ("1.6.0-rc1+build.7", (1, 6, 0, "rc1", "build.7")),
    # Strict semver only. `version -json` always emits major.minor.patch, so a
    # "v" prefix or a two-part version is malformed input, not a supported form.
    ("v1.9.5", (0, 0, 0, "", "")),
    ("1.9", (0, 0, 0, "", "")),
    ("not-a-version", (0, 0, 0, "", "")),
    ("", (0, 0, 0, "", "")),
])
def test_version_tuple_parse(text, expected) -> None:
    v = VersionTuple.parse(text)
    assert (v.major, v.minor, v.patch, v.prerelease, v.build) == expected


def test_version_ordering_ignores_prerelease_and_build() -> None:
    assert VersionTuple.parse("1.6.0-rc1") == VersionTuple(1, 6, 0, prerelease="rc1")
    assert VersionTuple.parse("1.6.0") == VersionTuple.parse("1.6.0-rc1")
    assert hash(VersionTuple.parse("1.6.0")) == hash(VersionTuple.parse("1.6.0-rc1"))
    assert VersionTuple.parse("1.6.0") < VersionTuple.parse("1.9.5")
    assert VersionTuple.parse("1.9.5") >= VersionTuple(1, 6, 0)
    assert sorted(VersionTuple.parse(s) for s in ("1.9.0", "0.15.0", "1.6.0")) == [
        VersionTuple.parse("0.15.0"),
        VersionTuple.parse("1.6.0"),
        VersionTuple.parse("1.9.0"),
    ]


def test_version_tuple_comparison_with_other_types_is_not_implemented() -> None:
    assert VersionTuple.parse("1.0.0").__eq__("1.0.0") is NotImplemented
    assert VersionTuple.parse("1.0.0").__lt__("1.0.0") is NotImplemented
    assert VersionTuple.parse("1.0.0") != "1.0.0"


def test_version_tuple_repr_and_str() -> None:
    assert str(VersionTuple.parse("1.9.5")) == "1.9.5"
    assert str(VersionTuple.parse("1.6.0-rc1")) == "1.6.0-rc1"
    assert str(VersionTuple.parse("1.6.0-rc1+b7")) == "1.6.0-rc1+b7"
    assert repr(VersionTuple.parse("1.6.0-rc1")) == "VersionTuple('1.6.0-rc1')"


def test_validate_invalid_fixture(json_fixture) -> None:
    result = ValidateResult.from_dict(json_fixture("validate_invalid.json"))
    assert result.valid is False
    assert result.error_count == 1
    assert result.diagnostics[0].severity is DiagnosticSeverity.ERROR
    assert result.diagnostics[0].range is not None
    assert result.diagnostics[0].range.filename == "main.tf"
    assert result.diagnostics[0].range.start.line == 12


def test_plan_show_fixture(json_fixture) -> None:
    plan = Plan.from_dict(json_fixture("plan_show.json"))
    assert plan.terraform_version == "1.9.5"
    assert len(plan.resource_changes) == 2

    created = plan.resource_changes[0]
    assert created.address == "aws_instance.web[0]"
    assert created.change.is_create
    assert not created.change.is_replace

    replaced = plan.resource_changes[1]
    assert replaced.change.is_replace
    assert replaced.action_reason == "replace_because_cannot_update"

    assert "vpc_id" in plan.output_changes
    assert plan.output_changes["vpc_id"].change.after == "vpc-0abc123"


def test_state_from_raw_state_fixture(json_fixture) -> None:
    state = State.from_dict(json_fixture("state_pull.json"))
    assert state.serial == 3
    assert state.lineage == "11111111-2222-3333-4444-555555555555"
    assert state.outputs["vpc_id"].value == "vpc-0abc123"
    addresses = {r.address for r in state.resources}
    assert "aws_instance.web" in addresses
    assert 'module.pool.aws_instance.web[0]' in addresses


def test_state_from_representation_shape() -> None:
    payload = {
        "format_version": "1.2",
        "terraform_version": "1.9.5",
        "values": {
            "outputs": {"vpc_id": {"value": "vpc-1", "type": "string", "sensitive": False}},
            "root_module": {
                "resources": [
                    {
                        "address": "aws_instance.web",
                        "mode": "managed",
                        "type": "aws_instance",
                        "name": "web",
                        "provider_name": "registry.terraform.io/hashicorp/aws",
                        "values": {"id": "i-1"},
                    }
                ],
                "child_modules": [
                    {
                        "address": "module.pool",
                        "resources": [
                            {
                                "address": "module.pool.aws_instance.web",
                                "mode": "managed",
                                "type": "aws_instance",
                                "name": "web",
                                "provider_name": "registry.terraform.io/hashicorp/aws",
                                "values": {"id": "i-2"},
                            }
                        ],
                        "child_modules": [],
                    }
                ],
            },
        },
    }
    state = State.from_dict(payload)
    addresses = {r.address for r in state.resources}
    assert addresses == {"aws_instance.web", "module.pool.aws_instance.web"}
    assert state.outputs["vpc_id"].value == "vpc-1"


def test_diagnostic_tolerates_unknown_severity_and_extra_fields(json_fixture) -> None:
    diag = Diagnostic.from_dict(json_fixture("future_unknown.json"))
    assert diag.severity.value == "catastrophic"
    assert diag.summary == "A brand new diagnostic severity from a future release"
    assert diag.raw["future_field_nobody_has_seen_yet"] == {"nested": True}


def test_change_summary_repr_is_compact() -> None:
    s = ChangeSummary(add=3, change=1, remove=0)
    assert repr(s) == "ChangeSummary(+3 ~1 -0)"


# -- Change classification ----------------------------------------------------


@pytest.mark.parametrize("actions,flag", [
    ((), "is_noop"),
    ((Action.NOOP,), "is_noop"),
    ((Action.CREATE,), "is_create"),
    ((Action.READ,), "is_read"),
    ((Action.UPDATE,), "is_update"),
    ((Action.DELETE,), "is_delete"),
    ((Action.CREATE, Action.DELETE), "is_replace"),
    ((Action.DELETE, Action.CREATE), "is_replace"),
    ((Action.FORGET,), "is_forget"),
])
def test_change_classification(actions, flag) -> None:
    change = Change(actions=actions)
    assert getattr(change, flag) is True
    # A replace is neither a plain create nor a plain delete.
    if flag == "is_replace":
        assert change.is_create is False
        assert change.is_delete is False


def test_change_from_dict_reads_actions_and_values() -> None:
    change = Change.from_dict({
        "actions": ["create", "delete"],
        "before": None,
        "after": {"name": "web"},
        "after_unknown": {"id": True},
    })
    assert change.is_replace is True
    assert change.after == {"name": "web"}


def test_change_from_dict_tolerates_unknown_action() -> None:
    change = Change.from_dict({"actions": ["teleport"]})
    # Unknown wire actions must not crash the parse.
    assert change.actions != ()


# -- sensitivity --------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    ({"password": True}, True),
    ({"a": False, "b": {"c": True}}, True),
    ({"a": False}, False),
    ([False, True], True),
    ([False, False], False),
    ((False, {"x": True}), True),
    ("string", False),
    (None, False),
    ({}, False),
    ([], False),
])
def test_any_sensitive(value, expected) -> None:
    assert _any_sensitive(value) is expected


def test_output_value_repr_redacts_sensitive() -> None:
    secret = OutputValue.from_dict({"value": "hunter2", "type": "string", "sensitive": True})
    assert "hunter2" not in repr(secret)
    assert "<sensitive>" in repr(secret)

    plain = OutputValue.from_dict({"value": "vpc-1", "type": "string", "sensitive": False})
    assert "vpc-1" in repr(plain)


# -- Snippet ------------------------------------------------------------------


def test_snippet_parses_traversal_values() -> None:
    snippet = Snippet.from_dict({
        "context": "resource",
        "code": "x = 1",
        "start_line": 3,
        "values": [{"traversal": "var.a", "statement": "is 1"}],
    })
    assert snippet.values == (("var.a", "is 1"),)
    assert snippet.start_line == 3


def test_snippet_accepts_plain_mapping_values() -> None:
    snippet = Snippet.from_dict({"values": {"var.a": "is 1"}})
    assert snippet.values == (("var.a", "is 1"),)


def test_snippet_ignores_junk_values() -> None:
    assert Snippet.from_dict({"values": "nope"}).values == ()
    assert Snippet.from_dict({"values": [1, "x"]}).values == ()


# -- exception rendering ------------------------------------------------------


def test_execution_error_str_includes_exit_code_and_command() -> None:
    exc = ExecutionError(
        "plan exited with code 1",
        command=("terraform", "plan", "-json"),
        exit_code=1,
        stdout="",
        stderr="boom",
        diagnostics=(),
    )
    text = str(exc)
    assert "exit 1" in text
    assert "terraform plan -json" in text


def test_validation_error_carries_the_result() -> None:
    result = ValidateResult(valid=False, error_count=1, diagnostics=(Diagnostic(summary="bad"),))
    exc = ValidationError(
        "validation failed",
        result=result,
        command=("terraform", "validate"),
        exit_code=1,
        stdout="",
        stderr="",
        diagnostics=(),
    )
    assert exc.result.error_count == 1
    assert exc.result.diagnostics[0].summary == "bad"
