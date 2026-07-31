# Deployment

## 1. Gateway / safe demo

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\echoweave serve
```

Open `http://127.0.0.1:8765`. This mode uses dependency-free endpointing, a
deterministic demo responder, browser speech synthesis and a synthetic face.
If `ECHOWEAVE_ACCESS_TOKEN` is set, open
`http://host:8765/#token=<value>`; the UI copies the fragment into memory and
immediately removes it from the address bar. Select only a persona ID listed
in `ECHOWEAVE_ALLOWED_PERSONAS`.

## 2. Silero v5

```powershell
.\.venv\Scripts\python -m pip install -e ".[silero-v5]"
$env:ECHOWEAVE_VAD_BACKEND = "silero_v5"
```

The adapter downloads the 2.3 MB ONNX file from the official upstream
`v5.1.2` Git tag, verifies its pinned SHA-256, and caches it under
`runtime/models/`. Set `SILERO_V5_MODEL_PATH` for an offline deployment.

## 3. Qwen3-ASR worker

Use the official isolated environment:

```bash
pip install -U "qwen-asr[vllm]"
qwen-asr-serve Qwen/Qwen3-ASR-1.7B \
  --gpu-memory-utilization 0.8 --host 0.0.0.0 --port 8001
```

Gateway:

```text
ECHOWEAVE_ASR_BACKEND=qwen_http
QWEN_ASR_BASE_URL=http://asr-worker:8001/v1
```

## 4. DeepSeek V4 Flash

After rotating any previously exposed key:

```text
ECHOWEAVE_LLM_BACKEND=deepseek
DEEPSEEK_API_KEY=<server secret>
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=disabled
```

Disabling thinking is recommended for ordinary realtime turns because it
reduces first-token latency.
Because this sends the reviewed persona profile and conversation to a hosted
processor, non-demo personas must include the
`third_party_model_processing` scope.

## 5. VoxCPM2 worker

For local Python integration, install `.[voxcpm]` and select
`voxcpm_local`. The official vLLM-Omni endpoint supports 48 kHz streaming PCM
and `ref_audio` data URIs:

```bash
vllm serve openbmb/VoxCPM2 --omni --port 8002
```

```text
ECHOWEAVE_TTS_BACKEND=voxcpm_http
VOXCPM_BASE_URL=http://tts-worker:8002/v1
VOXCPM_API_KEY=<the vLLM --api-key value, if configured>
```

Alternatively, run the included authenticated worker in an isolated VoxCPM
environment:

```bash
pip install -e ".[voxcpm]"
MODEL_WORKER_TOKEN='<long-random-secret>' \
uvicorn services.voxcpm_api:app --host 0.0.0.0 --port 8002
```

The gateway sends a consent-bound reference clip to the private worker as a
base64 data URI and never exposes it to the browser. Keep this worker on a
private network and set the same `MODEL_WORKER_TOKEN` on gateway and worker.

## 6. SoulX worker

Follow the official Linux setup:

```bash
git clone https://github.com/Soul-AILab/SoulX-FlashHead.git
conda create -n flashhead python=3.10 -y
conda activate flashhead
pip install torch==2.7.1 torchvision==0.22.1 \
  --index-url https://download.pytorch.org/whl/cu128
pip install -r SoulX-FlashHead/requirements.txt
pip install flash_attn==2.8.0.post2 --no-build-isolation
huggingface-cli download Soul-AILab/SoulX-FlashHead-1_3B \
  --local-dir /models/SoulX-FlashHead-1_3B
huggingface-cli download facebook/wav2vec2-base-960h \
  --local-dir /models/wav2vec2-base-960h
```

Start the provided bridge from an environment that also has FastAPI/Uvicorn:

```bash
SOULX_REPO_DIR=/opt/SoulX-FlashHead \
SOULX_CKPT_DIR=/models/SoulX-FlashHead-1_3B \
SOULX_WAV2VEC_DIR=/models/wav2vec2-base-960h \
MODEL_WORKER_TOKEN='<same-long-random-secret>' \
uvicorn services.soulx_api:app --host 0.0.0.0 --port 8003
```

Gateway:

```text
ECHOWEAVE_AVATAR_BACKEND=soulx_http
SOULX_BASE_URL=http://avatar-worker:8003
MODEL_WORKER_TOKEN=<same-long-random-secret>
```

The bridge validates uploads and official-model output paths, serializes GPU
inference, and burns `AI DIGITAL TWIN` into every MP4. It uses the official
Gradio streaming generator, whose segments are roughly three seconds; for
sub-second production video latency, replace only this worker boundary with a
raw-frame SoulX service while keeping the gateway protocol and authorization
checks.

## 7. Internet-facing gateway

The `echoweave` CLI refuses a non-loopback bind unless
`ECHOWEAVE_ACCESS_TOKEN` is set. The ASGI application independently rejects
tokenless WebSocket peers unless the client, local socket, and requested host
are all loopback, so overriding the bind through raw `uvicorn` does not expose
an unauthenticated realtime session. Also configure exact
`ECHOWEAVE_ALLOWED_ORIGINS`, HTTPS/WSS at a reverse proxy, and a private network
policy for all model workers.

The included gateway container installs the Silero v5 runtime and runs as a
non-root user with a read-only filesystem, dropped capabilities and a bounded
temporary filesystem. Its `.dockerignore` is an allowlist: `.env`, persona
biometrics, model caches, repository history and local virtual environments are
never sent in the Docker build context.

Compose requires a 32-byte-or-longer access token because the container binds
`0.0.0.0`; generate one before `docker compose up`. URLs using `127.0.0.1`
inside the gateway container point back to that container, not to the host.
Use private Compose service names for model workers, or
`host.docker.internal` on Docker Desktop when the workers run on the host.

Keep `ECHOWEAVE_CONSENT_STATE_PATH` on the persistent `runtime` volume and run
one gateway writer against that JSON state. For multi-worker or multi-replica
gateways, replace it with an external transactional consent/revocation
authority; filesystem snapshots of both the manifest and state can otherwise
roll back together.
