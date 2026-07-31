from __future__ import annotations


class SemanticChunker:
    """Turns token deltas into TTS-sized phrases without breaking too early."""

    STRONG_ENDINGS = frozenset("。！？!?；;\n")
    SOFT_ENDINGS = frozenset("，,：:")

    def __init__(self, min_chars: int = 8, max_chars: int = 48) -> None:
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        self._buffer += delta
        ready: list[str] = []
        while self._buffer:
            split_at = self._find_split()
            if split_at is None:
                break
            phrase = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:]
            if phrase:
                ready.append(phrase)
        return ready

    def flush(self) -> str:
        phrase = self._buffer.strip()
        self._buffer = ""
        return phrase

    def _find_split(self) -> int | None:
        if len(self._buffer) >= self.min_chars:
            for index, char in enumerate(self._buffer, start=1):
                if char in self.STRONG_ENDINGS and index >= self.min_chars:
                    return index
        if len(self._buffer) >= self.max_chars:
            candidates = [
                index
                for index, char in enumerate(self._buffer[: self.max_chars], start=1)
                if char in self.SOFT_ENDINGS or char.isspace()
            ]
            return candidates[-1] if candidates else self.max_chars
        return None
