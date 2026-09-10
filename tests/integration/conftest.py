"""Integration tests are skipped unless TERRAPY_INTEGRATION=1 and a real
terraform/tofu binary is on PATH. They exercise the full lifecycle
(init -> plan -> apply -> destroy) against a tiny throwaway config using only
the `local_file` resource, which needs no cloud credentials."""

from __future__ import annotations

import os
import shutil

import pytest

from terrapy.discovery import DEFAULT_NAMES

RUN_INTEGRATION = os.environ.get("TERRAPY_INTEGRATION") == "1"

AVAILABLE_BINARIES = [name for name in DEFAULT_NAMES if shutil.which(name) is not None]

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION, reason="set TERRAPY_INTEGRATION=1 and install terraform/tofu to run"
)

LOCAL_FILE_CONFIG = """
terraform {
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0"
    }
  }
}

variable "content" {
  type    = string
  default = "hello from terrapy"
}

resource "local_file" "example" {
  filename = "${path.module}/output.txt"
  content  = var.content
}

output "filename" {
  value = local_file.example.filename
}
"""


@pytest.fixture(params=AVAILABLE_BINARIES or [pytest.param(None, marks=pytest.mark.skip)])
def binary(request: pytest.FixtureRequest) -> str:
    """Each test runs once per tool found on PATH, against that tool by name."""
    return request.param


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "main.tf").write_text(LOCAL_FILE_CONFIG, encoding="utf-8")
    return tmp_path
