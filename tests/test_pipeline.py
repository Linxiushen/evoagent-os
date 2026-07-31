from echoweave.adapters.asr import DemoASR
from echoweave.adapters.avatar import ClientLipSyncAvatar
from echoweave.adapters.llm import DemoLLM
from echoweave.adapters.tts import BrowserTTS
from echoweave.adapters.vad import EnergyVAD
from echoweave.contracts import AudioFrame, AvatarSegment, PersonaProfile
from echoweave.pipeline import RealtimeSession
from echoweave.protocol import PacketKind, unpack_packet
from echoweave.runtime import RuntimeAdapters


async def test_text_turn_runs_end_to_end():
    events = []
    binary = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        binary.append(packet)

    adapters = RuntimeAdapters(
        EnergyVAD(), DemoASR(), DemoLLM(), BrowserTTS(), ClientLipSyncAvatar()
    )
    persona = PersonaProfile(
        persona_id="demo",
        display_name="Echo",
        system_prompt="You are a fictional AI.",
        disclosure_text="AI demo",
        is_fictional=True,
    )
    session = RealtimeSession("test", persona, adapters, emit_json, emit_binary)
    await session.start()
    await session.submit_text("测试一下")
    await session.wait_idle()

    event_types = [event["type"] for event in events]
    assert "assistant.delta" in event_types
    assert "assistant.final" in event_types
    assert "tts.browser" in event_types
    assert "avatar.segment" in event_types
    assert events[-1]["state"] == "listening"
    assert binary == []

    await session.cancel_response("client_cancelled")
    assert events[-2]["type"] == "playout.clear"
    assert events[-1] == {
        "type": "session.state",
        "state": "listening",
        "turn_id": 1,
    }


async def test_history_keeps_only_one_system_prompt():
    events = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(_packet):
        return None

    session = RealtimeSession(
        "history",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system-once",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            DemoLLM(),
            BrowserTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("first")
    await session.wait_idle()
    await session.submit_text("second")
    await session.wait_idle()

    assert [item["content"] for item in session._history].count("system-once") == 1


async def test_partial_tts_is_cleared_before_browser_fallback():
    outputs = []

    async def emit_json(event):
        outputs.append(("json", event))

    async def emit_binary(packet):
        outputs.append(("binary", packet))

    class PartialTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            raise RuntimeError("synthetic failure")

    session = RealtimeSession(
        "partial-tts",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            DemoLLM(),
            PartialTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()

    json_events = [item for kind, item in outputs if kind == "json"]
    clear_indexes = [
        index
        for index, event in enumerate(json_events)
        if event["type"] == "playout.clear" and event.get("reason") == "tts_fallback"
    ]
    fallback_indexes = [
        index
        for index, event in enumerate(json_events)
        if event["type"] == "tts.browser"
    ]
    assert clear_indexes
    assert fallback_indexes
    assert clear_indexes[0] < fallback_indexes[0]
    assert not any(event["type"] == "avatar.segment" for event in json_events)


async def test_authorization_failure_never_falls_back_to_browser_tts():
    events = []
    binary = []
    checks = 0

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        binary.append(packet)

    def authorize():
        nonlocal checks
        checks += 1
        if checks >= 6:
            raise RuntimeError("revoked")

    class OnePhraseLLM:
        async def stream(self, _messages, _cancel_event):
            yield "这是一段用于授权撤回测试的完整回答。"

    class TwoFrameTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            yield AudioFrame(b"\x00\x00" * 480, 48_000, pts_ms=10)

    session = RealtimeSession(
        "revoked",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            OnePhraseLLM(),
            TwoFrameTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
        authorization_check=authorize,
        authorization_check_interval=0,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()

    assert binary
    assert not any(event["type"] == "tts.browser" for event in events)
    assert any(
        event["type"] == "playout.clear"
        and event.get("reason") == "authorization_revoked"
        for event in events
    )
    assert any(event.get("code") == "authorization_revoked" for event in events)


async def test_synchronized_avatar_failure_does_not_duplicate_audio():
    events = []
    packets = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        packets.append(unpack_packet(packet))

    class OnePhraseLLM:
        async def stream(self, _messages, _cancel_event):
            yield "同步播放测试已经准备完成。"

    class TwoFrameTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            yield AudioFrame(b"\x00\x00" * 480, 48_000, pts_ms=10)

    class OneThenFailAvatar:
        synchronized_playback = True

        async def animate(
            self,
            _text,
            _audio,
            _sample_rate,
            _persona,
            _cancel_event,
        ):
            yield AvatarSegment(
                kind="soulx_mp4",
                data=b"video",
                duration_ms=3_000,
            )
            raise RuntimeError("later segment failed")

    session = RealtimeSession(
        "sync",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            OnePhraseLLM(),
            TwoFrameTTS(),
            OneThenFailAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()

    assert sum(packet.kind == PacketKind.TTS_PCM16 for packet in packets) == 2
    assert sum(packet.kind == PacketKind.VIDEO_FRAGMENT for packet in packets) == 1
    assert any(event["type"] == "av.sync_begin" for event in events)
