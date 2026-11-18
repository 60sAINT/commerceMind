"""Skill 文本包管理：读写 `skills/<id>/`、版本冲突保护与热重载。

设计约束：
- 只接受根目录 `SKILL.md` 与 `references/`、`templates/`、`scripts/` 下的文本资源；
- 写入前先在隐藏暂存目录里用 `SkillManager` 自校验，规则与启动扫描完全一致；
- 暂存、备份、rename 安装，失败时尽力回滚；目录快照在 reload 内原子交换；
- 同一进程内读包、导入、保存、删除和手动重载串行执行。
"""
from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from core.skill_loader import (
    RESOURCE_DIRS,
    SKILL_FILENAME,
    SkillBodyError,
    SkillDescriptor,
    SkillManager,
    SkillReloadReport,
)

logger = logging.getLogger(__name__)

SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEFAULT_MAX_FILE_BYTES = 64 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024
DEFAULT_MAX_FILES = 64

# 导入涉及“暂存 -> 替换 -> reload -> 清理”多步文件操作，必须串行。
_install_lock = threading.RLock()


def read_package(manager: SkillManager, skill_id: str, limits: SkillImportLimits | None = None) -> dict:
    """Read a complete editable text package; never follow symlinks or truncate files."""
    limits = limits or SkillImportLimits()
    if not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise SkillInstallError("skill_id_invalid", "技能 ID 格式不合法")
    with _install_lock:
        target = manager.root_dir / skill_id
        if target.is_symlink():
            raise SkillInstallError("skill_path_invalid", "不能编辑符号链接技能")
        if not target.is_dir():
            raise SkillInstallError("skill_not_found", "技能目录不存在", status=404)
        files = []
        total = 0
        for path in sorted(target.rglob("*")):
            relative = path.relative_to(target).as_posix()
            if path.is_symlink():
                raise SkillInstallError("skill_path_invalid", f"不能编辑包含符号链接的技能: {relative}")
            if path.is_dir():
                continue
            normalized = _normalize_path(relative)
            with path.open('rb') as handle:
                raw = handle.read(limits.max_file_bytes + 1)
            total += len(raw)
            if len(raw) > limits.max_file_bytes or total > limits.max_total_bytes or len(files) >= limits.max_files:
                raise SkillInstallError("skill_package_too_large", "技能包超过编辑大小或数量限制", status=413)
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise SkillInstallError("skill_content_not_text", f"不是 UTF-8 文本: {relative}") from exc
            if '\x00' in content:
                raise SkillInstallError("skill_content_not_text", f"不是文本: {relative}")
            if normalized != relative:
                raise SkillInstallError("skill_path_invalid", f"请先将入口文件命名为 SKILL.md: {relative}")
            files.append({"path": relative, "content": content})
        revision = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"id": skill_id, "files": files, "revision": revision}


def reload_skills(manager: SkillManager) -> SkillReloadReport:
    """Serialize manual reload with file mutations in this process."""
    with _install_lock:
        return manager.reload()


def delete_skill(manager: SkillManager, skill_id: str, revision: str,
                 limits: SkillImportLimits | None = None) -> SkillReloadReport:
    with _install_lock:
        current = read_package(manager, skill_id, limits)
        if current['revision'] != revision:
            raise SkillInstallError("skill_revision_conflict", "文件已被修改，请重新读取后再删除", status=409)
        target = manager.root_dir / skill_id
        backup = manager.root_dir / f'.trash-{uuid.uuid4().hex}'
        os.replace(target, backup)
        try:
            report = manager.reload()
        except Exception:
            os.replace(backup, target)
            manager.reload()
            raise
        # 目录和目录快照均已删除成功；残留备份清理失败不应误报业务失败。
        shutil.rmtree(backup, ignore_errors=True)
        return report


@dataclass(frozen=True, slots=True)
class SkillImportFile:
    """一个待写入的相对路径与文本内容。"""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillImportLimits:
    """上传包的大小与数量上限；由 SkillSettings 注入。"""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_files: int = DEFAULT_MAX_FILES


@dataclass(frozen=True, slots=True)
class SkillInstallResult:
    descriptor: SkillDescriptor
    report: SkillReloadReport
    revision: str


class SkillInstallError(ValueError):
    """可映射为 API 错误响应的导入失败。"""

    def __init__(self, code: str, detail: str, *, status: int = 422) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status


def install_skill(
    manager: SkillManager,
    skill_id: str,
    files: Sequence[SkillImportFile],
    *,
    overwrite: bool = False,
    limits: SkillImportLimits | None = None,
    expected_revision: str | None = None,
) -> SkillInstallResult:
    """校验并安装一个 Skill 包，成功后重载目录并返回最新描述。"""
    limits = limits or SkillImportLimits()
    normalized_id = (skill_id or "").strip()
    if not SKILL_ID_PATTERN.fullmatch(normalized_id):
        raise SkillInstallError(
            "skill_id_invalid",
            "技能 ID 只能包含小写字母、数字、下划线或短横线，且不超过 64 字符",
        )

    prepared = _prepare_files(files, limits)
    root = manager.root_dir
    staging_root = root / f".staging-{uuid.uuid4().hex}"
    trash_root = root / f".trash-{uuid.uuid4().hex}"
    target = root / normalized_id
    moved_aside = False
    installed = False
    rollback_failed = False

    with _install_lock:
        try:
            if expected_revision is not None:
                current = read_package(manager, normalized_id, limits)
                if current['revision'] != expected_revision:
                    raise SkillInstallError("skill_revision_conflict", "文件已被修改，请重新读取并合并草稿", status=409)
            root.mkdir(parents=True, exist_ok=True)
            staged_skill = staging_root / normalized_id
            _write_files(staged_skill, prepared)
            _validate_staged(staging_root, normalized_id, manager)

            if target.exists() or target.is_symlink():
                if not overwrite:
                    raise SkillInstallError(
                        "skill_exists",
                        f"技能 {normalized_id} 已存在，如需替换请勾选覆盖",
                        status=409,
                    )
                if target.is_symlink() or not target.is_dir():
                    raise SkillInstallError(
                        "skill_install_failed",
                        f"目标路径不是普通目录: {normalized_id}",
                        status=409,
                    )
                trash_root.mkdir(parents=True, exist_ok=True)
                os.replace(target, trash_root / normalized_id)
                moved_aside = True

            os.replace(staged_skill, target)
            installed = True
            report = manager.reload()
            descriptor = manager.get(normalized_id)
            if descriptor is None:
                raise SkillInstallError(
                    "skill_install_failed",
                    "技能已写入但未出现在目录中",
                    status=500,
                )
            logger.info(
                "Skill 已导入: id=%s files=%d version=%d overwrite=%s",
                normalized_id,
                len(prepared),
                report.version,
                overwrite,
            )
            return SkillInstallResult(descriptor=descriptor, report=report,
                                      revision=read_package(manager, normalized_id, limits)['revision'])
        except Exception:
            if installed:
                shutil.rmtree(target, ignore_errors=True)
            if moved_aside:
                try:
                    os.replace(trash_root / normalized_id, target)
                    moved_aside = False
                except OSError:
                    rollback_failed = True
                    logger.exception("Skill 回滚失败: id=%s", normalized_id)
            if installed or moved_aside:
                try:
                    manager.reload()
                except Exception:
                    logger.exception("Skill 回滚后重载失败: id=%s", normalized_id)
            raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            # 恢复失败时保留原文件备份，避免再次删除用户的唯一旧副本。
            if not rollback_failed:
                shutil.rmtree(trash_root, ignore_errors=True)


def _prepare_files(
    files: Sequence[SkillImportFile],
    limits: SkillImportLimits,
) -> dict[str, str]:
    """校验路径、大小与重复项，返回 path -> content 映射。"""
    if not files:
        raise SkillInstallError("skill_files_empty", "没有可导入的文件")
    if len(files) > limits.max_files:
        raise SkillInstallError(
            "skill_package_too_large",
            f"文件数量超过上限 {limits.max_files}",
            status=413,
        )

    prepared: dict[str, str] = {}
    total_bytes = 0
    for item in files:
        path = _normalize_path(item.path)
        if path in prepared:
            raise SkillInstallError("skill_path_invalid", f"文件路径重复: {path}")
        content = item.content
        if "\x00" in content:
            raise SkillInstallError("skill_content_not_text", f"文件不是文本内容: {path}")

        size = len(content.encode("utf-8"))
        if size > limits.max_file_bytes:
            raise SkillInstallError(
                "skill_file_too_large",
                f"{path} 超过单文件上限 {limits.max_file_bytes} 字节",
                status=413,
            )
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise SkillInstallError(
                "skill_package_too_large",
                f"技能包总大小超过上限 {limits.max_total_bytes} 字节",
                status=413,
            )
        prepared[path] = content

    if SKILL_FILENAME not in prepared:
        raise SkillInstallError("skill_manifest_missing", "技能根目录缺少 SKILL.md")
    for path in prepared:
        if any(parent.as_posix() in prepared for parent in PurePosixPath(path).parents if parent.as_posix() != '.'):
            raise SkillInstallError("skill_path_invalid", f"文件与目录路径冲突: {path}")
    return prepared


def _normalize_path(raw: str) -> str:
    """把前端路径规范化为包内相对路径；拒绝一切越界写法。"""
    if not isinstance(raw, str):
        raise SkillInstallError("skill_path_invalid", "文件路径必须是字符串")
    text = raw.strip()
    if not text:
        raise SkillInstallError("skill_path_invalid", "文件路径不能为空")
    if text != raw:
        raise SkillInstallError("skill_path_invalid", "文件路径不能带有首尾空白")
    if "\x00" in text:
        raise SkillInstallError("skill_path_invalid", "文件路径包含非法字符")
    if "\\" in text:
        raise SkillInstallError("skill_path_invalid", f"文件路径必须使用正斜杠: {text}")

    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise SkillInstallError("skill_path_invalid", f"文件路径不能是绝对路径: {text}")
    if pure.as_posix() != text:
        raise SkillInstallError("skill_path_invalid", f"文件路径不能含有空段或点段: {text}")
    parts = pure.parts
    if not parts:
        raise SkillInstallError("skill_path_invalid", "文件路径不能为空")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise SkillInstallError("skill_path_invalid", f"文件路径不能包含隐藏段或 ..: {text}")

    # 单文件上传时允许 skill.md，统一成加载器要求的 SKILL.md。
    if len(parts) == 1 and parts[0].lower() == SKILL_FILENAME.lower():
        return SKILL_FILENAME
    if len(parts) < 2 or parts[0] not in RESOURCE_DIRS:
        raise SkillInstallError(
            "skill_path_invalid",
            f"文件只能放在 {', '.join(RESOURCE_DIRS)} 目录: {text}",
        )
    return pure.as_posix()


def _write_files(skill_dir: Path, files: dict[str, str]) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = skill_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _validate_staged(
    staging_root: Path,
    skill_id: str,
    manager: SkillManager,
) -> SkillDescriptor:
    """用与启动时相同的加载器扫描暂存目录，提前暴露格式问题。"""
    probe = SkillManager(
        staging_root,
        max_skill_chars=manager.max_skill_chars,
        max_resource_chars=manager.max_resource_chars,
        strict_front_matter=True,
    )
    catalog = probe.load()
    if catalog.errors:
        first = catalog.errors[0]
        raise SkillInstallError(
            "skill_front_matter_invalid",
            f"{first.path}: {first.message}",
        )

    descriptor = catalog.get(skill_id)
    if descriptor is None:
        # strict 模式下 name/description 缺失会进 errors；这里通常是 enabled=false。
        raise SkillInstallError(
            "skill_disabled",
            "技能 front matter 的 enabled 为 false，导入后不会出现在目录中",
        )

    try:
        document = probe.load_skill(skill_id)
    except SkillBodyError as exc:
        raise SkillInstallError("skill_body_empty", str(exc)) from exc
    if not document.body:
        raise SkillInstallError("skill_body_empty", "技能正文为空")
    return descriptor
