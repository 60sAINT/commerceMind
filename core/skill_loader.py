"""Markdown Skill 加载器。

发现 `skills/<skill_id>/SKILL.md` 并解析轻量元数据；正文和包内资源只在
调用方明确请求时读取，避免启动时把所有手册都装进内存或模型上下文。

三层结构：
1. 目录层：`load()` 只读文件头，生成 `SkillCatalog`；
2. 正文层：`load_skill()` 按 skill id 读取 `SKILL.md` 正文；
3. 资源层：`list_skill_resources()` / `read_skill_resource()` 按路径读取
   `references/`、`templates/`、`scripts/` 里的内容。

设计约束：
- front matter 只支持项目需要的最小子集，不引入 PyYAML。
- 单个 Skill 解析失败只记录到 `errors`，不影响其他 Skill。
- 资源读取拒绝绝对路径、`..`、隐藏文件和符号链接逃逸。
- 脚本只作为文本返回，不在这里执行。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
MAX_HEADER_CHARS = 8 * 1024
DEFAULT_MAX_SKILL_CHARS = 8 * 1024
DEFAULT_MAX_RESOURCE_CHARS = 20 * 1024
RESOURCE_DIRS = ("references", "templates", "scripts")
_RESOURCE_DIR_SET = frozenset(RESOURCE_DIRS)
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


class SkillNotFoundError(LookupError):
    """请求的 Skill id 不在当前目录中。"""


class SkillResourceNotFoundError(LookupError):
    """请求的资源文件不存在。"""


class SkillResourceAccessError(ValueError):
    """资源路径非法、越界或不是可读取的文本文件。"""


class SkillBodyError(ValueError):
    """Skill 正文为空或无法解析。"""


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
class SkillResourceInfo:
    """资源清单项，只包含路径和大小，不包含内容。"""

    relative_path: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """按需加载后的 Skill 正文及其资源清单。"""

    descriptor: SkillDescriptor
    body: str
    truncated: bool = False
    resources: tuple[SkillResourceInfo, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.descriptor.to_summary(),
            "body": self.body,
            "truncated": self.truncated,
            "resources": [resource.to_dict() for resource in self.resources],
        }


@dataclass(frozen=True, slots=True)
class SkillResource:
    """按需读取后的单个资源内容。"""

    skill_id: str
    relative_path: str
    content: str
    size_bytes: int
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "path": self.relative_path,
            "content": self.content,
            "size_bytes": self.size_bytes,
            "truncated": self.truncated,
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


@dataclass(frozen=True, slots=True)
class SkillReloadReport:
    """一次热重载的变化报告。"""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    errors: tuple[SkillError, ...] = ()
    version: int = 0
    loaded_at: str = ""
    duration_ms: float = 0.0
    count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "unchanged": list(self.unchanged),
            "errors": [error.to_dict() for error in self.errors],
            "version": self.version,
            "loaded_at": self.loaded_at,
            "duration_ms": round(self.duration_ms, 3),
            "count": self.count,
        }


class SkillManager:
    """发现并管理 `skills/<skill_id>/SKILL.md` 的元数据目录。"""

    def __init__(
        self,
        root_dir: str | Path = "skills",
        *,
        max_skill_chars: int = DEFAULT_MAX_SKILL_CHARS,
        max_resource_chars: int = DEFAULT_MAX_RESOURCE_CHARS,
        strict_front_matter: bool = False,
    ) -> None:
        if max_skill_chars <= 0:
            raise ValueError("max_skill_chars 必须大于 0")
        if max_resource_chars <= 0:
            raise ValueError("max_resource_chars 必须大于 0")

        self.root_dir = Path(root_dir).expanduser().resolve()
        self.max_skill_chars = max_skill_chars
        self.max_resource_chars = max_resource_chars
        self.strict_front_matter = strict_front_matter
        self._lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self._version = 0
        self._catalog = SkillCatalog(loaded_at=_utc_now())
        self._last_report = SkillReloadReport()
        # path -> ((mtime_ns, size), body, truncated)
        self._body_cache: dict[str, tuple[tuple[int, int], str, bool]] = {}

    @property
    def catalog(self) -> SkillCatalog:
        with self._lock:
            return self._catalog

    @property
    def skills(self) -> tuple[SkillDescriptor, ...]:
        return self.catalog.skills

    @property
    def errors(self) -> tuple[SkillError, ...]:
        return self.catalog.errors

    @property
    def last_report(self) -> SkillReloadReport:
        with self._lock:
            return self._last_report

    def get(self, skill_id: str) -> SkillDescriptor | None:
        return self.catalog.get(skill_id)

    def summary(self) -> dict[str, Any]:
        return self.catalog.to_summary()

    def load_skill(self, skill_id: str) -> SkillDocument:
        """按需读取 Skill 正文，并附带资源清单（不读取资源内容）。"""
        with self._lock:
            catalog = self._catalog
        descriptor = self._require_skill(skill_id, catalog)
        path = Path(descriptor.path)
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            with self._lock:
                self._body_cache.pop(descriptor.path, None)
            raise SkillNotFoundError(f"Skill 文件不存在: {skill_id}") from exc

        cache_key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._body_cache.get(descriptor.path)
        if cached is not None and cached[0] == cache_key:
            body, truncated = cached[1], cached[2]
        else:
            raw, raw_truncated = _read_text_limited(
                path, self.max_skill_chars + MAX_HEADER_CHARS
            )
            _, body_part = _split_front_matter(raw)
            body = body_part.strip()
            if not body:
                raise SkillBodyError(f"Skill 正文为空: {skill_id}")
            truncated = raw_truncated or len(body) > self.max_skill_chars
            if len(body) > self.max_skill_chars:
                body = body[: self.max_skill_chars].rstrip()
            with self._lock:
                # 读取期间如果发生了 reload，只丢弃这次缓存写入，不用旧快照覆盖新目录。
                if self._catalog is catalog:
                    self._body_cache[descriptor.path] = (cache_key, body, truncated)

        return SkillDocument(
            descriptor=descriptor,
            body=body,
            truncated=truncated,
            resources=self._list_resources_for(descriptor),
        )

    def list_skill_resources(self, skill_id: str) -> tuple[SkillResourceInfo, ...]:
        """列出 `references/`、`templates/`、`scripts/` 下的资源，不读内容。"""
        with self._lock:
            catalog = self._catalog
        descriptor = self._require_skill(skill_id, catalog)
        return self._list_resources_for(descriptor)

    def _list_resources_for(
        self,
        descriptor: SkillDescriptor,
    ) -> tuple[SkillResourceInfo, ...]:
        skill_dir = Path(descriptor.path).parent.resolve()
        resources: list[SkillResourceInfo] = []

        for directory_name in RESOURCE_DIRS:
            directory = skill_dir / directory_name
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or _has_hidden_part(path.relative_to(skill_dir)):
                    continue
                if not _is_safe_resource_path(skill_dir, path):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                resources.append(
                    SkillResourceInfo(
                        relative_path=path.relative_to(skill_dir).as_posix(),
                        size_bytes=size,
                    )
                )

        resources.sort(key=lambda item: item.relative_path)
        return tuple(resources)

    def read_skill_resource(self, skill_id: str, relative_path: str) -> SkillResource:
        """读取单个资源文本；路径必须位于允许的资源子目录内。"""
        with self._lock:
            catalog = self._catalog
        descriptor = self._require_skill(skill_id, catalog)
        skill_dir = Path(descriptor.path).parent.resolve()
        normalized = _normalize_resource_path(relative_path)
        candidate = skill_dir / normalized

        if not _is_safe_resource_path(skill_dir, candidate):
            raise SkillResourceAccessError("资源路径越界或不是普通文件")
        if candidate.is_symlink():
            raise SkillResourceAccessError("资源文件不能是符号链接")
        if not candidate.is_file():
            raise SkillResourceNotFoundError(
                f"资源不存在: {skill_id}/{normalized}"
            )

        try:
            content, truncated = _read_text_limited(
                candidate, self.max_resource_chars
            )
            if "\x00" in content:
                raise SkillResourceAccessError("资源不是文本文件")
            stat = candidate.stat()
        except SkillResourceAccessError:
            raise
        except FileNotFoundError as exc:
            raise SkillResourceNotFoundError(
                f"资源不存在: {skill_id}/{normalized}"
            ) from exc

        return SkillResource(
            skill_id=skill_id,
            relative_path=normalized,
            content=content,
            size_bytes=stat.st_size,
            truncated=truncated,
        )

    def load(self) -> SkillCatalog:
        """兼容入口：执行一次重载并返回最新目录快照。"""
        self.reload()
        return self.catalog

    def reload(self) -> SkillReloadReport:
        """重新扫描目录并原子替换快照，返回本次变化报告。

        扫描阶段不持有读锁，已有请求可以继续使用旧快照；新目录构建完成后
        再一次性替换。并发 reload 由 `_reload_lock` 串行化。
        """
        started = time.perf_counter()
        with self._reload_lock:
            with self._lock:
                previous = self._catalog
                next_version = self._version + 1

            new_catalog = self._scan_catalog(next_version)
            added, updated, removed, unchanged = _diff_catalogs(previous, new_catalog)
            duration_ms = (time.perf_counter() - started) * 1000
            report = SkillReloadReport(
                added=added,
                updated=updated,
                removed=removed,
                unchanged=unchanged,
                errors=new_catalog.errors,
                version=new_catalog.version,
                loaded_at=new_catalog.loaded_at,
                duration_ms=duration_ms,
                count=len(new_catalog.skills),
            )

            with self._lock:
                self._version = next_version
                self._catalog = new_catalog
                self._last_report = report
                # reload 后统一清空正文缓存，避免文件内容变化但 stat 未变时命中旧正文。
                self._body_cache.clear()

        logger.info(
            "Skill 目录已重载: root=%s count=%d added=%d updated=%d removed=%d "
            "errors=%d version=%d duration_ms=%.1f",
            self.root_dir,
            report.count,
            len(report.added),
            len(report.updated),
            len(report.removed),
            len(report.errors),
            report.version,
            report.duration_ms,
        )
        return report

    def _scan_catalog(self, version: int) -> SkillCatalog:
        """扫描并构建新目录快照；不修改当前 catalog 和缓存。"""
        errors: list[SkillError] = []

        if not self.root_dir.exists():
            logger.info("Skill 目录不存在，按空目录处理: %s", self.root_dir)
            return SkillCatalog((), (), version, _utc_now())

        if not self.root_dir.is_dir():
            errors.append(SkillError(str(self.root_dir), "Skill 根路径不是目录"))
            return SkillCatalog((), tuple(errors), version, _utc_now())

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
        return SkillCatalog(
            skills=tuple(loaded),
            errors=tuple(errors),
            version=version,
            loaded_at=_utc_now(),
        )

    def _require_skill(
        self,
        skill_id: str,
        catalog: SkillCatalog | None = None,
    ) -> SkillDescriptor:
        if catalog is None:
            catalog = self.catalog
        descriptor = catalog.get(skill_id)
        if descriptor is None:
            raise SkillNotFoundError(f"Skill 不存在: {skill_id}")
        return descriptor

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
        raw = _read_front_matter_only(path) if self.strict_front_matter else _read_header(path)
        meta, body_prefix = _split_front_matter(raw)
        if self.strict_front_matter and not all(str(meta.get(key) or "").strip() for key in ("name", "description")):
            raise ValueError("Skill YAML front matter 必须包含 name 和 description")

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


def _descriptor_fingerprint(skill: SkillDescriptor) -> tuple[Any, ...]:
    """用于判断目录项是否变化的轻量指纹，不读取正文。"""
    return (
        skill.name,
        skill.description,
        skill.keywords,
        skill.version,
        skill.enabled,
        skill.mtime_ns,
        skill.size,
    )


def _diff_catalogs(
    previous: SkillCatalog,
    current: SkillCatalog,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """返回 added / updated / removed / unchanged 四组 skill id。"""
    old = {skill.id: skill for skill in previous.skills}
    new = {skill.id: skill for skill in current.skills}

    added = tuple(sorted(set(new) - set(old)))
    removed = tuple(sorted(set(old) - set(new)))
    common = set(old) & set(new)
    updated = tuple(
        sorted(
            skill_id
            for skill_id in common
            if _descriptor_fingerprint(old[skill_id])
            != _descriptor_fingerprint(new[skill_id])
        )
    )
    unchanged = tuple(sorted(common - set(updated)))
    return added, updated, removed, unchanged


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_header(path: Path) -> str:
    """只读取文件头，front matter 和回退标题都应落在这一段里。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(MAX_HEADER_CHARS)


def _read_front_matter_only(path: Path) -> str:
    """API 启动只消费 front matter，到闭合分隔符即停止，不读取正文。"""
    lines: list[str] = []
    length = 0
    opened = False
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        while length < MAX_HEADER_CHARS:
            line = handle.readline(MAX_HEADER_CHARS - length)
            if not line:
                break
            length += len(line)
            lines.append(line)
            if not opened:
                if not line.strip():
                    continue
                if line.strip() != "---":
                    raise ValueError("Skill 必须以 YAML front matter 开头")
                opened = True
            elif line.strip() == "---":
                return "".join(lines)
    raise ValueError("Skill front matter 未闭合或超过字符上限")


def _read_text_limited(path: Path, max_chars: int) -> tuple[str, bool]:
    """最多读取 max_chars 个字符，并返回是否被截断。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(max_chars + 1)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _has_hidden_part(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def _normalize_resource_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise SkillResourceAccessError("资源路径不能为空")
    text = relative_path.strip()
    if "\x00" in text:
        raise SkillResourceAccessError("资源路径包含非法字符")
    if "\\" in text:
        raise SkillResourceAccessError("资源路径必须使用正斜杠")

    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise SkillResourceAccessError("资源路径不能是绝对路径")
    parts = pure.parts
    if not parts:
        raise SkillResourceAccessError("资源路径不能为空")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise SkillResourceAccessError("资源路径不能包含隐藏目录或 ..")
    if parts[0] not in _RESOURCE_DIR_SET:
        raise SkillResourceAccessError(
            f"资源只能从 {', '.join(RESOURCE_DIRS)} 读取"
        )
    return pure.as_posix()


def _is_safe_resource_path(skill_dir: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(skill_dir.resolve())
    except (OSError, ValueError):
        return False
    return True


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
