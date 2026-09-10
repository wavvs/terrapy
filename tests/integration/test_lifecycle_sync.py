"""Full sync lifecycle against a real terraform/tofu binary:
init -> plan -> apply -> destroy, plus drift detection via -detailed-exitcode."""

from __future__ import annotations

import pytest

from terrapy.client import Terrapy
from terrapy.exceptions import ValidationError


def test_full_lifecycle(binary: str, config_dir) -> None:
    tf = Terrapy(str(config_dir), binary_path=binary)

    init_result = tf.init()
    assert init_result.exit_code == 0

    validate_result = tf.validate()
    assert validate_result.valid is True

    plan = tf.plan(var={"content": "hello from terrapy"})
    assert plan.has_changes is True
    assert plan.change_summary.add == 1

    apply_result = tf.apply(var={"content": "hello from terrapy"})
    assert apply_result.change_summary.add == 1
    assert (config_dir / "output.txt").read_text(encoding="utf-8") == "hello from terrapy"

    outs = tf.output()
    assert outs["filename"].value.endswith("output.txt")

    no_change_plan = tf.plan(var={"content": "hello from terrapy"})
    assert no_change_plan.has_changes is False

    state = tf.state.pull()
    assert any(r.type == "local_file" for r in state.resources)

    destroy_result = tf.destroy(var={"content": "hello from terrapy"})
    assert destroy_result.change_summary.remove == 1
    assert not (config_dir / "output.txt").exists()


def test_fmt_and_workspace(binary: str, config_dir) -> None:
    tf = Terrapy(str(config_dir), binary_path=binary)
    tf.init()

    fmt_result = tf.fmt()
    assert fmt_result.exit_code == 0

    assert tf.workspace.show() == "default"
    assert "default" in tf.workspace.list()


def test_output_named_lookup_keeps_metadata(binary: str, config_dir) -> None:
    """Regression: `output -json NAME` prints the BARE value with no
    type/sensitive metadata, so a named lookup must filter the map form.
    Fetching by name previously returned an empty OutputValue for scalars and
    reported every output as non-sensitive."""
    (config_dir / "outputs.tf").write_text(
        'output "plain" {\n'
        '  value = "vpc-1"\n'
        "}\n"
        'output "secret" {\n'
        '  value     = "postgres://user:pw@host/db"\n'
        "  sensitive = true\n"
        "}\n"
        'output "mapping" {\n'
        '  value = { user = "admin", pass = "hunter2" }\n'
        "}\n",
        encoding="utf-8",
    )
    tf = Terrapy(str(config_dir), binary_path=binary)
    tf.init()
    tf.apply()
    try:
        plain = tf.output("plain")
        assert plain.value == "vpc-1"
        assert plain.type == "string"
        assert plain.sensitive is False

        secret = tf.output("secret")
        assert secret.value == "postgres://user:pw@host/db"
        assert secret.sensitive is True
        assert "postgres" not in repr(secret)  # the repr must redact

        mapping = tf.output("mapping")
        assert mapping.value == {"user": "admin", "pass": "hunter2"}

        # The named lookup agrees with the full map.
        assert tf.output()["secret"].sensitive is True
    finally:
        tf.destroy()


def test_fmt_check_reports_unformatted_without_raising(binary: str, config_dir) -> None:
    """Regression: `fmt -check` exits 3 for "not yet formatted", which the
    client previously treated as a failure and raised on."""
    (config_dir / "ugly.tf").write_text(
        'output "ugly" {\n    value="x"\n}\n', encoding="utf-8"
    )
    tf = Terrapy(str(config_dir), binary_path=binary)

    result = tf.fmt(check=True, write=False)
    assert result.ok is False
    assert result.exit_code == 3
    assert any("ugly.tf" in f for f in result.changed_files)

    tf.fmt()  # rewrite the file for real
    assert tf.fmt(check=True, write=False).ok is True


def test_apply_reports_the_applied_tally_not_the_plan(binary: str, config_dir) -> None:
    """Regression: `apply -json` emits a plan-phase change_summary before the
    apply-phase one; the client must report the applied tally."""
    tf = Terrapy(str(config_dir), binary_path=binary)
    tf.init()
    try:
        result = tf.apply()
        assert result.change_summary.operation == "apply"
        assert result.change_summary.add == 1
    finally:
        tf.destroy()


def test_version_uses_the_clients_binary(binary: str, config_dir) -> None:
    """Regression: version() bypassed the runner entirely."""
    tf = Terrapy(str(config_dir), binary_path=binary)
    info = tf.version()
    assert tf.binary_path.endswith((binary, f"{binary}.exe"))
    assert info.version_tuple.major >= 1
    assert tf.version() is info  # cached


def test_validate_raises_with_real_diagnostics(binary: str, config_dir) -> None:
    """`validate` exits 1 and still prints the -json report; the client parses
    it before raising, so the diagnostics survive on the exception."""
    (config_dir / "broken.tf").write_text(
        'output "broken" {\n  value = local_file.nope.filename\n}\n', encoding="utf-8"
    )
    tf = Terrapy(str(config_dir), binary_path=binary)
    tf.init()

    with pytest.raises(ValidationError) as excinfo:
        tf.validate()
    exc = excinfo.value
    assert exc.exit_code == 1
    assert exc.result.valid is False
    assert exc.result.error_count >= 1
    assert exc.diagnostics and exc.diagnostics[0].summary
