import array
import math

from echoweave.adapters.vad import EnergyVAD


def _frame(amplitude: float, sample_rate: int = 16000, samples: int = 512) -> bytes:
    values = array.array(
        "h",
        (
            int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / sample_rate))
            for i in range(samples)
        ),
    )
    return values.tobytes()


async def test_energy_vad_detects_start_and_endpoint():
    vad = EnergyVAD(threshold=0.01, start_ms=64, end_silence_ms=160)
    events = []
    for _ in range(3):
        events.append(await vad.process(_frame(0.0), 16000))
    for _ in range(5):
        events.append(await vad.process(_frame(0.25), 16000))
    for _ in range(7):
        events.append(await vad.process(_frame(0.0), 16000))
    assert sum(event.speech_started for event in events) == 1
    assert sum(event.speech_ended for event in events) == 1
