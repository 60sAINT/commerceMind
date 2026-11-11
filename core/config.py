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
    intent: IntentSettings = field(default_factory=IntentSettings)

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
            memory=MemorySettings(
                enabled=_env_bool("COMMERCEMIND_MEMORY_ENABLED", False),
                required=_env_bool("COMMERCEMIND_MEMORY_REQUIRED", False),
                redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                ttl_s=_env_int("COMMERCEMIND_MEMORY_TTL_SECONDS", DEFAULT_MEMORY_TTL_S),
                chroma_host=os.getenv("CHROMA_HOST", "localhost"),
                chroma_port=_env_int("CHROMA_PORT", 8001),
                debug=_env_bool("COMMERCEMIND_MEMORY_DEBUG", False),
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
        )
