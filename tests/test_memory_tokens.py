"""使用随包官方资源离线验证；不依赖模型、Redis 或网络。"""
import json

import pytest

from memory.tokens import TokenCounter


@pytest.fixture
def counter():
    return TokenCounter()


def test_chat_format_and_no_double_overhead(counter):
    messages = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "您好"}]
    rendered = "<｜begin▁of▁sentence｜><｜User｜>你好<｜Assistant｜>您好<｜end▁of▁sentence｜>"
    assert counter.count_working_messages(messages) == counter._count_text(rendered)


def test_empty_working_memory_is_zero(counter):
    assert counter.count_working_messages([]) == 0


def test_storage_metadata_does_not_count(counter):
    original = [{"role": "user", "content": "订单123"}]
    enriched = [{**original[0], "ts": "2026-11-09", "metadata": {"request_id": "x" * 1000}}]
    assert counter.count_working_messages(original) == counter.count_working_messages(enriched)


def test_json_content_counted_as_text(counter):
    data = {"订单": ["已发货", 123], "ok": True}
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    as_json = [{"role": "tool", "content": data}]
    as_text = [{"role": "tool", "content": text}]
    assert counter.count_working_messages(as_json) == counter.count_working_messages(as_text)


def test_tool_call_and_result_use_official_template(counter):
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"type": "function", "function": {"name": "lookup_order", "arguments": '{"id":123}'}}]},
        {"role": "tool", "content": "已发货"},
        {"role": "assistant", "content": "您的订单已发货。"},
    ]
    template, config = counter._template, counter._config
    rendered = template.render(messages=messages, add_generation_prompt=False,
                               bos_token=config["bos_token"]["content"])
    assert counter.count_working_messages(messages) == counter._count_text(rendered)
    assert counter.count_working_messages(messages) > counter._count_text("您的订单已发货。")


@pytest.mark.parametrize("messages", [
    [{"role": "system", "content": "不应进入 working memory"}],
    [{"role": "summary", "content": "旧摘要"}],
    [{"role": "assistant", "content": None}],
    [{"role": "assistant", "content": "不能丢弃", "tool_calls": [{"function": {"name": "x", "arguments": "{}"}}]}],
])
def test_reject_unsupported_messages(counter, messages):
    with pytest.raises(ValueError):
        counter.count_working_messages(messages)


def test_backend_loaded_once(counter):
    assert counter._tokenizer is TokenCounter()._tokenizer
