# -*- coding: utf-8 -*-
"""捕获模块单测：非流式 output 遍历 + 流式 SSE 增量解析。"""
from __future__ import annotations

import json

from app.capture import SseCapture, capture_from_response_json
from app.reasoning_cache import ReasoningCache


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


# ---- 非流式 ----


def test_capture_non_stream_reasoning_before_function_call():
    cache = ReasoningCache()
    data = {
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "先查天气"}]},
            {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}"},
        ]
    }
    n = capture_from_response_json(data, cache)
    assert n == 1
    assert cache.get_call("call_1") == "先查天气"


def test_capture_non_stream_reasoning_before_message():
    cache = ReasoningCache()
    data = {
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "直接回答"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "答案是 42"}]},
        ]
    }
    n = capture_from_response_json(data, cache)
    assert n == 1
    assert cache.get_message([{"type": "output_text", "text": "答案是 42"}]) == "直接回答"


def test_capture_non_stream_ignores_summary_and_encrypted():
    cache = ReasoningCache()
    data = {
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "摘要"}],
             "encrypted_content": "xxx"},
            {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": "{}"},
        ]
    }
    n = capture_from_response_json(data, cache)
    assert n == 0  # 只有 summary/encrypted → 不缓存
    assert cache.get_call("call_1") is None


def test_capture_non_stream_no_output_field():
    assert capture_from_response_json({"id": "x"}, ReasoningCache()) == 0


def test_capture_multiple_reasoning_items_concatenated():
    cache = ReasoningCache()
    data = {
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "第一步"}]},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "第二步"}]},
            {"type": "function_call", "call_id": "call_2", "name": "f", "arguments": "{}"},
        ]
    }
    capture_from_response_json(data, cache)
    assert cache.get_call("call_2") == "第一步\n第二步"


# ---- 流式 ----


def _feed(capture: SseCapture, chunks: list[bytes]) -> None:
    for chunk in chunks:
        capture.feed(chunk)


def test_sse_capture_function_call():
    cache = ReasoningCache()
    cap = SseCapture(cache)
    _feed(cap, [
        _sse_event("response.created", {"type": "response.created"}),
        _sse_event("response.output_item.added",
                   {"item": {"type": "reasoning", "content": []}}),
        _sse_event("response.reasoning_text.delta", {"delta": "先查"}),
        _sse_event("response.reasoning_text.delta", {"delta": "天气"}),
        _sse_event("response.reasoning_text.done", {"text": "先查天气"}),
        _sse_event("response.output_item.added",
                   {"item": {"type": "function_call", "call_id": "call_1", "name": "get_weather"}}),
    ])
    assert cache.get_call("call_1") == "先查天气"
    assert cap.captured == 1


def test_sse_capture_bytes_split_mid_line():
    cache = ReasoningCache()
    cap = SseCapture(cache)
    # 单字节喂入：行边界/事件边界全被切开也不丢数据
    stream = b"".join([
        _sse_event("response.output_item.added", {"item": {"type": "reasoning", "content": []}}),
        _sse_event("response.reasoning_text.delta", {"delta": "想想"}),
        _sse_event("response.output_item.added",
                   {"item": {"type": "function_call", "call_id": "call_9", "name": "f"}}),
    ])
    for i in range(len(stream)):
        cap.feed(stream[i:i + 1])
    assert cache.get_call("call_9") == "想想"


def test_sse_capture_message_with_output_text_delta():
    cache = ReasoningCache()
    cap = SseCapture(cache)
    _feed(cap, [
        _sse_event("response.output_item.added", {"item": {"type": "reasoning", "content": []}}),
        _sse_event("response.reasoning_text.delta", {"delta": "算一下"}),
        _sse_event("response.output_item.added",
                   {"item": {"type": "message", "role": "assistant", "content": []}}),
        _sse_event("response.output_text.delta", {"delta": "42"}),
        _sse_event("response.output_item.done",
                   {"item": {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "42"}]}}),
    ])
    assert cache.get_message([{"type": "output_text", "text": "42"}]) == "算一下"


def test_sse_capture_comment_and_invalid_json_ignored():
    cache = ReasoningCache()
    cap = SseCapture(cache)
    cap.feed(b": keep-alive\n\n")
    cap.feed(b"event: response.reasoning_text.delta\ndata: {broken\n\n")
    cap.feed(_sse_event("response.reasoning_text.delta", {"delta": "ok"}))
    cap.feed(_sse_event("response.output_item.added",
                        {"item": {"type": "function_call", "call_id": "call_x", "name": "f"}}))
    assert cache.get_call("call_x") == "ok"


def test_sse_capture_custom_tool_call():
    cache = ReasoningCache()
    cap = SseCapture(cache)
    _feed(cap, [
        _sse_event("response.reasoning_text.delta", {"delta": "补丁思路"}),
        _sse_event("response.output_item.added",
                   {"item": {"type": "custom_tool_call", "call_id": "cc_1", "name": "apply_patch"}}),
    ])
    assert cache.get_call("cc_1") == "补丁思路"
