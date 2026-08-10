"""Acquire one public Bilibili video with provenance and authorization binding.

The controlled yt-dlp invocations deliberately ignore local configuration, do
not use cookies, disable playlists, and never enable DRM workarounds. Raw
yt-dlp metadata is used for validation only because it can contain transient
media URLs and request headers; the published metadata is an allowlisted view.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA_VERSION = 1
KIND = "echoweave-public-bilibili-acquisition"
METADATA_KIND = "echoweave-public-bilibili-metadata"
ALLOWED_HOSTS = frozenset(
    {
        "bilibili.com",
        "www.bilibili.com",
        "m.bilibili.com",
        "b23.tv",
        "www.b23.tv",
    }
)
SHORT_LINK_HOSTS = frozenset({"b23.tv", "www.b23.tv"})
BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")
UPLOADER_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
VERSION = re.compile(r"[ -~]{1,128}\Z")
MEDIA_NAME = re.compile(r"source\.([a-z0-9]{1,10})\Z")
ALLOWED_MEDIA_EXTENSIONS = frozenset({"flv", "m4a", "mkv", "mov", "mp4", "ogg", "webm"})
WINDOWS_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})
IS_WINDOWS = os.name == "nt"
MAX_METADATA_BYTES = 16 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 120
DOWNLOAD_TIMEOUT_SECONDS = 60 * 60

BASE_ARGUMENTS = (
    "--ignore-config",
    "--no-plugin-dirs",
    "--no-playlist",
    "--no-progress",
    "--no-warnings",
)
PROBE_ARGUMENTS = (
    *BASE_ARGUMENTS,
    "--skip-download",
    "--dump-single-json",
)
DOWNLOAD_ARGUMENTS = (
    *BASE_ARGUMENTS,
    "--no-simulate",
    "--max-downloads",
    "1",
    "--format",
    "bestvideo*+bestaudio/best",
    "--merge-output-format",
    "mp4",
    "--ffmpeg-location",
    "${FFMPEG_EXECUTABLE}",
    "--no-overwrites",
    "--output",
    "${OUTPUT_TEMPLATE}",
    "--print-json",
)


class AcquisitionError(ValueError):
    """Raised when an acquisition request or result is unsafe."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _reject_link_components(path: Path, label: str) -> None:
    absolute = _absolute(path)
    for component in reversed((absolute, *absolute.parents)):
        if _is_link(component):
            raise AcquisitionError(f"{label} must not contain symbolic links")


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _bound_file(path: Path, label: str) -> dict[str, Any]:
    candidate = _absolute(path)
    _reject_link_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AcquisitionError(f"{label} could not be resolved") from exc
    if not resolved.is_file():
        raise AcquisitionError(f"{label} must be a regular file")
    digest, size = _sha256(resolved)
    if size == 0:
        raise AcquisitionError(f"{label} must not be empty")
    return {"path": resolved, "sha256": digest, "size_bytes": size}


def _bound_executable(path: Path, label: str) -> dict[str, Any]:
    binding = _bound_file(path, label)
    suffix = Path(binding["path"].name.rstrip(" .")).suffix.casefold()
    if IS_WINDOWS and suffix in WINDOWS_BATCH_SUFFIXES:
        raise AcquisitionError(f"{label} must not be a Windows batch script")
    return binding


def _verify_unchanged(binding: dict[str, Any], label: str) -> None:
    path = binding["path"]
    _reject_link_components(path, label)
    try:
        digest, size = _sha256(path)
    except OSError as exc:
        raise AcquisitionError(f"{label} changed during acquisition") from exc
    if digest != binding["sha256"] or size != binding["size_bytes"]:
        raise AcquisitionError(f"{label} changed during acquisition")


def _normalize_expected_uploader_id(value: str) -> str:
    if not isinstance(value, str) or not UPLOADER_ID.fullmatch(value):
        raise AcquisitionError("expected uploader ID must be a positive decimal UID")
    return value


def _normalize_url(value: str, *, allow_short_link: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\x00" in value:
        raise AcquisitionError("source URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AcquisitionError("source URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise AcquisitionError("source URL must use https")
    if host not in ALLOWED_HOSTS:
        raise AcquisitionError("source URL host is not an allowed Bilibili host")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise AcquisitionError("source URL must not contain credentials or a port")
    if parsed.fragment:
        raise AcquisitionError("source URL must not contain a fragment")
    if not parsed.path.startswith("/") or "\\" in parsed.path:
        raise AcquisitionError("source URL path is invalid")

    is_short_link = host in SHORT_LINK_HOSTS
    if is_short_link:
        if not allow_short_link:
            raise AcquisitionError("resolved video URL must use a Bilibili video host")
        if not re.fullmatch(r"/[0-9A-Za-z_-]{4,64}/?", parsed.path):
            raise AcquisitionError("b23.tv URL path is invalid")
        if parsed.query:
            raise AcquisitionError("b23.tv URL must not contain query parameters")
        query = ""
    else:
        if not re.fullmatch(r"/video/BV[0-9A-Za-z]{10}/?", parsed.path):
            raise AcquisitionError("source URL must identify one Bilibili BV video")
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        if len(query_items) > 1 or any(key != "p" for key, _ in query_items):
            raise AcquisitionError("source URL contains unsupported query parameters")
        if query_items:
            page = query_items[0][1]
            if not re.fullmatch(r"[1-9][0-9]{0,3}", page):
                raise AcquisitionError("source URL has an invalid page number")
            query = urlencode({"p": page})
        else:
            query = ""
    return urlunsplit(("https", host, parsed.path, query, ""))


def _metadata_uploader_id(value: Any) -> str:
    if isinstance(value, str):
        result = value
    elif type(value) is int:
        result = str(value)
    else:
        raise AcquisitionError("yt-dlp metadata has no valid uploader_id")
    if not UPLOADER_ID.fullmatch(result):
        raise AcquisitionError("yt-dlp metadata has no valid uploader_id")
    return result


def _metadata_bvid(metadata: dict[str, Any], canonical_url: str) -> str:
    claims: set[str] = set()
    for field in ("id", "display_id"):
        value = metadata.get(field)
        if isinstance(value, str) and BVID.fullmatch(value):
            claims.add(value)
    path_claim = urlsplit(canonical_url).path.strip("/").split("/")[-1]
    if BVID.fullmatch(path_claim):
        claims.add(path_claim)
    if not claims:
        raise AcquisitionError("yt-dlp metadata has no valid BVID")
    if len(claims) != 1:
        raise AcquisitionError("yt-dlp metadata contains conflicting BVID claims")
    return claims.pop()


def _finite_duration(value: Any) -> float:
    if type(value) not in {int, float}:
        raise AcquisitionError("yt-dlp metadata has no valid duration")
    try:
        duration = float(value)
    except OverflowError as exc:
        raise AcquisitionError("yt-dlp metadata has no valid duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise AcquisitionError("yt-dlp metadata has no valid duration")
    return duration


def _validate_metadata(
    metadata: Any,
    *,
    requested_url: str,
    expected_uploader_id: str,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise AcquisitionError("yt-dlp metadata must be a JSON object")
    if "entries" in metadata or metadata.get("_type") in {
        "playlist",
        "multi_video",
    }:
        raise AcquisitionError("yt-dlp returned a playlist instead of one video")
    if metadata.get("is_live") is True or metadata.get("live_status") in {
        "is_live",
        "is_upcoming",
        "post_live",
    }:
        raise AcquisitionError("live or upcoming video acquisition is not supported")
    availability = metadata.get("availability")
    if availability not in {None, "public"}:
        raise AcquisitionError("video metadata does not identify a public video")
    if metadata.get("has_drm") is True:
        raise AcquisitionError("DRM-protected video acquisition is not supported")
    formats = metadata.get("formats")
    if isinstance(formats, list) and any(
        isinstance(item, dict) and item.get("has_drm") is True for item in formats
    ):
        raise AcquisitionError("DRM-protected formats are not supported")

    extractor_key = metadata.get("extractor_key")
    if extractor_key != "BiliBili":
        raise AcquisitionError("yt-dlp did not resolve the Bilibili video extractor")
    raw_canonical_url = metadata.get("webpage_url")
    if not isinstance(raw_canonical_url, str):
        raise AcquisitionError("yt-dlp metadata has no canonical webpage URL")
    canonical_url = _normalize_url(raw_canonical_url, allow_short_link=False)
    bvid = _metadata_bvid(metadata, canonical_url)
    requested_path = urlsplit(requested_url).path
    requested_bvid = requested_path.strip("/").split("/")[-1]
    if BVID.fullmatch(requested_bvid) and requested_bvid != bvid:
        raise AcquisitionError("yt-dlp metadata BVID does not match the requested URL")

    uploader_id = _metadata_uploader_id(metadata.get("uploader_id"))
    if uploader_id != expected_uploader_id:
        raise AcquisitionError(
            "yt-dlp uploader_id does not match --expected-uploader-id"
        )
    title = metadata.get("title")
    if (
        not isinstance(title, str)
        or not title.strip()
        or "\x00" in title
        or len(title) > 1000
    ):
        raise AcquisitionError("yt-dlp metadata has no valid title")
    uploader = metadata.get("uploader")
    if not isinstance(uploader, str) or not uploader.strip() or "\x00" in uploader:
        uploader = None

    return {
        "bvid": bvid,
        "canonical_url": canonical_url,
        "duration_seconds": _finite_duration(metadata.get("duration")),
        "extractor_key": extractor_key,
        "title": title.strip(),
        "uploader": uploader.strip() if uploader is not None else None,
        "uploader_id": uploader_id,
    }


def _run(command: list[str], *, timeout: int, cwd: Path | None = None) -> bytes:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AcquisitionError(f"yt-dlp failed with exit code {result.returncode}")
    return result.stdout


def _yt_dlp_version(executable: Path) -> str:
    raw = _run([str(executable), "--version"], timeout=30)
    try:
        version = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise AcquisitionError("yt-dlp returned an invalid version") from exc
    if not VERSION.fullmatch(version):
        raise AcquisitionError("yt-dlp returned an invalid version")
    return version


def _ffmpeg_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "-version"],
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode != 0:
        raise AcquisitionError(
            f"ffmpeg failed with exit code {result.returncode} during version check"
        )
    try:
        first_line = result.stdout.decode("utf-8").splitlines()[0].strip()
    except (UnicodeDecodeError, IndexError) as exc:
        raise AcquisitionError("ffmpeg returned an invalid version") from exc
    if not first_line.startswith("ffmpeg version ") or not VERSION.fullmatch(
        first_line
    ):
        raise AcquisitionError("ffmpeg returned an invalid version")
    return first_line


def _probe_metadata(executable: Path, source_url: str) -> dict[str, Any]:
    command = [str(executable), *PROBE_ARGUMENTS, source_url]
    raw = _run(command, timeout=PROBE_TIMEOUT_SECONDS)
    return _decode_metadata(raw, "metadata")


def _decode_metadata(raw: bytes, label: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_METADATA_BYTES:
        raise AcquisitionError(f"yt-dlp {label} response size is invalid")
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcquisitionError(
            f"yt-dlp {label} response is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(metadata, dict):
        raise AcquisitionError(f"yt-dlp {label} must be a JSON object")
    return metadata


def _download_media(
    executable: Path,
    ffmpeg: Path,
    source_url: str,
    staging: Path,
) -> dict[str, Any]:
    template = "source.%(ext)s"
    arguments = [
        str(executable),
        *(
            (
                template
                if argument == "${OUTPUT_TEMPLATE}"
                else str(ffmpeg)
                if argument == "${FFMPEG_EXECUTABLE}"
                else argument
            )
            for argument in DOWNLOAD_ARGUMENTS
        ),
        source_url,
    ]
    raw = _run(arguments, timeout=DOWNLOAD_TIMEOUT_SECONDS, cwd=staging)
    return _decode_metadata(raw, "download metadata")


def _find_downloaded_media(staging: Path) -> Path:
    entries = list(staging.iterdir())
    if len(entries) != 1:
        raise AcquisitionError("yt-dlp did not produce exactly one media file")
    media = entries[0]
    if _is_link(media) or not media.is_file():
        raise AcquisitionError("yt-dlp output must be one regular media file")
    match = MEDIA_NAME.fullmatch(media.name)
    if match is None or match.group(1) not in ALLOWED_MEDIA_EXTENSIONS:
        raise AcquisitionError("yt-dlp produced an unsupported media filename")
    if media.stat().st_size == 0:
        raise AcquisitionError("yt-dlp produced an empty media file")
    return media


def _publish_directory(
    staging: Path,
    output_dir: Path,
    *,
    expected_files: dict[str, tuple[str, int]],
) -> None:
    entries = list(staging.iterdir())
    if {path.name for path in entries} != set(expected_files):
        raise AcquisitionError("staging directory contains unexpected entries")
    if any(_is_link(path) or not path.is_file() for path in entries):
        raise AcquisitionError("staging directory contains an unsafe entry")
    for path in entries:
        if _sha256(path) != expected_files[path.name]:
            raise AcquisitionError(f"{path.name} changed before publication")
    if output_dir.exists() or _is_link(output_dir):
        raise AcquisitionError(f"output directory already exists: {output_dir}")
    try:
        staging.rename(output_dir)
    except FileExistsError as exc:
        raise AcquisitionError(
            f"output directory already exists: {output_dir}"
        ) from exc
    except OSError as exc:
        if output_dir.exists() or _is_link(output_dir):
            raise AcquisitionError(
                f"output directory already exists: {output_dir}"
            ) from exc
        raise AcquisitionError("could not publish acquisition directory") from exc


def acquire_bilibili_video(
    *,
    source_url: str,
    expected_uploader_id: str,
    authorization_path: Path,
    output_dir: Path,
    yt_dlp: Path,
    ffmpeg: Path,
) -> Path:
    source_url = _normalize_url(source_url, allow_short_link=True)
    expected_uploader_id = _normalize_expected_uploader_id(expected_uploader_id)
    output_dir = _absolute(output_dir)
    _reject_link_components(output_dir, "output path")
    if output_dir.exists() or _is_link(output_dir):
        raise AcquisitionError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_components(output_dir.parent, "output parent")

    authorization = _bound_file(authorization_path, "authorization artifact")
    tool = _bound_executable(yt_dlp, "yt-dlp executable")
    ffmpeg_tool = _bound_executable(ffmpeg, "ffmpeg executable")
    version = _yt_dlp_version(tool["path"])
    ffmpeg_version = _ffmpeg_version(ffmpeg_tool["path"])
    raw_metadata = _probe_metadata(tool["path"], source_url)
    source = _validate_metadata(
        raw_metadata,
        requested_url=source_url,
        expected_uploader_id=expected_uploader_id,
    )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        download_metadata = _download_media(
            tool["path"],
            ffmpeg_tool["path"],
            source["canonical_url"],
            staging,
        )
        downloaded_source = _validate_metadata(
            download_metadata,
            requested_url=source["canonical_url"],
            expected_uploader_id=expected_uploader_id,
        )
        if downloaded_source != source:
            raise AcquisitionError(
                "video metadata changed between identity check and download"
            )
        media_path = _find_downloaded_media(staging)
        media_sha256, media_size = _sha256(media_path)

        safe_metadata = {
            "schema_version": SCHEMA_VERSION,
            "kind": METADATA_KIND,
            "access": {
                "anonymous": True,
                "cookies_used": False,
                "drm_bypass": False,
                "playlist": False,
            },
            "requested_url": source_url,
            **source,
        }
        metadata_path = staging / "source-metadata.json"
        with metadata_path.open("xb") as stream:
            stream.write(_json_bytes(safe_metadata))
        metadata_sha256, metadata_size = _sha256(metadata_path)

        _verify_unchanged(authorization, "authorization artifact")
        _verify_unchanged(tool, "yt-dlp executable")
        _verify_unchanged(ffmpeg_tool, "ffmpeg executable")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "acquisition_policy": {
                "anonymous": True,
                "cookies_used": False,
                "drm_bypass": False,
                "playlist": False,
                "single_public_video": True,
            },
            "source": {
                "url": source_url,
                **source,
                "expected_uploader_id": expected_uploader_id,
            },
            "authorization_artifact": {
                "path": str(authorization["path"]),
                "sha256": authorization["sha256"],
                "size_bytes": authorization["size_bytes"],
                "validation": "opaque-hash-binding-only",
            },
            "yt_dlp": {
                "version": version,
                "executable_sha256": tool["sha256"],
                "executable_size_bytes": tool["size_bytes"],
            },
            "ffmpeg": {
                "version": ffmpeg_version,
                "executable_sha256": ffmpeg_tool["sha256"],
                "executable_size_bytes": ffmpeg_tool["size_bytes"],
            },
            "command_intent": {
                "metadata_probe": [*PROBE_ARGUMENTS, "${SOURCE_URL}"],
                "media_download": [
                    *DOWNLOAD_ARGUMENTS,
                    "${VERIFIED_CANONICAL_URL}",
                ],
            },
            "artifacts": {
                "media": {
                    "path": media_path.name,
                    "sha256": media_sha256,
                    "size_bytes": media_size,
                },
                "metadata": {
                    "path": metadata_path.name,
                    "sha256": metadata_sha256,
                    "size_bytes": metadata_size,
                },
            },
        }
        manifest_path = staging / "acquisition-manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(_json_bytes(manifest))
        manifest_sha256, manifest_size = _sha256(manifest_path)
        if output_dir.exists() or _is_link(output_dir):
            raise AcquisitionError(f"output directory already exists: {output_dir}")
        _publish_directory(
            staging,
            output_dir,
            expected_files={
                media_path.name: (media_sha256, media_size),
                metadata_path.name: (metadata_sha256, metadata_size),
                manifest_path.name: (manifest_sha256, manifest_size),
            },
        )
        return output_dir / "acquisition-manifest.json"
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="One public Bilibili video URL")
    parser.add_argument("--expected-uploader-id", required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--yt-dlp", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest = acquire_bilibili_video(
            source_url=args.url,
            expected_uploader_id=args.expected_uploader_id,
            authorization_path=args.authorization,
            output_dir=args.output,
            yt_dlp=args.yt_dlp,
            ffmpeg=args.ffmpeg,
        )
    except (AcquisitionError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Bilibili acquisition failed: {exc}") from exc
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
