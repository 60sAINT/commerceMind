"""Skill 目录与 front matter 解析测试。"""
from __future__ import annotations

import os

import pytest

from core.skill_loader import SkillManager


def write_skill(
    root,
    skill_id: str,
    *,
    front_matter: str | None = None,
    body: str = "# 示例 Skill\n\n处理示例问题。",
) -> None:
    directory = root / skill_id
    directory.mkdir(parents=True)
    content = ""
    if front_matter is not None:
        content += f"---\n{front_matter.strip()}\n---\n\n"
    content += body
    (directory / "SKILL.md").write_text(content, encoding="utf-8")


def test_loads_catalog_metadata_without_body(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter=(
            "name: 账单退款处理规范\n"
            "description: 处理退款、扣款、发票和订阅问题。\n"
            "keywords: 退款, 扣款, 发票\n"
            "version: 2\n"
            "enabled: true"
        ),
        body="# 账单退款处理规范\n\n" + "正文内容" * 2000,
    )

    manager = SkillManager(tmp_path)
    catalog = manager.load()

    assert len(catalog) == 1
    skill = catalog.skills[0]
    assert skill.id == "billing_support"
    assert skill.name == "账单退款处理规范"
    assert skill.description == "处理退款、扣款、发票和订阅问题。"
    assert skill.keywords == ("退款", "扣款", "发票")
    assert skill.version == "2"
    assert skill.enabled is True
    assert skill.relative_path == "billing_support/SKILL.md"
    assert not hasattr(skill, "content")
    assert "正文内容" not in str(manager.summary())


def test_front_matter_supports_quotes_chinese_colon_and_bracket_list(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter=(
            'name: "账单退款处理规范"\n'
            'description: "退款：查询、到账与争议处理"\n'
            'keywords: [退款, "扣款", 发票]\n'
            'enabled: "true"'
        ),
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 1
    assert catalog.errors == ()
    skill = catalog.skills[0]
    assert skill.name == "账单退款处理规范"
    assert skill.description == "退款：查询、到账与争议处理"
    assert skill.keywords == ("退款", "扣款", "发票")


def test_disabled_skill_is_skipped_without_error(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单\ndescription: 账单问题\nenabled: false",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert catalog.errors == ()


def test_missing_front_matter_falls_back_to_heading_and_paragraph(tmp_path):
    write_skill(
        tmp_path,
        "general_customer_service",
        body="# 通用客服规范\n\n先确认用户诉求，再给出处理步骤。",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 1
    skill = catalog.skills[0]
    assert skill.name == "通用客服规范"
    assert skill.description == "先确认用户诉求，再给出处理步骤。"
    assert skill.keywords == ()
    assert skill.version == "1"


def test_missing_heading_falls_back_to_skill_id_for_name(tmp_path):
    write_skill(
        tmp_path,
        "technical_support",
        body="处理登录和接口报错。",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 1
    assert catalog.skills[0].name == "technical_support"
    assert catalog.skills[0].description == "处理登录和接口报错。"


def test_missing_description_reports_error(tmp_path):
    write_skill(
        tmp_path,
        "broken_skill",
        front_matter="name: 缺少描述的处理规范",
        body="# 缺少描述的处理规范\n",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert catalog.errors[0].path == "broken_skill/SKILL.md"
    assert "description" in catalog.errors[0].message


def test_unclosed_front_matter_reports_error(tmp_path):
    directory = tmp_path / "broken_skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: 未闭合\n", encoding="utf-8")

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "未闭合" in catalog.errors[0].message


def test_invalid_front_matter_line_reports_error(tmp_path):
    write_skill(
        tmp_path,
        "broken_skill",
        front_matter="name: 坏掉的规范\n这不是键值对",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "格式错误" in catalog.errors[0].message


def test_duplicate_front_matter_field_reports_error(tmp_path):
    write_skill(
        tmp_path,
        "broken_skill",
        front_matter="name: 第一个名字\nname: 第二个名字\ndescription: 描述",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "重复" in catalog.errors[0].message


def test_invalid_skill_id_reports_error(tmp_path):
    write_skill(
        tmp_path,
        "Bad Skill",
        front_matter="name: 大写和空格\ndescription: 非法目录名",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "Skill id" in catalog.errors[0].message


def test_hidden_skill_directory_is_ignored(tmp_path):
    write_skill(
        tmp_path,
        ".hidden",
        front_matter="name: 隐藏\n描述: 不应该被加载",
        body="# 隐藏\n\n不应该被加载。",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert catalog.errors == ()


def test_missing_root_returns_empty_catalog_without_error(tmp_path):
    manager = SkillManager(tmp_path / "not-exists")

    catalog = manager.load()

    assert len(catalog) == 0
    assert catalog.errors == ()
    assert manager.summary()["count"] == 0


def test_root_is_file_reports_error(tmp_path):
    root = tmp_path / "skills"
    root.write_text("not a directory", encoding="utf-8")

    catalog = SkillManager(root).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "不是目录" in catalog.errors[0].message


def test_symlink_skill_directory_is_rejected(tmp_path):
    external = tmp_path / "external"
    write_skill(
        external,
        "billing_support",
        front_matter="name: 外部 Skill\ndescription: 通过符号链接混入",
    )
    root = tmp_path / "skills"
    root.mkdir()
    try:
        os.symlink(external / "billing_support", root / "billing_support", target_is_directory=True)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")

    catalog = SkillManager(root).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "符号链接" in catalog.errors[0].message


def test_broken_skill_does_not_block_valid_skill(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    write_skill(
        tmp_path,
        "broken_skill",
        front_matter="name: 坏规范",
        body="# 坏规范\n",
    )

    catalog = SkillManager(tmp_path).load()

    assert [skill.id for skill in catalog] == ["billing_support"]
    assert len(catalog.errors) == 1
    assert catalog.errors[0].path == "broken_skill/SKILL.md"


def test_skills_are_sorted_by_id(tmp_path):
    write_skill(tmp_path, "z_skill", front_matter="name: Z\ndescription: Z 描述")
    write_skill(tmp_path, "a_skill", front_matter="name: A\ndescription: A 描述")

    catalog = SkillManager(tmp_path).load()

    assert [skill.id for skill in catalog] == ["a_skill", "z_skill"]


def test_summary_exposes_relative_path_but_not_absolute_path(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )

    manager = SkillManager(tmp_path)
    manager.load()
    summary = manager.summary()

    assert summary["count"] == 1
    assert summary["version"] == 1
    assert summary["loaded_at"]
    assert summary["errors"] == []
    item = summary["skills"][0]
    assert item["path"] == "billing_support/SKILL.md"
    assert str(tmp_path) not in str(summary)
    assert "content" not in item


def test_get_returns_descriptor_or_none(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    assert manager.get("billing_support") is not None
    assert manager.get("not_exists") is None


def test_load_increments_version(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)

    first = manager.load()
    second = manager.load()

    assert first.version == 1
    assert second.version == 2
    assert second.loaded_at


def test_bom_and_crlf_are_supported(tmp_path):
    directory = tmp_path / "billing_support"
    directory.mkdir()
    (directory / "SKILL.md").write_bytes(
        b"\xef\xbb\xbf---\r\n"
        b"name: BOM Skill\r\n"
        b"description: BOM description\r\n"
        b"---\r\n"
        b"# BOM Skill\r\n"
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 1
    assert catalog.skills[0].name == "BOM Skill"
    assert catalog.skills[0].description == "BOM description"


def test_invalid_enabled_value_reports_error(tmp_path):
    write_skill(
        tmp_path,
        "broken_skill",
        front_matter="name: 坏规范\ndescription: 描述\nenabled: maybe",
    )

    catalog = SkillManager(tmp_path).load()

    assert len(catalog) == 0
    assert len(catalog.errors) == 1
    assert "enabled" in catalog.errors[0].message
