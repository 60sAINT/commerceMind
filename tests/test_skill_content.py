"""真实业务 Skill 内容测试。

这些测试直接读取仓库里的 `skills/`，确认三个业务能力包能被目录层和按需
读取层正确加载，同时避免把详细参考和模板塞回 SKILL.md 正文。
"""
from __future__ import annotations

from pathlib import Path

from core.skill_loader import SkillManager

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"

EXPECTED_SKILLS = {
    "billing_support": {
        "references/refund-policy.md",
        "templates/refund-reply.md",
    },
    "general_customer_service": {
        "references/handoff-policy.md",
        "templates/general-reply.md",
    },
    "technical_support": {
        "references/error-codes.md",
        "templates/troubleshooting-reply.md",
    },
}

# 这些标记只出现在详细参考里，不应该出现在 catalog 或 SKILL.md 正文里。
REFERENCE_ONLY_MARKERS = {
    "billing_support": "退款到账时效明细",
    "general_customer_service": "转人工交接摘要清单",
    "technical_support": "错误码逐项解释",
}


def test_business_skills_load_without_errors():
    manager = SkillManager(SKILLS_ROOT)
    catalog = manager.load()

    assert catalog.errors == ()
    assert [skill.id for skill in catalog] == [
        "billing_support",
        "general_customer_service",
        "technical_support",
    ]
    assert all(skill.enabled for skill in catalog)
    assert all(skill.name.strip() for skill in catalog)
    assert all(skill.description.strip() for skill in catalog)
    assert all(skill.version == "1" for skill in catalog)


def test_business_skills_are_discovered_from_skill_md_only():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()

    for skill in manager.catalog:
        assert skill.relative_path == f"{skill.id}/SKILL.md"
        assert (SKILLS_ROOT / skill.relative_path).is_file()


def test_catalog_does_not_include_body_or_reference_content():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()
    summary = str(manager.summary())

    for marker in REFERENCE_ONLY_MARKERS.values():
        assert marker not in summary
    assert "## 处理步骤" not in summary
    assert "templates/" not in summary


def test_each_skill_loads_body_and_resource_inventory():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()

    for skill_id, expected_resources in EXPECTED_SKILLS.items():
        document = manager.load_skill(skill_id)

        assert document.descriptor.id == skill_id
        assert document.body.strip()
        assert document.truncated is False
        assert "## 处理步骤" in document.body

        resource_paths = {item.relative_path for item in document.resources}
        assert expected_resources <= resource_paths
        assert all(item.size_bytes > 0 for item in document.resources)
        assert all(
            path.startswith(("references/", "templates/", "scripts/"))
            for path in resource_paths
        )


def test_skill_body_is_lean_and_points_to_resources():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()

    for skill_id, expected_resources in EXPECTED_SKILLS.items():
        document = manager.load_skill(skill_id)

        # 正文只放操作步骤和资源指针，详细手册留在资源文件里。
        assert len(document.body) < 6000
        for resource_path in expected_resources:
            assert resource_path in document.body


def test_body_does_not_embed_detailed_reference_content():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()

    for skill_id, marker in REFERENCE_ONLY_MARKERS.items():
        document = manager.load_skill(skill_id)
        assert marker not in document.body


def test_referenced_resources_are_readable_text():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()

    for skill_id, expected_resources in EXPECTED_SKILLS.items():
        for resource_path in expected_resources:
            resource = manager.read_skill_resource(skill_id, resource_path)

            assert resource.skill_id == skill_id
            assert resource.relative_path == resource_path
            assert resource.content.strip()
            assert resource.truncated is False
            assert resource.size_bytes > 0


def test_skill_body_is_not_leaked_into_catalog_summary():
    manager = SkillManager(SKILLS_ROOT)
    manager.load()
    summary = str(manager.summary())

    for skill_id in EXPECTED_SKILLS:
        document = manager.load_skill(skill_id)
        assert document.body not in summary
