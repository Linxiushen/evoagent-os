import pytest

from echoweave.chunking import SemanticChunker


def test_semantic_chunker_keeps_short_pause_together():
    chunker = SemanticChunker(min_chars=6, max_chars=20)
    assert chunker.push("你好，") == []
    assert chunker.push("欢迎使用。") == ["你好，欢迎使用。"]
    assert chunker.flush() == ""


def test_semantic_chunker_caps_long_text():
    chunker = SemanticChunker(min_chars=5, max_chars=10)
    chunks = chunker.push("这是一个没有任何标点而且非常长的文本")
    assert chunks
    assert len(chunks[0]) <= 10


def test_semantic_chunker_never_uses_distant_punctuation_past_hard_cap():
    chunker = SemanticChunker(min_chars=5, max_chars=10)
    chunks = chunker.push("a" * 15 + "。")
    chunks.append(chunker.flush())
    assert "".join(chunks) == "a" * 15 + "。"
    assert all(len(chunk) <= 10 for chunk in chunks)


@pytest.mark.parametrize(
    ("minimum", "maximum", "error"),
    [(0, 10, ValueError), (11, 10, ValueError), (True, 10, TypeError)],
)
def test_semantic_chunker_validates_limits(minimum, maximum, error):
    with pytest.raises(error):
        SemanticChunker(minimum, maximum)


def test_semantic_chunker_rejects_non_string_delta():
    with pytest.raises(TypeError):
        SemanticChunker().push(b"not text")
