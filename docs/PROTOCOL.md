# EchoWeave media protocol v1

Control and transcript events are UTF-8 JSON WebSocket messages. Media packets
are binary and use a 12-byte little-endian header:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 2 | magic bytes `EW` |
| 2 | 1 | protocol version (`1`) |
| 3 | 1 | kind (`1` mic PCM, `2` TTS PCM, `3` MP4 fragment) |
| 4 | 4 | unsigned turn ID |
| 8 | 4 | unsigned presentation timestamp in ms |
| 12 | n | payload |

Microphone input is mono signed PCM16 little-endian at 16 kHz. Each kind-1
packet must contain 10–250 ms of audio (320–8000 payload bytes). The gateway
uses a shared per-IP token bucket with a one-second burst and 1.25× realtime
refill; malformed, oversized, or faster-than-realtime microphone streams are
closed with WebSocket policy code 1008. A `tts.format` JSON event announces
output sample rate and codec before kind-2 packets.

Important JSON events:

- `session.hello`, `session.ready`, `session.state`
- `vad.level`, `vad.speech_started`, `vad.speech_ended`
- `asr.final`
- `assistant.delta`, `assistant.final`
- `tts.format`, `tts.browser`, `tts.phrase_end`
- `avatar.segment`, `av.sync_begin`
- `playout.clear`, `turn.cancelled`, `degraded`, `error`

The browser must immediately discard queued media when it receives
`playout.clear`.

When `av.sync_begin` is sent, kind-2 audio for that turn is held until the
first kind-3 MP4 fragment has loaded. Both start in the same client playout
epoch. Video fragments carry cumulative phrase PTS in the binary header.
