#!/usr/bin/env python3
"""Fail closed when an EchoWeave release wheel is incomplete or malformed."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

PROJECT_NAME = "echoweave-rtc"
REQUIRED_PACKAGE_FILES = {
    "echoweave/__init__.py",
    "echoweave/app.py",
    "echoweave/web/api.html",
    "echoweave/web/app.js",
    "echoweave/web/index.html",
    "echoweave/web/mic-worklet.js",
    "echoweave/web/styles.css",
}
FORBIDDEN_SUFFIXES = {
    ".env",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
}


def canonicalize_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def encoded_sha256(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def find_metadata(archive: zipfile.ZipFile) -> tuple[str, object]:
    names = [
        name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
    ]
    if len(names) != 1:
        raise ValueError(f"expected exactly one METADATA file, found {names}")
    return names[0], BytesParser().parsebytes(archive.read(names[0]))


def verify_record(archive: zipfile.ZipFile, record_name: str) -> None:
    names = set(archive.namelist())
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    recorded = {row[0] for row in rows}
    if recorded != names:
        missing = sorted(names - recorded)
        extra = sorted(recorded - names)
        raise ValueError(f"RECORD inventory mismatch: missing={missing}, extra={extra}")

    for row in rows:
        if len(row) != 3:
            raise ValueError(f"invalid RECORD row: {row!r}")
        name, digest, size = row
        if name == record_name:
            if digest or size:
                raise ValueError("RECORD must not hash itself")
            continue
        data = archive.read(name)
        if digest != encoded_sha256(data) or size != str(len(data)):
            raise ValueError(f"RECORD digest or size mismatch for {name}")


def verify_wheel(path: Path, expected_version: str) -> str:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("wheel contains duplicate ZIP members")

        for name in names:
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts or "\\" in name:
                raise ValueError(f"unsafe wheel member path: {name}")
            if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES:
                raise ValueError(f"forbidden release file: {name}")

        metadata_name, metadata = find_metadata(archive)
        if canonicalize_name(str(metadata["Name"])) != PROJECT_NAME:
            raise ValueError(f"unexpected project name: {metadata['Name']}")
        if metadata["Version"] != expected_version:
            raise ValueError(
                f"wheel version {metadata['Version']} does not match {expected_version}"
            )

        missing = sorted(REQUIRED_PACKAGE_FILES - set(names))
        if missing:
            raise ValueError(f"wheel is missing required package files: {missing}")
        empty = sorted(
            name for name in REQUIRED_PACKAGE_FILES if not archive.read(name)
        )
        if empty:
            raise ValueError(f"wheel contains empty required files: {empty}")

        record_name = metadata_name.removesuffix("METADATA") + "RECORD"
        if record_name not in names:
            raise ValueError("wheel has no RECORD")
        verify_record(archive, record_name)

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel_dir", type=Path)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    candidates = sorted(args.wheel_dir.glob("echoweave_rtc-*.whl"))
    successes: list[tuple[Path, str]] = []
    failures: list[str] = []
    for path in candidates:
        try:
            successes.append((path, verify_wheel(path, args.expected_version)))
        except ValueError as exc:
            failures.append(f"{path.name}: {exc}")

    if len(successes) != 1:
        details = "; ".join(failures) if failures else "no candidate wheels"
        raise SystemExit(
            f"expected exactly one valid {args.expected_version} wheel; "
            f"found {len(successes)} ({details})"
        )

    path, digest = successes[0]
    print(f"verified {path.name} sha256:{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
