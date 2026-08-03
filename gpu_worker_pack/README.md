# Remote GPU qualification pack

This directory turns the existing VoxCPM2 and SoulX worker integrations into a
reproducible, fail-closed qualification workflow. It contains no model weights,
credentials, authorization evidence, voice recordings, face images, prompts,
transcripts, checkpoints or generated media. The nested `.gitignore` rejects
everything except the reviewed templates in this directory.

The pack is for offline model qualification. After a candidate passes, use the
authenticated `services.voxcpm_api` and `services.soulx_api` processes for the
realtime path. Do not expose an upstream model server directly to the Internet.

## Supported paths

| Mode | Training | Minimum admitted GPU | Intended result |
|---|---:|---:|---|
| `voxcpm2_zero_shot` | No | 8 GiB total, 7 GiB free | First voice-clone baseline |
| `voxcpm2_lora` | Optional | 24 GiB total, 22 GiB free | LoRA plus an A/B evaluation sample |
| `soulx_lite` | No | 24 GiB total, 22 GiB free | 512 px, 25 fps talking-head candidate |

VoxCPM2 zero-shot is the default. LoRA is justified only when the zero-shot
candidate fails a documented quality target and the LoRA result improves
speaker similarity without materially regressing content accuracy. SoulX has
no released identity fine-tuning path; one authorized PNG and driving WAV are
inference inputs, not training data.

The current workstation's 4 GiB Quadro P1000 cannot pass any of these remote
qualification profiles. Use an isolated Linux GPU host. A single RTX 4090
24 GiB is the published realtime target for SoulX Lite and is also sufficient
for the pack's VoxCPM2 LoRA admission floor.

The official VoxCPM fine-tuning guide estimates about 20 GB for VoxCPM2 LoRA
and about 40 GB for full fine-tuning. The 24 GiB total / 22 GiB free LoRA gate
adds admission headroom around that estimate. Reducing `batch_size` can reduce
activation memory, but it cannot make the pinned 2B training loader fit a 4 GiB
GPU. The audited path also requires Linux and native bfloat16 support (NVIDIA
compute capability 8.0 or newer); it does not claim a Windows, CPU-offload,
quantized or gradient-checkpointed training mode.

## Immutable upstream locks

The CLI accepts only these audited identities:

| Artifact | Revision |
|---|---|
| `openbmb/VoxCPM2` | `bffb3df5a29440629464e5e839f4d214c8714c3d` |
| `OpenBMB/VoxCPM` | `616d3d3e630a9c96c2853250eef91b0f39dcd5fa` |
| `Soul-AILab/SoulX-FlashHead-1_3B` | `59119b6c681230c3eeee157e224ae1941746711e` |
| `Soul-AILab/SoulX-FlashHead` | `9bc03de06bb0de82cd6bc477804512ae06144bf2` |
| `facebook/wav2vec2-base-960h` | `22aad52d435eb6dbaf354bdad9b0da84ce7d6156` |

Never replace a revision with `main`. Download into a model volume while the
preparation host is connected, malware-scan it, then run qualification with
Hugging Face and Transformers offline modes. The CLI sets offline environment
variables again before invoking upstream code.

## Host preparation

Use separate Python 3.10 environments for VoxCPM2 and SoulX because their CUDA
stacks need independent review.

VoxCPM2 requires the audited source checkout for both inference and LoRA. The
locked commit is 22 commits after the `2.0.3` tag and reports the SCM package
version `2.0.3.post22+g616d3d3e6`; the PyPI `2.0.3` wheel is not equivalent.

```bash
git clone https://github.com/OpenBMB/VoxCPM.git /opt/VoxCPM
git -C /opt/VoxCPM checkout --detach 616d3d3e630a9c96c2853250eef91b0f39dcd5fa
python3.10 -m venv /opt/venvs/voxcpm2
source /opt/venvs/voxcpm2/bin/activate
pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
pip install -r gpu_worker_pack/constraints/voxcpm2-direct.txt
pip install -e /opt/VoxCPM
pip install -e .
python -c "import importlib.metadata as m; assert m.version('voxcpm') == '2.0.3.post22+g616d3d3e6'"
```

For LoRA, bind `/opt/VoxCPM` as the job's audited source repository. Preflight
also verifies that the imported `voxcpm` module resolves inside that checkout.

SoulX Lite:

```bash
python3.10 -m venv /opt/venvs/soulx
source /opt/venvs/soulx/bin/activate
git clone https://github.com/Soul-AILab/SoulX-FlashHead.git /opt/SoulX-FlashHead
git -C /opt/SoulX-FlashHead checkout --detach 9bc03de06bb0de82cd6bc477804512ae06144bf2
pip install torch==2.7.1 torchvision==0.22.1 \
  --index-url https://download.pytorch.org/whl/cu128
pip install -r /opt/SoulX-FlashHead/requirements.txt
pip install flash-attn==2.8.0.post2 --no-build-isolation
pip install -e .
```

The preflight also requires a CUDA-enabled PyTorch build, a clean pinned source
checkout, and drawtext-capable FFmpeg. It permanently burns `AI DIGITAL TWIN`
into every SoulX qualification output.

## Model snapshots

Download each model by full revision. Then hash every downloaded file into a
manifest stored outside both the source repository and model directory:

```bash
echoweave-gpu-worker snapshot \
  --root /models/VoxCPM2 \
  --model-id openbmb/VoxCPM2 \
  --revision bffb3df5a29440629464e5e839f4d214c8714c3d \
  --output /private/echoweave/locks/VoxCPM2.snapshot.json
sha256sum /private/echoweave/locks/VoxCPM2.snapshot.json
```

Repeat for the SoulX and wav2vec model IDs. Put the resulting manifest digest
in the private job JSON. `validate`, `preflight` and `run` re-hash all model
files and reject missing, changed or unmanifested files.

## Private input contract

Start from one of `examples/*.job.example.json`, but create the real job under
an encrypted private mount. Every path must be absolute, local, outside the
EchoWeave source checkout and SHA-256-bound. URL inputs, symbolic-link inputs,
unknown fields, secret-like fields and key-shaped values are rejected.

All modes require an approved authorization record artifact. Its digest must
match `authorization.record_sha256`; subject identity and authorizer authority
must both be verified; authorization must be unexpired and explicitly name the
model processors and scope. This binds an operator decision but does not create
or infer legal permission.

The authorization artifact is not a free-form note. It must contain exactly the
job authorization fields (except `record_sha256`) plus
`schema_version: 1` and
`artifact_kind: echoweave_authorization_record`. The record and job copies of
`record_id`, status, both verification booleans, approval/expiry timestamps,
scopes and allowed processors must match. A generic approval record cannot be
reused with broader fields in the job manifest.

Media normalization is intentionally strict:

- VoxCPM reference and training clips: mono PCM16 WAV, 16 kHz.
- Voice reference: 3-30 seconds with an exact UTF-8 transcript. Prefer one
  clean 8-20 second, single-speaker segment with no music or cross-talk.
- LoRA: unique 3-30 second clips, exact transcripts, at least 300 seconds total.
  The official recommendation is about 5-10 minutes; start at 5 minutes and do
  not add noisy material merely to increase duration.
- SoulX: one authorized 512-4096 px PNG and a mono PCM16 16 kHz driving WAV.
  Use a centered, unobstructed, front-facing face with stable lighting.

The LoRA JSONL uses the official format. Resolve relative audio paths from the
directory containing `train.jsonl`, which is also the dataset root:

```json
{"audio":"clips/001.wav","text":"Exact transcript.","duration":30.0}
```

Keep `dataset-manifest.json` beside `train.jsonl` and add it to the job with
the `voxcpm_dataset_manifest` role. Each training clip must appear in the job
as a `voxcpm_train_audio` artifact using its absolute private mount path and
SHA-256. The validator resolves each JSONL path inside the dataset root,
rejects links and path escape, and cross-checks it against both the job
inventory and the dataset manifest's `derivations[].output_path`,
`output_sha256` and `output_size_bytes` fields.

Copy `configs/voxcpm2-lora.yaml` into the private mount and adjust its model,
train-manifest and checkpoint paths. TensorBoard is locked to `logs` inside the
checkpoint directory, validation data is disabled, and every optimization and
LoRA parameter is fixed by the validator. This prevents a nominal run with zero
steps/learning rate or unbound validation inputs and log outputs.

## Validate, preflight and run

Metadata-only validation is useful before private files reach the GPU host:

```bash
echoweave-gpu-worker validate /private/echoweave/job/job.json --metadata-only
```

Full validation hashes every input and model file. Preflight adds GPU, CUDA,
package, pinned Git checkout, output collision and free-memory checks:

```bash
echoweave-gpu-worker validate /private/echoweave/job/job.json
echoweave-gpu-worker preflight /private/echoweave/job/job.json \
  --output /private/echoweave/job/preflight.json
```

Run only after both pass:

```bash
export ECHOWEAVE_RUN_ATTESTATION_KEY='<at-least-32-random-bytes>'
echoweave-gpu-worker run /private/echoweave/job/job.json \
  --record /private/echoweave/job/run-record.json
```

Keep the attestation key outside the manifest and private media mount. `run`
fails closed without it and HMAC-signs the exact input binding, preflight and
output hashes. `finalize` requires the same key and rejects handwritten or
modified run records. The key is removed from child-process environments.

The run record is private because it contains local output paths. It records no
prompt or transcript content. The LoRA mode runs the pinned upstream training
script, requires a new checkpoint directory, then produces a deterministic
evaluation candidate using the saved `latest` LoRA checkpoint.

## Independent acceptance

Evaluators run after generation and write a small JSON file based on
`examples/metrics.example.json`. Every evaluator must have an immutable version
and artifact path plus SHA-256; finalization re-hashes that local evaluator
artifact and omits its private path from provenance. Numeric metrics must be
finite. The initial promotion gates are deliberately fixed in code so a job
cannot lower its own bar:

| Candidate | Required automated gates |
|---|---|
| Vox zero-shot | speaker cosine >=0.82; ASR CER <=0.10; p95 RTF <=0.60; clipping <=0.001; 48 kHz |
| Vox LoRA | speaker cosine >=0.84; zero-shot cosine delta >=0.02; CER regression <=0.02; p95 RTF <=0.70; clipping <=0.001; 48 kHz |
| SoulX Lite | face cosine >=0.75; SyncNet LSE-C >=5.0; >=25 fps; p95 RTF <=1.0; dropped frames <=0.001 |

Every profile also requires accountable human review and visible AI disclosure.
These thresholds are admission gates, not claims of universal perceptual
quality. Benchmark data, evaluator revision and the reviewed candidate must be
retained together so a threshold change is an auditable code change.

Finalize the sanitized provenance sidecar:

```bash
echoweave-gpu-worker finalize /private/echoweave/job/job.json \
  --record /private/echoweave/job/run-record.json \
  --metrics /private/echoweave/job/metrics.json \
  --output /private/echoweave/job/provenance.json
```

The provenance includes input/output hashes, model and source revisions,
authorization-record digest, safe hardware/runtime versions, evaluator locks
and every pass/fail decision. It deliberately omits local paths, prompts,
transcripts, biometric bytes and credentials. A failed candidate remains an
auditable result but must not be promoted to a realtime persona.

## Secret and media handling

No API key or worker signing key belongs in a job manifest, command line,
container image or provenance file. Inject runtime signing material through the
deployment secret manager only when starting the authenticated realtime
workers. Never commit or upload the private job mount, model cache, training
clips, checkpoints, generated media, run record or authorization evidence.

The Apache/MIT licenses of model code do not grant rights to a person's voice,
likeness, source videos, background music or third-party faces and voices.
VoxCPM also explicitly forbids impersonation, fraud and disinformation. Keep
the synthetic-media disclosure visible and honor revocation and deletion rules.
