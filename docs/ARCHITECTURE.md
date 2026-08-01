# Architecture

EchoWeave-RTC owns the consent-aware realtime orchestration layer. It does not
rename, redistribute or silently substitute third-party model weights.

## Data path

```text
Browser
  AudioWorklet (16 kHz PCM16, 20 ms frames; ScriptProcessor fallback)
      |
      v
EW v1 WebSocket gateway
  authentication + origin policy + protocol/capability negotiation
  control/audio rate limits + session deadlines + bounded outbound pump
      |
      v
RealtimeSession (one isolated state machine per connection)
  VAD -> ASR -> streaming LLM -> bounded semantic phrase queue
                               |                         |
                               +-> transcript            +-> TTS PCM
                                                             |
                                                             +-> SoulX segments
      |
      v
Browser bounded playout + permanent "AI 数字分身 / SYNTHETIC MEDIA" disclosure
```

The browser worklet performs streaming resampling and transfers fixed PCM
buffers without retaining an unbounded recording. It checks WebSocket
`bufferedAmount` before sending and drops stale microphone frames if the client
network cannot keep up. This protects latency; it is not a recording or retry
mechanism.

## Session establishment

The gateway sends `session.hello` immediately after accepting the WebSocket.
The client replies with `start`, protocol version, desired capabilities,
persona ID, disclosure acknowledgement and (when configured) a demo credential
or scoped session token. The gateway intersects capabilities, consumes and
authorizes a one-time token, reserves global session capacity, then acquires an
isolated adapter set and sends `session.negotiated` and `session.ready`.
Unauthenticated sockets waiting for `start` are bounded per IP but do not reserve
the global pending/active model-session budget.
Admitted sessions remain inside that budget through bounded model cleanup after
socket closure, preventing a disconnect/reconnect burst from exploiting a
cleanup window to over-admit GPU work.

Runtime construction is warmed during application startup and serialized to
avoid duplicate model initialization storms. Concurrent waiters share exactly
one build future; caller timeout or cancellation cannot start a background build
queue. One caller owns each resulting stateful adapter set, while local Qwen and
Vox model weights are shared behind class-level inference locks. If a build
times out, the process remains live for diagnostics and readiness becomes false.
Lifespan shutdown retains deterministic ownership of the single build and every
warmed adapter cleanup.

## State, generations and cancellation

```text
READY -> LISTENING -> USER_SPEAKING -> TRANSCRIBING -> THINKING -> SPEAKING
             ^                                                |
             +------------------- barge-in --------------------+
```

Every response receives a monotonic `generation_id`. On speech start, explicit
cancel or authorization failure, EchoWeave advances the generation, sets its
cancellation event, cancels the active turn and tells the browser to clear
playout. Results from an older generation are checked and dropped at every
emission boundary. A non-interruptible GPU kernel may still finish internally,
but its stale result cannot re-enter the active playout generation.

Cancelled tasks that do not stop inside the cancellation budget are quarantined
and observed until completion, rather than blocking the next turn. Each unique
adapter is closed once when its session ends.

## Backpressure and bounded resources

Backpressure is enforced at multiple boundaries:

| Boundary | Mechanism |
|---|---|
| Browser microphone | 20 ms frames and WebSocket `bufferedAmount` ceiling |
| Gateway inbound audio | 10-250 ms frame validation and per-IP 1.25x realtime token bucket |
| Gateway control plane | bounded message size and per-IP token bucket |
| Gateway model sessions | global pending-plus-active-plus-cleanup admission ceiling |
| LLM to speech | serialized queue, default capacity four semantic phrases |
| Server to browser | one writer, bounded by message count and total bytes |
| TTS/avatar workers | bounded response sizes, duration, ordering and content types |

The speech queue waits only within its backpressure budget. The outbound pump
does not let producers call the socket concurrently: it serializes JSON and
binary frames, fails a slow consumer when queue limits are reached, and fails a
send that exceeds its deadline. Client disconnects or overload cancel the
session instead of accumulating stale audio/video.

Default gateway limits are documented in `.env.example`; important defaults
include 128 outbound messages, 64 MiB queued outbound bytes, a 5 second send
deadline, a 15 second start deadline, 300 second idle deadline and 1,800 second
maximum session. These are safety ceilings, not recommended capacity targets.
Tune them only with production-shaped load tests.

## Stage deadlines

`RealtimeSession` applies a 120 second end-to-end turn deadline and smaller
budgets for ASR (30 s), first LLM token (15 s), LLM idle (20 s), speech queue
backpressure (8 s), each TTS phrase (45 s), each avatar phrase (45 s), outbound
emission (5 s), cancellation (2 s) and adapter cleanup (5 s).

A stage timeout emits a bounded failure/degradation signal, cancels its current
generation and records a fixed low-cardinality outcome. Deadlines limit damage
from an unhealthy dependency; they do not prove that normal latency meets the
SLO.

## Process boundaries

Keep the gateway and GPU workers separate:

- **gateway**: FastAPI, EchoWeave, Silero, authorization and protocol state;
- **asr-worker**: `qwen-asr` or vLLM on an isolated GPU environment;
- **tts-worker**: the consent-verifying VoxCPM2 bridge in its compatible Python/CUDA stack;
- **avatar-worker**: SoulX official stack on a dedicated GPU.

The HTTP adapters use explicitly owned connection pools. They validate response
content types, response size/duration, streamed line size and avatar segment
ordering. VoxCPM and SoulX consume one-time assertions bound to audience,
persona, manifest revision, consent scope and reference-media hashes before
reading an admitted request body. Worker signing keys and model endpoints remain
server-side.

This process separation is functional: the verified SoulX, Qwen and VoxCPM2
stacks can require different Python/CUDA versions, and independent concurrency
limits prevent one GPU queue from consuming all gateway capacity.

## Observability and readiness

Each application instance owns a bounded, thread-safe metric registry. It
records gateway session/error/transport counters and, when injected into the
session engine, fixed-label VAD/ASR/LLM/TTS/avatar/turn latency and
backpressure/cancellation events. `turn.metrics` also gives the current client
its own first-token, text-complete and end-to-end measurements.

- `/api/health` is process liveness only.
- `/api/ready` reports whether the selected adapter set constructed successfully.
- `/api/metrics` exposes a bounded JSON snapshot only to an authenticated Bearer
  client when an access token exists, or to a fully loopback request otherwise.

Readiness currently has scope `adapter_construction`; it explicitly reports
`dependency_reachability=not_probed`. Operators must add bounded worker probes or
external synthetic checks before treating readiness as proof that remote model
dependencies can serve inference.

## Media edge

The 0.2 browser transport is a low-dependency WebSocket PCM/MP4 protocol. It is
appropriate for a controlled deployment and makes the orchestration testable.
The ASGI edge permits clear-text traffic only when every connection endpoint is
loopback, or when the operator explicitly asserts an all-private RFC1918/ULA
development path. All other connections require a native secure scheme; only
Uvicorn's exact trusted-proxy allowlist may normalize forwarded HTTPS/WSS.
For internet-scale or lossy-network deployment, retain the consent, adapter and
state-machine boundaries but replace the media edge with WebRTC (Opus plus
VP8/H.264) and TURN. WebSocket backpressure alone does not provide jitter
buffering, congestion control or NAT traversal equivalent to WebRTC.
