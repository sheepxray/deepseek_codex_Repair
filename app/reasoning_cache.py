# -*- coding: utf-8 -*-
# deepseek_codex_Repair / reasoning_cache — reasoning_text 缓存
#
# DeepSeek V4 思考模式要求：上游返回的 reasoning_text 必须在后续轮次
# 原样回传（Responses API 无状态，客户端每轮重放完整历史）。
# 客户端（Codex 等）重放时会丢弃 reasoning 块 → 400
# "The reasoning_text in the thinking mode must be passed back to the API"。
#
# 本模块缓存上游捕获的 reasoning_text，两个键：
#   c:<call_id>       — function_call / custom_tool_call 的 call_id
#   m:<content_hash>  — assistant 终答消息内容（sha1）
# 支持 JSON 文件持久化（代理重启后缓存不丢）。

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("app.reasoning")


def message_content_text(content) -> str | None:
    """message item 的 content 归一化为文本。

    支持: 字符串直接取；parts 数组取 input_text/output_text 的 text 用换行拼接。
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        texts = [
            p.get("text")
            for p in content
            if isinstance(p, dict)
            and p.get("type") in ("input_text", "output_text")
            and p.get("text")
        ]
        return "\n".join(texts) if texts else None
    return None


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class ReasoningCache:
    """reasoning_text 内存缓存 + JSON 文件持久化（LRU 超限丢最旧）。"""

    def __init__(
        self,
        file_path: str | Path | None = None,
        max_entries: int = 10000,
    ):
        self.file_path = Path(file_path) if file_path else None
        self.max_entries = max_entries
        self._data: dict[str, str] = {}
        if self.file_path and self.file_path.exists():
            try:
                self._data = json.loads(self.file_path.read_text(encoding="utf-8"))
                if isinstance(self._data, dict):
                    logger.info(
                        "reasoning cache loaded: %d entries from %s",
                        len(self._data), self.file_path,
                    )
                else:
                    self._data = {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("failed to load reasoning cache %s: %s", self.file_path, exc)

    # ---- call_id 键 ----

    def put_call(self, call_id: str, text: str) -> None:
        if not call_id or not text or not text.strip():
            return
        self._put(f"c:{call_id}", text)

    def get_call(self, call_id: str) -> str | None:
        return self._data.get(f"c:{call_id}")

    # ---- assistant 消息内容哈希键 ----

    def put_message(self, content, text: str) -> None:
        ctext = message_content_text(content)
        if not ctext or not text or not text.strip():
            return
        self._put(f"m:{content_hash(ctext)}", text)

    def get_message(self, content) -> str | None:
        ctext = message_content_text(content)
        if not ctext:
            return None
        return self._data.get(f"m:{content_hash(ctext)}")

    # ---- 内部 ----

    def _put(self, key: str, value: str) -> None:
        self._data[key] = value
        while len(self._data) > self.max_entries:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self.save()

    def save(self) -> None:
        if not self.file_path:
            return
        try:
            tmp = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.file_path)
        except OSError as exc:
            logger.warning("failed to save reasoning cache: %s", exc)
