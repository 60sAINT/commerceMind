"""Skills API：真实临时文件 + 替身模型，无外部网络。"""
from unittest.mock import Mock

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from api.main import _skill_system_prompt, create_app
from core.config import Settings, SkillSettings
from core.llm import SYSTEM_PROMPT, ChatResult
from memory.service import ConversationMemory
from memory.store import RedisMemoryStore

BODY = "仅显式选择后才能读取的正文_MARKER"
RESOURCE = "资源不应进入上下文_RESOURCE_MARKER"


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return ChatResult("收到")


def write_skill(root, skill_id="billing", body=BODY):
    directory = root / skill_id
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: 账单规范\ndescription: 退款相关流程\nkeywords: 退款\n---\n\n{body}",
        encoding="utf-8",
    )
    resources = directory / "references"
    resources.mkdir(exist_ok=True)
    (resources / "policy.md").write_text(RESOURCE, encoding="utf-8")


def make_app(root, **kwargs):
    app = create_app(Settings(
        api_key="test-key", skills=SkillSettings(root_dir=str(root), **kwargs),
    ))
    app.state.llm = FakeLLM()
    return app


async def send(app, method="POST", url="/chat", **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_catalog_is_always_injected_without_loading_body(tmp_path, monkeypatch):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    load = Mock(side_effect=AssertionError("未选择时不允许读正文"))
    monkeypatch.setattr(app.state.skill_manager, "load_skill", load)
    for ids in (None, []):
        response = await send(app, json={"message": "退款", "skill_ids": ids})
        assert response.status_code == 200
        system = app.state.llm.calls[-1][1]["system"]
        assert SYSTEM_PROMPT in system
        assert "billing" in system and "退款相关流程" in system
        assert BODY not in system and RESOURCE not in system
    load.assert_not_called()


@pytest.mark.asyncio
async def test_selected_body_only_and_deduplication(tmp_path):
    write_skill(tmp_path)
    write_skill(tmp_path, "other", "OTHER_BODY")
    app = make_app(tmp_path, enabled=True)
    response = await send(app, json={"message": "你好", "skill_ids": [" billing ", "billing"]})
    assert response.status_code == 200
    system = app.state.llm.calls[-1][1]["system"]
    assert system.count(BODY) == 1
    assert "OTHER_BODY" not in system and RESOURCE not in system


@pytest.mark.asyncio
async def test_unknown_skill_returns_clear_error_without_model_call(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    response = await send(app, json={"message": "你好", "skill_ids": ["missing"]})
    assert response.status_code == 404
    assert response.json()["error_code"] == "skill_not_found"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert app.state.llm.calls == []


@pytest.mark.asyncio
async def test_summary_does_not_load_or_expose_body(tmp_path, monkeypatch):
    write_skill(tmp_path)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("---\nnot yaml\n---", encoding="utf-8")
    app = make_app(tmp_path, enabled=True)
    monkeypatch.setattr(app.state.skill_manager, "load_skill", Mock(side_effect=AssertionError))
    response = await send(app, "GET", "/skills")
    summary = response.json()
    assert response.status_code == 200
    assert summary["count"] == 1 and summary["version"] == 1
    assert len(summary["errors"]) == 1
    assert summary["skills"][0]["id"] == "billing"
    assert BODY not in response.text and RESOURCE not in response.text
    assert '"body"' not in response.text and str(tmp_path) not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,debug", [(False, False), (False, True), (True, False), (True, True)])
async def test_reload_route_registration(tmp_path, enabled, debug):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=enabled, debug=debug)
    registered = any(route.path == "/skills/reload" for route in app.routes)
    assert registered is (enabled and debug)
    response = await send(app, url="/skills/reload")
    assert response.status_code == (200 if registered else 404)
    assert ("/skills/reload" in app.openapi()["paths"]) is registered


@pytest.mark.asyncio
async def test_reload_updates_catalog_and_next_prompt(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True, debug=True)
    await send(app, json={"message": "退款", "skill_ids": ["billing"]})
    write_skill(tmp_path, body="UPDATED_BODY")
    write_skill(tmp_path, "new")
    response = await send(app, url="/skills/reload")
    report = response.json()
    assert report["version"] == 2 and report["count"] == 2
    assert report["added"] == ["new"] and report["updated"] == ["billing"]
    assert BODY not in response.text and "UPDATED_BODY" not in response.text
    await send(app, json={"message": "退款", "skill_ids": ["billing"]})
    assert "UPDATED_BODY" in app.state.llm.calls[-1][1]["system"]


@pytest.mark.asyncio
async def test_disabled_skills_preserve_original_call(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=False, debug=True)
    assert app.state.skill_manager is None
    response = await send(app, json={"message": "你好", "skill_ids": ["missing"]})
    assert response.status_code == 200
    assert app.state.llm.calls == [("你好", {})]
    summary = (await send(app, "GET", "/skills")).json()
    assert summary["enabled"] is False and summary["count"] == 0


@pytest.mark.asyncio
async def test_selection_limit_and_invalid_ids(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True, max_selected_skills=1)
    for ids in (["billing", "other"], [""], ["  "], ["a" * 65], "billing"):
        response = await send(app, json={"message": "你好", "skill_ids": ids})
        assert response.status_code == 422
    assert app.state.llm.calls == []


@pytest.mark.asyncio
async def test_body_and_catalog_budgets(tmp_path):
    write_skill(tmp_path, body="X" * 100)
    app = make_app(tmp_path, enabled=True, catalog_max_chars=60, body_max_chars=10)
    response = await send(app, json={"message": "你好", "skill_ids": ["billing"]})
    assert response.status_code == 200
    system = app.state.llm.calls[-1][1]["system"]
    assert "X" * 10 in system and "X" * 11 not in system
    catalog_only = _skill_system_prompt(app.state.skill_manager, [], 60)
    assert len(catalog_only) <= len(SYSTEM_PROMPT) + 2 + 60


@pytest.mark.asyncio
async def test_empty_catalog_and_unavailable_body(tmp_path):
    app = make_app(tmp_path, enabled=True)
    assert (await send(app, json={"message": "你好"})).status_code == 200
    assert "空目录" in app.state.llm.calls[-1][1]["system"]
    write_skill(tmp_path, body="")
    app.state.skill_manager.reload()
    response = await send(app, json={"message": "你好", "skill_ids": ["billing"]})
    assert response.status_code == 503 and response.json()["error_code"] == "skill_unavailable"
    (tmp_path / "billing" / "SKILL.md").unlink()
    response = await send(app, json={"message": "你好", "skill_ids": ["billing"]})
    assert response.json()["error_code"] == "skill_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_fails", [False, True])
async def test_memory_path_preserves_skill_prompt(tmp_path, monkeypatch, read_fails):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    client = FakeRedis(decode_responses=True)
    memory = ConversationMemory(RedisMemoryStore(client, ttl_s=120))
    app.state.memory = memory
    if read_fails:
        async def fail(*args):
            raise RuntimeError("read failed")
        monkeypatch.setattr(memory, "read_context", fail)
    try:
        for message in ("第一轮", "第二轮"):
            response = await send(app, json={
                "message": message, "session_id": "s1", "skill_ids": ["billing"],
            })
            assert response.status_code == 200
            assert BODY in app.state.llm.calls[-1][1]["system"]
        if not read_fails:
            assert app.state.llm.calls[-1][1]["history"]
    finally:
        await client.aclose()


def test_skill_settings_env(monkeypatch):
    values = {
        "ENABLED": "true", "ROOT_DIR": "/tmp/custom-skills", "CATALOG_MAX_CHARS": "101",
        "BODY_MAX_CHARS": "102", "RESOURCE_MAX_CHARS": "103", "MAX_SELECTED_SKILLS": "2", "DEBUG": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv("COMMERCEMIND_SKILLS_" + key, value)
    assert Settings.from_env().skills == SkillSettings(
        enabled=True, root_dir="/tmp/custom-skills", catalog_max_chars=101,
        body_max_chars=102, resource_max_chars=103, max_selected_skills=2, debug=True,
    )
    monkeypatch.setenv("COMMERCEMIND_SKILLS_BODY_MAX_CHARS", "-1")
    assert Settings.from_env().skills.body_max_chars == 8192


@pytest.mark.parametrize("field", ["catalog_max_chars", "body_max_chars", "resource_max_chars", "max_selected_skills"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_skill_settings_validate_limits(field, value):
    with pytest.raises(ValueError):
        SkillSettings(**{field: value})


class SelectingLLM(FakeLLM):
    def __init__(self, selection='{"skill_ids":["billing"]}'):
        super().__init__()
        self.selection = selection

    async def chat(self, message, **kwargs):
        self.calls.append((message, kwargs))
        if kwargs.get("system", "").startswith("你是 Skill 选择器"):
            if isinstance(self.selection, Exception):
                raise self.selection
            return ChatResult(self.selection)
        return ChatResult("收到")


@pytest.mark.asyncio
async def test_automatic_description_selection_loads_only_selected_body(tmp_path, monkeypatch):
    write_skill(tmp_path)
    write_skill(tmp_path, "other", "OTHER_BODY")
    app = make_app(tmp_path, enabled=True)
    app.state.llm = SelectingLLM()
    original = app.state.skill_manager.load_skill
    loaded = []

    def load(skill_id):
        # 第一次模型调用只看到目录，正文必须在选择返回后才读。
        assert len(app.state.llm.calls) == 1
        loaded.append(skill_id)
        return original(skill_id)

    monkeypatch.setattr(app.state.skill_manager, "load_skill", load)
    response = await send(app, json={"message": "钱扣了两次，能帮我处理吗？"})
    assert response.status_code == 200
    assert loaded == ["billing"]
    selector_prompt = app.state.llm.calls[0][1]["system"]
    assert "退款相关流程" in selector_prompt
    assert BODY not in selector_prompt and RESOURCE not in selector_prompt
    assert "keywords" not in selector_prompt
    final_prompt = app.state.llm.calls[1][1]["system"]
    assert BODY in final_prompt and "OTHER_BODY" not in final_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [
    '{"skill_ids":[]}', 'not json', '{"skill_ids":["missing"]}',
    '{"skill_ids":"billing"}', '{"skill_ids":[null]}', '[]',
    '{"skill_ids":["billing","other"]}', RuntimeError("selector failed"),
])
async def test_failed_or_unmatched_selection_never_loads_bodies(tmp_path, monkeypatch, selection):
    write_skill(tmp_path)
    write_skill(tmp_path, "other")
    app = make_app(tmp_path, enabled=True, max_selected_skills=1)
    app.state.llm = SelectingLLM(selection)
    load = Mock(side_effect=AssertionError("不应读取正文"))
    monkeypatch.setattr(app.state.skill_manager, "load_skill", load)
    response = await send(app, json={"message": "今天天气如何"})
    assert response.status_code == 200
    assert len(app.state.llm.calls) == 2
    load.assert_not_called()


@pytest.mark.asyncio
async def test_selection_timeout_falls_back(tmp_path, monkeypatch):
    import asyncio
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    original = app.state.llm.chat

    async def slow(message, **kwargs):
        if kwargs.get("system", "").startswith("你是 Skill 选择器"):
            await asyncio.sleep(1)
        return await original(message, **kwargs)

    monkeypatch.setattr(app.state.llm, "chat", slow)
    monkeypatch.setattr("core.skill_selection.SELECTION_TIMEOUT_S", 0.001)
    response = await send(app, json={"message": "退款"})
    assert response.status_code == 200
    assert BODY not in app.state.llm.calls[-1][1]["system"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"message": "/billing 钱扣了两次"},
    {"message": "钱扣了两次", "skill_ids": ["billing"]},
    {"message": "/billing /billing 钱扣了两次", "skill_ids": ["billing"]},
])
async def test_manual_selection_skips_selector(tmp_path, payload):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    app.state.llm = SelectingLLM()
    response = await send(app, json=payload)
    assert response.status_code == 200
    assert len(app.state.llm.calls) == 1
    assert app.state.llm.calls[0][0] == "钱扣了两次"
    assert app.state.llm.calls[0][1]["system"].count(BODY) == 1


@pytest.mark.asyncio
async def test_explicit_empty_selection_disables_auto(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    app.state.llm = SelectingLLM()
    response = await send(app, json={"message": "退款", "skill_ids": []})
    assert response.status_code == 200
    assert len(app.state.llm.calls) == 1
    assert BODY not in app.state.llm.calls[0][1]["system"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message,code,status", [
    ("/missing 你好", "skill_not_found", 404),
    ("/billing", "skill_message_required", 422),
    ("/billing /other 你好", "too_many_skills", 422),
])
async def test_slash_errors(tmp_path, message, code, status):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True, max_selected_skills=1)
    response = await send(app, json={"message": message})
    assert response.status_code == status
    assert response.json()["error_code"] == code
    assert app.state.llm.calls == []


@pytest.mark.asyncio
async def test_disabled_does_not_parse_slash_or_select(tmp_path):
    app = make_app(tmp_path, enabled=False)
    response = await send(app, json={"message": "/billing 你好"})
    assert response.status_code == 200
    assert app.state.llm.calls == [("/billing 你好", {})]


@pytest.mark.asyncio
async def test_slash_with_memory_and_automatic_next_turn(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    app.state.llm = SelectingLLM()
    client = FakeRedis(decode_responses=True)
    app.state.memory = ConversationMemory(RedisMemoryStore(client, ttl_s=120))
    try:
        first = await send(app, json={"message": "/billing 扣了两次", "session_id": "s1"})
        assert first.status_code == 200
        assert app.state.llm.calls[-1][0] == "扣了两次"
        second = await send(app, json={"message": "退款怎么办", "session_id": "s1"})
        assert second.status_code == 200
        assert len(app.state.llm.calls) == 3
        assert BODY in app.state.llm.calls[-1][1]["system"]
        assert app.state.llm.calls[-1][1]["history"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_auto_selection_observes_reloaded_descriptions(tmp_path):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True, debug=True)
    app.state.llm = SelectingLLM()
    await send(app, json={"message": "退款"})
    path = tmp_path / "billing" / "SKILL.md"
    path.write_text(path.read_text().replace("退款相关流程", "新的账单争议描述"), encoding="utf-8")
    assert (await send(app, url="/skills/reload")).status_code == 200
    await send(app, json={"message": "账单争议"})
    assert "新的账单争议描述" in app.state.llm.calls[-2][1]["system"]
    assert BODY in app.state.llm.calls[-1][1]["system"]


@pytest.mark.asyncio
async def test_auto_selected_broken_body_has_safe_error(tmp_path):
    write_skill(tmp_path, body="")
    app = make_app(tmp_path, enabled=True)
    app.state.llm = SelectingLLM()
    response = await send(app, json={"message": "退款"})
    assert response.status_code == 503
    assert response.json()["error_code"] == "skill_unavailable"
    assert len(app.state.llm.calls) == 1


@pytest.mark.asyncio
async def test_auto_selection_during_memory_read_failure(tmp_path, monkeypatch):
    write_skill(tmp_path)
    app = make_app(tmp_path, enabled=True)
    app.state.llm = SelectingLLM()
    client = FakeRedis(decode_responses=True)
    memory = ConversationMemory(RedisMemoryStore(client, ttl_s=120))
    app.state.memory = memory
    async def fail(*args):
        raise RuntimeError("read failed")
    monkeypatch.setattr(memory, "read_context", fail)
    try:
        response = await send(app, json={"message": "退款", "session_id": "s1"})
        assert response.status_code == 200
        assert BODY in app.state.llm.calls[-1][1]["system"]
    finally:
        await client.aclose()
