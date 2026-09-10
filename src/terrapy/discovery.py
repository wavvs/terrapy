"""Binary discovery and version detection.

Finding the binary is a `PATH` lookup with `shutil.which`, which on Windows
reads `PATHEXT` and so resolves `terraform` to `terraform.exe`. With no
explicit name, `terraform` is tried before `tofu`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from .exceptions import BinaryNotFoundError, VersionDetectionError
from .models import VersionInfo

_log = logging.getLogger("terrapy.discovery")

DEFAULT_NAMES: tuple[str, ...] = ("terraform", "tofu")


def find_binary(
    binary_path: str | os.PathLike[str] | None = None,
    names: Sequence[str] = DEFAULT_NAMES,
) -> str:
    """Resolve a binary path, either the explicit override or a `PATH` lookup.

    An explicit `binary_path` is trusted as given, resolved through `PATH` if
    it is a bare name (`"tofu"`, `"terraform-1.9"`). Otherwise each of `names`
    is looked up on `PATH` in order and the first hit wins.
    """
    if binary_path is not None:
        candidate = os.fspath(binary_path)
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if resolved is None or not os.path.isfile(resolved):
            raise BinaryNotFoundError(
                f"binary_path {candidate!r} was not found or is not a file",
                searched=(candidate,),
            )
        return resolved

    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise BinaryNotFoundError(
        f"no {' or '.join(names)} binary was found on PATH; install one, or pass binary_path=",
        searched=tuple(names),
    )


def get_version_info(binary_path: str, *, timeout: float = 15.0) -> VersionInfo:
    """Run `<binary> version -json` and parse it into a `VersionInfo`."""
    try:
        proc = subprocess.run(
            [binary_path, "version", "-json"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        raise BinaryNotFoundError(
            f"failed to execute {binary_path!r}: {exc}", searched=(binary_path,)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VersionDetectionError(
            f"{binary_path!r} did not respond to `version -json` within {timeout}s",
            binary_path=binary_path,
        ) from exc

    text = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise VersionDetectionError(
            f"`version -json` failed for {binary_path!r} "
            f"(exit {proc.returncode}): {detail or '<no stderr>'}",
            binary_path=binary_path,
            raw_output=text,
        )
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise VersionDetectionError(
            f"could not parse `version -json` output from {binary_path!r} as JSON",
            binary_path=binary_path,
            raw_output=text,
        ) from exc
    if not isinstance(payload, Mapping):
        raise VersionDetectionError(
            f"`version -json` output from {binary_path!r} was not a JSON object",
            binary_path=binary_path,
            raw_output=text,
        )
    return VersionInfo.from_dict(payload)


__all__ = ["DEFAULT_NAMES", "find_binary", "get_version_info"]
