# -*- coding: utf-8 -*-
"""ReasoningCache 单测：存取、哈希键、持久化、超限淘汰。"""
from __future__ import annotations

from app.reasoning_cache import ReasoningCache, content_hash, message_content_text


def test_call_id_roundtrip():
    c = ReasoningCache()
    c.put_call("call_1", "thinking about weather")
    assert c.get_call("call_1") == "thinking about weather"
    assert c.get_call("call_unknown") is None


def test_empty_values_ignored():
    c = ReasoningCache()
    c.put_call("", "text")
    c.put_call("call_1", "")
    c.put_call("call_2", "   ")
    assert c.get_call("call_1") is None
    assert c.get_call("call_2") is None


def test_message_content_hash_roundtrip():
    c = ReasoningCache()
    c.put_message([{"type": "output_text", "text": "答案是 42"}], "reasoning...")
    # 相同内容（即使 parts 形状略有不同）命中同一哈希
    assert c.get_message("答案是 42") == "reasoning..."
    assert c.get_message([{"type": "output_text", "text": "答案是 42"}]) == "reasoning..."
    assert c.get_message("别的问题") is None


def test_message_content_text_normalization():
    assert message_content_text("hi") == "hi"
    assert message_content_text(None) is None
    assert message_content_text("") is None
    parts = [
        {"type": "output_text", "text": "第一行"},
        {"type": "output_text", "text": "第二行"},
    ]
    assert message_content_text(parts) == "第一行\n第二行"
    # 非文本 part 忽略
    assert message_content_text([{"type": "image", "image_url": "x"}]) is None


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    c1 = ReasoningCache(file_path=path)
    c1.put_call("call_1", "thinking 1")
    assert path.exists()

    c2 = ReasoningCache(file_path=path)
    assert c2.get_call("call_1") == "thinking 1"


def test_corrupt_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    c = ReasoningCache(file_path=path)
    assert c.get_call("call_1") is None


def test_eviction_when_over_max_entries():
    c = ReasoningCache(max_entries=2)
    c.put_call("a", "1")
    c.put_call("b", "2")
    c.put_call("c", "3")
    assert c.get_call("a") is None  # 最旧被淘汰
    assert c.get_call("b") == "2"
    assert c.get_call("c") == "3"


def test_content_hash_stable():
    assert content_hash("hello") == content_hash("hello")
    assert len(content_hash("hello")) == 40
