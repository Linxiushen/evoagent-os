# Verified model and repository map (2026-08-01)

| Role | Exact source | Audited revision | License | Integration |
|---|---|---|---|---|
| VAD | [snakers4/silero-vad v5.1.2](https://github.com/snakers4/silero-vad/tree/v5.1.2) | `6478567951ae5c9979ad7b234185b5515f4be7a1` plus adapter-pinned ONNX SHA-256 | MIT | CPU, 16 kHz, 512-sample frames |
| ASR | [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | `7278e1e70fe206f11671096ffdd38061171dd6e5` | Apache-2.0 | `qwen-asr` or vLLM |
| LLM | [DeepSeek API](https://api-docs.deepseek.com/) model `deepseek-v4-flash` | Hosted identifier; no immutable weight revision is exposed | hosted API terms | OpenAI-compatible SSE |
| TTS | [openbmb/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) | `bffb3df5a29440629464e5e839f4d214c8714c3d` | Apache-2.0 | local `generate_streaming()` or vLLM-Omni |
| Persona method | [alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill) | `27642f5bfed2dc1bbf8ee59a2c1ee602a626bbd7` | MIT | offline `SKILL.md`, never the hot path |
| Avatar | [Soul-AILab/SoulX-FlashHead-1_3B](https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B) | weights `59119b6c681230c3eeee157e224ae1941746711e`; code `9bc03de06bb0de82cd6bc477804512ae06144bf2` | Apache-2.0 | official Lite streaming code |

The revision column is the repository state audited on the date in this
document, not a promise that upstream `main` remains equivalent. Production
downloads must use the full revision, record every downloaded file digest and
run offline from the approved snapshot. SoulX also uses
`facebook/wav2vec2-base-960h` revision
`22aad52d435eb6dbaf354bdad9b0da84ce7d6156`. See
[Supply-chain policy](SUPPLY_CHAIN.md).

Corrections to common names:

- There is no official Silero Hugging Face repository named
  `silero-vad-v5`; Hugging Face v5 conversions are third-party. EchoWeave pins
  the upstream Git tag `v5.1.2`.
- SoulX uses `1_3B` in the model repository name, not `1.3b`.
- Nuwa is a research/distillation skill, not a neural checkpoint or realtime
  inference server.

## Hardware reality

- Silero is CPU-friendly.
- Qwen's BF16 weights alone are about 4 GB; vLLM needs additional memory.
- VoxCPM2 inference officially reports about 8 GB VRAM and 48 kHz output.
  This is not a training requirement: the official fine-tuning guide estimates
  about 20 GB for VoxCPM2 LoRA and about 40 GB for full fine-tuning.
- SoulX Lite's published realtime target is an RTX 4090-class 24 GB GPU. Its
  model repository is much larger than 4 GB and also needs wav2vec2.

A 4 GB GPU can run the EchoWeave gateway and safety/demo path, but cannot host
all three neural workers. The practical full local topology is one GPU for
ASR/TTS and a dedicated 24 GB GPU for SoulX Lite.

The pinned VoxCPM2 LoRA implementation has no CPU offload, FSDP/ZeRO,
activation checkpointing or 4/8-bit weight-training path. Its custom
accelerator moves the complete model to one device and uses bfloat16 autocast.
For private, no-upload training, use an owned Linux or WSL2 host with an
Ampere-or-newer 24 GB NVIDIA GPU. Keep zero-shot CPU cloning as the local
fallback when that hardware is unavailable; do not treat it as a completed
LoRA run.
