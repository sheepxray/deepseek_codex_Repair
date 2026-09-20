# -*- coding: utf-8 -*-
# deepseek_codex_Repair / debug_dump — 调试落盘
#
# DEBUG_DUMP=1 时把修复前原始请求、修复后请求、修复报告写入
# %TEMP%\deepseek_codex_repair_dump_<时间戳>.json（各部分截断 200KB 防巨型文件）。

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("app.debug")

MAX_PART_CHARS = 200_000


def save_debug_dump(
    raw_body: bytes,
    repaired_body,
    report,
    directory: Path | None = None,
) -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    target_dir = Path(directory) if directory is not None else Path(tempfile.gettempdir())
    path = target_dir / f"deepseek_codex_repair_dump_{ts}.json"

    try:
        original = json.loads(raw_body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        original = {"_error": "unparseable original body"}

    def _serialize(part) -> str:
        text = json.dumps(part, ensure_ascii=False, indent=2, default=str)
        if len(text) > MAX_PART_CHARS:
            return text[:MAX_PART_CHARS] + "\n... (truncated)"
        return text

    dump = {
        "original": _serialize(original),
        "repaired": _serialize(repaired_body),
        "report": [
            {"rule": e.rule, "index": e.index, "detail": e.detail}
            for e in report.entries
        ],
    }
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("debug dump saved to %s", path)
    return path
