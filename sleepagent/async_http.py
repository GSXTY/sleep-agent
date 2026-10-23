"""共享的异步 HTTP 客户端。"""
from __future__ import annotations

import asyncio
import os

DEFAULT_TIMEOUT = 60.0
TRUST_ENV = os.getenv("SLEEPAGENT_HTTP_TRUST_ENV", "0").lower() in (
    "1", "true", "yes",
)

_client = None
_client_lock = asyncio.Lock()
_transport = None


def configure_transport(transport) -> None:
    """测试或多环境注入 transport。"""
    global _transport, _client
    _transport = transport
    _client = None


async def get_client():
    global _client
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "异步 HTTP 需要安装 requirements-async.txt"
        ) from exc
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=httpx.Timeout(DEFAULT_TIMEOUT),
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                ),
                trust_env=TRUST_ENV,
                transport=_transport,
            )
    return _client


async def close_client() -> None:
    global _client
    async with _client_lock:
        if _client is not None and not _client.is_closed:
            await _client.aclose()
        _client = None


async def request(method: str, url: str, **kwargs):
    client = await get_client()
    return await client.request(method, url, **kwargs)
