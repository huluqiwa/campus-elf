"""嵌入层单测（M3 KB-03A）：BM25 sparse 权重/uint32 哈希/上下文前缀（纯函数，无网络）。"""

from app.kb.embedding import SparseVector, embed_input_text, version_sparse_weights


def test_sparse_weights_nonempty_and_indexed() -> None:
    vectors = version_sparse_weights(["知识库文档分块一", "知识库文档分块二"])
    assert len(vectors) == 2
    for vector in vectors:
        assert isinstance(vector, SparseVector)
        assert vector.indices and vector.values
        assert len(vector.indices) == len(vector.values)
        assert all(0 <= index <= 0xFFFFFFFF for index in vector.indices)
        assert all(value > 0 for value in vector.values)


def test_idf_common_tokens_weighted_lower() -> None:
    """两块共有的 token 权重应低于独有 token（IDF 语义；探针用 jieba 稳定切分的英文词）。"""

    vectors = version_sparse_weights(["common uniqueA", "common uniqueB"])
    first, second = vectors

    def weight_of(vector: SparseVector, probe: str) -> float:
        import hashlib

        slot = int.from_bytes(hashlib.sha256(probe.encode()).digest()[:4], "big") & 0xFFFFFFFF
        return dict(zip(vector.indices, vector.values, strict=True)).get(slot, 0.0)

    # 「common」在两块都出现（df=2），「uniqueA」只在第一块（df=1）
    assert weight_of(first, "uniqueA") > weight_of(first, "common")
    assert weight_of(second, "uniqueB") > weight_of(second, "common")


def test_hash_collision_merges_into_same_slot() -> None:
    """同块内哈希碰撞的 token 合并到同一 slot（值相加，不产生重复 index）。"""

    vectors = version_sparse_weights(["词甲 词乙"])
    vector = vectors[0]
    assert len(vector.indices) == len(set(vector.indices))


def test_embed_input_context_prefix() -> None:
    """dense 输入前缀 = 文档名 > 章节路径；无路径时只带文档名。"""

    assert embed_input_text(
        document_title="安全手册", section_path="第一章 > 1.1", chunk_text="正文"
    ) == "安全手册 > 第一章 > 1.1\n正文"
    assert embed_input_text(document_title="安全手册", section_path=None, chunk_text="正文") == (
        "安全手册\n正文"
    )


def test_empty_corpus_returns_empty() -> None:
    assert version_sparse_weights([]) == []
