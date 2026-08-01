# EchoWeave media and control protocol v1

EchoWeave uses one WebSocket for UTF-8 JSON control/events and binary media.
Protocol v1 is capability-negotiated: a client must not assume that browser TTS,
PCM audio, avatar events or MP4 fragments are all enabled together.

Except for fully loopback development, the endpoint requires WSS. A clear-text
non-loopback handshake is rejected with close code `1008` before
`session.hello`; the browser must not send a token, microphone frame or control
message over that transport. Trusted reverse proxies must terminate TLS and be
allowlisted by exact direct-peer IP/CIDR.

For a signed session token, `exp` is both an admission limit and an active
session deadline. At expiry the server emits fatal `session_expired`, cancels
remaining media generation and closes the connection; reconnect with a newly
issued one-time token.

## Handshake

After the WebSocket is accepted, the server sends `session.hello`:

```json
{
  "type": "session.hello",
  "protocol": {
    "name": "echoweave.media",
    "magic": "EW",
    "supported_versions": [1],
    "preferred_version": 1,
    "control_schema": "echoweave.control.v1",
    "audio_input": {
      "codec": "pcm_s16le",
      "sample_rate_hz": 16000,
      "channels": 1,
      "frame_duration_ms": {"min": 10, "max": 250}
    }
  },
  "capabilities": ["input.audio_pcm16", "input.text"],
  "limits": {
    "max_control_bytes": 262144,
    "max_text_chars": 1000,
    "max_session_seconds": 1800,
    "start_timeout_seconds": 15
  },
  "requires_ai_disclosure_ack": true
}
```

The capability list above is abbreviated; use the list in the actual event.
The client replies before the advertised start deadline:

```json
{
  "type": "start",
  "persona_id": "demo",
  "access_token": "<demo shared token or scoped one-time session token>",
  "ai_disclosure_ack": true,
  "protocol": {
    "version": 1,
    "capabilities": [
      "input.audio_pcm16",
      "input.text",
      "output.text_stream",
      "output.browser_tts",
      "output.avatar_events",
      "control.cancel",
      "control.ping",
      "identity.ai_disclosure"
    ]
  }
}
```

Capability names contain only lowercase letters, digits, `.`, `_` and `-`.
The server intersects requested and available capabilities. Negotiation needs at
least one input capability and one output capability. Omitting `capabilities`
requests all server capabilities for compatibility; new clients should send an
explicit list.

On success the server sends, in order:

1. `session.negotiated` with `protocol_version`, accepted `capabilities` and
   requested but `unavailable_capabilities`;
2. `session.state` with `state="listening"`;
3. `session.ready` with the authorized persona display data and
   `synthetic=true`.

A WebSocket connection cannot be renegotiated after start. Open a new
connection to change persona, version or capability set.

When `ECHOWEAVE_SESSION_SIGNING_KEY` is enabled, `access_token` is a signed,
short-lived token whose persona and capability claims must cover this exact
request. It is consumed once at successful admission and cannot be replayed.
Every non-demo persona requires this mode; the shared demo access token is not
an authorization mechanism for a real person's identity.

## Server event envelope

Every JSON event emitted by the server includes:

| Field | Meaning |
|---|---|
| `session_id` | non-PII, time-sortable session correlation ID |
| `event_id` | unique event correlation ID |
| `sequence` | monotonically increasing sequence within the connection |
| `server_time_ms` | Unix time in milliseconds |

An `error` event also includes `code`, `message`, `error_id`, `category`,
`retryable` and `fatal`. Clients should branch on `code`, `retryable` and
`fatal`, not parse human-readable `message`. A fatal error is followed by a
connection close; retryable does not mean that an immediate retry is guaranteed
to succeed.

Sequence gaps should be logged as a transport diagnostic. They are not a replay
request: protocol v1 has no event-replay endpoint.

## Client controls

| Type | Required fields | Meaning |
|---|---|---|
| `start` | shown above | authenticate, authorize and negotiate |
| `text` | `text` | submit a text turn after `session.ready` |
| `cancel` | none | cancel the active generation and clear playout |
| `ping` | optional finite `client_time_ms` | liveness/RTT probe; server returns `session.pong` |
| `stop` | none | close the session normally |

Controls are subject to the advertised byte limit and a per-IP token bucket
(default 10 messages/second with a burst of 20). A client may make at most three
start attempts on one connection.

## Binary media packet

Binary packets use a 12-byte little-endian header:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 2 | magic bytes `EW` |
| 2 | 1 | protocol version (`1`) |
| 3 | 1 | kind (`1` mic PCM, `2` TTS PCM, `3` MP4 fragment) |
| 4 | 4 | unsigned turn ID |
| 8 | 4 | unsigned presentation timestamp in ms |
| 12 | n | payload |

The codec-level maximum payload is 32 MiB. The gateway's WebSocket message
limit is normally much lower (default 256 KiB) and is enforced first.

Clients may send only kind-1 packets: mono signed PCM16 little-endian at 16 kHz.
Each frame must contain an even number of bytes and represent 10-250 ms of
audio (320-8,000 payload bytes). The gateway uses a shared per-IP token bucket
with a one-second burst and 1.25x realtime refill. Malformed, oversized or
faster-than-realtime audio closes the connection with a policy/message-size
code.

A `tts.format` event announces sample rate, channel count and codec before
kind-2 packets. Kind-3 packets contain MP4 fragments and use cumulative phrase
PTS in the header.

## Turn events

Important events include:

- `session.state`, `vad.level`, `vad.speech_started`, `vad.speech_ended`;
- `asr.final`;
- `assistant.delta`, `assistant.final`;
- `tts.format`, `tts.browser`, `tts.phrase_end`;
- `avatar.segment`, `av.sync_begin`;
- `playout.clear`, `turn.cancelled`, `degraded`, `error`;
- `turn.metrics`.

`turn.metrics` is emitted after text and all scheduled speech/avatar work finish:

```json
{
  "type": "turn.metrics",
  "turn_id": 4,
  "generation_id": 7,
  "first_token_ms": 183,
  "text_complete_ms": 640,
  "end_to_end_ms": 1420,
  "speech_queue_capacity": 4
}
```

Values are server-side elapsed times rounded to milliseconds. `first_token_ms`
or `text_complete_ms` may be null when no corresponding event was produced.
They are per-turn diagnostics, not billing data or a server-wide percentile.

## Playout synchronization and cancellation

The browser must immediately discard queued media and invalidate the current
playout epoch on `playout.clear`. Media or events from an older `generation_id`
must not become audible/visible after that point.

For synchronized SoulX output, `av.sync_begin` announces the shared audio
format. Kind-2 audio is held until the first kind-3 fragment is available; both
start in the same client playout epoch. If the avatar path degrades, the server
may fall back to PCM/client lip-sync or browser speech only when that fallback
was negotiated, while preserving the AI disclosure.

## Backpressure behavior

The server has one bounded outbound writer per connection. Queue capacity is
limited independently by message count and bytes, and each socket send has a
deadline. A slow consumer is disconnected instead of letting media accumulate
without bound. Clients should keep their receive loop non-blocking, decode media
outside it and reconnect with bounded exponential backoff only for errors marked
retryable.
