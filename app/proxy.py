# -*- coding: utf-8 -*-
# deepseek_codex_Repair / proxy — httpx 上游客户端与转发
#
# 职责:
#   - AsyncClient 生命周期（lifespan 创建/关闭）
#   - 客户端 header 过滤（剥 hop-by-hop、强制 identity、api key 可覆盖）
#   - forward(): 自动选择中继方式 —
#       上游 content-type 含 text/event-stream → aiter_raw() 原始字节流式透传
#       否则缓冲整个响应体后返回
#   - 错误映射: ConnectError/ConnectTimeout → 502, Timeout → 504

from __future__ import annotations

import json
import logging

import anyio
import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.capture import SseCapture, capture_from_response_json
from app.reasoning_cache import ReasoningCache

logger = logging.getLogger("app.proxy")

HOP_BY_HOP_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding",
    "upgrade", "keep-alive", "te", "trailer",
}


def create_client(settings) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=settings.timeout_connect,
        read=settings.timeout_read,
        write=30.0,
        pool=10.0,
    )
    return httpx.AsyncClient(follow_redirects=True, timeout=timeout)


def build_upstream_url(settings, path: str, query: str | None) -> str:
    """上游 URL = 基址 + 原路径 + 原查询串。"""
    base = settings.upstream_url.rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    if query:
        url += f"?{query}"
    return url


def filter_client_headers(request: Request, settings) -> dict[str, str]:
    """复制客户端 header，剥 hop-by-hop；PROXY_API_KEY 设置时覆盖 Authorization。"""
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower.startswith("proxy-"):
            continue
        headers[name] = value
    # 关键: 强制 identity，避免上游 gzip 后中继压缩字节配上错误的 Content-Encoding
    headers["accept-encoding"] = "identity"
    if settings.proxy_api_key:
        headers["authorization"] = f"Bearer {settings.proxy_api_key}"
    return headers


def passthrough_response_headers(resp: httpx.Response, streaming: bool) -> dict[str, str]:
    """从上游响应挑出可透传的头；剥 hop-by-hop 与 content-length/encoding。"""
    headers: dict[str, str] = {}
    for name, value in resp.headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in ("content-length", "content-encoding"):
            continue
        headers[name] = value
    if streaming:
        headers["cache-control"] = "no-cache"
    return headers


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message}}, status_code=status)


async def forward(
    client: httpx.AsyncClient,
    upstream_req: httpx.Request,
    repairs_count: int = 0,
    cache: ReasoningCache | None = None,
):
    """转发请求；连接期错误在此抛出前映射为 JSON 错误响应。

    repairs_count > 0 时附加 X-Proxy-Repairs 响应头（count=0 不加）。
    cache 非 None 时从上游响应捕获 reasoning_text（流式边转发边捕获）。
    """
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError as exc:
        logger.error("upstream unreachable: %s", exc)
        return error_response(502, f"upstream unreachable: {exc}")
    except httpx.ConnectTimeout as exc:
        logger.error("upstream connect timeout: %s", exc)
        return error_response(502, f"upstream connect timeout: {exc}")
    except httpx.TimeoutException as exc:
        logger.error("upstream timeout: %s", exc)
        return error_response(504, f"upstream timeout: {exc}")
    except httpx.HTTPError as exc:
        logger.error("upstream error: %s", exc)
        return error_response(502, f"upstream error: {exc}")

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        headers = passthrough_response_headers(resp, streaming=True)
        if repairs_count > 0:
            headers["x-proxy-repairs"] = str(repairs_count)

        sse_capture = SseCapture(cache) if cache is not None else None

        async def stream_body():
            # aiter_raw(): 原始字节逐块中继，无 SSE 重构、无文本解码；
            # 捕获器同步喂入同样字节，不产生延迟
            try:
                async for chunk in resp.aiter_raw():
                    if sse_capture is not None:
                        sse_capture.feed(chunk)
                    yield chunk
            except (anyio.ClosedResourceError, GeneratorExit):
                return  # 客户端断连
            finally:
                await resp.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=resp.status_code,
            headers=headers,
        )

    content = await resp.aread()
    await resp.aclose()
    if cache is not None and "application/json" in content_type:
        try:
            data = json.loads(content)
            capture_from_response_json(data, cache)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("upstream JSON response unparseable for capture: %s", exc)
    headers = passthrough_response_headers(resp, streaming=False)
    if repairs_count > 0:
        headers["x-proxy-repairs"] = str(repairs_count)
    return Response(content=content, status_code=resp.status_code, headers=headers)
