from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from echoweave.chunking import SemanticChunker
from echoweave.contracts import AudioFrame, PersonaProfile, SessionState
from echoweave.protocol import PacketKind, pack_packet
from echoweave.runtime import RuntimeAdapters

EmitJSON = Callable[[dict[str, Any]], Awaitable[None]]
EmitBinary = Callable[[bytes], Awaitable[None]]
AuthorizationCheck = Callable[[], None]
LOGGER = logging.getLogger("echoweave.pipeline")
MAX_PHRASE_AUDIO_BYTES = 16 * 1024 * 1024


class AuthorizationFailure(RuntimeError):
    """Raised when a session's server-side persona authorization is no longer valid."""


class RealtimeSession:
    """EchoWeave's turn engine with endpointing, streaming and barge-in."""

    def __init__(
        self,
        session_id: str,
        persona: PersonaProfile,
        adapters: RuntimeAdapters,
        emit_json: EmitJSON,
        emit_binary: EmitBinary,
        sample_rate: int = 16_000,
        authorization_check: AuthorizationCheck | None = None,
        max_utterance_seconds: int = 60,
        max_text_chars: int = 1_000,
        max_turns: int = 100,
        authorization_check_interval: float = 0.25,
    ) -> None:
        self.session_id = session_id
        self.persona = persona
        self.adapters = adapters
        self.emit_json = emit_json
        self.emit_binary = emit_binary
        self.sample_rate = sample_rate
        self.authorization_check = authorization_check
        self.max_utterance_bytes = sample_rate * 2 * max_utterance_seconds
        self.max_text_chars = max_text_chars
        self.max_turns = max_turns
        self.authorization_check_interval = authorization_check_interval
        self._last_authorization_check = 0.0
        self.state = SessionState.READY
        self.turn_id = 0
        self.generation_id = 0
        self._speech_active = False
        self._utterance = bytearray()
        self._pre_roll: deque[bytes] = deque(maxlen=8)
        self._response_task: asyncio.Task | None = None
        self._cancel_event = asyncio.Event()
        self._history: list[dict[str, str]] = [
            {"role": "system", "content": persona.system_prompt}
        ]

    async def start(self) -> None:
        await self._set_state(SessionState.LISTENING)
        await self.emit_json(
            {
                "type": "session.ready",
                "session_id": self.session_id,
                "persona_id": self.persona.persona_id,
                "persona": self.persona.display_name,
                "disclosure": self.persona.disclosure_text,
                "synthetic": True,
                "fictional": self.persona.is_fictional,
            }
        )

    async def ingest_pcm(self, pcm16: bytes, sample_rate: int) -> None:
        if sample_rate != self.sample_rate:
            await self._error(
                "invalid_audio_rate",
                f"expected {self.sample_rate} Hz PCM, got {sample_rate} Hz",
            )
            return
        if not pcm16 or len(pcm16) % 2:
            await self._error(
                "invalid_audio_frame",
                "audio frames must contain an even number of PCM16 bytes",
            )
            return
        self._pre_roll.append(pcm16)
        try:
            decision = await self.adapters.vad.process(pcm16, sample_rate)
        except Exception as exc:  # noqa: BLE001 - VAD is an adapter boundary
            self.adapters.vad.reset()
            await self._internal_error("vad_failed", exc)
            await self._set_state(SessionState.LISTENING)
            return
        await self.emit_json(
            {
                "type": "vad.level",
                "probability": round(decision.probability, 4),
            }
        )
        if decision.speech_started and not self._speech_active:
            self._speech_active = True
            self._utterance = bytearray(b"".join(self._pre_roll))
            await self.cancel_response("barge_in")
            await self._set_state(SessionState.USER_SPEAKING)
            await self.emit_json({"type": "vad.speech_started"})
        elif self._speech_active:
            self._utterance.extend(pcm16)
            if len(self._utterance) > self.max_utterance_bytes:
                self._speech_active = False
                self._utterance.clear()
                self.adapters.vad.reset()
                await self._error(
                    "utterance_too_long",
                    "speech exceeded the configured duration limit",
                )
                await self._set_state(SessionState.LISTENING)
                return

        if decision.speech_ended and self._speech_active:
            self._speech_active = False
            audio = bytes(self._utterance)
            self._utterance.clear()
            self.adapters.vad.reset()
            await self.emit_json({"type": "vad.speech_ended"})
            await self._start_audio_turn(audio)

    async def submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if len(text) > self.max_text_chars:
            await self._error("text_too_long", "text turn exceeds the configured limit")
            return
        if self.turn_id >= self.max_turns:
            await self._error("turn_limit", "session turn limit reached")
            return
        try:
            self._authorize(force=True)
        except AuthorizationFailure:
            await self.cancel_response("authorization_revoked")
            await self._error(
                "authorization_revoked",
                "persona authorization is no longer valid",
            )
            await self._set_state(SessionState.LISTENING)
            return
        await self.cancel_response("new_text_turn")
        self.turn_id += 1
        turn_id = self.turn_id
        self._response_task = asyncio.create_task(
            self._respond(turn_id, text),
            name=f"echoweave-text-{self.session_id}-{turn_id}",
        )

    async def _start_audio_turn(self, pcm16: bytes) -> None:
        if self.turn_id >= self.max_turns:
            await self._error("turn_limit", "session turn limit reached")
            return
        try:
            self._authorize(force=True)
        except AuthorizationFailure:
            await self.cancel_response("authorization_revoked")
            await self._error(
                "authorization_revoked",
                "persona authorization is no longer valid",
            )
            await self._set_state(SessionState.LISTENING)
            return
        await self.cancel_response("new_audio_turn")
        self.turn_id += 1
        turn_id = self.turn_id
        self._response_task = asyncio.create_task(
            self._transcribe_and_respond(turn_id, pcm16),
            name=f"echoweave-audio-{self.session_id}-{turn_id}",
        )

    async def _transcribe_and_respond(self, turn_id: int, pcm16: bytes) -> None:
        try:
            await self._set_state(SessionState.TRANSCRIBING, turn_id)
            started = time.perf_counter()
            transcript = await self.adapters.asr.transcribe(pcm16, self.sample_rate)
            text = transcript.text.strip()
            await self.emit_json(
                {
                    "type": "asr.final",
                    "turn_id": turn_id,
                    "text": text,
                    "language": transcript.language,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            if not text:
                await self._error("empty_transcript", "ASR returned no text", turn_id)
                await self._set_state(SessionState.LISTENING, turn_id)
                return
            await self._respond(turn_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary must degrade safely
            await self._internal_error("asr_failed", exc, turn_id)
            await self._set_state(SessionState.LISTENING, turn_id)

    async def _respond(self, turn_id: int, user_text: str) -> None:
        self.generation_id += 1
        generation_id = self.generation_id
        self._cancel_event = asyncio.Event()
        cancel_event = self._cancel_event
        assistant_text = ""
        speech_queue: asyncio.Queue[str | None] = asyncio.Queue()
        speech_task = asyncio.create_task(
            self._speech_worker(
                speech_queue,
                turn_id,
                generation_id,
                cancel_event,
            ),
            name=f"echoweave-speech-{self.session_id}-{turn_id}",
        )
        try:
            self._authorize(force=True)
            self._history.append({"role": "user", "content": user_text})
            await self._set_state(SessionState.THINKING, turn_id)
            chunker = SemanticChunker()
            async for delta in self.adapters.llm.stream(self._history, cancel_event):
                if not self._is_current(generation_id, cancel_event):
                    return
                self._authorize()
                assistant_text += delta
                await self.emit_json(
                    {
                        "type": "assistant.delta",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "text": delta,
                    }
                )
                for phrase in chunker.push(delta):
                    speech_queue.put_nowait(phrase)
            tail = chunker.flush()
            if tail and self._is_current(generation_id, cancel_event):
                speech_queue.put_nowait(tail)
            speech_queue.put_nowait(None)
            if not self._is_current(generation_id, cancel_event):
                return
            assistant_text = assistant_text.strip()
            self._history.append({"role": "assistant", "content": assistant_text})
            self._history = [self._history[0], *self._history[1:][-12:]]
            await self.emit_json(
                {
                    "type": "assistant.final",
                    "turn_id": turn_id,
                    "generation_id": generation_id,
                    "text": assistant_text,
                }
            )
            await speech_task
            if not self._is_current(generation_id, cancel_event):
                return
            await self._set_state(SessionState.LISTENING, turn_id)
        except asyncio.CancelledError:
            await self.emit_json(
                {
                    "type": "turn.cancelled",
                    "turn_id": turn_id,
                    "generation_id": generation_id,
                }
            )
            raise
        except AuthorizationFailure:
            await self.cancel_response("authorization_revoked")
            await self._error(
                "authorization_revoked",
                "persona authorization is no longer valid",
                turn_id,
            )
            await self._set_state(SessionState.LISTENING, turn_id)
        except Exception as exc:  # noqa: BLE001 - adapter boundary must degrade safely
            await self._internal_error("generation_failed", exc, turn_id)
            await self._set_state(SessionState.LISTENING, turn_id)
        finally:
            if not speech_task.done():
                speech_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await speech_task

    async def _speech_worker(
        self,
        queue: asyncio.Queue[str | None],
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        while self._is_current(generation_id, cancel_event):
            phrase = await queue.get()
            if phrase is None:
                return
            await self._speak_phrase(
                phrase,
                turn_id,
                generation_id,
                cancel_event,
            )

    async def _speak_phrase(
        self,
        phrase: str,
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        if not self._is_current(generation_id, cancel_event):
            return
        self._authorize()
        await self._set_state(SessionState.SPEAKING, turn_id)
        audio = bytearray()
        audio_frames: list[AudioFrame] = []
        sample_rate = 0
        sent_format = False
        tts_failed = False
        synchronized_avatar = bool(
            getattr(self.adapters.avatar, "synchronized_playback", False)
        )
        try:
            async for frame in self.adapters.tts.synthesize(
                phrase, self.persona, cancel_event
            ):
                if not self._is_current(generation_id, cancel_event):
                    return
                self._authorize()
                sample_rate = frame.sample_rate
                if not sent_format:
                    await self.emit_json(
                        {
                            "type": "tts.format",
                            "turn_id": turn_id,
                            "generation_id": generation_id,
                            "sample_rate": frame.sample_rate,
                            "channels": frame.channels,
                            "codec": frame.codec,
                        }
                    )
                    sent_format = True
                audio.extend(frame.pcm)
                if len(audio) > MAX_PHRASE_AUDIO_BYTES:
                    raise RuntimeError("TTS phrase exceeded the audio buffer limit")
                audio_frames.append(frame)
                if not synchronized_avatar:
                    packet = pack_packet(
                        PacketKind.TTS_PCM16, turn_id, frame.pts_ms, frame.pcm
                    )
                    await self.emit_binary(packet)
        except AuthorizationFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - TTS failure triggers safe fallback
            tts_failed = True
            audio.clear()
            sample_rate = 0
            reference = self._log_exception("tts", exc)
            await self.emit_json(
                {
                    "type": "playout.clear",
                    "reason": "tts_fallback",
                    "preserve_transcript": True,
                }
            )
            await self.emit_json(
                {
                    "type": "degraded",
                    "component": "tts",
                    "reason": f"internal error ({reference})",
                    "fallback": "browser_speech",
                }
            )
            await self.emit_json(
                {
                    "type": "tts.browser",
                    "turn_id": turn_id,
                    "generation_id": generation_id,
                    "text": phrase,
                }
            )
        else:
            if self.adapters.tts.browser_fallback:
                await self.emit_json(
                    {
                        "type": "tts.browser",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "text": phrase,
                    }
                )
        await self.emit_json(
            {
                "type": "tts.phrase_end",
                "turn_id": turn_id,
                "generation_id": generation_id,
                "text": phrase,
            }
        )
        if not tts_failed:
            synchronized_media_started = await self._animate_phrase(
                phrase,
                bytes(audio),
                sample_rate or 16_000,
                turn_id,
                generation_id,
                cancel_event,
                audio_frames if synchronized_avatar else None,
            )
            if (
                synchronized_avatar
                and not synchronized_media_started
                and self._is_current(generation_id, cancel_event)
            ):
                for frame in audio_frames:
                    await self.emit_binary(
                        pack_packet(
                            PacketKind.TTS_PCM16,
                            turn_id,
                            frame.pts_ms,
                            frame.pcm,
                        )
                    )

    async def _animate_phrase(
        self,
        phrase: str,
        audio: bytes,
        sample_rate: int,
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
        synchronized_audio: list[AudioFrame] | None = None,
    ) -> bool:
        synchronized_media_started = False
        video_pts_ms = 0
        try:
            self._authorize()
            async for segment in self.adapters.avatar.animate(
                phrase, audio, sample_rate, self.persona, cancel_event
            ):
                if not self._is_current(generation_id, cancel_event):
                    return False
                self._authorize()
                await self.emit_json(
                    {
                        "type": "avatar.segment",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "kind": segment.kind,
                        "index": segment.index,
                        "url": segment.url,
                        "mime_type": segment.mime_type,
                        "duration_ms": segment.duration_ms,
                        "metadata": segment.metadata,
                    }
                )
                if segment.data:
                    if (
                        synchronized_audio is not None
                        and not synchronized_media_started
                    ):
                        await self.emit_json(
                            {
                                "type": "av.sync_begin",
                                "turn_id": turn_id,
                                "generation_id": generation_id,
                                "sample_rate": synchronized_audio[0].sample_rate,
                                "channels": synchronized_audio[0].channels,
                                "codec": synchronized_audio[0].codec,
                            }
                        )
                        for frame in synchronized_audio:
                            await self.emit_binary(
                                pack_packet(
                                    PacketKind.TTS_PCM16,
                                    turn_id,
                                    frame.pts_ms,
                                    frame.pcm,
                                )
                            )
                    await self.emit_binary(
                        pack_packet(
                            PacketKind.VIDEO_FRAGMENT,
                            turn_id,
                            video_pts_ms,
                            segment.data,
                        )
                    )
                    synchronized_media_started = True
                    video_pts_ms += max(0, segment.duration_ms)
            return synchronized_media_started or synchronized_audio is None
        except AuthorizationFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - avatar failure triggers safe fallback
            reference = self._log_exception("avatar", exc)
            await self.emit_json(
                {
                    "type": "degraded",
                    "component": "avatar",
                    "reason": f"internal error ({reference})",
                    "fallback": "static_avatar",
                }
            )
            return synchronized_media_started

    async def cancel_response(self, reason: str = "cancelled") -> None:
        had_prior_generation = self.generation_id > 0 or self._response_task is not None
        self.generation_id += 1
        self._cancel_event.set()
        task = self._response_task
        self._response_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if had_prior_generation:
            await self.emit_json({"type": "playout.clear", "reason": reason})
        if reason == "client_cancelled" and self.state is not SessionState.CLOSED:
            await self._set_state(SessionState.LISTENING)

    async def close(self) -> None:
        await self.cancel_response("session_closed")
        await self._set_state(SessionState.CLOSED)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        task = self._response_task
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout)

    def _is_current(self, generation_id: int, cancel_event: asyncio.Event) -> bool:
        return generation_id == self.generation_id and not cancel_event.is_set()

    async def _set_state(self, state: SessionState, turn_id: int | None = None) -> None:
        self.state = state
        await self.emit_json(
            {
                "type": "session.state",
                "state": state.value,
                "turn_id": turn_id if turn_id is not None else self.turn_id,
            }
        )

    async def _error(self, code: str, message: str, turn_id: int | None = None) -> None:
        await self.emit_json(
            {
                "type": "error",
                "code": code,
                "message": message,
                "turn_id": turn_id if turn_id is not None else self.turn_id,
            }
        )

    def _authorize(self, *, force: bool = False) -> None:
        if self.authorization_check is not None:
            now = time.monotonic()
            if (
                not force
                and now - self._last_authorization_check
                < self.authorization_check_interval
            ):
                return
            try:
                self.authorization_check()
            except Exception as exc:
                raise AuthorizationFailure from exc
            self._last_authorization_check = now

    @staticmethod
    def _log_exception(component: str, exc: Exception) -> str:
        reference = uuid.uuid4().hex[:12]
        LOGGER.error(
            "%s failure reference=%s: %s",
            component,
            reference,
            type(exc).__name__,
            exc_info=exc,
        )
        return reference

    async def _internal_error(
        self, code: str, exc: Exception, turn_id: int | None = None
    ) -> None:
        reference = self._log_exception(code, exc)
        await self._error(
            code,
            f"component failed; reference {reference}",
            turn_id,
        )
