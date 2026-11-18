"""SSE 测试辅助：解析 /chat/stream 的事件流并汇总最终文本。"""
from __future__ import annotations

import json
from typing import Any

import httpx


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把 `event:` / `data:` 文本块解析成 (event, payload) 列表。"""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        payload: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                raw = line[len("data:"):].strip()
                payload = json.loads(raw) if raw else {}
        if event:
            events.append((event, payload))
    return events


class StreamResult:
    """把流式响应包装成便于断言的只读结果。"""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.status_code = response.status_code
        # 只有 200 才是事件流；参数校验或前置错误仍是 JSON。
        self.events = parse_sse(response.text) if response.status_code == 200 else []
        self.meta = next((data for event, data in self.events if event == "meta"), {})
        self.done = next((data for event, data in self.events if event == "done"), {})
        self.error = next((data for event, data in self.events if event == "error"), None)
        self.text = "".join(
            data.get("text", "") for event, data in self.events if event == "delta"
        )
        self.session_id = self.meta.get("session_id") or self.done.get("session_id")
        self.request_id = self.done.get("request_id") or self.meta.get("request_id")

    def json(self) -> Any:
        """前置错误（4xx/5xx）的 JSON 响应。"""
        return self.response.json()


async def chat_stream(app, body: dict[str, Any], **kwargs: Any) -> StreamResult:
    """发送一次 /chat/stream 请求。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/chat/stream", json=body, **kwargs)
    return StreamResult(response)
