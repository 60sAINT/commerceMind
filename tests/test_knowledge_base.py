"""知识库文档导入、切片和版本替换测试。"""
from __future__ import annotations

import hashlib
import threading

import pytest

from core.config import KnowledgeBaseSettings
from tooling.knowledge_base import KnowledgeBase


class FakeCollection:
    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}
        self.upsert_calls: list[dict] = []
        self.query_calls: list[dict] = []

    def count(self):
        return len(self.rows)

    def get(self, *, where=None, include, limit=None):
        rows = [
            (doc_id, metadata)
            for doc_id, (_, metadata) in self.rows.items()
            if where is None or all(metadata.get(key) == value for key, value in where.items())
        ]
        if limit is not None:
            rows = rows[:limit]
        selected = {row[0]: (self.rows[row[0]][0], row[1]) for row in rows}
        result = {"ids": list(selected)}
        if "metadatas" in include:
            result["metadatas"] = [row[1] for row in selected.values()]
        if "documents" in include:
            result["documents"] = [row[0] for row in selected.values()]
        return result

    def upsert(self, *, ids, documents, metadatas):
        self.upsert_calls.append({"ids": ids, "documents": documents, "metadatas": metadatas})
        for doc_id, content, metadata in zip(ids, documents, metadatas):
            self.rows[doc_id] = (content, metadata)

    def delete(self, *, ids):
        for doc_id in ids:
            self.rows.pop(doc_id, None)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        selected = list(self.rows.items())[:kwargs["n_results"]]
        return {
            "documents": [[content for _, (content, _) in selected]],
            "metadatas": [[metadata for _, (_, metadata) in selected]],
            "distances": [[index / 10 for index in range(len(selected))]],
        }


def make_kb(**settings):
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.settings = KnowledgeBaseSettings(enabled=True, **settings)
    kb._write_lock = threading.RLock()
    kb._collection = FakeCollection()
    return kb


def test_import_is_idempotent_and_records_document_version_metadata():
    kb = make_kb(chunk_size=32, chunk_overlap=4)
    doc = {"document_id": "refund-v1", "title": "退款规则", "content": "七天内可申请退款。"}

    assert kb.add_documents([doc]) == 1
    first_row = next(iter(kb._collection.rows.items()))
    assert kb.add_documents([doc]) == 1
    assert len(kb._collection.rows) == 1
    assert len(kb._collection.upsert_calls) == 2
    assert first_row[0] == next(iter(kb._collection.rows))
    metadata = first_row[1][1]
    assert metadata["document_id"]
    assert metadata["version"] == hashlib.sha256("七天内可申请退款。".encode()).hexdigest()
    assert metadata["chunk_index"] == 0 and metadata["total_chunks"] == 1


def test_updated_document_replaces_old_chunks_and_preserves_other_documents():
    kb = make_kb(chunk_size=40, chunk_overlap=5)
    kb.add_documents([
        {"document_id": "a", "title": "同标题", "content": "旧规则。" * 40},
        {"document_id": "b", "title": "同标题", "content": "另一份规则。"},
    ])
    kb.add_documents([{"document_id": "a", "title": "更新标题", "content": "新规则。"}])

    rows = list(kb._collection.rows.values())
    assert len(rows) == 2
    assert {metadata["title"] for _, metadata in rows} == {"更新标题", "同标题"}
    assert {content for content, _ in rows} == {"新规则。", "另一份规则。"}


def test_title_identity_is_stable_and_explicit_ids_can_share_title():
    kb = make_kb()
    kb.add_documents([
        {"title": "政策", "content": "无 ID 版本"},
        {"document_id": "a", "title": "政策", "content": "甲"},
        {"document_id": "b", "title": "政策", "content": "乙"},
    ])
    kb.add_documents([{"title": "政策", "content": "无 ID 更新"}])

    assert len(kb._collection.rows) == 3
    assert {content for content, _ in kb._collection.rows.values()} == {"无 ID 更新", "甲", "乙"}


def test_chunking_respects_limit_and_overlaps_long_unpunctuated_text():
    kb = make_kb(chunk_size=20, chunk_overlap=4)

    chunks = kb._chunk_text("甲" * 53)

    assert all(0 < len(chunk) <= 20 for chunk in chunks)
    assert "".join(chunks[:2]).find("甲") >= 0
    assert len(chunks) > 2
    assert chunks[0][-4:] == chunks[1][:4]


def test_long_sentence_after_existing_chunk_consumes_text_with_overlap():
    kb = make_kb(chunk_size=20, chunk_overlap=4)

    chunks = kb._chunk_text("短句。" + "长" * 51)

    assert chunks
    assert all(0 < len(chunk) <= 20 for chunk in chunks)
    assert len(chunks) < 20


def test_chunking_prefers_sentence_boundaries_and_normalizes_paragraphs():
    kb = make_kb(chunk_size=12, chunk_overlap=0)

    chunks = kb._chunk_text("第一句。\r\n第二句！第三句。")

    assert all(len(chunk) <= 12 for chunk in chunks)
    assert "第一句。" in chunks[0]
    assert "第二句！" in "".join(chunks)


def test_import_batches_writes_using_configured_limit():
    kb = make_kb(chunk_size=10, chunk_overlap=0, write_batch_size=2)

    count = kb.add_documents([{"title": "长文", "content": "甲" * 35}])

    assert count == 3
    assert [len(call["ids"]) for call in kb._collection.upsert_calls] == [2, 1]


@pytest.mark.parametrize("document", [
    {"title": "", "content": "正文"},
    {"title": "标题", "content": " "},
    {"title": "标题", "content": 123},
    {"title": "标题", "content": "正文", "document_id": " "},
])
def test_import_rejects_invalid_documents(document):
    kb = make_kb()

    with pytest.raises(ValueError):
        kb.add_documents([document])


def test_chunk_configuration_validates_overlap():
    with pytest.raises(ValueError, match="OVERLAP"):
        KnowledgeBaseSettings(chunk_size=10, chunk_overlap=10)


def test_search_validates_query_and_top_k_and_maps_similarity_results():
    kb = make_kb()
    kb.add_documents([{"document_id": "a", "title": "退款", "content": "退款规则"}])

    result = kb.search("  怎么退款  ", top_k=4)

    assert result[0]["score"] == 1.0
    assert result[0]["document_id"] == next(iter(kb._collection.rows.values()))[1]["document_id"]
    assert kb._collection.query_calls[0]["query_texts"] == ["怎么退款"]
    with pytest.raises(ValueError, match="top_k"):
        kb.search("退款", top_k=True)
    with pytest.raises(ValueError, match="query"):
        kb.search(" ")


def test_search_empty_collection_returns_empty_without_query():
    kb = make_kb()

    assert kb.search("查询") == []
    assert kb._collection.query_calls == []


def test_document_list_detail_stats_and_delete_operate_on_documents():
    kb = make_kb(chunk_size=8, chunk_overlap=0)
    kb.add_documents([
        {"document_id": "b", "title": "乙文档", "content": "乙内容较长会分片扩展"},
        {"document_id": "a", "title": "甲文档", "content": "甲文档正文"},
    ])

    stats = kb.stats()
    docs = kb.list_documents(limit=1)
    detail = kb.get_document("b")
    assert stats == {"document_count": 2, "chunk_count": 3}
    assert len(docs) == 1 and docs[0]["document_id"] == "a"
    assert detail and detail["chunk_count"] == 2
    assert [chunk["chunk_index"] for chunk in detail["chunks"]] == [0, 1]
    assert kb.get_document("missing") is None
    assert kb.delete_document("b") is True
    assert kb.delete_document("b") is False
    assert kb.stats() == {"document_count": 1, "chunk_count": 1}


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 201}, {"offset": -1},
])
def test_list_documents_validates_pagination(kwargs):
    kb = make_kb()
    with pytest.raises(ValueError):
        kb.list_documents(**kwargs)
