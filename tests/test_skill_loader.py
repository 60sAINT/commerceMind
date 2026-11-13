"""Skill 目录、正文与资源按需加载测试。"""
from __future__ import annotations

import os
import shutil
import threading

import pytest

import core.skill_loader as skill_loader
from core.skill_loader import (
    SkillBodyError,
    SkillManager,
    SkillNotFoundError,
    SkillResourceAccessError,
    SkillResourceNotFoundError,
)


def write_skill(
    root,
    skill_id: str,
    *,
    front_matter: str | None = None,
    body: str = "# 示例 Skill\n\n处理示例问题。",
) -> None:
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
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


def write_resource(root, skill_id: str, relative_path: str, content: str) -> None:
    path = root / skill_id / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_skill_reads_body_without_front_matter(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 账单规范\n\n1. 确认订单。\n2. 说明退款时效。",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    document = manager.load_skill("billing_support")

    assert document.body == "# 账单规范\n\n1. 确认订单。\n2. 说明退款时效。"
    assert document.truncated is False
    assert document.descriptor.id == "billing_support"


def test_load_skill_returns_resource_inventory_without_content(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 账单规范\n\n查看 references/refund-policy.md。",
    )
    write_resource(tmp_path, "billing_support", "references/refund-policy.md", "退款规则")
    write_resource(tmp_path, "billing_support", "templates/reply.md", "回复模板")
    write_resource(tmp_path, "billing_support", "scripts/check.py", "print('check')")
    write_resource(tmp_path, "billing_support", "private/secret.txt", "不应该出现")
    manager = SkillManager(tmp_path)
    manager.load()

    document = manager.load_skill("billing_support")

    assert [item.relative_path for item in document.resources] == [
        "references/refund-policy.md",
        "scripts/check.py",
        "templates/reply.md",
    ]
    assert all(item.size_bytes > 0 for item in document.resources)
    assert not hasattr(document.resources[0], "content")


def test_body_cache_avoids_repeated_reads(tmp_path, monkeypatch):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 账单规范\n\n处理步骤。",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    calls: list[str] = []
    original = skill_loader._read_text_limited

    def counting_read(path, max_chars):
        calls.append(str(path))
        return original(path, max_chars)

    monkeypatch.setattr(skill_loader, "_read_text_limited", counting_read)

    first = manager.load_skill("billing_support")
    second = manager.load_skill("billing_support")

    assert first.body == second.body
    assert len(calls) == 1


def test_body_cache_invalidates_when_file_changes(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 第一版\n\n旧内容。",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    assert "旧内容" in manager.load_skill("billing_support").body

    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 第二版\n\n新内容，长度已经变化。",
    )

    assert "新内容" in manager.load_skill("billing_support").body


def test_load_clears_body_cache(tmp_path, monkeypatch):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 账单规范\n\n处理步骤。",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    calls: list[str] = []
    original = skill_loader._read_text_limited

    def counting_read(path, max_chars):
        calls.append(str(path))
        return original(path, max_chars)

    monkeypatch.setattr(skill_loader, "_read_text_limited", counting_read)

    manager.load_skill("billing_support")
    manager.load()
    manager.load_skill("billing_support")

    assert len(calls) == 2


def test_body_is_truncated_at_limit(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 0123456789ABCDEF",
    )
    manager = SkillManager(tmp_path, max_skill_chars=10)
    manager.load()

    document = manager.load_skill("billing_support")

    assert document.truncated is True
    assert len(document.body) <= 10


def test_empty_body_after_front_matter_raises(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillBodyError):
        manager.load_skill("billing_support")


def test_unknown_skill_raises(tmp_path):
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillNotFoundError):
        manager.load_skill("not_exists")


def test_deleted_skill_file_raises(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    (tmp_path / "billing_support" / "SKILL.md").unlink()

    with pytest.raises(SkillNotFoundError):
        manager.load_skill("billing_support")


def test_list_resources_skips_hidden_and_disallowed_paths(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    write_resource(tmp_path, "billing_support", "references/rules.md", "规则")
    write_resource(tmp_path, "billing_support", "references/.hidden.md", "隐藏")
    write_resource(tmp_path, "billing_support", "references/nested/deep.md", "嵌套")
    write_resource(tmp_path, "billing_support", "private/secret.txt", "私有")
    write_resource(tmp_path, "billing_support", "notes.md", "根目录笔记")
    manager = SkillManager(tmp_path)
    manager.load()

    resources = manager.list_skill_resources("billing_support")

    assert [item.relative_path for item in resources] == [
        "references/nested/deep.md",
        "references/rules.md",
    ]


def test_read_resource_returns_content_and_size(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    write_resource(
        tmp_path,
        "billing_support",
        "references/refund-policy.md",
        "退款通常原路退回。",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    resource = manager.read_skill_resource(
        "billing_support", "references/refund-policy.md"
    )

    assert resource.content == "退款通常原路退回。"
    assert resource.size_bytes > 0
    assert resource.truncated is False
    assert resource.relative_path == "references/refund-policy.md"


def test_resource_is_truncated_at_limit(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    write_resource(tmp_path, "billing_support", "references/rules.md", "0123456789")
    manager = SkillManager(tmp_path, max_resource_chars=5)
    manager.load()

    resource = manager.read_skill_resource("billing_support", "references/rules.md")

    assert resource.truncated is True
    assert resource.content == "01234"


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "../secret.txt",
        "/etc/passwd",
        "references/../../secret.txt",
        "references/../templates/reply.md",
        "private/secret.txt",
        "SKILL.md",
        "references\\rules.md",
        "references/.hidden.md",
    ],
)
def test_invalid_resource_paths_are_rejected(tmp_path, relative_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    write_resource(tmp_path, "billing_support", "references/rules.md", "规则")
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillResourceAccessError):
        manager.read_skill_resource("billing_support", relative_path)


def test_missing_resource_raises(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillResourceNotFoundError):
        manager.read_skill_resource("billing_support", "references/not-exists.md")


def test_binary_resource_is_rejected(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    path = tmp_path / "billing_support" / "references" / "binary.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"abc\x00def")
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillResourceAccessError):
        manager.read_skill_resource("billing_support", "references/binary.bin")


def test_symlink_resource_is_rejected(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    external = tmp_path / "outside.md"
    external.write_text("外部内容", encoding="utf-8")
    link = tmp_path / "billing_support" / "references" / "link.md"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(external, link)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillResourceAccessError):
        manager.read_skill_resource("billing_support", "references/link.md")
    assert manager.list_skill_resources("billing_support") == ()


def test_resource_methods_raise_for_unknown_skill(tmp_path):
    manager = SkillManager(tmp_path)
    manager.load()

    with pytest.raises(SkillNotFoundError):
        manager.list_skill_resources("not_exists")
    with pytest.raises(SkillNotFoundError):
        manager.read_skill_resource("not_exists", "references/rules.md")


def test_reload_reports_added_updated_removed_and_unchanged(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 账单规范\n\n旧正文。",
    )
    write_skill(
        tmp_path,
        "technical_support",
        front_matter="name: 技术规范\ndescription: 处理技术问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    first_version = manager.catalog.version

    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款、扣款和发票问题",
        body="# 账单规范\n\n新正文，长度也变了。",
    )
    shutil.rmtree(tmp_path / "technical_support")
    write_skill(
        tmp_path,
        "general_customer_service",
        front_matter="name: 通用客服规范\ndescription: 处理通用咨询",
    )

    report = manager.reload()

    assert report.added == ("general_customer_service",)
    assert report.updated == ("billing_support",)
    assert report.removed == ("technical_support",)
    assert report.unchanged == ()
    assert report.version == first_version + 1
    assert report.count == 2
    assert report.errors == ()
    assert report.duration_ms >= 0
    assert report.has_changes is True
    assert manager.last_report is report
    assert [skill.id for skill in manager.catalog] == [
        "billing_support",
        "general_customer_service",
    ]
    assert report.to_dict()["errors"] == []
    assert report.to_dict()["count"] == 2


def test_reload_reports_unchanged_skill(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    report = manager.reload()

    assert report.added == ()
    assert report.updated == ()
    assert report.removed == ()
    assert report.unchanged == ("billing_support",)
    assert report.has_changes is False


def test_reload_with_broken_skill_keeps_other_valid_skill(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    write_skill(
        tmp_path,
        "technical_support",
        front_matter="name: 技术规范\ndescription: 处理技术问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    write_skill(
        tmp_path,
        "technical_support",
        front_matter="name: 技术规范",
        body="# 技术规范\n",
    )
    report = manager.reload()

    assert [skill.id for skill in manager.catalog] == ["billing_support"]
    assert report.removed == ("technical_support",)
    assert len(report.errors) == 1
    assert report.errors[0].path == "technical_support/SKILL.md"


def test_reload_clears_body_cache(tmp_path, monkeypatch):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
        body="# 账单规范\n\n处理步骤。",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    calls: list[str] = []
    original = skill_loader._read_text_limited

    def counting_read(path, max_chars):
        calls.append(str(path))
        return original(path, max_chars)

    monkeypatch.setattr(skill_loader, "_read_text_limited", counting_read)

    manager.load_skill("billing_support")
    manager.reload()
    manager.load_skill("billing_support")

    assert len(calls) == 2


def test_reload_removed_skill_is_not_found(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    manager.load_skill("billing_support")

    shutil.rmtree(tmp_path / "billing_support")
    report = manager.reload()

    assert report.removed == ("billing_support",)
    with pytest.raises(SkillNotFoundError):
        manager.load_skill("billing_support")


def test_reload_missing_root_removes_all_skills(tmp_path):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    shutil.rmtree(tmp_path)
    report = manager.reload()

    assert report.removed == ("billing_support",)
    assert report.errors == ()
    assert report.count == 0


def test_reload_uses_old_snapshot_until_swap(tmp_path, monkeypatch):
    write_skill(
        tmp_path,
        "billing_support",
        front_matter="name: 账单规范\ndescription: 处理退款问题",
    )
    manager = SkillManager(tmp_path)
    manager.load()

    scan_started = threading.Event()
    release_scan = threading.Event()
    original_scan = manager._scan_catalog

    def slow_scan(version):
        scan_started.set()
        release_scan.wait(2)
        return original_scan(version)

    monkeypatch.setattr(manager, "_scan_catalog", slow_scan)
    reports = []

    def run_reload():
        reports.append(manager.reload())

    thread = threading.Thread(target=run_reload)
    thread.start()
    assert scan_started.wait(2)

    # 扫描期间读取方仍然看到完整的旧快照，而不是空目录或半成品。
    assert [skill.id for skill in manager.catalog] == ["billing_support"]

    write_skill(
        tmp_path,
        "general_customer_service",
        front_matter="name: 通用客服规范\ndescription: 处理通用咨询",
    )
    release_scan.set()
    thread.join(2)

    assert not thread.is_alive()
    assert [skill.id for skill in manager.catalog] == [
        "billing_support",
        "general_customer_service",
    ]
    assert reports[0].added == ("general_customer_service",)
    assert reports[0].version == 2


def test_strict_metadata_reader_stops_before_body(tmp_path, monkeypatch):
    import io
    from pathlib import Path
    header = "---\nname: 测试\ndescription: 处理测试问题\n---\n"
    class HeaderOnly(io.StringIO):
        def readline(self, size=-1):
            if self.tell() >= len(header):
                raise AssertionError("不允许读取正文")
            return super().readline(size)
    real_open = Path.open
    def fake_open(path, *args, **kwargs):
        if path.name == "SKILL.md":
            return HeaderOnly(header + "BODY_MUST_NOT_BE_READ")
        return real_open(path, *args, **kwargs)
    write_skill(tmp_path, "test", front_matter="name: 测试\ndescription: 处理测试问题")
    monkeypatch.setattr(Path, "open", fake_open)
    manager = SkillManager(tmp_path, strict_front_matter=True)
    assert len(manager.load()) == 1


@pytest.mark.parametrize("front_matter", [None, "name: 名称", "description: 描述"])
def test_strict_metadata_requires_front_matter_fields(tmp_path, front_matter):
    write_skill(tmp_path, "test", front_matter=front_matter)
    manager = SkillManager(tmp_path, strict_front_matter=True)
    catalog = manager.load()
    assert len(catalog) == 0 and len(catalog.errors) == 1
