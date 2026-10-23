"""FastAPI/ASGI 入口：与 demo_server.py 并行运行，便于灰度与回滚。"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import demo_server
from sleepagent import async_http, data_source
from sleepagent.logging_config import configure_logging

MAX_CHAT_CONCURRENCY = max(1, int(os.getenv("API_MAX_CONCURRENCY", "16")))
AUTH_MODE = os.getenv("SLEEPAGENT_API_AUTH_MODE", "demo").lower()

_chat_slots = asyncio.Semaphore(MAX_CHAT_CONCURRENCY)
_session_locks: dict[tuple[str, str], asyncio.Lock] = {}
_dataset_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None
    use_llm: bool = True
    user_id: str | None = None


class FeedbackRequest(BaseModel):
    request_id: str = ""
    rating: str = ""
    comment: str = ""
    session_id: str = ""
    question: str = ""
    answer: str = ""
    mode: str = ""


async def get_session_lock(user_id: str, session_id: str) -> asyncio.Lock:
    key = (user_id, session_id)
    async with _locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[key] = lock
        return lock


async def ensure_nights(user_id: str):
    if user_id in demo_server._datasets:
        return
    async with _locks_guard:
        lock = _dataset_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _dataset_locks[user_id] = lock
    async with lock:
        if user_id in demo_server._datasets:
            return
        nights = await data_source.load_nights_async(
            user_id=user_id,
            source=demo_server.DATA_SOURCE or None,
        )
        dataset_lock = demo_server.keyed_lock(demo_server._dataset_locks, user_id)
        with dataset_lock:
            demo_server._datasets[user_id] = nights


def resolve_user_id(payload: ChatRequest) -> str:
    """Demo 允许显式测试用户；正式部署必须替换为 Token 鉴权依赖。"""
    if AUTH_MODE != "demo":
        raise RuntimeError("正式鉴权尚未配置，请设置可信的 Token 校验依赖")
    return str(payload.user_id or demo_server.USER_ID)


async def run_chat(payload: ChatRequest) -> dict:
    user_id = resolve_user_id(payload)
    session_id = payload.session_id or uuid.uuid4().hex[:16]
    await ensure_nights(user_id)
    lock = await get_session_lock(user_id, session_id)
    async with lock:
        async with _chat_slots:
            return await demo_server.chat_async(
                payload.message,
                session_id,
                payload.use_llm,
                user_id,
            )


def sse(event: str, data: dict) -> str:
    import json

    return (
        "event: " + event + "\n"
        + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(verbose=False)
    settings = data_source.settings()
    demo_server.DATA_SOURCE = settings["source"]
    demo_server.USER_ID = settings["user_id"]
    yield
    await async_http.close_client()


app = FastAPI(
    title="Sleep Agent API",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(demo_server.WEB / "index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "streaming": True,
        "data_source": demo_server.DATA_SOURCE or "configured",
        "sessions": len(demo_server._sessions),
        "max_chat_concurrency": MAX_CHAT_CONCURRENCY,
        "auth_mode": AUTH_MODE,
        "async_backend": True,
        "async_model_client": True,
    }


@app.post("/api/chat")
async def chat_json(payload: ChatRequest):
    try:
        data = await run_chat(payload)
    except RuntimeError as exc:
        return JSONResponse(
            status_code=503,
            content={"code": 503, "message": str(exc), "data": None},
        )
    return {"code": 0, "message": "ok", "data": data}


@app.post("/api/chat/stream")
async def chat_stream(request: Request, payload: ChatRequest):
    async def events():
        yield sse("status", {
            "stage": "processing",
            "text": "正在检索资料并分析数据…",
        })
        task = asyncio.create_task(run_chat(payload))
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                done, _ = await asyncio.wait({task}, timeout=15)
                if task in done:
                    break
                yield ": keep-alive\n\n"
            data = await task
            yield sse("message", data)
            yield sse("done", {"request_id": data.get("request_id", "")})
        except asyncio.CancelledError:
            task.cancel()
            return
        except Exception:
            yield sse("error", {
                "message": "服务暂时不可用，请稍后再试。",
            })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/feedback")
async def feedback(payload: FeedbackRequest):
    await asyncio.to_thread(
        demo_server.save_feedback, payload.model_dump()
    )
    return {"code": 0, "message": "ok", "data": None}
