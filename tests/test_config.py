# -*- coding: utf-8 -*-
"""load_settings 环境变量解析与校验测试。"""
from __future__ import annotations

import pytest

from app.config import Settings, load_settings


def test_defaults_with_empty_env():
    s = load_settings({})
    assert s == Settings()


def test_full_override():
    s = load_settings({
        "PROXY_HOST": "0.0.0.0",
        "PROXY_PORT": "9999",
        "UPSTREAM_URL": "https://example.com/api/",
        "PROXY_API_KEY": "sk-proxy-key",
        "ORPHAN_STRATEGY": "remove",
        "MISSING_ID_STRATEGY": "convert_to_user",
        "SYNTHETIC_CALL_PREFIX": "call_fix_",
        "LOG_LEVEL": "DEBUG",
        "DEBUG_DUMP": "1",
        "MAX_BODY_BYTES": "1024",
        "TIMEOUT_CONNECT": "2.5",
        "TIMEOUT_READ": "30",
    })
    assert s.proxy_host == "0.0.0.0"
    assert s.proxy_port == 9999
    assert s.upstream_url == "https://example.com/api/"
    assert s.proxy_api_key == "sk-proxy-key"
    assert s.orphan_strategy == "remove"
    assert s.missing_id_strategy == "convert_to_user"
    assert s.synthetic_call_prefix == "call_fix_"
    assert s.log_level == "DEBUG"
    assert s.debug_dump is True
    assert s.max_body_bytes == 1024
    assert s.timeout_connect == 2.5
    assert s.timeout_read == 30.0


def test_empty_api_key_becomes_none():
    assert load_settings({"PROXY_API_KEY": ""}).proxy_api_key is None


@pytest.mark.parametrize("value", ["bogus", "CONVERT_TO_USER"])
def test_invalid_orphan_strategy_raises(value):
    # 注意: 空字符串回退默认值（_get 语义），不属于非法值
    with pytest.raises(ValueError, match="ORPHAN_STRATEGY"):
        load_settings({"ORPHAN_STRATEGY": value})


@pytest.mark.parametrize("value", ["bogus", "SYNTHESIZE"])
def test_invalid_missing_id_strategy_raises(value):
    with pytest.raises(ValueError, match="MISSING_ID_STRATEGY"):
        load_settings({"MISSING_ID_STRATEGY": value})


def test_invalid_port_raises():
    with pytest.raises(ValueError, match="PROXY_PORT"):
        load_settings({"PROXY_PORT": "not-a-port"})


def test_invalid_max_body_raises():
    with pytest.raises(ValueError, match="MAX_BODY_BYTES"):
        load_settings({"MAX_BODY_BYTES": "huge"})


def test_invalid_timeout_raises():
    with pytest.raises(ValueError, match="TIMEOUT_CONNECT"):
        load_settings({"TIMEOUT_CONNECT": "soon"})


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("TRUE", True),
    ("0", False), ("false", False), ("", False), ("", False),
])
def test_debug_dump_truthy_variants(value, expected):
    s = load_settings({"DEBUG_DUMP": value})
    assert s.debug_dump is expected
