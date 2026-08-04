from __future__ import annotations

import hashlib
import importlib.util
import json
import ssl
import sys
import threading
import wave
from pathlib import Path

import pytest

from echoweave.protocol import PacketKind, pack_packet, unpack_packet

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_realtime.py"
SPEC = importlib.util.spec_from_file_location("benchmark_realtime", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write_wav(
    path: Path,
    *,
    frame_count: int = 640,
    channels: int = 1,
    sample_width: int = 2,
    sample_rate: int = 16_000,
) -> bytes:
    sample = b"\x10\x00" if sample_width == 2 else b"\x10"
    pcm = sample * channels * frame_count
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm)
    return pcm


def _config(**overrides):
    values = {
        "url": "ws://127.0.0.1:8765/ws",
        "token": "",
        "persona": "demo",
        "workers": 1,
        "turns": 1,
        "messages": ("benchmark",),
        "timeout_seconds": 5.0,
        "insecure_tls": False,
        "inject_delay_ms": 0.0,
        "inject_invalid_rate": 0.0,
        "inject_disconnect_rate": 0.0,
        "seed": 7,
        "audio": None,
        "audio_pacing": "none",
    }
    values.update(overrides)
    return MODULE.BenchmarkConfig(**values)


class FakeClient:
    def __init__(self, received, *, wait_for_audio_packets: int = 0):
        self.received = list(received)
        self.sent_text: list[str] = []
        self.sent_binary: list[bytes] = []
        self.closed = False
        self._wait_for_audio_packets = wait_for_audio_packets
        self._audio_ready = threading.Event()

    def send_text(self, value: str) -> None:
        self.sent_text.append(value)

    def send_binary(self, value: bytes) -> None:
        self.sent_binary.append(value)
        if len(self.sent_binary) >= self._wait_for_audio_packets:
            self._audio_ready.set()

    def receive(self) -> str | bytes:
        if self._wait_for_audio_packets:
            assert self._audio_ready.wait(timeout=1)
            self._wait_for_audio_packets = 0
        if not self.received:
            raise AssertionError("fake server response queue is empty")
        return self.received.pop(0)

    def close(self, *, send_frame: bool = True) -> None:
        self.closed = True


def _event(event_type: str, **values) -> str:
    return json.dumps({"type": event_type, **values}, separators=(",", ":"))


def _successful_turn_events(*media: bytes) -> list[str | bytes]:
    return [
        _event("vad.speech_ended"),
        _event("asr.final", turn_id=1, text="not reported"),
        _event("assistant.delta", turn_id=1, text="not reported"),
        _event("assistant.final", turn_id=1, text="not reported"),
        *media,
        _event("session.state", state="listening", turn_id=1),
    ]


def test_load_audio_fixture_pads_to_20ms_and_appends_tail(tmp_path: Path) -> None:
    source = tmp_path / "input.wav"
    pcm = _write_wav(source, frame_count=650)

    fixture = MODULE._load_audio_fixture(source, tail_silence_ms=40)

    assert fixture.pcm_sha256 == hashlib.sha256(pcm).hexdigest()
    assert fixture.source_duration_ms == pytest.approx(40.625)
    assert fixture.frame_count == 5
    assert fixture.stream_duration_ms == 100
    assert len(fixture.pcm16) == 5 * MODULE._AUDIO_FRAME_BYTES
    assert fixture.pcm16.startswith(pcm)
    assert fixture.pcm16[len(pcm) :] == b"\x00" * (len(fixture.pcm16) - len(pcm))


@pytest.mark.parametrize("insecure", [False, True])
def test_tls_context_requires_tls_1_2_and_bounds_verification(insecure: bool) -> None:
    context = MODULE._tls_context(insecure=insecure)

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.check_hostname is (not insecure)
    assert context.verify_mode == (ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED)


@pytest.mark.parametrize(
    ("wav_kwargs", "error_code"),
    [
        ({"channels": 2}, "audio_wav_not_mono"),
        ({"sample_width": 1}, "audio_wav_not_pcm16"),
        ({"sample_rate": 48_000}, "audio_wav_not_16khz"),
    ],
)
def test_load_audio_fixture_rejects_wrong_format(
    tmp_path: Path,
    wav_kwargs: dict[str, int],
    error_code: str,
) -> None:
    source = tmp_path / "invalid.wav"
    _write_wav(source, **wav_kwargs)

    with pytest.raises(MODULE.BenchmarkError) as raised:
        MODULE._load_audio_fixture(source)

    assert raised.value.code == error_code


def test_send_audio_uses_exact_20ms_ew_packets(tmp_path: Path) -> None:
    source = tmp_path / "input.wav"
    _write_wav(source, frame_count=320)
    fixture = MODULE._load_audio_fixture(source, tail_silence_ms=40)
    client = FakeClient([])

    MODULE._send_audio(
        client,
        fixture,
        pacing="none",
        stop_event=threading.Event(),
    )

    packets = [unpack_packet(raw) for raw in client.sent_binary]
    assert len(packets) == 3
    assert all(packet.kind == PacketKind.MIC_PCM16 for packet in packets)
    assert all(packet.turn_id == 0 for packet in packets)
    assert [packet.pts_ms for packet in packets] == [0, 20, 40]
    assert all(len(packet.payload) == MODULE._AUDIO_FRAME_BYTES for packet in packets)


def test_audio_turn_sends_while_receiving_and_collects_media(tmp_path: Path) -> None:
    source = tmp_path / "private-input.wav"
    _write_wav(source, frame_count=320)
    fixture = MODULE._load_audio_fixture(source, tail_silence_ms=40)
    audio_one = pack_packet(PacketKind.TTS_PCM16, 1, 20, b"\x00\x00" * 4)
    audio_two = pack_packet(PacketKind.TTS_PCM16, 1, 10, b"\x01\x00" * 4)
    video = pack_packet(PacketKind.VIDEO_FRAGMENT, 1, 0, b"video")
    responses = _successful_turn_events(audio_one, audio_two, video)
    responses.insert(
        -1,
        _event("degraded", component="avatar", fallback="static_avatar"),
    )
    client = FakeClient(responses, wait_for_audio_packets=fixture.frame_count)
    result = MODULE.WorkerResult()

    MODULE._run_turn(
        client,
        _config(audio=fixture),
        result,
        MODULE.random.Random(1),
        "unused private text",
    )

    assert len(client.sent_binary) == fixture.frame_count
    assert client.sent_text == []
    assert result.audio_packets == 2
    assert result.audio_bytes == 16
    assert result.audio_pts_violations == 1
    assert result.video_packets == 1
    assert result.video_bytes == 5
    assert result.video_pts_violations == 0
    assert result.media_turn_mismatches == 0
    assert len(result.speech_end_to_asr_final_ms) == 1
    assert len(result.first_audio_ms) == 1
    assert len(result.first_video_ms) == 1
    assert result.degraded_events == 1
    assert result.degraded_by_component == {"avatar": 1}
    assert result.degraded_by_fallback == {"static_avatar": 1}


def test_schema_v2_report_omits_sensitive_inputs(monkeypatch) -> None:
    fixture = MODULE.AudioFixture(
        pcm16=b"\x00" * MODULE._AUDIO_FRAME_BYTES,
        pcm_sha256="a" * 64,
        source_duration_ms=20.0,
        stream_duration_ms=20,
        frame_count=1,
        tail_silence_ms=0,
    )
    result = MODULE.WorkerResult(
        attempted=1,
        succeeded=1,
        connect_ms=[1.0],
        first_token_ms=[2.0],
        text_final_ms=[3.0],
        turn_complete_ms=[4.0],
        speech_end_to_asr_final_ms=[0.5],
        first_audio_ms=[3.5],
        audio_packets=1,
        audio_bytes=640,
    )
    monkeypatch.setattr(MODULE, "_worker", lambda *_args: result)
    config = _config(
        url="wss://private.example/ws?access_token=url-secret",
        token="token-secret",
        persona="private-persona",
        messages=("private prompt",),
        audio=fixture,
    )

    report = MODULE.run_benchmark(config)
    encoded = json.dumps(report, ensure_ascii=False)

    assert report["schema_version"] == 2
    assert report["config"]["input"]["mode"] == "audio_wav"
    assert report["media"]["audio"] == {
        "packets": 1,
        "bytes": 640,
        "pts_monotonic": True,
        "pts_violations": 0,
    }
    for sensitive in (
        "private.example",
        "url-secret",
        "token-secret",
        "private-persona",
        "private prompt",
    ):
        assert sensitive not in encoded


def test_cli_rejects_message_and_audio_together(tmp_path: Path) -> None:
    source = tmp_path / "input.wav"
    _write_wav(source)

    with pytest.raises(SystemExit):
        MODULE.parse_args(["--message", "text", "--audio-wav", str(source)])
