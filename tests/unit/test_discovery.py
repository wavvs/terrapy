"""Binary discovery and version detection, fully mocked (no real binary)."""

from __future__ import annotations

import json
import subprocess

import pytest

from terrapy import discovery
from terrapy.exceptions import BinaryNotFoundError, VersionDetectionError


def test_find_binary_with_explicit_path(tmp_path) -> None:
    fake = tmp_path / "terraform"
    fake.write_text("#!/bin/sh\n")
    resolved = discovery.find_binary(str(fake))
    assert resolved == str(fake)


def test_find_binary_missing_explicit_path_raises(tmp_path) -> None:
    with pytest.raises(BinaryNotFoundError):
        discovery.find_binary(str(tmp_path / "does-not-exist"))


def test_find_binary_searches_path(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/tofu" if name == "tofu" else None

    monkeypatch.setattr(discovery.shutil, "which", fake_which)
    assert discovery.find_binary() == "/usr/local/bin/tofu"


def test_find_binary_prefers_terraform_over_tofu(monkeypatch) -> None:
    monkeypatch.setattr(discovery.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert discovery.find_binary() == "/usr/local/bin/terraform"


def test_find_binary_honours_an_explicit_name_list(monkeypatch) -> None:
    # This is how Terrapy(binary_path="tofu") narrows the PATH search.
    monkeypatch.setattr(discovery.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert discovery.find_binary(names=("tofu",)) == "/usr/local/bin/tofu"


def test_find_binary_none_found_raises(monkeypatch) -> None:
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    with pytest.raises(BinaryNotFoundError) as excinfo:
        discovery.find_binary()
    assert "terraform" in excinfo.value.searched
    assert "tofu" in excinfo.value.searched


def test_get_version_info_parses_json(monkeypatch) -> None:
    payload = {"terraform_version": "1.9.5", "platform": "linux_amd64"}

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)
    info = discovery.get_version_info("/usr/bin/terraform")
    assert info.version == "1.9.5"
    assert info.platform == "linux_amd64"


def test_get_version_info_bad_json_raises(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=b"not json", stderr=b"")

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)
    with pytest.raises(VersionDetectionError):
        discovery.get_version_info("/usr/bin/terraform")


def test_get_version_info_binary_missing_raises(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)
    with pytest.raises(BinaryNotFoundError):
        discovery.get_version_info("/usr/bin/terraform")


def test_version_info_surfaces_stderr_on_nonzero_exit(monkeypatch) -> None:
    # A binary too old for `-json` writes the reason to stderr and exits
    # non-zero; without checking returncode this surfaced as a bare parse
    # error with the real cause discarded.
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout=b"", stderr=b"Error: unsupported flag -json"
        )

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)
    with pytest.raises(VersionDetectionError, match="unsupported flag"):
        discovery.get_version_info("/usr/bin/terraform")
