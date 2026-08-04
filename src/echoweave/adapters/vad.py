"""Voice activity adapters.

Portions of ``SileroV5VAD`` adapt the Silero v5.1.2 ONNX streaming state and
endpoint algorithm at commit 6478567951ae5c9979ad7b234185b5515f4be7a1.
Copyright (c) 2020-present Silero Team; see ``LICENSES/Silero-MIT.txt``.
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from echoweave.contracts import VADDecision


def _pcm16_values(pcm16: bytes) -> array.array[int]:
    usable = len(pcm16) - (len(pcm16) % 2)
    values = array.array("h")
    values.frombytes(pcm16[:usable])
    if sys.byteorder != "little":
        values.byteswap()
    return values


class EnergyVAD:
    """Dependency-free fallback VAD used by the runnable demo and tests."""

    def __init__(
        self,
        threshold: float = 0.018,
        start_ms: int = 96,
        end_silence_ms: int = 550,
    ) -> None:
        self.threshold = threshold
        self.start_ms = start_ms
        self.end_silence_ms = end_silence_ms
        self.reset()

    def reset(self) -> None:
        self._speaking = False
        self._hot_ms = 0.0
        self._silent_ms = 0.0

    async def process(self, pcm16: bytes, sample_rate: int) -> VADDecision:
        samples = _pcm16_values(pcm16)
        if not samples or sample_rate <= 0:
            return VADDecision(0.0)
        square_sum = sum(float(value) * value for value in samples)
        rms = math.sqrt(square_sum / len(samples)) / 32768.0
        probability = max(0.0, min(1.0, (rms - self.threshold * 0.4) * 35.0))
        frame_ms = len(samples) * 1000.0 / sample_rate
        is_hot = rms >= self.threshold

        started = False
        ended = False
        if not self._speaking:
            self._hot_ms = self._hot_ms + frame_ms if is_hot else 0.0
            if self._hot_ms >= self.start_ms:
                self._speaking = True
                self._silent_ms = 0.0
                started = True
        else:
            self._silent_ms = 0.0 if is_hot else self._silent_ms + frame_ms
            if self._silent_ms >= self.end_silence_ms:
                self._speaking = False
                self._hot_ms = 0.0
                self._silent_ms = 0.0
                ended = True
        return VADDecision(probability, started, ended)


class SileroV5VAD:
    """Pure ONNX Silero v5.1.2 with the upstream streaming state algorithm."""

    MODEL_URL = (
        "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/"
        "src/silero_vad/data/silero_vad.onnx"
    )
    MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"

    def __init__(
        self,
        threshold: float = 0.5,
        min_silence_duration_ms: int = 550,
        speech_pad_ms: int = 96,
        model_path: str | Path | None = None,
    ) -> None:
        try:
            import numpy as np
            import onnxruntime
        except ImportError as exc:
            raise RuntimeError(
                "Silero v5 is not installed. Install `.[silero-v5]`."
            ) from exc
        self._np = np
        configured_path = model_path or os.getenv("SILERO_V5_MODEL_PATH")
        self.model_path = (
            Path(configured_path)
            if configured_path
            else Path("runtime/models/silero_vad_v5.1.2.onnx")
        ).resolve()
        self._ensure_model()
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(self.model_path),
            providers=["CPUExecutionProvider"],
            sess_options=options,
        )
        self.threshold = threshold
        self.min_silence_samples = int(16_000 * min_silence_duration_ms / 1000)
        self.speech_pad_samples = int(16_000 * speech_pad_ms / 1000)
        self.reset()

    def reset(self) -> None:
        self._buffer = self._np.empty(0, dtype=self._np.float32)
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)
        self._context = self._np.zeros((1, 64), dtype=self._np.float32)
        self._triggered = False
        self._temp_end = 0
        self._current_sample = 0

    def _ensure_model(self) -> None:
        if self.model_path.is_file() and self._valid_model(self.model_path):
            return
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="silero-v5-", suffix=".onnx", delete=False
        ) as handle:
            temporary = Path(handle.name)
        try:
            urllib.request.urlretrieve(self.MODEL_URL, temporary)
            if not self._valid_model(temporary):
                raise RuntimeError("downloaded Silero v5 model failed SHA-256 check")
            temporary.replace(self.model_path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _valid_model(cls, path: Path) -> bool:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == cls.MODEL_SHA256

    def _infer(self, chunk: object) -> float:
        model_input = self._np.concatenate(
            (self._context, chunk.reshape(1, 512)), axis=1
        )
        output, self._state = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": self._np.array(16_000, dtype=self._np.int64),
            },
        )
        self._context = model_input[:, -64:]
        return float(output.reshape(-1)[0])

    async def process(self, pcm16: bytes, sample_rate: int) -> VADDecision:
        if sample_rate != 16_000:
            raise ValueError("Silero v5 adapter requires 16 kHz mono PCM")
        values = self._np.frombuffer(pcm16, dtype="<i2").astype(self._np.float32)
        values /= 32768.0
        self._buffer = self._np.concatenate((self._buffer, values))
        started = False
        ended = False
        probability = 0.0
        while len(self._buffer) >= 512:
            chunk = self._buffer[:512]
            self._buffer = self._buffer[512:]
            probability = self._infer(chunk)
            self._current_sample += 512
            if probability >= self.threshold and self._temp_end:
                self._temp_end = 0
            if probability >= self.threshold and not self._triggered:
                self._triggered = True
                started = True
            if probability < self.threshold - 0.15 and self._triggered:
                if not self._temp_end:
                    self._temp_end = self._current_sample
                if self._current_sample - self._temp_end >= self.min_silence_samples:
                    self._temp_end = 0
                    self._triggered = False
                    ended = True
        return VADDecision(probability, started, ended)
