# -*- coding: utf-8 -*-
# deepseek_codex_Repair / config — 环境变量配置加载与校验
#
# 非法值直接 ValueError（启动即失败，绝不静默降级）。
# load_settings 接受可注入 env 映射（默认 os.environ），便于测试。

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

VALID_ORPHAN_STRATEGIES = ("convert_to_user", "convert_to_developer", "remove")
VALID_MISSING_ID_STRATEGIES = ("synthesize", "convert_to_user")

_TRUE_VALUES = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8080
    upstream_url: str = "https://api.deepseek.com"
    proxy_api_key: str | None = None  # None/"" → 原样转发客户端 Authorization
    orphan_strategy: str = "convert_to_user"
    missing_id_strategy: str = "synthesize"
    synthetic_call_prefix: str = "call_proxy_"
    restore_reasoning: bool = True  # 回注缓存的 reasoning_text（DeepSeek 思考模式要求回传）
    reasoning_cache_file: str | None = None  # None → %TEMP%\deepseek_codex_reasoning_cache.json
    reasoning_cache_max_entries: int = 10000
    log_level: str = "INFO"
    debug_dump: bool = False
    max_body_bytes: int = 52_428_800  # 50 MiB
    timeout_connect: float = 10.0
    timeout_read: float = 600.0  # 需容纳长流式


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """从环境变量加载配置；非法值抛 ValueError 并列出合法选项。"""
    env = env if env is not None else os.environ

    def _get(name: str, default: str) -> str:
        val = env.get(name)
        return val if val not in (None, "") else default

    orphan = _get("ORPHAN_STRATEGY", "convert_to_user")
    if orphan not in VALID_ORPHAN_STRATEGIES:
        raise ValueError(
            f"ORPHAN_STRATEGY must be one of {VALID_ORPHAN_STRATEGIES}, got {orphan!r}"
        )

    missing = _get("MISSING_ID_STRATEGY", "synthesize")
    if missing not in VALID_MISSING_ID_STRATEGIES:
        raise ValueError(
            f"MISSING_ID_STRATEGY must be one of {VALID_MISSING_ID_STRATEGIES}, got {missing!r}"
        )

    try:
        port = int(_get("PROXY_PORT", "8080"))
    except ValueError:
        raise ValueError(f"PROXY_PORT must be an integer, got {_get('PROXY_PORT', '8080')!r}") from None

    try:
        max_body = int(_get("MAX_BODY_BYTES", "52428800"))
    except ValueError:
        raise ValueError(f"MAX_BODY_BYTES must be an integer, got {_get('MAX_BODY_BYTES', '52428800')!r}") from None

    try:
        timeout_connect = float(_get("TIMEOUT_CONNECT", "10"))
        timeout_read = float(_get("TIMEOUT_READ", "600"))
    except ValueError:
        raise ValueError(
            f"TIMEOUT_CONNECT/TIMEOUT_READ must be numbers, got "
            f"{_get('TIMEOUT_CONNECT', '10')!r}/{_get('TIMEOUT_READ', '600')!r}"
        ) from None

    api_key = _get("PROXY_API_KEY", "") or None
    debug_dump = _get("DEBUG_DUMP", "0").strip().lower() in _TRUE_VALUES

    try:
        cache_max = int(_get("REASONING_CACHE_MAX_ENTRIES", "10000"))
    except ValueError:
        raise ValueError(
            f"REASONING_CACHE_MAX_ENTRIES must be an integer, got "
            f"{_get('REASONING_CACHE_MAX_ENTRIES', '10000')!r}"
        ) from None

    restore_reasoning = _get("RESTORE_REASONING", "1").strip().lower() not in ("0", "false", "no", "off")

    return Settings(
        proxy_host=_get("PROXY_HOST", "127.0.0.1"),
        proxy_port=port,
        upstream_url=_get("UPSTREAM_URL", "https://api.deepseek.com"),
        proxy_api_key=api_key,
        orphan_strategy=orphan,
        missing_id_strategy=missing,
        synthetic_call_prefix=_get("SYNTHETIC_CALL_PREFIX", "call_proxy_"),
        restore_reasoning=restore_reasoning,
        reasoning_cache_file=_get("REASONING_CACHE_FILE", "") or None,
        reasoning_cache_max_entries=cache_max,
        log_level=_get("LOG_LEVEL", "INFO"),
        debug_dump=debug_dump,
        max_body_bytes=max_body,
        timeout_connect=timeout_connect,
        timeout_read=timeout_read,
    )
