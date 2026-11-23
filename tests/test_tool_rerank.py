"""T6 回归测试，使用进程内假模型验证执行和缓存。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from core.config import ToolSettings
from tooling import ToolContext, ToolManager, ToolSpec, build_reranker


class FakeLLM:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def chat(self, message, *, system):
        self.calls.append(json.loads(message))
        return SimpleNamespace(text=next(self.responses))


async def handler(params, context):
    return ["a", "b", "c", "d"]


def setup_tool(reranker, *, supports_rerank=True, query_field="keyword"):
    manager = ToolManager(reranker)
    manager.register(ToolSpec(
        "search", "查询", {"type": "object", "properties": {"keyword": {}}},
        handler, cache_ttl_s=30, supports_rerank=supports_rerank, rerank_query_field=query_field,
    ))

    async def execute(top_k=2, *, params=None, **kwargs):
        return await manager.execute(
            "search", {"keyword": "  包裹  "} if params is None else params,
            ToolContext("u", "s", "r"), rerank_top_k=top_k, **kwargs,
        )

    return manager, execute


@pytest.mark.asyncio
async def test_model_ranks_by_declared_query_and_caches_final_result_per_top_k():
    llm = FakeLLM("[3,3]", "[2,1,3]")
    settings = ToolSettings(rerank_enabled=True, rerank_top_k=2)
    _, execute = setup_tool(build_reranker(settings, llm))

    ranked = await execute(settings.rerank_top_k)
    hit = await execute(settings.rerank_top_k)
    other_size = await execute(3)
    raw = await execute(0)

    assert ranked.success and ranked.reranked and ranked.data == ["d", "a"]
    assert hit.cached and hit.reranked and hit.data == ranked.data
    assert other_size.reranked and other_size.data == ["c", "b", "d"]
    assert not raw.reranked and raw.data == ["a", "b", "c", "d"]
    assert [call["top_k"] for call in llm.calls] == [2, 3]
    assert llm.calls[0]["query"] == "包裹"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["oops", "[]", "[true]", "[4]"])
async def test_bad_model_order_uses_original_top_k_without_poisoning_cache(response):
    llm = FakeLLM(response, "[3,2]")
    manager, execute = setup_tool(build_reranker(ToolSettings(rerank_enabled=True), llm))

    original = await execute()
    assert original.success and original.usable and not original.reranked
    assert original.data == ["a", "b"]
    assert manager.cache_size == 0

    recovered = await execute()
    assert recovered.reranked and not recovered.cached and recovered.data == ["d", "c"]
    assert manager.get_stats()["search"]["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "unsupported", "no_query", "few_items"])
async def test_rerank_skips_ineligible_results(case):
    llm = FakeLLM()
    settings = ToolSettings(rerank_enabled=case != "disabled")
    _, execute = setup_tool(build_reranker(settings, llm), supports_rerank=case != "unsupported")
    result = await execute(4 if case == "few_items" else 2, params={} if case == "no_query" else None)
    assert result.success and not result.reranked and result.data == ["a", "b", "c", "d"]
    assert not llm.calls


@pytest.mark.asyncio
async def test_rerank_honors_phase_deadline_and_keeps_handler_success():
    class SlowReranker:
        async def rerank(self, query, items, top_k):
            await asyncio.Event().wait()

    manager, execute = setup_tool(SlowReranker())
    deadline = manager._clock() + 0.1
    result = await asyncio.wait_for(execute(deadline=deadline), timeout=1)
    assert result.success and result.attempts == 1 and not result.reranked
    assert result.data == ["a", "b"]
