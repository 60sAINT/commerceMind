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

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "user_message": self.user_message,
            "assistant_message": self.assistant_message,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(
            turn_id=data["turn_id"],
            request_id=data["request_id"],
            user_message=data["user_message"],
            assistant_message=data["assistant_message"],
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata") or {},
        )

    @classmethod
    def from_json(cls, raw: str) -> Turn:
        return cls.from_dict(json.loads(raw))

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


def memory_identity(value: str) -> str:
    """长期库仅保存稳定哈希身份，查询时使用同一转换。"""
    import hashlib
    return hashlib.sha256(str(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    user_key: str
    session_key: str
    turn_start: str
    turn_end: str
    turn_count: int
    summary: str
    created_at: str

    def __post_init__(self) -> None:
        if type(self.turn_count) is not int or self.turn_count <= 0:
            raise ValueError("turn_count 必须为正整数")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("episode 摘要不能为空")
        for name in ("episode_id", "user_key", "session_key", "turn_start", "turn_end"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError(f"{name} 必须是 SHA-256 标识")
        datetime.fromisoformat(self.created_at)

    @classmethod
    def from_turns(cls, user_id: str, session_id: str, turns: tuple[Turn, ...], summary: str) -> Episode:
        from memory.redaction import redact
        if not turns:
            raise ValueError("episode 不能没有轮次")
        user, session = memory_identity(user_id), memory_identity(session_id)
        start, end = memory_identity(turns[0].turn_id), memory_identity(turns[-1].turn_id)
        identity = json.dumps([user, session, [t.turn_id for t in turns]], separators=(",", ":"))
        return cls(memory_identity(identity), user, session, start, end, len(turns),
                   redact(summary).strip(), turns[-1].created_at.isoformat())

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        from memory.redaction import redact
        data = asdict(self)
        data["summary"] = redact(self.summary)
        return data
