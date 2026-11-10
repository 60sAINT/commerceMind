"""短期记忆数据模型：一轮一个 Turn，外加状态和摘要快照。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Turn:
    """一轮完整问答；Redis 中也只占一个 list item。"""

    request_id: str
    user_message: str
    assistant_message: str
    turn_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("request_id", "user_message", "assistant_message", "turn_id"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} 必须是字符串")
        if not self.user_message or not self.assistant_message:
            raise ValueError("user_message 和 assistant_message 不能为空")
        if not isinstance(self.created_at, datetime):
            raise TypeError("created_at 必须是 datetime")
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata 必须是字典")

    def to_json(self) -> str:
        return json.dumps({
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "user_message": self.user_message,
            "assistant_message": self.assistant_message,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> Turn:
        data = json.loads(raw)
        return cls(
            turn_id=data["turn_id"],
            request_id=data["request_id"],
            user_message=data["user_message"],
            assistant_message=data["assistant_message"],
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata") or {},
        )

    def to_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "user", "content": self.user_message},
            {"role": "assistant", "content": self.assistant_message},
        ]


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """一次读取得到的 working memory、state 和 summary。"""

    turns: tuple[Turn, ...] = ()
    state: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.turns, tuple):
            object.__setattr__(self, "turns", tuple(self.turns))
        if not isinstance(self.state, dict):
            raise TypeError("state 必须是字典")
        if not isinstance(self.summary, str):
            raise TypeError("summary 必须是字符串")

    def to_messages(self) -> list[dict[str, str]]:
        return [message for turn in self.turns for message in turn.to_messages()]
