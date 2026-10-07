"""Verify user-approved frozen repository assets against their SHA-256 baseline."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import CONFIG_ROOT, PROJECT_ROOT

_FROZEN_FLAGS = importlib.util.spec_from_file_location(
    "verify_frozen_flags", Path(__file__).resolve().parent / "ci/frozen_flags.py")
frozen_flags = importlib.util.module_from_spec(_FROZEN_FLAGS)
_FROZEN_FLAGS.loader.exec_module(frozen_flags)


FROZEN_FILES_MANIFEST = CONFIG_ROOT / "frozen_files.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_frozen_files(manifest_path: Path = FROZEN_FILES_MANIFEST) -> list[str]:
    payload: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        return [f"unsupported frozen-files schema: {payload.get('schema_version')!r}"]

    errors: list[str] = []
    for item in payload.get("files") or []:
        relative_path = Path(str(item.get("path") or ""))
        expected = str(item.get("sha256") or "")
        target = (PROJECT_ROOT / relative_path).resolve()
        try:
            target.relative_to(PROJECT_ROOT)
        except ValueError:
            errors.append(f"frozen path escapes project root: {relative_path}")
            continue
        if not target.is_file():
            errors.append(f"frozen file is missing: {relative_path}")
            continue
        actual = sha256_file(target)
        if actual != expected:
            errors.append(f"frozen file hash mismatch: {relative_path} expected={expected} actual={actual}")
        # macOS 检查 uchg，Linux 检查 chattr +i；见 scripts/ci/frozen_flags.py。
        if item.get("require_immutable_flag") and not frozen_flags.is_immutable(target):
            errors.append(f"frozen file is not filesystem-immutable: {relative_path}")
    return errors


def main() -> int:
    errors = verify_frozen_files()
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Frozen file verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
