"""知识库管理 API：替身知识库，无外部 Chroma 依赖。"""
from __future__ import annotations

import httpx
import pytest

from api.main import create_app
from core.config import KnowledgeBaseSettings, Settings
from tooling.knowledge_base import KnowledgeBase


class FakeKnowledgeBase:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.documents: dict[str, dict] = {}

    def _check(self):
        if self.fail:
            raise RuntimeError("sensitive backend detail C:\\private\\chroma")

    async def stats_async(self):
        self._check()
        return {
            "document_count": len(self.documents),
            "chunk_count": sum(item["chunk_count"] for item in self.documents.values()),
        }

    async def list_documents_async(self, *, limit=50, offset=0):
        self._check()
        values = sorted(self.documents.values(), key=lambda item: item["title"].casefold())
        return values[offset:offset + limit]

    async def add_documents_async(self, documents):
        self._check()
        imported = 0
        for document in documents:
            document_id = document.get("document_id") or document["title"]
            self.documents[document_id] = {
                "document_id": document_id,
                "title": document["title"],
                "version": "a" * 64,
                "imported_at": "2026-11-24T00:00:00+00:00",
                "chunk_count": 1,
                "chunks": [{"content": document["content"], "chunk_index": 0, "total_chunks": 1}],
            }
            imported += 1
        return imported

    async def get_document_async(self, document_id):
        self._check()
        return self.documents.get(document_id)

    async def delete_document_async(self, document_id):
        self._check()
        return self.documents.pop(document_id, None) is not None

    async def search_async(self, query, top_k=5):
        self._check()
        return [{"title": item["title"], "content": item["chunks"][0]["content"], "score": 0.9}
                for item in list(self.documents.values())[:top_k]]


async def request(app, method="GET", path="/knowledge/stats", **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def make_app(kb=None, *, enabled=True, required=False):
    return create_app(
        Settings(knowledge=KnowledgeBaseSettings(enabled=enabled, required=required)),
        knowledge=kb,
    )


@pytest.mark.asyncio
async def test_knowledge_api_import_list_detail_search_and_delete_flow():
    app = make_app(FakeKnowledgeBase())
    imported = await request(app, "POST", "/knowledge/documents", json={"documents": [
        {"document_id": "refund-v1", "title": "退款说明", "content": "实际政策以商家规则为准。"},
    ]})
    assert imported.status_code == 200
    assert imported.json()["imported_chunks"] == 1
    assert imported.json()["stats"] == {"document_count": 1, "chunk_count": 1}

    stats = await request(app)
    listing = await request(app, path="/knowledge/documents?limit=1&offset=0")
    detail = await request(app, path="/knowledge/documents/refund-v1")
    search = await request(app, "POST", "/knowledge/search", json={"query": "怎么退款", "top_k": 2})
    assert stats.json() == {"document_count": 1, "chunk_count": 1}
    assert listing.json()["total"] == 1 and listing.json()["items"][0]["document_id"] == "refund-v1"
    assert detail.json()["chunks"][0]["content"] == "实际政策以商家规则为准。"
    assert search.json()["results"][0]["score"] == 0.9

    deleted = await request(app, "DELETE", "/knowledge/documents/refund-v1")
    missing = await request(app, "GET", "/knowledge/documents/refund-v1")
    assert deleted.json() == {"deleted": True}
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "knowledge_document_not_found"


@pytest.mark.asyncio
async def test_knowledge_routes_return_safe_unavailable_when_disabled_or_uninitialized():
    for app in (make_app(enabled=False), make_app(enabled=True)):
        response = await request(app)
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "knowledge_unavailable"


@pytest.mark.asyncio
async def test_knowledge_dependency_errors_are_sanitized():
    app = make_app(FakeKnowledgeBase(fail=True))
    response = await request(app)

    assert response.status_code == 503
    assert "private" not in response.text
    assert "sensitive backend" not in response.text


@pytest.mark.asyncio
async def test_optional_initialization_failure_keeps_app_available(monkeypatch):
    def fail(cls, settings):
        raise RuntimeError("private endpoint details")

    monkeypatch.setattr(KnowledgeBase, "from_settings", classmethod(fail))
    app = make_app(enabled=True, required=False)

    async with app.router.lifespan_context(app):
        response = await request(app)

    assert response.status_code == 503
    assert "private endpoint" not in response.text


@pytest.mark.asyncio
async def test_required_initialization_failure_aborts_startup(monkeypatch):
    def fail(cls, settings):
        raise RuntimeError("private endpoint details")

    monkeypatch.setattr(KnowledgeBase, "from_settings", classmethod(fail))
    app = make_app(enabled=True, required=True)

    with pytest.raises(RuntimeError, match="KNOWLEDGE_REQUIRED"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"documents": []},
    {"documents": [{"title": " ", "content": "正文"}]},
    {"documents": [{"title": "标题", "content": ""}]},
])
async def test_import_validation_rejects_empty_or_invalid_documents(body):
    response = await request(make_app(FakeKnowledgeBase()), "POST", "/knowledge/documents", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_and_pagination_limits_are_validated():
    app = make_app(FakeKnowledgeBase())
    invalid_search = await request(app, "POST", "/knowledge/search", json={"query": " ", "top_k": 99})
    invalid_page = await request(app, path="/knowledge/documents?limit=201")
    too_many_docs = await request(app, "POST", "/knowledge/documents", json={
        "documents": [{"title": f"title-{i}", "content": "x"} for i in range(21)],
    })

    assert invalid_search.status_code == 422
    assert invalid_page.status_code == 422
    assert too_many_docs.status_code == 422


def test_knowledge_routes_are_documented_in_openapi():
    paths = make_app(FakeKnowledgeBase()).openapi()["paths"]

    assert "/knowledge/stats" in paths
    assert "/knowledge/documents" in paths
    assert "/knowledge/documents/{document_id}" in paths
    assert "/knowledge/search" in paths
