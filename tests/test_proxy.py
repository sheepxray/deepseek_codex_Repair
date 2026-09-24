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


# ---- reasoning_text 回传修复（DeepSeek 思考模式） ----


_REASONING_RESPONSE = {
    "id": "resp_1",
    "output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "先查天气再回答"}]},
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}"},
    ],
}


def _replay_input():
    """模拟客户端第二轮回放：function_call + output，但丢了 reasoning 块。"""
    return [
        {"type": "message", "role": "user", "content": "北京天气如何"},
        _fc("call_1"),
        _fco("call_1", "晴"),
    ]


async def test_reasoning_restore_e2e_non_stream(harness_factory):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        # 第一轮：上游返回 reasoning + function_call（代理应捕获）
        return httpx.Response(200, json=_REASONING_RESPONSE, headers={"content-type": "application/json"})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        # 第一轮（触发捕获）
        r1 = await client.post("/v1/responses", json={"model": "m", "input": "北京天气如何"})
        assert r1.status_code == 200
        assert "x-proxy-repairs" not in r1.headers

        # 第二轮：客户端重放历史但丢了 reasoning → 代理应回注
        r2 = await client.post("/v1/responses", json={"model": "m", "input": _replay_input()})
        assert r2.status_code == 200
        assert r2.headers["x-proxy-repairs"] == "1"

    upstream_input = captured[1]["input"]
    assert upstream_input[0] == {"type": "message", "role": "user", "content": "北京天气如何"}
    assert upstream_input[1]["type"] == "reasoning"
    assert upstream_input[1]["content"] == [{"type": "reasoning_text", "text": "先查天气再回答"}]
    assert upstream_input[2] == _fc("call_1")
    assert upstream_input[3] == _fco("call_1", "晴")


async def test_reasoning_restore_e2e_streaming_capture(harness_factory):
    sse_stream = b"".join([
        b'event: response.output_item.added\ndata: {"item":{"type":"reasoning","content":[]}}\n\n',
        b'event: response.reasoning_text.delta\ndata: {"delta":"stream thinking"}\n\n',
        b'event: response.output_item.added\n'
        b'data: {"item":{"type":"function_call","call_id":"call_1","name":"get_weather"}}\n\n',
    ])
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        # 第一轮流式返回，第二轮普通返回
        if len(captured) == 1:
            return httpx.Response(
                200, stream=httpx.ByteStream(sse_stream),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json={"id": "resp_2"}, headers={"content-type": "application/json"})

    h = harness_factory(Settings(), handler)
    async with h.client() as client:
        r1 = await client.post("/v1/responses", json={"model": "m", "input": "q", "stream": True})
        assert r1.status_code == 200

        r2 = await client.post("/v1/responses", json={"model": "m", "input": _replay_input()})
        assert r2.status_code == 200
        assert r2.headers["x-proxy-repairs"] == "1"

    upstream_input = captured[1]["input"]
    assert upstream_input[0] == {"type": "message", "role": "user", "content": "北京天气如何"}
    assert upstream_input[1]["type"] == "reasoning"
    assert upstream_input[1]["content"] == [{"type": "reasoning_text", "text": "stream thinking"}]


async def test_reasoning_restore_disabled_by_settings(harness_factory):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_REASONING_RESPONSE, headers={"content-type": "application/json"})

    h = harness_factory(Settings(restore_reasoning=False), handler)
    async with h.client() as client:
        await client.post("/v1/responses", json={"model": "m", "input": "q"})
        r2 = await client.post("/v1/responses", json={"model": "m", "input": _replay_input()})
        assert r2.status_code == 200
        assert "x-proxy-repairs" not in r2.headers  # 不修复

    # 上游收到的第二轮 input 没有注入 reasoning
    assert captured[1]["input"][0]["type"] == "message"
