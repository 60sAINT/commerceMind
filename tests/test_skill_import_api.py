"""Skills 导入接口：真实临时目录 + 原子替换与回滚。"""
from pathlib import Path

import httpx
import pytest

from api.main import create_app
from core.config import Settings, SkillSettings

SKILL_MD = """---
name: 退款政策
description: 退款时效与异常账单处理
keywords: 退款,账单
version: 1
---

# 退款政策

先确认订单号，再核对退款时效。
"""

RESOURCE_MD = "退款一般 3-5 个工作日到账。"


def make_app(root: Path, **kwargs):
    options = {"enabled": True, "debug": True, **kwargs}
    return create_app(Settings(api_key="test-key", skills=SkillSettings(root_dir=str(root), **options)))


async def post_import(app, **payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return await client.post("/skills/import", json=payload)


def package(skill_md: str = SKILL_MD, resource: str | None = RESOURCE_MD) -> list[dict[str, str]]:
    files = [{"path": "SKILL.md", "content": skill_md}]
    if resource is not None:
        files.append({"path": "references/refund-policy.md", "content": resource})
    return files


def write_existing(root: Path, skill_id: str = "billing", body: str = "旧正文") -> Path:
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: 旧技能\ndescription: 旧描述\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_import_writes_package_and_reloads(tmp_path):
    app = make_app(tmp_path)

    response = await post_import(app, id="refund_policy", files=package())

    assert response.status_code == 200
    data = response.json()
    assert data["skill"]["id"] == "refund_policy"
    assert data["skill"]["name"] == "退款政策"
    assert data["skill"]["path"] == "refund_policy/SKILL.md"
    assert data["report"]["added"] == ["refund_policy"]
    assert data["report"]["version"] == 2

    assert (tmp_path / "refund_policy" / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    assert (tmp_path / "refund_policy" / "references" / "refund-policy.md").read_text(
        encoding="utf-8"
    ) == RESOURCE_MD

    # 目录已刷新，正文与资源都能按需读取。
    manager = app.state.skill_manager
    assert manager.get("refund_policy") is not None
    assert manager.load_skill("refund_policy").body.startswith("# 退款政策")
    resources = manager.list_skill_resources("refund_policy")
    assert [item.relative_path for item in resources] == ["references/refund-policy.md"]


@pytest.mark.asyncio
async def test_import_accepts_lowercase_manifest_name(tmp_path):
    app = make_app(tmp_path)
    files = [{"path": "skill.md", "content": SKILL_MD}]

    response = await post_import(app, id="refund_policy", files=files)

    assert response.status_code == 200
    assert (tmp_path / "refund_policy" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_import_requires_manifest(tmp_path):
    app = make_app(tmp_path)

    response = await post_import(
        app,
        id="refund_policy",
        files=[{"path": "references/a.md", "content": "x"}],
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_manifest_missing"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_id", ["Refund", "../escape", "a" * 65, "-leading"])
async def test_import_rejects_invalid_id(tmp_path, skill_id):
    app = make_app(tmp_path)

    response = await post_import(app, id=skill_id, files=package())

    assert response.status_code == 422
    if len(skill_id) <= 64:
        assert response.json()["error_code"] == "skill_id_invalid"
    else:
        assert response.json()["detail"]  # Pydantic 先校验字段长度。


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "references/../../etc/passwd",
        "references\\a.md",
        "references/.hidden.md",
        "other/a.md",
        "references",
        "references//a.md",
        "references/./a.md",
        "references/a.md ",
    ],
)
async def test_import_rejects_unsafe_paths(tmp_path, path):
    app = make_app(tmp_path)

    response = await post_import(
        app,
        id="refund_policy",
        files=[{"path": "SKILL.md", "content": SKILL_MD}, {"path": path, "content": "x"}],
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_path_invalid"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
async def test_import_rejects_duplicate_paths(tmp_path):
    app = make_app(tmp_path)

    response = await post_import(
        app,
        id="refund_policy",
        files=[
            {"path": "SKILL.md", "content": SKILL_MD},
            {"path": "SKILL.md", "content": SKILL_MD},
        ],
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_path_invalid"


@pytest.mark.asyncio
async def test_import_rejects_file_directory_collision(tmp_path):
    app = make_app(tmp_path)
    response = await post_import(app, id="refund_policy", files=package() + [
        {"path": "templates/reply", "content": "text"},
        {"path": "templates/reply/a.md", "content": "text"},
    ])
    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_path_invalid"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
async def test_import_rejects_missing_description(tmp_path):
    app = make_app(tmp_path)
    bad = "---\nname: 只有名称\n---\n\n正文\n"

    response = await post_import(app, id="refund_policy", files=[{"path": "SKILL.md", "content": bad}])

    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_front_matter_invalid"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
async def test_import_rejects_empty_body(tmp_path):
    app = make_app(tmp_path)
    empty = "---\nname: 空正文\ndescription: 没有正文\n---\n\n"

    response = await post_import(
        app,
        id="refund_policy",
        files=[{"path": "SKILL.md", "content": empty}],
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_body_empty"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
async def test_import_rejects_disabled_skill(tmp_path):
    app = make_app(tmp_path)
    disabled = "---\nname: 停用技能\ndescription: 停用描述\nenabled: false\n---\n\n正文\n"

    response = await post_import(
        app,
        id="refund_policy",
        files=[{"path": "SKILL.md", "content": disabled}],
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "skill_disabled"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
async def test_import_conflict_without_overwrite_keeps_original(tmp_path):
    app = make_app(tmp_path)
    original = write_existing(tmp_path)

    response = await post_import(app, id="billing", files=package())

    assert response.status_code == 409
    assert response.json()["error_code"] == "skill_exists"
    assert original.read_text(encoding="utf-8") == (
        "---\nname: 旧技能\ndescription: 旧描述\n---\n\n旧正文\n"
    )
    assert not (tmp_path / "billing" / "references").exists()


@pytest.mark.asyncio
async def test_import_overwrite_replaces_skill_and_drops_stale_resources(tmp_path):
    app = make_app(tmp_path)
    write_existing(tmp_path)
    stale = tmp_path / "billing" / "references"
    stale.mkdir(parents=True)
    (stale / "old.md").write_text("旧资源", encoding="utf-8")
    app.state.skill_manager.reload()

    response = await post_import(app, id="billing", files=package(), overwrite=True)

    assert response.status_code == 200
    assert response.json()["report"]["updated"] == ["billing"]
    assert (tmp_path / "billing" / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    assert not (stale / "old.md").exists()
    assert (stale / "refund-policy.md").read_text(encoding="utf-8") == RESOURCE_MD


@pytest.mark.asyncio
async def test_import_rejects_oversized_file(tmp_path):
    app = make_app(tmp_path, upload_max_file_bytes=200)

    response = await post_import(app, id="refund_policy", files=package(SKILL_MD + "x" * 201))

    assert response.status_code == 413
    assert response.json()["error_code"] == "skill_file_too_large"
    assert not (tmp_path / "refund_policy").exists()


@pytest.mark.asyncio
async def test_import_rejects_too_many_files(tmp_path):
    app = make_app(tmp_path, upload_max_files=1)

    response = await post_import(app, id="refund_policy", files=package())

    assert response.status_code == 413
    assert response.json()["error_code"] == "skill_package_too_large"


@pytest.mark.asyncio
async def test_import_route_hidden_without_debug(tmp_path):
    app = make_app(tmp_path, debug=False)

    response = await post_import(app, id="refund_policy", files=package())

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_import_rolls_back_when_reload_fails(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    original = write_existing(tmp_path)
    manager = app.state.skill_manager
    real_reload = manager.reload
    calls = {"count": 0}

    def flaky_reload():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("reload boom")
        return real_reload()

    monkeypatch.setattr(manager, "reload", flaky_reload)

    response = await post_import(app, id="billing", files=package(), overwrite=True)
    assert response.status_code == 500
    assert response.json()["error_code"] == "skill_install_failed"

    assert original.read_text(encoding="utf-8").endswith("旧正文\n")
    assert not (tmp_path / "billing" / "references").exists()
    assert manager.get("billing") is not None
    assert not list(tmp_path.glob(".staging-*"))
    assert not list(tmp_path.glob(".trash-*"))
