from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from .models import SkillManifest

EXCLUDED = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache"}


def build(root: Path | str, manifest: SkillManifest, output: Path | str) -> tuple[Path, str]:
    root, output = Path(root).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and output != path.resolve()
        and not any(
            part in EXCLUDED or part.startswith(".") for part in path.relative_to(root).parts
        )
    ]
    metadata = json.dumps(manifest.as_dict(), sort_keys=True, separators=(",", ":")).encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        _write(archive, "forge-manifest.json", metadata)
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            _write(archive, path.relative_to(root).as_posix(), path.read_bytes())
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


def _write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)
