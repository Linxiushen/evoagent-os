# Architecture

EchoWeave-RTC owns the realtime orchestration layer. It does not rename or
repackage third-party model weights.

```text
Browser microphone (16 kHz PCM)
        │
        ▼
EchoWeave media protocol + bounded session state
        │
        ├── Silero VAD v5 ── endpoint / barge-in
        │
        └── Qwen3-ASR-1.7B ── final transcript
                                 │
              reviewed Nuwa profile + disclosure policy
                                 │
                                 ▼
                   DeepSeek V4 Flash SSE
                                 │ bounded semantic phrases
                 ┌───────────────┴───────────────┐
                 │ LLM text keeps streaming      │ serialized speech queue
                 ▼                               ▼
             browser transcript        VoxCPM2 streaming PCM
                                             │
                            ┌────────────────┴──────────────┐
                            │ 48 kHz browser audio          │ phrase WAV
                            │                               ▼
                            │                    SoulX FlashHead Lite
                            │                               │
                            └──────── synchronized ─────────┤
                                                            ▼
                                             browser playout + permanent
                                                "AI 数字分身" overlay
```

## State and cancellation

```text
READY -> LISTENING -> USER_SPEAKING -> TRANSCRIBING -> THINKING -> SPEAKING
             ^                                                |
             └──────────────── barge-in ──────────────────────┘
```

Every response receives a monotonic `generation_id`. On speech start or explicit
cancel, EchoWeave increments the generation, sets the cancellation event,
cancels the active task and tells the browser to clear playout. A GPU kernel
that cannot be interrupted may finish, but output from its old generation is
dropped.

## Process boundaries

Keep the gateway and three GPU workers separate:

- **gateway**: FastAPI, EchoWeave, Silero; Python 3.12 / CPU.
- **asr-worker**: `qwen-asr` + vLLM; Python 3.12 / GPU.
- **tts-worker**: VoxCPM2 or vLLM-Omni; Python 3.10–3.12 / GPU.
- **avatar-worker**: SoulX official stack; Python 3.10, Torch 2.7.1,
  CUDA 12.8 / dedicated GPU.

This separation is functional, not stylistic: the verified SoulX and Qwen
dependency stacks require different CUDA/Python combinations.

## Backpressure

The browser sends bounded microphone frames; the server retains only a short
pre-roll. LLM text is converted to short semantic phrases and handed to a
serialized speech worker, so TTS does not stop receipt of later SSE tokens.
For SoulX, EchoWeave buffers one short phrase of PCM and releases it on the
first watermarked video segment; the browser starts muted video and PCM from
the same playout epoch. On production workers, cap:

- microphone ring buffer: 2 seconds;
- LLM-to-TTS phrase queue: 4 phrases;
- audio playout buffer: 500–1000 ms;
- video frame queue: 2–3 frames, dropping old video before delaying audio.

The v0.1 browser transport is a low-dependency WebSocket PCM/MP4 protocol. For
internet-scale deployment, keep the same adapters/state machine and replace the
media edge with WebRTC (Opus + VP8/H.264) and TURN.
