"""离线检查 working memory token 阈值：python -m tooling.memory_budget_report。"""
from __future__ import annotations

import json

from memory.budget import TokenBudget
from memory.tokens import TokenCounter


def build_report() -> dict:
    counter = TokenCounter()
    budget = TokenBudget.from_env()
    scenarios = {
        "短消息": [{"role": "user" if i % 2 == 0 else "assistant", "content": "你好，查询订单。"}
                 for i in range(20)],
        "长消息": [{"role": "user" if i % 2 == 0 else "assistant", "content": "请查询订单的物流状态和退款进度。" * 500}
                 for i in range(6)],
        "工具 JSON": [{"role": "tool", "content": {"order_id": "CM20261109001", "status": "已发货"}}],
    }
    samples = []
    for name, messages in scenarios.items():
        tokens = counter.count_working_messages(messages)
        samples.append({"sample": name, "message_count": len(messages), "working_tokens": tokens,
                        "should_compact": budget.should_compact(tokens)})
    return {
        "compact_trigger_tokens": budget.compact_trigger_tokens,
        "trigger_rule": "working_tokens >= compact_trigger_tokens (not message count)",
        "samples": samples,
    }


if __name__ == "__main__":
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
