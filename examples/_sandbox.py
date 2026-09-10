"""The temporary Terraform config used by the examples.

Every example runs against a temporary directory holding one `local_file`
resource, so no cloud credentials are required. The directory and the state
inside it are deleted when the block exits.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAIN_TF = """
terraform {
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0"
    }
  }
}

variable "greeting" {
  type    = string
  default = "hello from terrapy"
}

variable "file_count" {
  type    = number
  default = 3
}

resource "local_file" "note" {
  count    = var.file_count
  filename = "${path.module}/note-${count.index}.txt"
  content  = "${var.greeting} (${count.index})"
}

output "filenames" {
  value = local_file.note[*].filename
}
"""

BROKEN_TF = """
resource "local_file" "broken" {
  filename = "out.txt"
  contnet  = "misspelled argument, and no content"
}
"""


@contextmanager
def sandbox(config: str = MAIN_TF) -> Iterator[Path]:
    """Yield a temporary directory containing `main.tf`, then delete it."""
    path = Path(tempfile.mkdtemp(prefix="terrapy-example-"))
    (path / "main.tf").write_text(config, encoding="utf-8")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
