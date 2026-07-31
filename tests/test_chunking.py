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
