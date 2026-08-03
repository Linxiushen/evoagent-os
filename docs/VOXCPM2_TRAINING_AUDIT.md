# VoxCPM2 training feasibility audit (2026-08-03)

This audit answers whether the pinned official VoxCPM2 LoRA path can train on
the current 4 GiB Quadro P1000 without sending authorized voice material to a
third party. It covers source revision
`616d3d3e630a9c96c2853250eef91b0f39dcd5fa` and model revision
`bffb3df5a29440629464e5e839f4d214c8714c3d`.

## Upstream evidence

The pinned official repository links to the
[fine-tuning guide](https://voxcpm.readthedocs.io/en/latest/finetuning/finetune.html).
ReadTheDocs identifies its source as `a710128/voxcpm-docs`; the page audited on
2026-08-03 is bound to docs commit
`1935db1cd9826a685cfbe22e52ffb3aa0183a47c` and `finetune.rst` Git blob
`837ac06d1b6cd7bf7981d63edfcf4c0c3f000cf6`. It estimates:

| Model and mode | Approximate VRAM |
|---|---:|
| VoxCPM2 LoRA | 20 GB |
| VoxCPM2 full fine-tuning | 40 GB |

The guide associates that estimate with `batch_size=16` and
`max_batch_tokens=8192`. Its OOM guidance is limited to reducing batch size or
token length, increasing gradient accumulation, or choosing LoRA. The pinned
official config uses a physical batch size of 2, eight accumulation steps and
the same 8192-token ceiling:

- `runtime/upstream/VoxCPM/conf/voxcpm_v2/voxcpm_finetune_lora.yaml:6`
- `runtime/upstream/VoxCPM/conf/voxcpm_v2/voxcpm_finetune_lora.yaml:7`
- `runtime/upstream/VoxCPM/conf/voxcpm_v2/voxcpm_finetune_lora.yaml:17`

## Code-path findings

The pinned implementation does not contain a hidden low-memory training mode:

- `src/voxcpm/training/accelerator.py:84-92` moves the complete model to one
  device and optionally wraps it in DDP.
- `src/voxcpm/training/accelerator.py:117-121` provides only CUDA AMP and a
  gradient scaler.
- `scripts/train_voxcpm_finetune.py:293` hard-codes bfloat16 autocast.
- `src/voxcpm/model/voxcpm2.py:1166-1177` casts to the configured low precision
  only for inference. In training mode it freezes the AudioVAE and non-LoRA
  parameters but does not quantize or offload them.
- Repository search found no bitsandbytes, 4/8-bit loading, CPU offload,
  DeepSpeed, FSDP/ZeRO or `torch.utils.checkpoint` training integration.
  `ScalarQuantizationLayer` is part of the acoustic representation and is not
  weight quantization.
- Multi-GPU initialization uses NCCL at
  `src/voxcpm/training/accelerator.py:26-27`. Single-GPU Windows is not
  explicitly blocked upstream, but it is neither documented as a training
  target nor admitted by the reproducible worker. The qualified path is Linux
  (native or WSL2).

## Memory lower bound

A metadata-only inspection of the locked safetensors file found 577 tensors,
2,290,004,544 parameters, all stored as BF16. Their serialized payload is
4,580,009,088 bytes; the file is 4,580,080,592 bytes.

The training loader builds ordinary FP32 PyTorch modules and does not apply the
inference-only BF16 cast. Loading a BF16 state dict into such a module converts
it to the module's FP32 dtype. The base weights therefore require approximately
9,160,018,176 bytes (8.53 GiB) before accounting for:

- the 376,951,122-byte AudioVAE checkpoint, explicitly kept in FP32;
- static caches, forward activations and temporary tensors;
- gradients and optimizer state for the LoRA parameters.

For the official rank-32 targets, safetensors shape inspection gives 18,087,936
LoRA parameters across 192 attention projections. FP32 parameters, gradients
and two AdamW moments alone add about 289,406,976 bytes (276 MiB). The official
20 GB estimate is therefore consistent with the pinned implementation.

Lowering the physical batch size to 1 can reduce activations but cannot cross
the 8.53 GiB base-weight floor. It cannot make this path fit in 4 GiB.

Even a hypothetical unsupported FP16 rewrite would not fit. The base weights
alone would occupy about 4.265 GiB, and the FP32 AudioVAE adds about 0.351 GiB,
for a static 4.617 GiB lower bound before LoRA weights, gradients, optimizer
state, activations, CUDA context or allocator overhead.

## Current host verdict

The audited host reports:

- Windows 10, 64-bit;
- Quadro P1000, 4096 MiB VRAM, compute capability 6.1;
- approximately 48 GiB system RAM;
- the isolated VoxCPM environment currently uses `torch==2.7.1+cpu`, with no
  CUDA runtime exposed to PyTorch.

The host fails three independent requirements: capacity, native bfloat16
(Ampere starts at compute capability 8.0), and the qualified Linux runtime.
No formal training was started because failure is deterministic before the
first optimization step.

A restricted local CPU zero-shot baseline was generated separately in 247.7
seconds. Its private run manifest records a 48 kHz mono 4.8-second WAV, no
clipped samples, a peak working set of 11.637 GiB, and
`training_performed: false`. The output digest and biometric media remain in
the ignored private workspace. It remains pending human listening and quality
review, so it is feasibility evidence rather than a promoted voice candidate.

## Private no-upload recommendation

1. Use the completed local model download and CPU zero-shot clone to establish
   a disclosed baseline. This is inference, not LoRA training.
2. Run formal LoRA only on an owned Linux or WSL2 workstation with an
   Ampere-or-newer NVIDIA GPU providing at least 24 GiB total and 22 GiB free
   VRAM. Keep the private dataset, authorization record and checkpoints on
   encrypted local storage.
3. Do not upload biometric material to a rented or hosted GPU unless the
   authorization record explicitly adds that processor and transfer scope.
4. Treat QLoRA, CPU offload and activation checkpointing as a separate upstream
   engineering project. None exists in the pinned implementation, and adding
   it would require new numerical, quality and provenance qualification; it is
   not a credible immediate route to 4 GiB training.
