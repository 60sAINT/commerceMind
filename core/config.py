"""应用配置：从环境变量读取并校验模型配置。

设计原则：
- 启动时不强制要求 API Key，缺失时由 `/health` 明确报告，方便先启动服务再补配置。
- 所有配置集中在这里，避免在业务代码中散落 `os.getenv`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MEMORY_TTL_S = 7 * 24 * 60 * 60
_PLACEHOLDER_KEYS = frozenset({"xxx", "your-key", "your_api_key", "replace-me", "changeme"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_float(name: str, default: float) -> float:
    """读取正数浮点配置，非法值回退到默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    """读取正整数配置，非法值回退到默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔配置；无法识别时回退默认值。"""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def _env_non_negative_int(name: str, default: int) -> int:
    """读取非负整数配置；工具重试次数、缓存时间等允许 0。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_non_negative_float(name: str, default: float) -> float:
    """读取非负浮点配置；退避基数等允许 0。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_choice(name: str, default: str, allowed: frozenset[str]) -> str:
    """读取枚举配置；非法值回退默认值，避免错误配置阻塞启动。"""
    raw = os.getenv(name, "").strip().lower()
    return raw if raw in allowed else default


@dataclass(frozen=True, slots=True)
class MemorySettings:
    """短期记忆（Redis）配置。

    默认关闭，避免库调用方在未显式配置时连接真实 Redis；应用进程通过
    `Settings.from_env()` 读取 `.env`，生产环境通常为开启状态。
    """

    enabled: bool = False
    required: bool = False
    redis_url: str = "redis://localhost:6379/0"
    ttl_s: int = DEFAULT_MEMORY_TTL_S
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    debug: bool = False

    def __post_init__(self) -> None:
        if self.required and not self.enabled:
            raise ValueError("记忆 required 为 true 时必须同时 enabled")
        object.__setattr__(self, "redis_url", (self.redis_url or "").strip())
        object.__setattr__(self, "chroma_host", (self.chroma_host or "").strip())
        if self.enabled and not self.redis_url:
            raise ValueError("COMMERCEMIND_MEMORY_ENABLED=true 时 REDIS_URL 不能为空")
        if type(self.ttl_s) is not int or self.ttl_s <= 0:
            raise ValueError("COMMERCEMIND_MEMORY_TTL_SECONDS 必须是正整数")
        if type(self.chroma_port) is not int or not 0 < self.chroma_port < 65536:
            raise ValueError("CHROMA_PORT 必须是合法端口")


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSettings:
    """RAG 知识库的 Chroma collection 配置，与记忆 collection 分离。"""

    enabled: bool = False
    required: bool = False
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    collection_name: str = "knowledge_base"
    chunk_size: int = 500
    chunk_overlap: int = 50
    write_batch_size: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "chroma_host", (self.chroma_host or "").strip())
        object.__setattr__(self, "collection_name", (self.collection_name or "").strip())
        if self.required and not self.enabled:
            raise ValueError("知识库 required 为 true 时必须同时 enabled")
        if self.enabled and not self.chroma_host:
            raise ValueError("COMMERCEMIND_KNOWLEDGE_ENABLED=true 时 CHROMA_HOST 不能为空")
        if type(self.chroma_port) is not int or not 0 < self.chroma_port < 65536:
            raise ValueError("知识库 CHROMA_PORT 必须是合法端口")
        if not 3 <= len(self.collection_name) <= 63:
            raise ValueError("COMMERCEMIND_KNOWLEDGE_COLLECTION 必须为 3 到 63 个字符")
        if not self.collection_name[0].isalnum() or not self.collection_name[-1].isalnum():
            raise ValueError("知识库 collection 名称必须以字母或数字开头和结尾")
        if any(not (char in "abcdefghijklmnopqrstuvwxyz0123456789._-") for char in self.collection_name):
            raise ValueError("知识库 collection 名称只允许小写字母、数字、点、下划线和连字符")
        if type(self.chunk_size) is not int or self.chunk_size <= 0:
            raise ValueError("COMMERCEMIND_KNOWLEDGE_CHUNK_SIZE 必须是正整数")
        if type(self.chunk_overlap) is not int or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("COMMERCEMIND_KNOWLEDGE_CHUNK_OVERLAP 必须是非负整数且小于 chunk_size")
        if type(self.write_batch_size) is not int or self.write_batch_size <= 0:
            raise ValueError("COMMERCEMIND_KNOWLEDGE_WRITE_BATCH_SIZE 必须是正整数")


@dataclass(frozen=True, slots=True)
class IntentSettings:
    """三路意图识别参数；关闭时不影响现有聊天路径。"""

    enabled: bool = False
    llm_enabled: bool = True
    embedding_enabled: bool = True
    pattern_enabled: bool = True
    llm_weight: float = 0.70
    embedding_weight: float = 0.20
    pattern_weight: float = 0.10
    confidence_threshold: float = 0.35
    embedding_threshold: float = 0.72
    failure_policy: str = "available_sources"

    def __post_init__(self) -> None:
        for name in ("llm_weight", "embedding_weight", "pattern_weight"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} 必须是非负数")
        if sum((self.llm_weight, self.embedding_weight, self.pattern_weight)) <= 0:
            raise ValueError("至少一个意图来源权重必须大于 0")
        for name in ("confidence_threshold", "embedding_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"{name} 必须在 0 到 1 之间")
        if self.failure_policy not in {"available_sources", "other"}:
            raise ValueError("failure_policy 仅支持 available_sources 或 other")


@dataclass(frozen=True, slots=True)
class SkillSettings:
    """Skill 目录和按需正文预算；默认关闭以保持原聊天行为。"""

    enabled: bool = False
    root_dir: str = "skills"
    catalog_max_chars: int = 4000
    body_max_chars: int = 8192
    resource_max_chars: int = 20480
    max_selected_skills: int = 3
    debug: bool = False
    # 导入接口的上传预算；只在前端上传 Skill 时使用。
    upload_max_file_bytes: int = 64 * 1024
    upload_max_total_bytes: int = 1024 * 1024
    upload_max_files: int = 64

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_dir", str(self.root_dir).strip())
        if not self.root_dir:
            raise ValueError("Skill root_dir 不能为空")
        for name in (
            "catalog_max_chars",
            "body_max_chars",
            "resource_max_chars",
            "max_selected_skills",
            "upload_max_file_bytes",
            "upload_max_total_bytes",
            "upload_max_files",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")


@dataclass(frozen=True, slots=True)
class ToolSettings:
    """进程内 function calling 与工具管理器配置。

    默认关闭；所有执行预算都在这里集中管理，避免散落在 tool_manager 或 API 层。
    `ToolSettings()` 的默认值只用于库调用和测试，应用进程通过 `Settings.from_env()` 读取环境变量。
    """

    enabled: bool = False
    trigger: str = "auto"  # auto / always / off
    max_rounds: int = 2
    max_calls_per_round: int = 4
    max_calls_total: int = 6
    phase_timeout_s: float = 15.0
    timeout_s: float = 3.0
    max_retries: int = 1
    retry_backoff_s: float = 0.1
    circuit_failure_threshold: int = 5
    circuit_recovery_s: float = 60.0
    cache_ttl_s: float = 30.0
    cache_max_entries: int = 1024
    max_result_chars: int = 4000
    max_schema_hint_chars: int = 800
    rerank_enabled: bool = False
    rerank_top_k: int = 5
    stream_events: bool = False
    debug: bool = False
    protocol_fallback: bool = True

    def __post_init__(self) -> None:
        trigger = str(self.trigger).strip().lower()
        object.__setattr__(self, "trigger", trigger)
        if trigger not in {"auto", "always", "off"}:
            raise ValueError("COMMERCEMIND_TOOLS_TRIGGER 仅支持 auto、always、off")

        positive_ints = (
            "max_rounds",
            "max_calls_per_round",
            "max_calls_total",
            "circuit_failure_threshold",
            "cache_max_entries",
            "rerank_top_k",
        )
        for name in positive_ints:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")

        for name in ("max_retries",):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} 必须是非负整数")

        for name in ("phase_timeout_s", "timeout_s", "retry_backoff_s", "circuit_recovery_s", "cache_ttl_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} 必须是非负数")
        for name in ("phase_timeout_s", "timeout_s", "circuit_recovery_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")

        if self.timeout_s > self.phase_timeout_s:
            raise ValueError("单个工具超时不能大于工具阶段总预算")
        if self.max_calls_per_round > self.max_calls_total:
            raise ValueError("max_calls_per_round 不能大于 max_calls_total")
        if not 256 <= self.max_result_chars <= 20000:
            raise ValueError("max_result_chars 必须在 256 到 20000 之间")
        if not 128 <= self.max_schema_hint_chars <= 4000:
            raise ValueError("max_schema_hint_chars 必须在 128 到 4000 之间")

    @classmethod
    def from_env(cls) -> "ToolSettings":
        """从 `COMMERCEMIND_TOOLS_*` 构建配置；非法数值回退默认值。"""
        return cls(
            enabled=_env_bool("COMMERCEMIND_TOOLS_ENABLED", False),
            trigger=_env_choice(
                "COMMERCEMIND_TOOLS_TRIGGER", "auto", frozenset({"auto", "always", "off"}),
            ),
            max_rounds=_env_int("COMMERCEMIND_TOOLS_MAX_ROUNDS", 2),
            max_calls_per_round=_env_int("COMMERCEMIND_TOOLS_MAX_CALLS_PER_ROUND", 4),
            max_calls_total=_env_int("COMMERCEMIND_TOOLS_MAX_CALLS_TOTAL", 6),
            phase_timeout_s=_env_float("COMMERCEMIND_TOOLS_PHASE_TIMEOUT_S", 15.0),
            timeout_s=_env_float("COMMERCEMIND_TOOLS_TIMEOUT_S", 3.0),
            max_retries=_env_non_negative_int("COMMERCEMIND_TOOLS_MAX_RETRIES", 1),
            retry_backoff_s=_env_non_negative_float("COMMERCEMIND_TOOLS_RETRY_BACKOFF_S", 0.1),
            circuit_failure_threshold=_env_int("COMMERCEMIND_TOOLS_CIRCUIT_FAILURE_THRESHOLD", 5),
            circuit_recovery_s=_env_float("COMMERCEMIND_TOOLS_CIRCUIT_RECOVERY_S", 60.0),
            cache_ttl_s=_env_non_negative_float("COMMERCEMIND_TOOLS_CACHE_TTL_S", 30.0),
            cache_max_entries=_env_int("COMMERCEMIND_TOOLS_CACHE_MAX_ENTRIES", 1024),
            max_result_chars=_env_int("COMMERCEMIND_TOOLS_MAX_RESULT_CHARS", 4000),
            max_schema_hint_chars=_env_int("COMMERCEMIND_TOOLS_MAX_SCHEMA_HINT_CHARS", 800),
            rerank_enabled=_env_bool("COMMERCEMIND_TOOLS_RERANK_ENABLED", False),
            rerank_top_k=_env_int("COMMERCEMIND_TOOLS_RERANK_TOP_K", 5),
            stream_events=_env_bool("COMMERCEMIND_TOOLS_STREAM_EVENTS", False),
            debug=_env_bool("COMMERCEMIND_TOOLS_DEBUG", False),
            protocol_fallback=_env_bool("COMMERCEMIND_TOOLS_PROTOCOL_FALLBACK", True),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """运行期配置。

    对应环境变量：
    - ANTHROPIC_API_KEY
    - ANTHROPIC_BASE_URL（可选，兼容 DeepSeek 等 Anthropic 协议代理）
    - ANTHROPIC_MODEL
    - COMMERCEMIND_LLM_TIMEOUT_S
    - COMMERCEMIND_LLM_MAX_TOKENS
    """

    api_key: str = ""
    base_url: str | None = None
    model: str = DEFAULT_MODEL
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    memory: MemorySettings = field(default_factory=MemorySettings)
    knowledge: KnowledgeBaseSettings = field(default_factory=KnowledgeBaseSettings)
    intent: IntentSettings = field(default_factory=IntentSettings)
    skills: SkillSettings = field(default_factory=SkillSettings)
    tools: ToolSettings = field(default_factory=ToolSettings)

    def __post_init__(self) -> None:
        object.__setattr__(self, "api_key", self.api_key.strip())
        base_url = (self.base_url or "").strip()
        object.__setattr__(self, "base_url", base_url or None)
        model = (self.model or "").strip()
        object.__setattr__(self, "model", model or DEFAULT_MODEL)
        if self.timeout_s <= 0:
            raise ValueError("COMMERCEMIND_LLM_TIMEOUT_S 必须大于 0")
        if self.max_tokens <= 0:
            raise ValueError("COMMERCEMIND_LLM_MAX_TOKENS 必须大于 0")

    @property
    def model_configured(self) -> bool:
        """是否已配置模型 Key；模板占位符视为未配置。"""
        return bool(self.api_key) and self.api_key.lower() not in _PLACEHOLDER_KEYS

    @classmethod
    def from_env(cls) -> "Settings":
        """从当前进程环境变量构建配置。"""
        return cls(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
            model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
            timeout_s=_env_float("COMMERCEMIND_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S),
            max_tokens=_env_int("COMMERCEMIND_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            skills=SkillSettings(
                enabled=_env_bool("COMMERCEMIND_SKILLS_ENABLED", False),
                root_dir=os.getenv("COMMERCEMIND_SKILLS_ROOT_DIR", "skills"),
                catalog_max_chars=_env_int("COMMERCEMIND_SKILLS_CATALOG_MAX_CHARS", 4000),
                body_max_chars=_env_int("COMMERCEMIND_SKILLS_BODY_MAX_CHARS", 8192),
                resource_max_chars=_env_int("COMMERCEMIND_SKILLS_RESOURCE_MAX_CHARS", 20480),
                max_selected_skills=_env_int("COMMERCEMIND_SKILLS_MAX_SELECTED_SKILLS", 3),
                debug=_env_bool("COMMERCEMIND_SKILLS_DEBUG", False),
                upload_max_file_bytes=_env_int("COMMERCEMIND_SKILLS_UPLOAD_MAX_FILE_BYTES", 64 * 1024),
                upload_max_total_bytes=_env_int("COMMERCEMIND_SKILLS_UPLOAD_MAX_TOTAL_BYTES", 1024 * 1024),
                upload_max_files=_env_int("COMMERCEMIND_SKILLS_UPLOAD_MAX_FILES", 64),
            ),
            memory=MemorySettings(
                enabled=_env_bool("COMMERCEMIND_MEMORY_ENABLED", False),
                required=_env_bool("COMMERCEMIND_MEMORY_REQUIRED", False),
                redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                ttl_s=_env_int("COMMERCEMIND_MEMORY_TTL_SECONDS", DEFAULT_MEMORY_TTL_S),
                chroma_host=os.getenv("CHROMA_HOST", "localhost"),
                chroma_port=_env_int("CHROMA_PORT", 8001),
                debug=_env_bool("COMMERCEMIND_MEMORY_DEBUG", False),
            ),
            knowledge=KnowledgeBaseSettings(
                enabled=_env_bool("COMMERCEMIND_KNOWLEDGE_ENABLED", False),
                required=_env_bool("COMMERCEMIND_KNOWLEDGE_REQUIRED", False),
                chroma_host=os.getenv("CHROMA_HOST", "localhost"),
                chroma_port=_env_int("CHROMA_PORT", 8001),
                collection_name=os.getenv("COMMERCEMIND_KNOWLEDGE_COLLECTION", "knowledge_base"),
                chunk_size=_env_int("COMMERCEMIND_KNOWLEDGE_CHUNK_SIZE", 500),
                chunk_overlap=_env_non_negative_int("COMMERCEMIND_KNOWLEDGE_CHUNK_OVERLAP", 50),
                write_batch_size=_env_int("COMMERCEMIND_KNOWLEDGE_WRITE_BATCH_SIZE", 100),
            ),
            intent=IntentSettings(
                enabled=_env_bool("COMMERCEMIND_INTENT_ENABLED", False),
                llm_enabled=_env_bool("COMMERCEMIND_INTENT_LLM_ENABLED", True),
                embedding_enabled=_env_bool("COMMERCEMIND_INTENT_EMBEDDING_ENABLED", True),
                pattern_enabled=_env_bool("COMMERCEMIND_INTENT_PATTERN_ENABLED", True),
                llm_weight=_env_float("COMMERCEMIND_INTENT_LLM_WEIGHT", 0.70),
                embedding_weight=_env_float("COMMERCEMIND_INTENT_EMBEDDING_WEIGHT", 0.20),
                pattern_weight=_env_float("COMMERCEMIND_INTENT_PATTERN_WEIGHT", 0.10),
                confidence_threshold=_env_float("COMMERCEMIND_INTENT_CONFIDENCE_THRESHOLD", 0.35),
                embedding_threshold=_env_float("COMMERCEMIND_INTENT_EMBEDDING_THRESHOLD", 0.72),
                failure_policy=os.getenv("COMMERCEMIND_INTENT_FAILURE_POLICY", "available_sources").strip(),
            ),
            tools=ToolSettings.from_env(),
        )
