"""应用配置：从环境变量读取并校验模型配置。

设计原则：
- 启动时不强制要求 API Key，缺失时由 `/health` 明确报告，方便先启动服务再补配置。
- 所有配置集中在这里，避免在业务代码中散落 `os.getenv`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_TOKENS = 1024
_PLACEHOLDER_KEYS = frozenset({"xxx", "your-key", "your_api_key", "replace-me", "changeme"})


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
        )
