# -*- coding: utf-8 -*-
"""集成测试脚手架：create_app + MockTransport fake upstream。"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.main import create_app


class ProxyHarness:
    """代理 + fake upstream 组合。

    - upstream_requests: fake upstream 收到的全部请求（含修复后的 body）
    - handler 可在测试中替换，构造任意上游行为
    """

    def __init__(self, settings: Settings | None = None, handler=None):
        self.settings = settings or Settings()
        self.upstream_requests: list[httpx.Request] = []
        self._handler = handler or (lambda req: httpx.Response(200, json={"ok": True}))
        self.app = create_app(self.settings)
        # ASGITransport 不运行 lifespan，手动注入 client（MockTransport 不进真实网络）
        self.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.upstream_requests.append(request)
        return self._handler(request)

    def client(self) -> httpx.AsyncClient:
        """通过 ASGITransport 直接打 FastAPI 应用，不经真实 socket。"""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )


@pytest.fixture
async def harness_factory():
    """产出 ProxyHarness，测试结束后统一关闭内部 AsyncClient。"""
    harnesses: list[ProxyHarness] = []

    def _make(settings=None, handler=None) -> ProxyHarness:
        h = ProxyHarness(settings, handler)
        harnesses.append(h)
        return h

    yield _make
    for h in harnesses:
        await h.app.state.client.aclose()
