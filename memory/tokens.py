"""用本地 DeepSeek 官方 tokenizer 统计 working memory，不请求 API。

按随包固定的官方 chat_template 编码完整消息列表，无 generation prompt，
不额外叠加 4 tokens/message。不保证等于服务端账单；这是稳定的压缩尺度。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

_RESOURCE_DIR = Path(__file__).parent / "tokenizer" / "deepseek-v4"


@lru_cache(maxsize=1)
def _load_backend():
    # 延迟加载；依赖/资源缺失直接报错，不能悄悄退回字符估算。
    from jinja2 import StrictUndefined
    from jinja2.sandbox import ImmutableSandboxedEnvironment
    from tokenizers import Tokenizer

    config = json.loads((_RESOURCE_DIR / "tokenizer_config.json").read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(_RESOURCE_DIR / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    template = ImmutableSandboxedEnvironment(undefined=StrictUndefined).from_string(config["chat_template"])
    return tokenizer, template, config


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _normalize(message: Mapping[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role not in ("user", "assistant", "tool"):
        raise ValueError("working memory 只接受 user / assistant / tool，不包含 system / summary")
    content = message.get("content", "")
    calls = message.get("tool_calls")
    if calls:
        if role != "assistant" or content not in (None, ""):
            raise ValueError("官方模板仅支持 content 为空的 assistant tool_calls，不能静默丢弃字段")
        normalized_calls = []
        for call in calls:
            function = call["function"]
            arguments = function["arguments"]
            normalized_calls.append({
                "type": "function", "function": {
                    "name": function["name"],
                    "arguments": arguments if isinstance(arguments, str) else _json(arguments),
                },
            })
        return {"role": role, "content": None, "tool_calls": normalized_calls}
    if content is None:
        raise ValueError("非工具调用消息的 content 不能为 None")
    if not isinstance(content, str):
        # 字典/列表作为消息中的 JSON 文本；不是图片/音频原生 token 计费。
        content = _json(content)
    # 仅保留模型消息；忽略 Redis 的 ts/timestamp/metadata/request_id 等。
    return {"role": role, "content": content}


class TokenCounter:
    """一次加载本地资源；每次对完整快照编码，返回整数 token 数。"""

    def __init__(self) -> None:
        self._tokenizer, self._template, self._config = _load_backend()

    def _count_text(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)

    def count_text(self, text: str) -> int:
        """独立文本计数，用于情景记忆输出；不套 working memory 消息模板。"""
        return self._count_text(text)

    def truncate_text(self, text: str, max_tokens: int, *, prefix: str = "") -> str:
        """按字符边界缩短副本；计入前文及分隔符，返回前再次验证硬上限。"""
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("max_tokens 必须为正整数")
        if self.count_text(prefix + text) <= max_tokens:
            return text
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.count_text(prefix + text[:mid]) <= max_tokens:
                low = mid
            else:
                high = mid - 1
        result = text[:low]
        # BPE 的前缀计数未必单调；二分只找可用前缀，不保证最长。
        while result and self.count_text(prefix + result) > max_tokens:
            result = result[:-1]
        return result

    def count_working_messages(self, messages: Iterable[Mapping[str, Any]]) -> int:
        """输入 working memory 消息列表，输出压缩判断用的 token 数。"""
        normalized = [_normalize(message) for message in messages]
        if not normalized:
            return 0
        rendered = self._template.render(
            messages=normalized, add_generation_prompt=False,
            bos_token=self._config["bos_token"]["content"],
            eos_token=self._config["eos_token"]["content"],
        )
        return self._count_text(rendered)
