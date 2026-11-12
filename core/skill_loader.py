"""Markdown Skill 目录加载器。

只发现 `skills/<skill_id>/SKILL.md` 并解析轻量元数据，不读取正文和包内
资源。正文、模板、参考文档和脚本由后续按需加载接口读取，避免启动时把
所有手册都装进内存或模型上下文。

设计约束：
- front matter 只支持项目需要的最小子集，不引入 PyYAML。
- 单个 Skill 解析失败只记录到 `errors`，不影响其他 Skill。
- 目录扫描只读文件头，大段正文不会在这里进入内存。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
MAX_HEADER_CHARS = 8 * 1024
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


@dataclass(frozen=True, slots=True)
class SkillError:
    """单个 Skill 的加载错误；path 使用相对根目录的路径。"""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    """Skill 的轻量元数据；不包含正文。"""

    id: str
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    version: str = "1"
    enabled: bool = True
    path: str = field(default="", repr=False)
    relative_path: str = ""
    mtime_ns: int = field(default=0, repr=False)
    size: int = field(default=0, repr=False)

    def to_summary(self) -> dict[str, Any]:
        """返回可序列化摘要；不暴露正文和服务器绝对路径。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "version": self.version,
            "enabled": self.enabled,
            "path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """一次扫描得到的 Skill 目录快照。"""

    skills: tuple[SkillDescriptor, ...] = ()
    errors: tuple[SkillError, ...] = ()
    version: int = 0
    loaded_at: str = ""

    def __iter__(self) -> Iterator[SkillDescriptor]:
        return iter(self.skills)

    def __len__(self) -> int:
        return len(self.skills)

    def get(self, skill_id: str) -> SkillDescriptor | None:
        for skill in self.skills:
            if skill.id == skill_id:
                return skill
        return None

    def to_summary(self) -> dict[str, Any]:
        """返回目录摘要，供健康检查、调试接口和后续 prompt 构建使用。"""
        return {
            "count": len(self.skills),
            "version": self.version,
            "loaded_at": self.loaded_at,
            "skills": [skill.to_summary() for skill in self.skills],
            "errors": [error.to_dict() for error in self.errors],
        }


class SkillManager:
    """发现并管理 `skills/<skill_id>/SKILL.md` 的元数据目录。"""

    def __init__(self, root_dir: str | Path = "skills") -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self._version = 0
        self._catalog = SkillCatalog(loaded_at=_utc_now())

    @property
    def catalog(self) -> SkillCatalog:
        return self._catalog

    @property
    def skills(self) -> tuple[SkillDescriptor, ...]:
        return self._catalog.skills

    @property
    def errors(self) -> tuple[SkillError, ...]:
        return self._catalog.errors

    def get(self, skill_id: str) -> SkillDescriptor | None:
        return self._catalog.get(skill_id)

    def summary(self) -> dict[str, Any]:
        return self._catalog.to_summary()

    def load(self) -> SkillCatalog:
        """扫描目录并替换当前目录快照；失败项不会影响其他 Skill。"""
        self._version += 1
        errors: list[SkillError] = []

        if not self.root_dir.exists():
            logger.info("Skill 目录不存在，按空目录处理: %s", self.root_dir)
            self._catalog = self._new_catalog((), errors)
            return self._catalog

        if not self.root_dir.is_dir():
            errors.append(SkillError(str(self.root_dir), "Skill 根路径不是目录"))
            self._catalog = self._new_catalog((), errors)
            return self._catalog

        loaded: list[SkillDescriptor] = []
        seen_ids: set[str] = set()
        for path in sorted(self.root_dir.glob(f"*/{SKILL_FILENAME}")):
            relative = _relative_path(path, self.root_dir)
            try:
                if path.parent.name.startswith("."):
                    continue
                if not self._is_safe_skill_file(path):
                    raise ValueError("Skill 文件不能是符号链接或逃逸根目录")
                skill = self._load_metadata(path)
                if skill is None:
                    continue
                if skill.id in seen_ids:
                    raise ValueError(f"Skill id 重复: {skill.id}")
                seen_ids.add(skill.id)
                loaded.append(skill)
            except Exception as exc:
                errors.append(SkillError(relative, str(exc)))
                logger.warning("Skill 解析失败: %s: %s", relative, exc)

        loaded.sort(key=lambda skill: skill.id)
        self._catalog = self._new_catalog(tuple(loaded), errors)
        logger.info(
            "Skill 目录已加载: root=%s count=%d errors=%d version=%d",
            self.root_dir,
            len(loaded),
            len(errors),
            self._version,
        )
        return self._catalog

    def _new_catalog(
        self,
        skills: tuple[SkillDescriptor, ...],
        errors: list[SkillError],
    ) -> SkillCatalog:
        return SkillCatalog(
            skills=skills,
            errors=tuple(errors),
            version=self._version,
            loaded_at=_utc_now(),
        )

    def _is_safe_skill_file(self, path: Path) -> bool:
        """拒绝符号链接和逃逸根目录的 SKILL.md。"""
        if path.is_symlink() or path.parent.is_symlink():
            return False
        try:
            path.resolve().relative_to(self.root_dir)
        except ValueError:
            return False
        return True

    def _load_metadata(self, path: Path) -> SkillDescriptor | None:
        """只读取文件头并解析元数据；enabled=false 时返回 None。"""
        raw = _read_header(path)
        meta, body_prefix = _split_front_matter(raw)

        skill_id = path.parent.name
        if not _SKILL_ID_PATTERN.fullmatch(skill_id):
            raise ValueError(
                "Skill id 必须是小写字母、数字、下划线或短横线，且不超过 64 字符"
            )

        name = str(meta.get("name") or _first_heading(body_prefix) or skill_id).strip()
        description = str(
            meta.get("description") or _first_paragraph(body_prefix) or ""
        ).strip()
        if not name:
            raise ValueError("缺少 name，且正文没有可用标题")
        if not description:
            raise ValueError("缺少 description，且正文没有可用段落")

        enabled = _as_bool(meta.get("enabled"), default=True)
        if not enabled:
            return None

        stat = path.stat()
        return SkillDescriptor(
            id=skill_id,
            name=name,
            description=description,
            keywords=_as_list(meta.get("keywords")),
            version=str(meta.get("version") or "1").strip() or "1",
            enabled=True,
            path=str(path),
            relative_path=_relative_path(path, self.root_dir),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_header(path: Path) -> str:
    """只读取文件头，front matter 和回退标题都应落在这一段里。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(MAX_HEADER_CHARS)


def _split_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown 顶部的简单 front matter。

    支持 `---` 包裹的 `key: value`，不处理嵌套 YAML。没有 front matter 时
    返回空元数据和原文，供标题/首段回退逻辑使用。
    """
    text = raw.lstrip("\ufeff \t\r\n")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw

    end_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break

    if end_index is None:
        if len(raw) >= MAX_HEADER_CHARS:
            raise ValueError(
                f"front matter 超过 {MAX_HEADER_CHARS} 字符或未闭合"
            )
        raise ValueError("front matter 未闭合")

    meta = _parse_front_matter(lines[1:end_index])
    return meta, "\n".join(lines[end_index + 1 :])


def _parse_front_matter(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"front matter 行格式错误: {line!r}")

        key, value = line.split(":", 1)
        key = key.strip().lower()
        if not key:
            raise ValueError(f"front matter 字段名为空: {line!r}")
        if key in meta:
            raise ValueError(f"front matter 字段重复: {key}")

        cleaned = _strip_value(value)
        if key == "enabled":
            meta[key] = _as_bool(cleaned, default=True)
        elif key == "keywords":
            meta[key] = _as_list(cleaned)
        else:
            meta[key] = cleaned
    return meta


def _strip_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = re.split(r"[,，]", text)

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        keyword = _strip_value(str(item)).strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        normalized.append(keyword)
    return tuple(normalized)


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValueError(f"enabled 不是合法布尔值: {value!r}")


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return None


def _first_paragraph(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        return stripped
    return None
