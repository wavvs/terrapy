"""Exact argv assertions for the pure command builders. No mocking is needed:
building a command touches nothing, so each test calls a builder and compares
the resulting tuple."""

from __future__ import annotations

import pytest

from terrapy import command
from terrapy.config import RunConfig
from terrapy.exceptions import UsageError


@pytest.fixture
def cfg() -> RunConfig:
    return RunConfig(
        working_dir="/wd",
        binary_path="terraform",
        env={"PATH": "/usr/bin"},
    )


def test_version(cfg: RunConfig) -> None:
    spec = command.build_version(cfg)
    assert spec.argv == ("terraform", "version", "-json")
    assert spec.json_mode is True


def test_init_defaults(cfg: RunConfig) -> None:
    spec = command.build_init(cfg)
    assert spec.argv == ("terraform", "init", "-input=false", "-no-color")


def test_init_full(cfg: RunConfig) -> None:
    spec = command.build_init(
        cfg,
        backend_config={"bucket": "b", "key": "k"},
        reconfigure=True,
        migrate_state=True,
        upgrade=True,
        get=False,
        plugin_dirs=["/plugins"],
        lockfile="readonly",
        json_mode=True,
    )
    assert spec.argv == (
        "terraform",
        "init",
        "-json",
        "-input=false",
        "-no-color",
        "-backend-config",
        "bucket=b",
        "-backend-config",
        "key=k",
        "-reconfigure",
        "-migrate-state",
        "-upgrade",
        "-get=false",
        "-plugin-dir",
        "/plugins",
        "-lockfile=readonly",
    )


def test_init_from_module(cfg: RunConfig) -> None:
    spec = command.build_init(cfg, from_module="./modules/base")
    assert spec.argv == (
        "terraform",
        "init",
        "-input=false",
        "-no-color",
        "-from-module=./modules/base",
    )


def test_init_backend_false(cfg: RunConfig) -> None:
    spec = command.build_init(cfg, backend=False)
    assert "-backend=false" in spec.argv


def test_validate(cfg: RunConfig) -> None:
    spec = command.build_validate(cfg)
    assert spec.argv == ("terraform", "validate", "-json", "-no-color")
    assert spec.success_exit_codes == frozenset({0, 1})


def test_fmt_defaults(cfg: RunConfig) -> None:
    spec = command.build_fmt(cfg)
    assert spec.argv == ("terraform", "fmt", "-no-color")
    assert spec.success_exit_codes == frozenset({0})


def test_fmt_check(cfg: RunConfig) -> None:
    spec = command.build_fmt(cfg, ["a.tf", "b.tf"], check=True, diff=True, recursive=True)
    assert spec.argv == ("terraform", "fmt", "-no-color", "-check", "-diff", "-recursive", "a.tf", "b.tf")
    # terraform's fmt.go returns 3 for "not yet formatted"; 1 and 2 are errors.
    assert spec.success_exit_codes == frozenset({0, 3})


def test_plan_always_detailed_exitcode(cfg: RunConfig) -> None:
    spec = command.build_plan(cfg)
    assert "-detailed-exitcode" in spec.argv
    assert spec.success_exit_codes == frozenset({0, 2})


def test_plan_vars_and_targets(cfg: RunConfig) -> None:
    spec = command.build_plan(
        cfg,
        var={"count": 3, "enabled": True, "name": "x", "tags": {"env": "prod"}},
        var_files=["a.tfvars"],
        targets=["aws_instance.web"],
        replace=["aws_instance.old"],
        out="tfplan",
        json_mode=True,
    )
    argv = spec.argv
    assert "-out" in argv and argv[argv.index("-out") + 1] == "tfplan"
    assert ("-var", "count=3") == tuple(argv[argv.index("-var") : argv.index("-var") + 2])
    assert "-var-file" in argv and "a.tfvars" in argv
    assert "-target" in argv and "aws_instance.web" in argv
    assert "-replace" in argv and "aws_instance.old" in argv
    assert "-json" in argv


def test_plan_destroy_and_refresh_only_conflict(cfg: RunConfig) -> None:
    with pytest.raises(UsageError):
        command.build_plan(cfg, destroy=True, refresh_only=True)


def test_plan_no_refresh(cfg: RunConfig) -> None:
    spec = command.build_plan(cfg, refresh=False)
    assert "-refresh=false" in spec.argv


def test_apply_auto_approve(cfg: RunConfig) -> None:
    spec = command.build_apply(cfg)
    assert "-auto-approve" in spec.argv


def test_apply_plan_file(cfg: RunConfig) -> None:
    spec = command.build_apply(cfg, "tfplan")
    assert spec.argv[-1] == "tfplan"
    assert "-auto-approve" not in spec.argv


def test_apply_json_requires_approve_or_planfile(cfg: RunConfig) -> None:
    with pytest.raises(UsageError):
        command.build_apply(cfg, auto_approve=False, json_mode=True)
    # fine: explicit plan file satisfies the invariant
    command.build_apply(cfg, "tfplan", auto_approve=False, json_mode=True)


def test_apply_plan_file_rejects_var(cfg: RunConfig) -> None:
    with pytest.raises(UsageError):
        command.build_apply(cfg, "tfplan", var={"x": 1})


def test_destroy(cfg: RunConfig) -> None:
    spec = command.build_destroy(cfg, var={"x": 1}, targets=["aws_instance.web"])
    assert spec.argv[:3] == ("terraform", "destroy", "-input=false")
    assert "-auto-approve" in spec.argv
    assert "-var" in spec.argv


def test_output(cfg: RunConfig) -> None:
    assert command.build_output(cfg).argv == ("terraform", "output", "-json")
    assert command.build_output(cfg, "vpc_id").argv == ("terraform", "output", "-json", "vpc_id")


def test_show(cfg: RunConfig) -> None:
    assert command.build_show(cfg).argv == ("terraform", "show", "-json")
    assert command.build_show(cfg, "tfplan").argv == ("terraform", "show", "-json", "tfplan")


def test_force_unlock(cfg: RunConfig) -> None:
    spec = command.build_force_unlock(cfg, "lock-123")
    assert spec.argv == ("terraform", "force-unlock", "-no-color", "-force", "lock-123")


def test_state_subcommands(cfg: RunConfig) -> None:
    assert command.build_state_list(cfg).argv == ("terraform", "state", "list", "-no-color")
    assert command.build_state_pull(cfg).argv == ("terraform", "state", "pull")
    assert command.build_state_mv(cfg, "a", "b").argv == ("terraform", "state", "mv", "-no-color", "a", "b")
    assert command.build_state_rm(cfg, ["a"]).argv == ("terraform", "state", "rm", "-no-color", "a")
    with pytest.raises(UsageError):
        command.build_state_rm(cfg, [])


def test_workspace_subcommands(cfg: RunConfig) -> None:
    assert command.build_workspace_list(cfg).argv == ("terraform", "workspace", "list", "-no-color")
    assert command.build_workspace_show(cfg).argv == ("terraform", "workspace", "show", "-no-color")
    assert command.build_workspace_new(cfg, "dev").argv == ("terraform", "workspace", "new", "dev", "-no-color")
    assert command.build_workspace_select(cfg, "dev", or_create=True).argv == (
        "terraform",
        "workspace",
        "select",
        "-or-create=true",
        "dev",
        "-no-color",
    )
    assert command.build_workspace_delete(cfg, "dev", force=True).argv == (
        "terraform",
        "workspace",
        "delete",
        "-force",
        "dev",
        "-no-color",
    )


def test_use_chdir_prepends_global_flag() -> None:
    cfg = RunConfig(
        working_dir="/wd",
        binary_path="terraform",
        env={},
        use_chdir=True,
    )
    spec = command.build_validate(cfg)
    assert spec.argv[:2] == ("terraform", "-chdir=/wd")
    assert spec.cwd != "/wd"
