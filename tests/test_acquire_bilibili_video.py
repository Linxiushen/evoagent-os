from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "acquire_bilibili_video.py"
SPEC = importlib.util.spec_from_file_location("acquire_bilibili_video", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

BVID = "BV1pxeBzUEfa"
UPLOADER_ID = "35847683"
VIDEO_URL = f"https://www.bilibili.com/video/{BVID}/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata(*, uploader_id: str = UPLOADER_ID) -> dict[str, object]:
    return {
        "id": BVID,
        "display_id": BVID,
        "extractor_key": "BiliBili",
        "webpage_url": VIDEO_URL,
        "uploader_id": uploader_id,
        "uploader": "Public uploader",
        "title": "A public source video",
        "duration": 42.25,
        "availability": "public",
        "is_live": False,
        "formats": [
            {
                "format_id": "fixture",
                "has_drm": False,
                "url": "https://cdn.example.invalid/media?signature=RAW-SECRET",
                "http_headers": {"Cookie": "SESSDATA=RAW-COOKIE"},
            }
        ],
        "http_headers": {"Cookie": "SESSDATA=RAW-COOKIE"},
    }


class FakeYtDlp:
    def __init__(
        self,
        metadata: dict[str, object],
        *,
        download_metadata: dict[str, object] | None = None,
        media: bytes = b"video-bytes",
    ):
        self.metadata = metadata
        self.download_metadata = download_metadata or metadata
        self.media = media
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        assert isinstance(command, list)
        assert all(isinstance(argument, str) for argument in command)
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        self.calls.append(command.copy())
        if command[1:] == ["-version"]:
            stdout = b"ffmpeg version 7.1-fixture\n"
        elif command[1:] == ["--version"]:
            stdout = b"2026.07.31\n"
        elif "--dump-single-json" in command:
            stdout = json.dumps(self.metadata).encode("utf-8")
        else:
            template = Path(command[command.index("--output") + 1])
            destination = Path(str(template).replace("%(ext)s", "mp4"))
            if not destination.is_absolute():
                destination = Path(kwargs["cwd"]) / destination
            destination.write_bytes(self.media)
            stdout = json.dumps(self.download_metadata).encode("utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")


def _inputs(tmp_path: Path) -> dict[str, Path]:
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        '{"approved":true,"private_note":"AUTHORIZATION-SECRET"}\n',
        encoding="utf-8",
    )
    yt_dlp = tmp_path / "yt-dlp.exe"
    yt_dlp.write_bytes(b"local yt-dlp fixture")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"local ffmpeg fixture")
    return {
        "authorization": authorization,
        "yt_dlp": yt_dlp,
        "ffmpeg": ffmpeg,
    }


def _acquire(
    tmp_path: Path,
    monkeypatch,
    fake: FakeYtDlp,
    *,
    output_name: str = "acquisition",
    source_url: str = VIDEO_URL,
    expected_uploader_id: str = UPLOADER_ID,
) -> tuple[Path, dict[str, Path]]:
    paths = _inputs(tmp_path)
    monkeypatch.setattr(MODULE.subprocess, "run", fake)
    manifest = MODULE.acquire_bilibili_video(
        source_url=source_url,
        expected_uploader_id=expected_uploader_id,
        authorization_path=paths["authorization"],
        output_dir=tmp_path / output_name,
        yt_dlp=paths["yt_dlp"],
        ffmpeg=paths["ffmpeg"],
    )
    return manifest, paths


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.bilibili.com/video/{BVID}/",
        f"https://example.com/video/{BVID}/",
        f"https://bilibili.com.example.com/video/{BVID}/",
        f"https://api.bilibili.com/video/{BVID}/",
        f"https://user@www.bilibili.com/video/{BVID}/",
        f"https://www.bilibili.com:443/video/{BVID}/",
        f"https://www.bilibili.com/video/{BVID}/?token=secret",
    ],
)
def test_rejects_non_https_or_non_allowlisted_source_hosts(tmp_path, url):
    paths = _inputs(tmp_path)

    with pytest.raises(MODULE.AcquisitionError):
        MODULE.acquire_bilibili_video(
            source_url=url,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not (tmp_path / "output").exists()


def test_checks_uploader_identity_before_downloading(tmp_path, monkeypatch):
    fake = FakeYtDlp(_metadata(uploader_id="999999"))
    paths = _inputs(tmp_path)
    monkeypatch.setattr(MODULE.subprocess, "run", fake)

    with pytest.raises(MODULE.AcquisitionError, match="expected-uploader-id"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert len(fake.calls) == 3
    assert "--dump-single-json" in fake.calls[-1]
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output-*"))


def test_rejects_existing_output_without_overwriting(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    fake = FakeYtDlp(_metadata())
    monkeypatch.setattr(MODULE.subprocess, "run", fake)

    with pytest.raises(MODULE.AcquisitionError, match="already exists"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=output,
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not fake.calls


def test_rejects_symbolic_link_output(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "output"
    try:
        output.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    fake = FakeYtDlp(_metadata())
    monkeypatch.setattr(MODULE.subprocess, "run", fake)

    with pytest.raises(MODULE.AcquisitionError, match="symbolic links"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=output,
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not list(target.iterdir())
    assert not fake.calls


def test_rejects_link_output_even_when_platform_cannot_create_links(
    tmp_path, monkeypatch
):
    paths = _inputs(tmp_path)
    output = (tmp_path / "output").resolve()
    original_is_link = MODULE._is_link

    def simulated_is_link(path):
        return Path(path) == output or original_is_link(path)

    fake = FakeYtDlp(_metadata())
    monkeypatch.setattr(MODULE, "_is_link", simulated_is_link)
    monkeypatch.setattr(MODULE.subprocess, "run", fake)

    with pytest.raises(MODULE.AcquisitionError, match="symbolic links"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=output,
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not fake.calls


@pytest.mark.parametrize(
    ("tool_name", "suffix"),
    [("yt_dlp", ".bat"), ("ffmpeg", ".CMD")],
)
def test_windows_rejects_batch_tools_before_execution(
    tmp_path, monkeypatch, tool_name, suffix
):
    paths = _inputs(tmp_path)
    batch_tool = tmp_path / f"tool{suffix}"
    batch_tool.write_text("@echo off\n", encoding="ascii")
    paths[tool_name] = batch_tool
    fake = FakeYtDlp(_metadata())
    monkeypatch.setattr(MODULE, "IS_WINDOWS", True)
    monkeypatch.setattr(MODULE.subprocess, "run", fake)

    with pytest.raises(MODULE.AcquisitionError, match="Windows batch script"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not fake.calls
    assert not (tmp_path / "output").exists()


def test_success_manifest_is_hash_bound_redacted_and_repeatable(tmp_path, monkeypatch):
    fake = FakeYtDlp(_metadata())
    manifest_path, paths = _acquire(tmp_path, monkeypatch, fake)

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    output = manifest_path.parent
    metadata_path = output / manifest["artifacts"]["metadata"]["path"]
    media_path = output / manifest["artifacts"]["media"]["path"]
    assert sorted(path.name for path in output.iterdir()) == [
        "acquisition-manifest.json",
        "source-metadata.json",
        "source.mp4",
    ]
    assert manifest["kind"] == MODULE.KIND
    assert manifest["acquisition_policy"] == {
        "anonymous": True,
        "cookies_used": False,
        "drm_bypass": False,
        "playlist": False,
        "single_public_video": True,
    }
    assert manifest["source"] == {
        "url": VIDEO_URL,
        "bvid": BVID,
        "canonical_url": VIDEO_URL,
        "duration_seconds": 42.25,
        "expected_uploader_id": UPLOADER_ID,
        "extractor_key": "BiliBili",
        "title": "A public source video",
        "uploader": "Public uploader",
        "uploader_id": UPLOADER_ID,
    }
    assert manifest["authorization_artifact"] == {
        "path": str(paths["authorization"].resolve()),
        "sha256": _sha256(paths["authorization"]),
        "size_bytes": paths["authorization"].stat().st_size,
        "validation": "opaque-hash-binding-only",
    }
    assert manifest["yt_dlp"] == {
        "version": "2026.07.31",
        "executable_sha256": _sha256(paths["yt_dlp"]),
        "executable_size_bytes": paths["yt_dlp"].stat().st_size,
    }
    assert manifest["ffmpeg"] == {
        "version": "ffmpeg version 7.1-fixture",
        "executable_sha256": _sha256(paths["ffmpeg"]),
        "executable_size_bytes": paths["ffmpeg"].stat().st_size,
    }
    assert manifest["artifacts"]["media"]["sha256"] == _sha256(media_path)
    assert manifest["artifacts"]["media"]["size_bytes"] == media_path.stat().st_size
    assert manifest["artifacts"]["metadata"]["sha256"] == _sha256(metadata_path)
    assert (
        manifest["artifacts"]["metadata"]["size_bytes"] == metadata_path.stat().st_size
    )
    published = manifest_bytes + metadata_path.read_bytes()
    assert b"RAW-SECRET" not in published
    assert b"RAW-COOKIE" not in published
    assert b"AUTHORIZATION-SECRET" not in published
    assert str(output).encode() not in manifest_bytes

    assert len(fake.calls) == 4
    probe_command, download_command = fake.calls[2:]
    for command in (probe_command, download_command):
        assert "--ignore-config" in command
        assert "--no-plugin-dirs" in command
        assert "--no-playlist" in command
        assert "--cookies" not in command
        assert "--cookies-from-browser" not in command
        assert "--allow-unplayable-formats" not in command
    assert "--dump-single-json" in probe_command
    assert "--skip-download" in probe_command
    assert "--max-downloads" in download_command
    assert "--no-overwrites" in download_command
    assert download_command[download_command.index("--output") + 1] == (
        "source.%(ext)s"
    )
    assert download_command[download_command.index("--ffmpeg-location") + 1] == str(
        paths["ffmpeg"].resolve()
    )
    assert all(isinstance(command, list) for command in fake.calls)

    second_manifest, _ = _acquire(
        tmp_path,
        monkeypatch,
        fake,
        output_name="acquisition-second",
    )
    assert second_manifest.read_bytes() == manifest_bytes


def test_output_path_template_syntax_cannot_escape_staging(tmp_path, monkeypatch):
    fake = FakeYtDlp(_metadata())

    manifest, _ = _acquire(
        tmp_path,
        monkeypatch,
        fake,
        output_name="acquisition-%(id)s",
    )

    assert manifest.parent == tmp_path / "acquisition-%(id)s"
    assert sorted(path.name for path in manifest.parent.iterdir()) == [
        "acquisition-manifest.json",
        "source-metadata.json",
        "source.mp4",
    ]


def test_accepts_b23_short_link_only_when_it_resolves_to_valid_bilibili_metadata(
    tmp_path, monkeypatch
):
    fake = FakeYtDlp(_metadata())
    manifest_path, _ = _acquire(
        tmp_path,
        monkeypatch,
        fake,
        source_url="https://b23.tv/AbcD1234",
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source"]["url"] == "https://b23.tv/AbcD1234"
    assert manifest["source"]["canonical_url"] == VIDEO_URL
    assert fake.calls[-1][-1] == VIDEO_URL


def test_detects_authorization_change_and_publishes_nothing(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    fake = FakeYtDlp(_metadata())

    def mutate_after_download(command, **kwargs):
        result = fake(command, **kwargs)
        if "--output" in command:
            paths["authorization"].write_bytes(b"changed authorization")
        return result

    monkeypatch.setattr(MODULE.subprocess, "run", mutate_after_download)
    with pytest.raises(MODULE.AcquisitionError, match="authorization artifact changed"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output-*"))


@pytest.mark.parametrize(
    "artifact_name",
    ["source.mp4", "source-metadata.json", "acquisition-manifest.json"],
)
def test_rejects_artifact_changed_during_final_publication(
    tmp_path, monkeypatch, artifact_name
):
    paths = _inputs(tmp_path)
    fake = FakeYtDlp(_metadata())
    original_publish = MODULE._publish_directory

    def tamper_then_publish(staging, output_dir, *, expected_files):
        (staging / artifact_name).write_bytes(b"changed before publication")
        return original_publish(
            staging,
            output_dir,
            expected_files=expected_files,
        )

    monkeypatch.setattr(MODULE.subprocess, "run", fake)
    monkeypatch.setattr(MODULE, "_publish_directory", tamper_then_publish)
    with pytest.raises(MODULE.AcquisitionError, match="changed before publication"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output-*"))


def test_rejects_unexpected_yt_dlp_sidecar_and_publishes_nothing(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    fake = FakeYtDlp(_metadata())

    def add_unexpected_sidecar(command, **kwargs):
        result = fake(command, **kwargs)
        if "--output" in command:
            template = Path(command[command.index("--output") + 1])
            output_parent = template.parent
            if not output_parent.is_absolute():
                output_parent = Path(kwargs["cwd"]) / output_parent
            (output_parent / "source.info.json").write_text(
                '{"untrusted":true}', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(MODULE.subprocess, "run", add_unexpected_sidecar)
    with pytest.raises(MODULE.AcquisitionError, match="exactly one media file"):
        MODULE.acquire_bilibili_video(
            source_url=VIDEO_URL,
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output-*"))


def test_rechecks_uploader_identity_from_download_metadata(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    fake = FakeYtDlp(_metadata(), download_metadata=_metadata(uploader_id="999999"))
    monkeypatch.setattr(MODULE.subprocess, "run", fake)

    with pytest.raises(MODULE.AcquisitionError, match="expected-uploader-id"):
        MODULE.acquire_bilibili_video(
            source_url="https://b23.tv/AbcD1234",
            expected_uploader_id=UPLOADER_ID,
            authorization_path=paths["authorization"],
            output_dir=tmp_path / "output",
            yt_dlp=paths["yt_dlp"],
            ffmpeg=paths["ffmpeg"],
        )

    assert fake.calls[-1][-1] == VIDEO_URL
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output-*"))
