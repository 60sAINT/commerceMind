"""working memory 压缩触发阈值。

只保存阈值并判断是否触发，不执行摘要或截断；达到阈值后继续走
EchoMind 原有的 summary + 截断流程。阈值不是模型上下文上限。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_COMPACT_TRIGGER_TOKENS = 10_800
_ENV_KEY = "COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS"


def _positive_int(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} 必须是正整数")


@dataclass(frozen=True, slots=True)
class TokenBudget:
    compact_trigger_tokens: int = DEFAULT_COMPACT_TRIGGER_TOKENS

    def __post_init__(self) -> None:
        _positive_int("compact_trigger_tokens", self.compact_trigger_tokens)

    @classmethod
    def from_env(cls) -> TokenBudget:
        """读取 COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS，未设置时用默认值。"""
        raw = os.getenv(_ENV_KEY, "").strip()
        if not raw:
            return cls()
        try:
            return cls(compact_trigger_tokens=int(raw))
        except ValueError as exc:
            raise ValueError(f"{_ENV_KEY} 必须是正整数") from exc

    def should_compact(self, working_tokens: int) -> bool:
        """调用方传入 TokenCounter.count_working_messages(完整快照) 的结果。"""
        if type(working_tokens) is not int or working_tokens < 0:
            raise ValueError("working_tokens 必须是非负整数")
        return working_tokens >= self.compact_trigger_tokens
