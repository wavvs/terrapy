"""End-to-end client orchestration via `FakeRunner`: argv dispatch, typed
result construction from streamed events, error classification, and the thin
state/workspace/streaming wrappers."""

from __future__ import annotations

import json

import pytest

from terrapy.client import Terrapy
from terrapy.exceptions import (
    ExecutionError,
    InitializationError,
    LockError,
    UsageError,
    ValidationError,
)
from terrapy.runner import Runner

from ..conftest import FakeRun


@pytest.fixture
def client(fake_runner, monkeypatch, tmp_path) -> Terrapy:
    monkeypatch.setattr("terrapy.discovery.find_binary", lambda binary_path: "/usr/bin/terraform")
    return Terrapy(str(tmp_path), runner=fake_runner)


def argv_of(fake_runner, *needle: str) -> tuple[str, ...]:
    """The argv of the first recorded call whose tail matches `needle`."""
    for spec in fake_runner.calls:
        if needle == spec.argv[1 : 1 + len(needle)]:
            return spec.argv
    raise AssertionError(f"no call matching {needle}; got {[s.argv for s in fake_runner.calls]}")


def test_client_resolves_binary(client: Terrapy) -> None:
    assert client.binary_path == "/usr/bin/terraform"


def test_client_accepts_a_tofu_binary(fake_runner, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("terrapy.discovery.find_binary", lambda binary_path: "/usr/bin/tofu")
    assert Terrapy(str(tmp_path), runner=fake_runner).binary_path == "/usr/bin/tofu"


def test_plan_reports_has_changes_on_exit_2(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "plan"),
        FakeRun(
            exit_code=2,
            stdout_lines=['{"type":"change_summary","changes":{"add":1,"change":0,"remove":0}}'],
        ),
    )
    result = client.plan()
    assert result.has_changes is True
    assert result.change_summary.add == 1
    assert result.exit_code == 2


def test_plan_exit_1_raises_execution_error(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "plan"),
        FakeRun(exit_code=1, stderr="something went wrong"),
    )
    with pytest.raises(ExecutionError) as excinfo:
        client.plan()
    assert excinfo.value.exit_code == 1


def test_apply_builds_typed_result_from_events(client: Terrapy, fake_runner, jsonl_fixture) -> None:
    lines = jsonl_fixture("apply_stream.jsonl")
    fake_runner.register(("/usr/bin/terraform", "apply"), FakeRun(exit_code=0, stdout_lines=lines))
    seen = []
    result = client.apply(on_event=seen.append)
    assert result.change_summary.add == 3
    assert result.change_summary.change == 1
    assert result.outputs["vpc_id"].value == "vpc-0abc123"
    assert result.outputs["db_password"].sensitive is True
    assert len(result.resources) == 2
    assert {r.address for r in result.resources} == {"module.network.aws_vpc.main", "aws_instance.web[0]"}
    assert len(seen) == len(result.events)


def test_apply_lock_error_classified(client: Terrapy, fake_runner) -> None:
    stderr = (
        "Error acquiring the state lock\n\n"
        "Lock Info:\n"
        "  ID:        abc-123\n"
        "  Path:      terraform.tfstate\n"
        "  Operation: OperationTypeApply\n"
        "  Who:       me@host\n"
        "  Version:   1.9.5\n"
        "  Created:   2026-01-01 00:00:00\n"
    )
    fake_runner.register(("/usr/bin/terraform", "apply"), FakeRun(exit_code=1, stderr=stderr))
    with pytest.raises(LockError) as excinfo:
        client.apply()
    assert excinfo.value.lock_id == "abc-123"
    assert excinfo.value.lock_info["Path"] == "terraform.tfstate"


def test_init_failure_classified(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "init"),
        FakeRun(exit_code=1, stderr='Failed to configure backend "s3"'),
    )
    with pytest.raises(InitializationError):
        client.init()


def test_validate_raises_with_the_parsed_report_attached(
    client: Terrapy, fake_runner, json_fixture
) -> None:
    # Exit 1 is "ran, found problems". The -json report must be parsed before
    # the raise, so the diagnostics arrive on the exception.
    payload = json_fixture("validate_invalid.json")
    fake_runner.register(
        ("/usr/bin/terraform", "validate"),
        FakeRun(exit_code=1, stdout_lines=[json.dumps(payload)]),
    )
    with pytest.raises(ValidationError) as excinfo:
        client.validate()
    exc = excinfo.value
    assert exc.result.valid is False
    assert exc.result.diagnostics[0].summary == "Reference to undeclared resource"
    assert exc.diagnostics == exc.result.diagnostics
    assert exc.exit_code == 1


def test_validate_returns_the_report_when_valid(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "validate"),
        FakeRun(exit_code=0, stdout_lines=[json.dumps({"valid": True, "error_count": 0})]),
    )
    assert client.validate().valid is True


def test_validate_raises_when_validate_itself_fails(client: Terrapy, fake_runner) -> None:
    # Distinct from an invalid config: validate crashed, so there is no report.
    # It must surface as a plain ExecutionError, not a ValidationError with an
    # empty result standing in for one.
    fake_runner.register(
        ("/usr/bin/terraform", "validate"),
        FakeRun(exit_code=2, stdout_lines=[], stderr="could not load plugin schemas"),
    )
    with pytest.raises(ExecutionError) as excinfo:
        client.validate()
    assert not isinstance(excinfo.value, ValidationError)


def test_output_single_and_all(client: Terrapy, fake_runner) -> None:
    # `output -json NAME` prints the bare value with no metadata, so the client
    # always fetches the map form and filters it.
    payload = {
        "vpc_id": {"value": "vpc-1", "type": "string", "sensitive": False},
        "db_url": {"value": "postgres://x", "type": "string", "sensitive": True},
    }
    run = FakeRun(exit_code=0, stdout_lines=[json.dumps(payload)])
    fake_runner.register(("/usr/bin/terraform", "output"), run)

    outs = client.output()
    assert outs["vpc_id"].value == "vpc-1"

    one = client.output("vpc_id")
    assert one.value == "vpc-1"
    assert one.type == "string"

    # Sensitivity survives a named lookup; the bare-value form could not carry it.
    secret = client.output("db_url")
    assert secret.sensitive is True
    assert "postgres" not in repr(secret)

    with pytest.raises(UsageError):
        client.output("nope")


def test_show_plan_populates_resource_changes(client: Terrapy, fake_runner, json_fixture, tmp_path) -> None:
    payload = json_fixture("plan_show.json")
    fake_runner.register(
        ("/usr/bin/terraform", "show"),
        FakeRun(exit_code=0, stdout_lines=[json.dumps(payload)]),
    )
    plan = client.show_plan("tfplan")
    assert len(plan.resource_changes) == 2


def test_plan_with_out_populates_full_plan(client: Terrapy, fake_runner, json_fixture) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "plan"),
        FakeRun(
            exit_code=2,
            stdout_lines=['{"type":"change_summary","changes":{"add":1,"change":0,"remove":0}}'],
        ),
    )
    fake_runner.register(
        ("/usr/bin/terraform", "show"),
        FakeRun(exit_code=0, stdout_lines=[json.dumps(json_fixture("plan_show.json"))]),
    )
    result = client.plan(out="tfplan")
    assert result.plan_file == "tfplan"
    assert result.plan is not None
    assert len(result.resource_changes) == 2


def test_state_list_and_pull(client: Terrapy, fake_runner, json_fixture) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "state", "list"),
        FakeRun(exit_code=0, stdout_lines=["aws_instance.web", "aws_vpc.main"]),
    )
    assert client.state.list() == ["aws_instance.web", "aws_vpc.main"]

    fake_runner.register(
        ("/usr/bin/terraform", "state", "pull"),
        FakeRun(exit_code=0, stdout_lines=[json.dumps(json_fixture("state_pull.json"))]),
    )
    state = client.state.pull()
    assert state.serial == 3


def test_workspace_use_sets_tf_workspace_env(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "plan"),
        FakeRun(exit_code=0, stdout_lines=[]),
    )
    with client.workspace.use("staging"):
        client.plan()
    spec = fake_runner.calls[-1]
    assert spec.env["TF_WORKSPACE"] == "staging"
    # outside the `with`, the override must not leak to later calls
    client.plan()
    assert "TF_WORKSPACE" not in fake_runner.calls[-1].env


def test_run_raw_escape_hatch(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "graph"),
        FakeRun(exit_code=0, stdout_lines=["digraph {}"]),
    )
    result = client.run_raw("graph", "-type=plan")
    assert result.stdout == "digraph {}"
    assert argv_of(fake_runner, "graph")[1:] == ("graph", "-type=plan")


def test_run_raw_honours_custom_success_codes(client: Terrapy, fake_runner) -> None:
    fake_runner.register(("/usr/bin/terraform", "providers"), FakeRun(exit_code=7))
    assert client.run_raw("providers", success_exit_codes=(0, 7)).exit_code == 7
    with pytest.raises(ExecutionError):
        client.run_raw("providers", success_exit_codes=(0,))


def test_runner_is_injectable_protocol(fake_runner) -> None:
    assert isinstance(fake_runner, Runner)


def test_apply_uses_apply_phase_change_summary(client: Terrapy, fake_runner) -> None:
    # `apply` plans before it applies, emitting a plan-phase change_summary
    # first. The applied tally is the LAST one.
    fake_runner.register(
        ("/usr/bin/terraform", "apply"),
        FakeRun(
            exit_code=0,
            stdout_lines=[
                '{"type":"change_summary","changes":{"add":3,"change":0,"remove":0,'
                '"operation":"plan"}}',
                '{"type":"change_summary","changes":{"add":1,"change":0,"remove":0,'
                '"operation":"apply"}}',
            ],
        ),
    )
    result = client.apply()
    assert result.change_summary.add == 1
    assert result.change_summary.operation == "apply"


def test_version_goes_through_the_runner(client: Terrapy, fake_runner) -> None:
    # Must not bypass the injected runner by shelling out via discovery.
    fake_runner.register(
        ("/usr/bin/terraform", "version"),
        FakeRun(
            exit_code=0,
            stdout_lines=['{"terraform_version":"1.9.5","platform":"linux_amd64"}'],
        ),
    )
    info = client.version()
    assert info.version == "1.9.5"
    assert any(spec.argv[1] == "version" for spec in fake_runner.calls)

    client.version()  # cached: no second invocation
    assert sum(spec.argv[1] == "version" for spec in fake_runner.calls) == 1


def test_destroy_builds_apply_result(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "destroy"),
        FakeRun(
            exit_code=0,
            stdout_lines=[
                '{"type":"change_summary","changes":{"add":0,"change":0,"remove":2,'
                '"operation":"destroy"}}'
            ],
        ),
    )
    result = client.destroy()
    assert result.change_summary.remove == 2
    assert "-auto-approve" in argv_of(fake_runner, "destroy")


def test_destroy_failure_raises(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "destroy"), FakeRun(exit_code=1, stderr="destroy blew up")
    )
    with pytest.raises(ExecutionError):
        client.destroy()


# -- show / force-unlock / run_raw --------------------------------------------


def test_show_state_parses_state(client: Terrapy, fake_runner, json_fixture) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "show"),
        FakeRun(exit_code=0, stdout_lines=[json.dumps(json_fixture("state_pull.json"))]),
    )
    state = client.show_state()
    assert state.serial == 3


def test_force_unlock(client: Terrapy, fake_runner) -> None:
    fake_runner.register(("/usr/bin/terraform", "force-unlock"), FakeRun(exit_code=0))
    result = client.force_unlock("abc-123")
    assert result.exit_code == 0
    assert "abc-123" in argv_of(fake_runner, "force-unlock")


def test_state_show_finds_and_misses(client: Terrapy, fake_runner, json_fixture) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "state", "pull"),
        FakeRun(exit_code=0, stdout_lines=[json.dumps(json_fixture("state_pull.json"))]),
    )
    state = client.state.pull()
    address = state.resources[0].address
    assert client.state.show(address).address == address
    with pytest.raises(UsageError):
        client.state.show("aws_instance.nope")


def test_state_mv_rm_push_replace_provider(client: Terrapy, fake_runner, tmp_path) -> None:
    for sub in ("mv", "rm", "push", "replace-provider"):
        fake_runner.register(("/usr/bin/terraform", "state", sub), FakeRun(exit_code=0))

    assert client.state.mv("a.b", "c.d").exit_code == 0
    assert argv_of(fake_runner, "state", "mv")[-2:] == ("a.b", "c.d")

    assert client.state.rm("a.b", "c.d").exit_code == 0
    assert argv_of(fake_runner, "state", "rm")[-2:] == ("a.b", "c.d")

    state_file = tmp_path / "terraform.tfstate"
    state_file.write_text("{}", encoding="utf-8")
    assert client.state.push(state_file, force=True).exit_code == 0
    assert "-force" in argv_of(fake_runner, "state", "push")

    assert client.state.replace_provider("registry/a", "registry/b").exit_code == 0
    assert argv_of(fake_runner, "state", "replace-provider")[-2:] == ("registry/a", "registry/b")


def test_state_mv_dry_run(client: Terrapy, fake_runner) -> None:
    fake_runner.register(("/usr/bin/terraform", "state", "mv"), FakeRun(exit_code=0))
    client.state.mv("a.b", "c.d", dry_run=True)
    assert "-dry-run" in argv_of(fake_runner, "state", "mv")


# -- workspace ----------------------------------------------------------------


def test_workspace_list_strips_active_marker(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "workspace", "list"),
        FakeRun(exit_code=0, stdout_lines=["  default", "* staging", "  prod", "   "]),
    )
    assert client.workspace.list() == ["default", "staging", "prod"]


def test_workspace_show_new_select_delete(client: Terrapy, fake_runner) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", "workspace", "show"),
        FakeRun(exit_code=0, stdout_lines=["staging"]),
    )
    assert client.workspace.show() == "staging"

    for sub in ("new", "select", "delete"):
        fake_runner.register(("/usr/bin/terraform", "workspace", sub), FakeRun(exit_code=0))

    assert client.workspace.new("dev").exit_code == 0
    assert "dev" in argv_of(fake_runner, "workspace", "new")

    client.workspace.select("dev", or_create=True)
    assert "-or-create=true" in argv_of(fake_runner, "workspace", "select")

    client.workspace.delete("dev", force=True)
    assert "-force" in argv_of(fake_runner, "workspace", "delete")


@pytest.mark.parametrize("method,sub", [
    ("plan_stream", "plan"),
    ("apply_stream", "apply"),
    ("init_stream", "init"),
    ("destroy_stream", "destroy"),
])
def test_stream_entry_points_return_a_live_run(client: Terrapy, fake_runner, method, sub) -> None:
    fake_runner.register(
        ("/usr/bin/terraform", sub),
        FakeRun(exit_code=0, stdout_lines=['{"type":"log","@message":"hello"}']),
    )
    with getattr(client, method)() as run:
        events = list(run)
    assert [e.message for e in events] == ["hello"]
    assert argv_of(fake_runner, sub)[1] == sub
