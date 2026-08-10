from __future__ import annotations


class SemanticChunker:
    """Turns token deltas into TTS-sized phrases without breaking too early."""

    STRONG_ENDINGS = frozenset("。！？!?；;\n")
    SOFT_ENDINGS = frozenset("，,：:")

    def __init__(self, min_chars: int = 8, max_chars: int = 48) -> None:
        if isinstance(min_chars, bool) or not isinstance(min_chars, int):
            raise TypeError("min_chars must be an integer")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise TypeError("max_chars must be an integer")
        if min_chars < 1:
            raise ValueError("min_chars must be positive")
        if max_chars < min_chars:
            raise ValueError("max_chars must be greater than or equal to min_chars")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        if not isinstance(delta, str):
            raise TypeError("delta must be a string")
        if not delta:
            return []
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
        search_end = min(len(self._buffer), self.max_chars)
        if len(self._buffer) >= self.min_chars:
            for index, char in enumerate(self._buffer[:search_end], start=1):
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
