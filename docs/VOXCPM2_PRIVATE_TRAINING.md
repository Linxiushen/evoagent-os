# Private VoxCPM2 training workflow

This is the shortest local, provenance-bound path from authorized 16 kHz WAV
sources to a VoxCPM2 LoRA job. Keep the authorization record, source media,
transcripts, review decisions, dataset and checkpoints on an encrypted private
volume outside this checkout. Do not commit any of them.

Nothing before manual review approves a clip. Silero ranges, Qwen text, OCR and
the review queue are machine evidence only. The exporter includes only clips
whose stored decision has every gate satisfied; rejected clips stay bound by
the decisions-file hash but never enter the dataset plan.

## 1. Local paths and authorization

```powershell
$Python = '.\.venv\Scripts\python.exe'
$PrivateRoot = 'D:\private\echoweave-voxcpm'
$Audio = "$PrivateRoot\source-01.wav"
$Authorization = "$PrivateRoot\authorization.json"
$Ffmpeg = (Get-Command ffmpeg).Source
$Silero = 'D:\models\silero_vad_v5.1.2.onnx'
$Qwen = 'D:\models\Qwen3-ASR-1.7B'
```

The authorization JSON must contain a `status` equal to either
`operator-verified` or `user-attested-local-collection-and-training`. The same
literal is supplied to the trusted exporter. Missing, unknown or conflicting
values fail closed. The later GPU-worker job applies its stricter identity,
authority, validity, scope and processor checks.

## 2. Silero ranges and Qwen transcript

Run this sequence once per source WAV, using globally unique clip prefixes:

```powershell
$Ranges = "$PrivateRoot\source-01.ranges.json"
$Transcript = "$PrivateRoot\source-01.qwen.json"

& $Python scripts\segment_silero_ranges.py `
  --audio $Audio --output $Ranges --clip-prefix source01 `
  --model-path $Silero

& $Python scripts\transcribe_qwen_ranges.py `
  --audio $Audio --ranges $Ranges --model-path $Qwen `
  --model-id Qwen/Qwen3-ASR-1.7B `
  --model-revision 7278e1e70fe206f11671096ffdd38061171dd6e5 `
  --output $Transcript --device cpu
```

Each candidate must be 3-30 seconds. Qwen output is still unverified text.

## 3. Subtitle evidence or audio-only evidence

For a source video with burned subtitles, align OCR to the Qwen output:

```powershell
$OcrEvidence = "$PrivateRoot\source-01.ocr.json"
& $Python scripts\ocr_burned_subtitles.py `
  --input "$PrivateRoot\source-01.mp4" `
  --segments $Transcript --output $OcrEvidence `
  --ffmpeg $Ffmpeg --samples-per-segment 3
```

For an audio-only WAV, do not claim that OCR found no subtitles. Create an
explicit, source-bound not-applicable record instead:

```powershell
$OcrEvidence = "$PrivateRoot\source-01.audio-only-evidence.json"
& $Python scripts\prepare_audio_only_review_evidence.py `
  --audio $Audio --transcript $Transcript --output $OcrEvidence
```

This records `ocr_performed=false` and binds the WAV and Qwen transcript by
path, size and SHA-256. Empty subtitle text means unavailable, not that
subtitles were proven absent.

## 4. Immutable queue and human review

```powershell
$QueueRoot = "$PrivateRoot\source-01-review-queue"
$Decisions = "$PrivateRoot\source-01-decisions.json"

& $Python scripts\prepare_voxcpm_review_queue.py `
  --ranges $Transcript --ocr-evidence $OcrEvidence --audio $Audio `
  --authorization $Authorization --output $QueueRoot --ffmpeg $Ffmpeg

& $Python scripts\review_voxcpm_queue.py `
  --queue "$QueueRoot\review-queue.json" --decisions $Decisions
```

The reviewer listens to every clip, corrects its transcript and explicitly
records target-speaker-only, transcript-verified, no-third-party-speech,
no-background-music and approval. Saving a rejected decision is valid; it will
not be exported. Keep decisions outside the immutable queue directory.

## 5. Trusted plan export and dataset build

Repeat `--review-pair` for every source. All queues must bind the same supplied
authorization record, and clip IDs must be globally unique.

```powershell
$Plan = "$PrivateRoot\dataset-plan.json"
& $Python scripts\export_voxcpm_dataset_plan.py `
  --review-pair "$PrivateRoot\source-01-review-queue\review-queue.json" "$PrivateRoot\source-01-decisions.json" `
  --review-pair "$PrivateRoot\source-02-review-queue\review-queue.json" "$PrivateRoot\source-02-decisions.json" `
  --authorization $Authorization `
  --authorization-status user-attested-local-collection-and-training `
  --dataset-id subject-v1 --subject-id subject --output $Plan

& $Python scripts\prepare_voxcpm_dataset.py `
  --plan $Plan --output "$PrivateRoot\dataset" --ffmpeg $Ffmpeg
```

The plan binds every queue and decisions file by absolute path, SHA-256 and
size. Dataset preparation reloads those files, revalidates reviewed WAVs,
timestamps, corrected text, approval subset, source audio and authorization,
then repeats the evidence check before publishing output.

Training audio in the `train` split must total 300-600 approved seconds. Do not
add noisy or unauthorized material merely to reach 300 seconds.

## 6. Owned GPU worker

Formal LoRA needs an owned Linux or WSL2 host with an Ampere-or-newer NVIDIA GPU
and about 24 GB VRAM; the audited 4 GB P1000 host cannot run it. Prepare the
private job from `gpu_worker_pack/examples/voxcpm2-lora.job.example.json`, bind
`dataset/dataset-manifest.json`, `dataset/train.jsonl`, every training WAV, the
authorization record, pinned model snapshot and LoRA config, then run:

```powershell
$env:ECHOWEAVE_RUN_ATTESTATION_KEY = '<at-least-32-random-bytes-from-your-secret-manager>'
echoweave-gpu-worker validate "$PrivateRoot\job\job.json"
echoweave-gpu-worker preflight "$PrivateRoot\job\job.json" --output "$PrivateRoot\job\preflight.json"
echoweave-gpu-worker run "$PrivateRoot\job\job.json" --record "$PrivateRoot\job\run-record.json"
```

Keep the same secret available to `finalize`; it verifies the HMAC on the
private run record before accepting evaluator metrics. Never put this key in a
job, command-line argument, repository file or provenance output.

Successful dataset preparation is not training completion, and a checkpoint is
not runtime promotion. Keep the independent automated and human acceptance
gates described in `gpu_worker_pack/README.md`.
