# -*- coding: utf-8 -*-
# deepseek_codex_Repair / main — FastAPI 路由与编排
#
# 请求生命周期:
#   POST /v1/responses → 读 body → 超限 413 / 坏 JSON 400 → repair_input_items()
#   修复 input[] → 重新序列化 → 转发上游（流式/缓冲自动）→ 附加 X-Proxy-Repairs
#   其余路径 → 透明反向代理
#
# create_app() 为应用工厂：测试可注入自定义 Settings + fake upstream client。

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings, load_settings
from app.debug_dump import save_debug_dump
from app.proxy import build_upstream_url, create_client, filter_client_headers, forward
from app.reasoning_cache import ReasoningCache
from app.repair import RepairConfig, repair_input_items

logger = logging.getLogger("app.main")

DEFAULT_REASONING_CACHE_FILE = Path(tempfile.gettempdir()) / "deepseek_codex_reasoning_cache.json"


def _make_reasoning_lookup(cache: ReasoningCache):
    """构建 repair 用的回注查找函数: item -> 缓存 reasoning_text 或 None。"""

    def lookup(item: dict) -> str | None:
        cid = item.get("call_id")
        if isinstance(cid, str) and cid:
            text = cache.get_call(cid)
            if text:
                return text
        if item.get("type") == "message":
            return cache.get_message(item.get("content"))
        return None

    return lookup


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。settings=None 时从环境变量加载。"""
    settings = settings or load_settings()
    # uvicorn 只配置自己的 logger；这里给根 logger 挂 handler，
    # 保证 app.* 的修复日志（INFO）可见。force=False 不覆盖已有配置。
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=False,
    )
    repair_config = RepairConfig(
        orphan_strategy=settings.orphan_strategy,
        missing_id_strategy=settings.missing_id_strategy,
        synthetic_call_prefix=settings.synthetic_call_prefix,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = create_client(settings)
        logger.info(
            "proxy started: http://%s:%s -> %s",
            settings.proxy_host, settings.proxy_port, settings.upstream_url,
        )
        yield
        await app.state.client.aclose()

    app = FastAPI(title="deepseek-codex-repair-proxy", lifespan=lifespan)
    # settings/repair_config/缓存挂到 state（ASGITransport 测试不经 lifespan 也可用）
    app.state.settings = settings
    app.state.repair_config = repair_config
    cache_file = Path(settings.reasoning_cache_file) if settings.reasoning_cache_file else DEFAULT_REASONING_CACHE_FILE
    app.state.reasoning_cache = ReasoningCache(
        file_path=cache_file,
        max_entries=settings.reasoning_cache_max_entries,
    )
    app.state.reasoning_lookup = (
        _make_reasoning_lookup(app.state.reasoning_cache)
        if settings.restore_reasoning
        else None
    )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "upstream": settings.upstream_url}

    @app.post("/v1/responses")
    async def post_responses(request: Request):
        st = request.app.state.settings
        cfg = request.app.state.repair_config
        client = request.app.state.client

        raw = await request.body()
        if len(raw) > st.max_body_bytes:
            return JSONResponse(
                {"error": {"message": "request body too large"}}, status_code=413,
            )
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 坏 JSON 正是上游会拒绝的，不转发
            return JSONResponse(
                {"error": {"message": "invalid JSON body"}}, status_code=400,
            )

        repairs_count = 0
        if isinstance(body, dict) and isinstance(body.get("input"), list):
            repaired, report = repair_input_items(
                body["input"], cfg,
                reasoning_lookup=request.app.state.reasoning_lookup,
            )
            repairs_count = report.count
            if repairs_count:
                body["input"] = repaired
                for entry in report.entries:
                    logger.info(
                        "repair rule=%s index=%d detail=%s",
                        entry.rule, entry.index, entry.detail,
                    )
                if st.debug_dump:
                    save_debug_dump(raw, body, report)

        new_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = build_upstream_url(st, request.url.path, request.url.query)
        headers = filter_client_headers(request, st)
        headers["content-type"] = "application/json"
        upstream_req = client.build_request("POST", url, headers=headers, content=new_body)
        return await forward(
            client, upstream_req, repairs_count,
            cache=request.app.state.reasoning_cache,
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def passthrough(request: Request, path: str):
        """透明反向代理: 任意方法/路径/请求体原样中继（GET /v1/models 等）。"""
        st = request.app.state.settings
        client = request.app.state.client
        raw = await request.body()
        url = build_upstream_url(st, request.url.path, request.url.query)
        headers = filter_client_headers(request, st)
        upstream_req = client.build_request(
            request.method, url, headers=headers, content=raw,
        )
        return await forward(client, upstream_req)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    st = app.state.settings
    uvicorn.run(
        "app.main:app",
        host=st.proxy_host,
        port=st.proxy_port,
        log_level=st.log_level.lower(),
    )
