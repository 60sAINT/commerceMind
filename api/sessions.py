"""PostgreSQL persistence for user sessions, complete chat history, and idempotency."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class SessionRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "SessionRepository":
        import asyncpg

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        repo = cls(pool)
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    message_count BIGINT NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS sessions_user_updated
                    ON sessions(user_id, updated_at DESC, session_id DESC);
                CREATE TABLE IF NOT EXISTS session_messages (
                    message_id BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    client_request_id TEXT,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
                );
                CREATE INDEX IF NOT EXISTS session_messages_page
                    ON session_messages(user_id, session_id, message_id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS session_messages_idempotency
                    ON session_messages(user_id, session_id, client_request_id, role)
                    WHERE client_request_id IS NOT NULL;
            """)
        return repo

    async def close(self) -> None:
        await self.pool.close()

    async def create_session(self, user_id: str, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO sessions(session_id, user_id) VALUES($1, $2)
                RETURNING session_id, user_id, title, created_at, updated_at, message_count
            """, sid, user_id)
        return dict(row)

    async def list_sessions(self, user_id: str, limit: int, before: str | None) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            if before:
                timestamp, _, session_id = before.partition('|')
                cursor_time = datetime.fromisoformat(timestamp)
                rows = await conn.fetch("""
                    SELECT session_id, user_id, title, created_at, updated_at, message_count
                    FROM sessions WHERE user_id=$1 AND
                      (updated_at, session_id) < ($2::timestamptz, $3::text)
                    ORDER BY updated_at DESC, session_id DESC LIMIT $4
                """, user_id, cursor_time, session_id, limit)
            else:
                rows = await conn.fetch("""
                    SELECT session_id, user_id, title, created_at, updated_at, message_count
                    FROM sessions WHERE user_id=$1
                    ORDER BY updated_at DESC, session_id DESC LIMIT $2
                """, user_id, limit)
        return [dict(row) for row in rows]

    async def list_messages(self, user_id: str, session_id: str, limit: int, before: int | None) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT message_id, session_id, client_request_id, role, content, status, created_at, metadata
                FROM session_messages
                WHERE user_id=$1 AND session_id=$2 AND ($3::bigint IS NULL OR message_id < $3)
                ORDER BY message_id DESC LIMIT $4
            """, user_id, session_id, before, limit)
        items = [dict(row) for row in reversed(rows)]
        for item in items:
            if isinstance(item["metadata"], str):
                item["metadata"] = json.loads(item["metadata"])
        return items

    async def owns_session(self, user_id: str, session_id: str) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM sessions WHERE user_id=$1 AND session_id=$2)",
                user_id, session_id,
            ))

    async def get_receipt(self, user_id: str, session_id: str, client_request_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT u.content AS user_message, a.content AS assistant_message,
                       a.metadata->>'request_id' AS request_id
                FROM session_messages u JOIN session_messages a
                  ON a.session_id=u.session_id AND a.client_request_id=u.client_request_id
                 AND a.role='assistant' AND a.user_id=u.user_id
                WHERE u.user_id=$1 AND u.session_id=$2 AND u.client_request_id=$3 AND u.role='user'
            """, user_id, session_id, client_request_id)
        return dict(row) if row else None

    async def find_receipt(self, user_id: str, client_request_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT u.session_id, u.content AS user_message, a.content AS assistant_message,
                       a.metadata->>'request_id' AS request_id
                FROM session_messages u JOIN session_messages a
                  ON a.session_id=u.session_id AND a.client_request_id=u.client_request_id
                 AND a.role='assistant' AND a.user_id=u.user_id
                WHERE u.user_id=$1 AND u.client_request_id=$2 AND u.role='user'
                ORDER BY u.message_id DESC LIMIT 1
            """, user_id, client_request_id)
        return dict(row) if row else None

    async def commit_turn(self, user_id: str, session_id: str, client_request_id: str, request_id: str,
                          question: str, answer: str, state: dict[str, Any] | None = None) -> dict[str, str]:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("""
                INSERT INTO sessions(session_id, user_id) VALUES($1, $2)
                ON CONFLICT(session_id) DO NOTHING
            """, session_id, user_id)
            owner = await conn.fetchval("SELECT user_id FROM sessions WHERE session_id=$1 FOR UPDATE", session_id)
            if owner != user_id:
                raise PermissionError("session 不属于当前用户")
            if client_request_id:
                existing = await conn.fetchrow("""
                    SELECT content FROM session_messages
                    WHERE user_id=$1 AND session_id=$2 AND client_request_id=$3 AND role='user'
                """, user_id, session_id, client_request_id)
                if existing:
                    if existing["content"] != question:
                        raise ValueError("client_request_id_conflict")
                    receipt = await conn.fetchrow("""
                        SELECT content, metadata->>'request_id' AS request_id
                        FROM session_messages
                        WHERE user_id=$1 AND session_id=$2 AND client_request_id=$3 AND role='assistant'
                    """, user_id, session_id, client_request_id)
                    if receipt:
                        return {"response": receipt["content"], "request_id": receipt["request_id"]}
            await conn.execute("""
                INSERT INTO session_messages(session_id,user_id,client_request_id,role,content)
                VALUES($1,$2,$3,'user',$4)
            """, session_id, user_id, client_request_id or None, question)
            await conn.execute("""
                INSERT INTO session_messages(session_id,user_id,client_request_id,role,content,metadata)
                VALUES($1,$2,$3,'assistant',$4,$5::jsonb)
            """, session_id, user_id, client_request_id or None, answer,
                json.dumps({"request_id": request_id, "state": state or {}}, ensure_ascii=False))
            await conn.execute("""
                UPDATE sessions SET updated_at=now(), message_count=message_count+2,
                    title=CASE WHEN title='' THEN left($2, 80) ELSE title END
                WHERE session_id=$1 AND user_id=$3
            """, session_id, question, user_id)
            return {"response": answer, "request_id": request_id}

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE user_id=$1 AND session_id=$2", user_id, session_id,
            )
        return result.endswith("1")
