# -*- coding: utf-8 -*-
"""集成测试：ASGITransport 打 FastAPI 应用 + MockTransport fake upstream。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings


def _fc(call_id="call_1"):
    return {"type": "function_call", "name": "get_weather", "arguments": "{}", "call_id": call_id}


def _fco(call_id="call_1", output="ok"):
    item = {"type": "function_call_output", "output": output}
    if call_id is not None:
        item["call_id"] = call_id
    return item


# ---- 修复 E2E ----


async def test_non_stream_repair_e2e(harness_factory):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resp_1"}, headers={"content-type": "application/json"})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post("/v1/responses", json={
            "model": "deepseek-chat",
            "input": [
                _fc("call_1"),
                _fco("call_dead", "sunny"),   # 孤立输出 → user 消息
                _fco(None, "second"),         # 缺 call_id → 采用前导 fc
            ],
        })

    assert resp.status_code == 200
    assert resp.json() == {"id": "resp_1"}  # 上游响应体原样透传
    assert resp.headers["x-proxy-repairs"] == "2"

    upstream_input = captured["body"]["input"]
    assert upstream_input[0] == _fc("call_1")
    assert upstream_input[1] == {
        "type": "message", "role": "user",
        "content": [{"type": "output_text", "text": "sunny"}],
    }
    assert upstream_input[2] == {**_fco("call_1", "second")}  # call_id 已补齐
    # 其余字段原样保留
    assert captured["body"]["model"] == "deepseek-chat"


async def test_streaming_passthrough_byte_identical(harness_factory):
    sse_bytes = b'data: {"delta":"hi"}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        # 用 ByteStream 模拟真实网络流（content= 构造的响应已消费，无法再次流式读取）
        return httpx.Response(
            200, stream=httpx.ByteStream(sse_bytes),
            headers={"content-type": "text/event-stream"},
        )

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post("/v1/responses", json={
            "model": "m",
            "input": [_fco("call_9", "x")],  # 孤立 → 触发修复
            "stream": True,
        })

    assert resp.status_code == 200
    assert resp.content == sse_bytes  # 字节级一致
    assert resp.headers["content-type"] == "text/event-stream"
    assert resp.headers["x-proxy-repairs"] == "1"


# ---- 错误映射 ----


async def test_upstream_down_returns_502(harness_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post("/v1/responses", json={"model": "m", "input": []})
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["error"]["message"]


async def test_upstream_timeout_returns_504(harness_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post("/v1/responses", json={"model": "m", "input": []})
    assert resp.status_code == 504


# ---- 透传 ----


async def test_models_endpoint_passthrough(harness_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/v1/models")
        return httpx.Response(200, json={"data": []}, headers={"content-type": "application/json"})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json() == {"data": []}
    assert "x-proxy-repairs" not in resp.headers


async def test_unknown_path_passthrough_verbatim(harness_factory):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = request.content
        return httpx.Response(200, content=b"pong", headers={"content-type": "text/plain"})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post("/whatever/custom", content=b"raw-body")
    assert resp.status_code == 200
    assert resp.content == b"pong"
    assert captured["method"] == "POST"
    assert captured["body"] == b"raw-body"
    assert "x-proxy-repairs" not in resp.headers


# ---- 请求校验 ----


async def test_invalid_json_returns_400_not_forwarded(harness_factory):
    h = harness_factory(Settings(), None)
    async with h.client() as client:
        resp = await client.post(
            "/v1/responses", content=b"{bad json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 400
    assert h.upstream_requests == []  # 未转发


async def test_oversized_body_returns_413(harness_factory):
    h = harness_factory(Settings(max_body_bytes=10), None)
    async with h.client() as client:
        resp = await client.post("/v1/responses", json={"model": "m", "input": [1, 2, 3, 4, 5]})
    assert resp.status_code == 413
    assert h.upstream_requests == []


# ---- API key ----


async def test_proxy_api_key_overrides_client_key(harness_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-proxy-key"
        return httpx.Response(200, json={"ok": True})

    h = harness_factory(Settings(proxy_api_key="sk-proxy-key"), handler)
    async with h.client() as client:
        resp = await client.post(
            "/v1/responses", json={"model": "m", "input": []},
            headers={"authorization": "Bearer sk-client-key"},
        )
    assert resp.status_code == 200


async def test_client_key_forwarded_when_no_proxy_key(harness_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-client-key"
        return httpx.Response(200, json={"ok": True})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post(
            "/v1/responses", json={"model": "m", "input": []},
            headers={"authorization": "Bearer sk-client-key"},
        )
    assert resp.status_code == 200


# ---- 细节行为 ----


async def test_query_string_preserved(harness_factory):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        await client.post("/v1/responses?foo=bar", json={"model": "m", "input": []})
    assert captured["url"].endswith("/v1/responses?foo=bar")


async def test_body_without_input_array_untouched(harness_factory):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        resp = await client.post("/v1/responses", json={"model": "m", "instructions": "hi"})
    assert resp.status_code == 200
    assert captured["body"] == {"model": "m", "instructions": "hi"}  # 原样转发
    assert "x-proxy-repairs" not in resp.headers


async def test_healthz_no_upstream_call(harness_factory):
    h = harness_factory(Settings(upstream_url="https://example.com"), None)
    async with h.client() as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "upstream": "https://example.com"}
    assert h.upstream_requests == []
