"""Pure argv builders. One function per subcommand. No I/O.

Each function takes an immutable `RunConfig` plus the command's typed keyword
arguments and returns a `RunSpec` describing exactly what to run. A builder
never touches the filesystem or a process, so a test can check the whole
surface with plain argv assertions and no installed binary.

The builders also enforce the mode rules, rejecting an illegal combination
before anything runs, and inject `-json`, `-input=false`, and `-no-color`
consistently. `apply` in `-json` mode needs either a saved plan file or
`auto_approve=True`. `plan` always adds `-detailed-exitcode`, which separates
"changes present" (exit 2) from "error" (exit 1).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from .config import RunConfig, RunSpec
from .exceptions import UsageError

if TYPE_CHECKING:
    from .config import StrPath


def _base_argv(config: RunConfig, subcommand: str) -> list[str]:
    argv = [config.binary_path]
    if config.use_chdir:
        argv.append(f"-chdir={config.working_dir}")
    argv.append(subcommand)
    return argv


def resolve_cwd(config: RunConfig) -> str:
    return os.getcwd() if config.use_chdir else config.working_dir


def _add_no_color(argv: list[str], config: RunConfig) -> None:
    if config.no_color:
        argv.append("-no-color")


def _add_input(argv: list[str], config: RunConfig) -> None:
    if not config.input:
        argv.append("-input=false")


def _add_parallelism(argv: list[str], config: RunConfig, parallelism: int | None) -> None:
    value = parallelism if parallelism is not None else config.parallelism
    if value is not None:
        argv.append(f"-parallelism={value}")


def _encode_var_value(value: object) -> str:
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _add_vars(argv: list[str], var: Mapping[str, object] | None) -> None:
    if not var:
        return
    for key, value in var.items():
        argv.extend(["-var", f"{key}={_encode_var_value(value)}"])


def _add_repeated(
    argv: list[str],
    flag: str,
    values: Sequence[StrPath] | None,
    transform: Callable[[StrPath], str] = str,
) -> None:
    """Append `flag value` for each item, e.g. `-target aws_x -target aws_y`."""
    if not values:
        return
    for value in values:
        argv.extend([flag, transform(value)])


def _make_spec(
    config: RunConfig,
    argv: list[str],
    *,
    success_exit_codes: frozenset[int] = frozenset({0}),
    json_mode: bool = False,
    timeout: float | None = None,
    inactivity_timeout: float | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> RunSpec:
    env = config.env
    if env_overrides:
        env = {**config.env, **env_overrides}
    return RunSpec(
        argv=tuple(argv),
        cwd=resolve_cwd(config),
        env=env,
        success_exit_codes=success_exit_codes,
        timeout=timeout if timeout is not None else config.timeout,
        inactivity_timeout=(
            inactivity_timeout if inactivity_timeout is not None else config.inactivity_timeout
        ),
        json_mode=json_mode,
    )


def build_version(config: RunConfig) -> RunSpec:
    argv = [config.binary_path, "version", "-json"]
    return _make_spec(config, argv, json_mode=True)


def build_raw(
    config: RunConfig,
    *args: str,
    timeout: float | None = None,
    success_exit_codes: Sequence[int] = (0,),
) -> RunSpec:
    """The `run_raw` escape hatch: the binary plus a `-chdir` prefix (when
    configured) and the caller's raw arguments, with no flag injection."""
    argv = [config.binary_path]
    if config.use_chdir:
        argv.append(f"-chdir={config.working_dir}")
    argv.extend(args)
    return _make_spec(
        config, argv, success_exit_codes=frozenset(success_exit_codes), timeout=timeout
    )


def build_init(
    config: RunConfig,
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
    json_mode: bool = False,
    timeout: float | None = None,
) -> RunSpec:
    argv = _base_argv(config, "init")
    if json_mode:
        argv.append("-json")
    _add_input(argv, config)
    _add_no_color(argv, config)
    if not backend:
        argv.append("-backend=false")
    if isinstance(backend_config, Mapping):
        for key, value in backend_config.items():
            argv.extend(["-backend-config", f"{key}={value}"])
    elif isinstance(backend_config, (str, os.PathLike)):
        argv.extend(["-backend-config", os.fspath(backend_config)])
    elif backend_config is not None:
        for item in backend_config:
            argv.extend(["-backend-config", str(item)])
    if reconfigure:
        argv.append("-reconfigure")
    if migrate_state:
        argv.append("-migrate-state")
    if upgrade:
        argv.append("-upgrade")
    if not get:
        argv.append("-get=false")
    if plugin_dirs:
        for path in plugin_dirs:
            argv.extend(["-plugin-dir", os.fspath(path)])
    if lockfile is not None:
        argv.append(f"-lockfile={lockfile}")
    if from_module is not None:
        argv.append(f"-from-module={os.fspath(from_module)}")
    return _make_spec(config, argv, json_mode=json_mode, timeout=timeout)


def build_validate(config: RunConfig) -> RunSpec:
    # Exit 1 means "ran, and found problems", mirroring plan's
    # -detailed-exitcode. It counts as success here so the -json report is
    # parsed rather than discarded; validate() raises ValidationError from the
    # parsed result, which is how the diagnostics reach the exception.
    argv = _base_argv(config, "validate")
    argv.append("-json")
    _add_no_color(argv, config)
    return _make_spec(config, argv, success_exit_codes=frozenset({0, 1}), json_mode=True)


def build_fmt(
    config: RunConfig,
    paths: Sequence[StrPath] | None = None,
    *,
    check: bool = False,
    write: bool = True,
    diff: bool = False,
    recursive: bool = False,
) -> RunSpec:
    argv = _base_argv(config, "fmt")
    _add_no_color(argv, config)
    if check:
        argv.append("-check")
    if not write:
        argv.append("-write=false")
    if diff:
        argv.append("-diff")
    if recursive:
        argv.append("-recursive")
    if paths:
        argv.extend(os.fspath(p) for p in paths)
    # With -check, fmt exits 3 to mean "not yet formatted" (see terraform's
    # command/fmt.go: `if ok { return 0 } else { return 3 }`). 1 and 2 stay
    # genuine errors and must still raise.
    success_exit_codes = frozenset({0, 3}) if check else frozenset({0})
    return _make_spec(config, argv, success_exit_codes=success_exit_codes)


def build_plan(
    config: RunConfig,
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
    json_mode: bool = False,
    timeout: float | None = None,
) -> RunSpec:
    if destroy and refresh_only:
        raise UsageError("plan(): destroy=True and refresh_only=True are mutually exclusive")
    argv = _base_argv(config, "plan")
    if json_mode:
        argv.append("-json")
    _add_input(argv, config)
    _add_no_color(argv, config)
    argv.append("-detailed-exitcode")
    if out is not None:
        argv.extend(["-out", os.fspath(out)])
    if destroy:
        argv.append("-destroy")
    if refresh_only:
        argv.append("-refresh-only")
    if not refresh:
        argv.append("-refresh=false")
    _add_vars(argv, var)
    _add_repeated(argv, "-var-file", var_files, os.fspath)  # type: ignore[arg-type]
    _add_repeated(argv, "-target", targets)
    _add_repeated(argv, "-replace", replace)
    _add_parallelism(argv, config, parallelism)
    return _make_spec(
        config, argv, success_exit_codes=frozenset({0, 2}), json_mode=json_mode, timeout=timeout
    )


def build_apply(
    config: RunConfig,
    plan_file: StrPath | None = None,
    *,
    auto_approve: bool = True,
    var: Mapping[str, object] | None = None,
    var_files: Sequence[StrPath] | None = None,
    targets: Sequence[str] | None = None,
    replace: Sequence[str] | None = None,
    refresh_only: bool = False,
    parallelism: int | None = None,
    json_mode: bool = False,
    timeout: float | None = None,
) -> RunSpec:
    if json_mode and plan_file is None and not auto_approve:
        raise UsageError(
            "apply(): -json mode requires either plan_file or auto_approve=True "
            "(interactive approval is impossible; stdin is always closed)"
        )
    argv = _base_argv(config, "apply")
    if json_mode:
        argv.append("-json")
    _add_input(argv, config)
    _add_no_color(argv, config)
    if plan_file is None:
        if auto_approve:
            argv.append("-auto-approve")
        if refresh_only:
            argv.append("-refresh-only")
        _add_vars(argv, var)
        _add_repeated(argv, "-var-file", var_files, os.fspath)  # type: ignore[arg-type]
        _add_repeated(argv, "-target", targets)
        _add_repeated(argv, "-replace", replace)
    else:
        if var or var_files or targets or replace:
            raise UsageError(
                "apply(): var/var_files/targets/replace are not accepted alongside a plan_file "
                "(the plan file already fixes these)"
            )
    _add_parallelism(argv, config, parallelism)
    if plan_file is not None:
        argv.append(os.fspath(plan_file))
    return _make_spec(config, argv, json_mode=json_mode, timeout=timeout)


def build_destroy(
    config: RunConfig,
    *,
    auto_approve: bool = True,
    var: Mapping[str, object] | None = None,
    var_files: Sequence[StrPath] | None = None,
    targets: Sequence[str] | None = None,
    parallelism: int | None = None,
    json_mode: bool = False,
    timeout: float | None = None,
) -> RunSpec:
    argv = _base_argv(config, "destroy")
    if json_mode:
        argv.append("-json")
    _add_input(argv, config)
    _add_no_color(argv, config)
    if auto_approve:
        argv.append("-auto-approve")
    _add_vars(argv, var)
    _add_repeated(argv, "-var-file", var_files, os.fspath)  # type: ignore[arg-type]
    _add_repeated(argv, "-target", targets)
    _add_parallelism(argv, config, parallelism)
    return _make_spec(config, argv, json_mode=json_mode, timeout=timeout)


def build_output(config: RunConfig, name: str | None = None) -> RunSpec:
    argv = _base_argv(config, "output")
    argv.append("-json")
    if name is not None:
        argv.append(name)
    return _make_spec(config, argv, json_mode=True)


def build_show(config: RunConfig, path: StrPath | None = None) -> RunSpec:
    argv = _base_argv(config, "show")
    argv.append("-json")
    if path is not None:
        argv.append(os.fspath(path))
    return _make_spec(config, argv, json_mode=True)


def build_force_unlock(config: RunConfig, lock_id: str, *, force: bool = True) -> RunSpec:
    argv = _base_argv(config, "force-unlock")
    _add_no_color(argv, config)
    if force:
        argv.append("-force")
    argv.append(lock_id)
    return _make_spec(config, argv)


# -- state subcommands -------------------------------------------------------


def build_state_list(config: RunConfig, addresses: Sequence[str] | None = None) -> RunSpec:
    argv = _base_argv(config, "state")
    argv.append("list")
    _add_no_color(argv, config)
    if addresses:
        argv.extend(addresses)
    return _make_spec(config, argv)


def build_state_pull(config: RunConfig) -> RunSpec:
    argv = _base_argv(config, "state")
    argv.append("pull")
    return _make_spec(config, argv, json_mode=True)


def build_state_mv(
    config: RunConfig, source: str, destination: str, *, dry_run: bool = False
) -> RunSpec:
    argv = _base_argv(config, "state")
    argv.append("mv")
    _add_no_color(argv, config)
    if dry_run:
        argv.append("-dry-run")
    argv.extend([source, destination])
    return _make_spec(config, argv)


def build_state_rm(
    config: RunConfig, addresses: Sequence[str], *, dry_run: bool = False
) -> RunSpec:
    if not addresses:
        raise UsageError("state.rm(): at least one address is required")
    argv = _base_argv(config, "state")
    argv.append("rm")
    _add_no_color(argv, config)
    if dry_run:
        argv.append("-dry-run")
    argv.extend(addresses)
    return _make_spec(config, argv)


def build_state_push(config: RunConfig, state_file: StrPath, *, force: bool = False) -> RunSpec:
    argv = _base_argv(config, "state")
    argv.append("push")
    if force:
        argv.append("-force")
    argv.append(os.fspath(state_file))
    return _make_spec(config, argv)


def build_state_replace_provider(config: RunConfig, from_: str, to: str) -> RunSpec:
    argv = _base_argv(config, "state")
    argv.append("replace-provider")
    argv.append("-auto-approve")
    _add_no_color(argv, config)
    argv.extend([from_, to])
    return _make_spec(config, argv)


# -- workspace subcommands ----------------------------------------------------


def build_workspace_list(config: RunConfig) -> RunSpec:
    argv = _base_argv(config, "workspace")
    argv.append("list")
    _add_no_color(argv, config)
    return _make_spec(config, argv)


def build_workspace_show(config: RunConfig) -> RunSpec:
    argv = _base_argv(config, "workspace")
    argv.append("show")
    _add_no_color(argv, config)
    return _make_spec(config, argv)


def build_workspace_new(config: RunConfig, name: str) -> RunSpec:
    argv = _base_argv(config, "workspace")
    argv.extend(["new", name])
    _add_no_color(argv, config)
    return _make_spec(config, argv)


def build_workspace_select(config: RunConfig, name: str, *, or_create: bool = False) -> RunSpec:
    argv = _base_argv(config, "workspace")
    argv.append("select")
    if or_create:
        argv.append("-or-create=true")
    argv.append(name)
    _add_no_color(argv, config)
    return _make_spec(config, argv)


def build_workspace_delete(config: RunConfig, name: str, *, force: bool = False) -> RunSpec:
    argv = _base_argv(config, "workspace")
    argv.append("delete")
    if force:
        argv.append("-force")
    argv.append(name)
    _add_no_color(argv, config)
    return _make_spec(config, argv)


__all__ = [
    "resolve_cwd",
    "build_version",
    "build_raw",
    "build_init",
    "build_validate",
    "build_fmt",
    "build_plan",
    "build_apply",
    "build_destroy",
    "build_output",
    "build_show",
    "build_force_unlock",
    "build_state_list",
    "build_state_pull",
    "build_state_mv",
    "build_state_rm",
    "build_state_push",
    "build_state_replace_provider",
    "build_workspace_list",
    "build_workspace_show",
    "build_workspace_new",
    "build_workspace_select",
    "build_workspace_delete",
]
