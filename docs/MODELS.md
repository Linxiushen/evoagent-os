# Verified model and repository map (2026-07-31)

| Role | Exact source | License | Integration |
|---|---|---|---|
| VAD | [snakers4/silero-vad v5.1.2](https://github.com/snakers4/silero-vad/tree/v5.1.2) | MIT | CPU, 16 kHz, 512-sample frames |
| ASR | [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Apache-2.0 | `qwen-asr` or vLLM |
| LLM | [DeepSeek API](https://api-docs.deepseek.com/) model `deepseek-v4-flash` | hosted API terms | OpenAI-compatible SSE |
| TTS | [openbmb/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) | Apache-2.0 | local `generate_streaming()` or vLLM-Omni |
| Persona method | [alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill) | MIT | offline `SKILL.md`, never the hot path |
| Avatar | [Soul-AILab/SoulX-FlashHead-1_3B](https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B) | Apache-2.0 | official Lite streaming code |

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
- VoxCPM2 officially reports about 8 GB VRAM and 48 kHz output.
- SoulX Lite's published realtime target is an RTX 4090-class 24 GB GPU. Its
  model repository is much larger than 4 GB and also needs wav2vec2.

A 4 GB GPU can run the EchoWeave gateway and safety/demo path, but cannot host
all three neural workers. The practical full local topology is one GPU for
ASR/TTS and a dedicated 24 GB GPU for SoulX Lite.
