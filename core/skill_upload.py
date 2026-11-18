"""Parse single SKILL.md / ZIP uploads without extracting untrusted archive paths."""
from __future__ import annotations

import io
import re
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from core.skill_install import (
    SkillImportFile, SkillImportLimits, SkillInstallError,
    _normalize_path, _prepare_files, _validate_staged, _write_files,
)
from core.skill_loader import SkillManager, _split_front_matter

UPLOAD_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class SkillUpload:
    id: str
    files: tuple[SkillImportFile, ...]
    skill: dict
    total_bytes: int

    def preview(self) -> dict:
        return {"skill": self.skill, "file_count": len(self.files), "total_bytes": self.total_bytes}


def _decode(raw: bytes, path: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillInstallError("skill_content_not_text", f"只支持 UTF-8 文本文件：{path}") from exc
    if '\x00' in text:
        raise SkillInstallError("skill_content_not_text", f"不是文本文件：{path}")
    return text


def _zip_files(raw: bytes, limits: SkillImportLimits) -> tuple[str, list[SkillImportFile]]:
    files: list[SkillImportFile] = []
    roots: set[str] = set()
    paths: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_files * 4 + 64:
                raise SkillInstallError("skill_package_too_large", "ZIP 条目过多", status=413)
            for entry in entries:
                # Finder 压缩生成的辅助元数据不是技能内容，不读取或落盘。
                if entry.filename.startswith('__MACOSX/') or entry.filename.split('/')[-1] == '.DS_Store':
                    continue
                name = entry.filename[:-1] if entry.is_dir() else entry.filename
                parts = name.split('/')
                mode = entry.external_attr >> 16
                if (entry.orig_filename != entry.filename or '\\' in name or
                        any(not p or p.startswith('.') for p in parts) or
                        stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise SkillInstallError("skill_path_invalid", "ZIP 含非法路径、符号链接或特殊文件")
                roots.add(parts[0])
                if len(roots) != 1 or (len(parts) < 2 and not entry.is_dir()):
                    raise SkillInstallError("skill_zip_structure_invalid", "ZIP 根目录必须只有一个技能文件夹，不能直接放 SKILL.md 或多个技能包")
                if not UPLOAD_NAME.fullmatch(parts[0]):
                    raise SkillInstallError("skill_id_invalid", "技能文件夹名只允许小写字母、数字和连字符，最多 64 字符")
                if entry.is_dir():
                    if len(parts) > 1 and parts[1] not in ('references', 'templates', 'scripts'):
                        raise SkillInstallError("skill_path_invalid", "ZIP 的资源目录只支持 references/、templates/、scripts/")
                    continue
                relative = '/'.join(parts[1:])
                if len(relative) > 256:
                    raise SkillInstallError("skill_path_invalid", "文件路径超过 256 字符")
                relative = _normalize_path(relative)
                if relative in paths:
                    raise SkillInstallError("skill_path_invalid", f"ZIP 文件路径重复：{relative}")
                paths.add(relative)
                total += entry.file_size
                if entry.file_size > limits.max_file_bytes or total > limits.max_total_bytes or len(files) >= limits.max_files:
                    raise SkillInstallError("skill_package_too_large", "ZIP 解压后超过文件大小或数量限制", status=413)
                if entry.flag_bits & 1:
                    raise SkillInstallError("skill_zip_invalid", "不支持加密 ZIP")
                with archive.open(entry) as handle:
                    content = handle.read(limits.max_file_bytes + 1)
                if len(content) > limits.max_file_bytes:
                    raise SkillInstallError("skill_package_too_large", "ZIP 解压文件过大", status=413)
                files.append(SkillImportFile(relative, _decode(content, relative)))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError,
            EOFError, OSError, zlib.error, UnicodeError) as exc:
        raise SkillInstallError("skill_zip_invalid", "无法读取 ZIP，请检查文件是否损坏或使用了不支持的压缩方式") from exc
    if not files or len(roots) != 1:
        raise SkillInstallError("skill_zip_structure_invalid", "ZIP 中缺少技能文件夹和 SKILL.md")
    return next(iter(roots)), files


def prepare_upload(manager: SkillManager, filename: str, raw: bytes, limits: SkillImportLimits) -> SkillUpload:
    if not raw:
        raise SkillInstallError("skill_files_empty", "上传文件为空")
    if len(raw) > limits.max_total_bytes:
        raise SkillInstallError("skill_package_too_large", "上传文件超过大小限制", status=413)
    folder = None
    if filename.lower().endswith('.zip'):
        folder, files = _zip_files(raw, limits)
    elif filename.lower() == 'skill.md':
        files = [SkillImportFile('SKILL.md', _decode(raw, filename))]
    else:
        raise SkillInstallError("skill_upload_type_invalid", "请选择 SKILL.md 或 .zip 文件")
    prepared = _prepare_files(files, limits)
    try:
        meta, _ = _split_front_matter(prepared['SKILL.md'])
    except ValueError as exc:
        raise SkillInstallError("skill_front_matter_invalid", str(exc)) from exc
    name = str(meta.get('name') or '')
    if not UPLOAD_NAME.fullmatch(name):
        raise SkillInstallError("skill_name_invalid", "SKILL.md 的 name 只允许小写字母、数字和连字符，最多 64 字符")
    if folder is not None and folder != name:
        raise SkillInstallError("skill_name_mismatch", "ZIP 内的技能文件夹名必须与 SKILL.md 的 name 一致")
    # 预览只使用临时目录；正式 skills 目录和在线快照不发生变化。
    with tempfile.TemporaryDirectory(prefix='cm-skill-preview-') as temporary:
        root = Path(temporary)
        _write_files(root / name, prepared)
        descriptor = _validate_staged(root, name, manager)
    return SkillUpload(name, tuple(files), descriptor.to_summary(), sum(len(f.content.encode('utf-8')) for f in files))
