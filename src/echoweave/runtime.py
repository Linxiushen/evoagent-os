from __future__ import annotations

from dataclasses import dataclass

from echoweave.adapters.asr import DemoASR, Qwen3ASRHTTP, Qwen3ASRLocal
from echoweave.adapters.avatar import ClientLipSyncAvatar, SoulXHTTPAvatar
from echoweave.adapters.llm import DeepSeekV4Flash, DemoLLM
from echoweave.adapters.tts import BrowserTTS, VoxCPM2HTTP, VoxCPM2Local
from echoweave.adapters.vad import EnergyVAD, SileroV5VAD
from echoweave.config import Settings


@dataclass(slots=True)
class RuntimeAdapters:
    vad: object
    asr: object
    llm: object
    tts: object
    avatar: object


def build_runtime(settings: Settings) -> RuntimeAdapters:
    vad = SileroV5VAD() if settings.vad_backend == "silero_v5" else EnergyVAD()
    if settings.asr_backend == "qwen_local":
        asr = Qwen3ASRLocal(settings.qwen_model)
    elif settings.asr_backend == "qwen_http":
        asr = Qwen3ASRHTTP(
            settings.qwen_base_url,
            settings.qwen_model,
            settings.qwen_api_key,
        )
    else:
        asr = DemoASR()

    if settings.llm_backend == "deepseek":
        llm = DeepSeekV4Flash(
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.deepseek_model,
            settings.deepseek_thinking,
        )
    else:
        llm = DemoLLM()

    if settings.tts_backend == "voxcpm_local":
        tts = VoxCPM2Local(settings.voxcpm_model)
    elif settings.tts_backend == "voxcpm_http":
        tts = VoxCPM2HTTP(
            settings.voxcpm_base_url,
            settings.voxcpm_model,
            settings.voxcpm_api_key,
            settings.voxcpm_sample_rate,
            settings.model_worker_token,
        )
    else:
        tts = BrowserTTS()

    avatar = (
        SoulXHTTPAvatar(settings.soulx_base_url, settings.model_worker_token)
        if settings.avatar_backend == "soulx_http"
        else ClientLipSyncAvatar()
    )
    return RuntimeAdapters(vad, asr, llm, tts, avatar)
