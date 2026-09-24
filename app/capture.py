# -*- coding: utf-8 -*-
# deepseek_codex_Repair / capture — 从上游响应捕获 reasoning_text
#
# 两条路径:
#   capture_from_response_json() — 非流式: 解析响应对象 output[]，
#      reasoning item 的纯文本缓存到紧随其后的 function_call / message。
#   SseCapture — 流式: 增量解析 SSE 字节流（边转发边捕获，不缓冲整条流）,
#      tracking reasoning_text.delta / output_item.added / output_text.delta 等事件。
#
# 只缓存纯文本 reasoning_text；summary / encrypted_content 不支持回传，忽略。

from __future__ import annotations

import json
import logging

from app.reasoning_cache import ReasoningCache

logger = logging.getLogger("app.capture")

_CALL_TYPES = ("function_call", "custom_tool_call")


def _reasoning_item_text(item: dict) -> str | None:
    """提取 reasoning item 的纯文本（只取 reasoning_text parts）。"""
    content = item.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        p.get("text")
        for p in content
        if isinstance(p, dict) and p.get("type") == "reasoning_text" and p.get("text")
    ]
    return "\n".join(texts) if texts else None


def capture_from_response_json(data: dict, cache: ReasoningCache) -> int:
    """非流式响应捕获。返回缓存条数。"""
    output = data.get("output")
    if not isinstance(output, list):
        return 0
    pending = ""
    captured = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            text = _reasoning_item_text(item)
            if text:
                pending = f"{pending}\n{text}" if pending else text
        elif itype in _CALL_TYPES:
            if pending and item.get("call_id"):
                cache.put_call(item["call_id"], pending)
                captured += 1
                logger.debug(
                    "captured reasoning for call_id=%s (%d chars)",
                    item["call_id"], len(pending),
                )
            pending = ""
        elif itype == "message":
            if pending:
                cache.put_message(item.get("content"), pending)
                captured += 1
                logger.debug("captured reasoning for assistant message (%d chars)", len(pending))
            pending = ""
    return captured


class SseCapture:
    """流式 SSE 增量捕获：feed() 喂入原始字节块，事件完整时入库。"""

    def __init__(self, cache: ReasoningCache):
        self.cache = cache
        self.captured = 0
        self._buf = b""
        self._event: str | None = None
        self._data_lines: list[str] = []
        self._reasoning = ""     # 当前 reasoning 累计文本
        self._output_text = ""   # 当前 message 累计文本

    def feed(self, chunk: bytes) -> None:
        """喂入一块原始字节；不产生任何输出，不延迟转发。"""
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._feed_line(line)

    def _feed_line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        if not line:
            # 事件块结束（SSE 空行分隔）
            self._dispatch()
            self._event = None
            self._data_lines = []
            return
        if line.startswith(b":"):
            return  # 注释行
        if line.startswith(b"event:"):
            self._event = line[6:].strip().decode("utf-8", "replace")
        elif line.startswith(b"data:"):
            self._data_lines.append(line[5:].strip().decode("utf-8", "replace"))

    def _dispatch(self) -> None:
        if not self._event:
            return
        data = None
        if self._data_lines:
            try:
                data = json.loads("\n".join(self._data_lines))
            except json.JSONDecodeError:
                return
        ev = self._event
        if ev == "response.reasoning_text.delta":
            if isinstance(data, dict) and isinstance(data.get("delta"), str):
                self._reasoning += data["delta"]
        elif ev == "response.reasoning_text.done":
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                self._reasoning = data["text"]
        elif ev == "response.output_text.delta":
            if isinstance(data, dict) and isinstance(data.get("delta"), str):
                self._output_text += data["delta"]
        elif ev == "response.output_item.added":
            self._on_output_item_added(data)
        elif ev == "response.output_item.done":
            self._on_output_item_done(data)

    def _on_output_item_added(self, data) -> None:
        if not isinstance(data, dict):
            return
        item = data.get("item") or {}
        itype = item.get("type")
        if itype == "reasoning":
            self._reasoning = _reasoning_item_text(item) or ""
        elif itype == "message":
            self._output_text = ""
        elif itype in _CALL_TYPES:
            self._capture_call(item)

    def _on_output_item_done(self, data) -> None:
        if not isinstance(data, dict):
            return
        item = data.get("item") or {}
        itype = item.get("type")
        if itype == "message":
            content = item.get("content")
            # done 事件携带完整 item；若无 content 字段回退到 delta 累计
            if not content and self._output_text:
                content = self._output_text
            if self._reasoning and content:
                self.cache.put_message(content, self._reasoning)
                self.captured += 1
                logger.debug("stream captured reasoning for assistant message")
            self._reasoning = ""
            self._output_text = ""
        elif itype == "reasoning":
            # 兜底: delta 事件缺失时从完整 item 取
            text = _reasoning_item_text(item)
            if text and not self._reasoning:
                self._reasoning = text
        elif itype in _CALL_TYPES:
            self._capture_call(item)

    def _capture_call(self, item: dict) -> None:
        cid = item.get("call_id")
        if cid and self._reasoning:
            self.cache.put_call(cid, self._reasoning)
            self.captured += 1
            logger.debug("stream captured reasoning for call_id=%s", cid)
        self._reasoning = ""
