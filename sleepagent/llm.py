"""大模型客户端：支持本地 Ollama 和任意 OpenAI 兼容接口。

设计要点（沿用原项目）：
  1. 只用标准库 urllib，不引入 requests/openai 等依赖
  2. 任何失败都返回 status != success，由调用方回退到规则版，绝不中断对话
  3. API Key 只从环境变量或 config.json 读取，任何输出里都做掩码

配置优先级：环境变量 > config.json > 默认值
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import async_http

CONFIG_FILE = Path(__file__).resolve().parents[1] / "config.json"

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
OLLAMA_DEFAULT_MODEL = "qwen3:1.7b"
OPENAI_DEFAULT_URL = "https://api.openai.com/v1"

DEFAULT_TIMEOUT = 15.0
MAX_CONCURRENCY = max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "8")))
QUEUE_TIMEOUT = max(0.1, float(os.getenv("LLM_QUEUE_TIMEOUT_SECONDS", "15")))
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
_async_chat_slots = asyncio.Semaphore(MAX_CONCURRENCY)
_ASYNC_BRIDGE = contextvars.ContextVar("sleepagent_async_bridge", default=None)


class LLMRequestCancelled(Exception):
    """外层请求取消后，让同步 Agent 线程立即停止继续生成。"""


class AsyncLLMBridge:
    """把工作线程里的同步 llm.chat 桥接到主事件循环的异步客户端。"""

    def __init__(self, loop):
        self.loop = loop
        self.cancelled = False
        self._futures = set()
        self._lock = threading.Lock()

    def call(self, coroutine):
        if self.cancelled:
            coroutine.close()
            raise LLMRequestCancelled("请求已取消")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        with self._lock:
            self._futures.add(future)
        try:
            return future.result()
        except concurrent.futures.CancelledError as exc:
            raise LLMRequestCancelled("请求已取消") from exc
        finally:
            with self._lock:
                self._futures.discard(future)

    def cancel(self):
        self.cancelled = True
        with self._lock:
            futures = list(self._futures)
        for future in futures:
            future.cancel()


def set_async_bridge(bridge):
    return _ASYNC_BRIDGE.set(bridge)


def reset_async_bridge(token) -> None:
    _ASYNC_BRIDGE.reset(token)


def _config() -> dict:
    data = {}
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    return data


CONFIG_OVERRIDE = {"disabled": False}


def _get(key: str, env_names, default=""):
    for name in env_names:
        value = os.getenv(name)
        if value:
            return value
    if CONFIG_OVERRIDE["disabled"]:
        return default
    value = _config().get(key)
    return value if value else default


def settings() -> dict:
    # 如果环境变量切换了 provider，config.json 里配套的 base_url / model / api_key
    # 就不再适用，否则会出现「provider=ollama 却用着 DeepSeek 的地址」这种错配。
    CONFIG_OVERRIDE["disabled"] = False
    file_config = _config()
    env_provider = os.getenv("LLM_PROVIDER")
    if env_provider and env_provider.lower() != str(file_config.get("provider", "")).lower():
        file_config = {}
        CONFIG_OVERRIDE["disabled"] = True
    provider = (env_provider or file_config.get("provider") or "ollama").lower()
    timeout_raw = _get("timeout_seconds", ["LLM_TIMEOUT_SECONDS", "OLLAMA_TIMEOUT_SECONDS"], "")
    try:
        timeout = max(1.0, float(timeout_raw)) if timeout_raw else DEFAULT_TIMEOUT
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    if provider == "openai":
        return {
            "provider": "openai",
            "model": _get("model", ["LLM_MODEL", "OPENAI_MODEL"], "gpt-4o-mini"),
            "base_url": _get("base_url", ["LLM_BASE_URL", "OPENAI_BASE_URL"], OPENAI_DEFAULT_URL).rstrip("/"),
            "api_key": _get("api_key", ["LLM_API_KEY", "OPENAI_API_KEY"], ""),
            "timeout": timeout,
        }
    return {
        "provider": "ollama",
        "model": _get("model", ["LLM_MODEL", "OLLAMA_MODEL"], OLLAMA_DEFAULT_MODEL),
        "base_url": _get("base_url", ["LLM_BASE_URL", "OLLAMA_URL"], OLLAMA_DEFAULT_URL).rstrip("/"),
        "api_key": "",
        "timeout": timeout,
    }


def mask_key(key: str) -> str:
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def describe() -> str:
    cfg = settings()
    lines = ["当前大模型配置：", "  提供方：" + cfg["provider"], "  模型：" + cfg["model"], "  地址：" + cfg["base_url"]]
    if cfg["provider"] == "openai":
        lines.append("  API Key：" + mask_key(cfg["api_key"]))
        if not cfg["api_key"]:
            lines.append("  [!] 没有配置 API Key，无法调用。")
    lines.append("  超时：" + str(cfg["timeout"]) + " 秒")
    return "\n".join(lines)


def available() -> bool:
    """轻量可用性检查。OpenAI 兼容接口只检查配置是否齐全，不做联网探测。"""
    cfg = settings()
    if cfg["provider"] == "openai":
        return bool(cfg["api_key"])
    return _probe(cfg["base_url"] + "/api/tags")["ok"]


def _probe(url: str, headers=None) -> dict:
    try:
        request = urllib.request.Request(url, headers=headers or {}, method="GET")
        with _open_request(request, timeout=5) as response:
            return {"ok": 200 <= response.status < 300, "detail": "HTTP " + str(response.status)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": "HTTP " + str(exc.code)}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


def test_connection() -> str:
    cfg = settings()
    if cfg["provider"] == "openai":
        if not cfg["api_key"]:
            return "没有配置 API Key。请设置环境变量 LLM_API_KEY，或在 config.json 里填写 api_key。"
        result = _probe(cfg["base_url"] + "/models", {"Authorization": "Bearer " + cfg["api_key"]})
        return ("连通正常（" + result["detail"] + "）" if result["ok"]
                else "连接失败：" + result["detail"])
    result = _probe(cfg["base_url"] + "/api/tags")
    if result["ok"]:
        return "连通正常（" + result["detail"] + "）"
    return ("连接失败：" + result["detail"]
            + "\n请确认 Ollama 已安装并启动。安装后执行：ollama pull " + cfg["model"])


def _post(url: str, payload: dict, headers: dict, timeout: float):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    try:
        with _open_request(request, timeout=timeout) as response:
            return {"ok": True, "body": json.loads(response.read().decode("utf-8")), "detail": ""}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code == 401:
            return {"ok": False, "body": None, "detail": "鉴权失败（401），请检查 API Key。"}
        if exc.code == 404:
            return {"ok": False, "body": None, "detail": "接口或模型不存在（404）：" + detail}
        return {"ok": False, "body": None, "detail": "HTTP " + str(exc.code) + " " + detail}
    except urllib.error.URLError as exc:
        return {"ok": False, "body": None, "detail": "无法连接：" + str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "body": None, "detail": str(exc)}


def _usage(body: dict, provider: str) -> dict:
    raw = body.get("usage") or {}
    if provider == "openai":
        input_tokens = raw.get("prompt_tokens", 0) or 0
        output_tokens = raw.get("completion_tokens", 0) or 0
        total_tokens = raw.get("total_tokens", input_tokens + output_tokens) or 0
    else:
        input_tokens = body.get("prompt_eval_count", 0) or 0
        output_tokens = body.get("eval_count", 0) or 0
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
    }


def _open_request(request, timeout: float):
    """发送请求；失效代理导致连接被拒时，自动绕过代理再试一次。"""
    started = time.monotonic()
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).lower()
        if not any(marker in reason for marker in ("refused", "10061")):
            raise
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("模型连接等待超时") from exc
        return direct.open(request, timeout=remaining)


def chat(messages: list, *, temperature: float = 0.2, max_tokens: int = 400,
         timeout_seconds: float | None = None) -> dict:
    """带进程内并发上限的聊天入口。"""
    bridge = _ASYNC_BRIDGE.get()
    if bridge is not None:
        return bridge.call(chat_async(
            messages, temperature=temperature, max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        ))
    queue_timeout = min(QUEUE_TIMEOUT, 0.5, timeout_seconds) if timeout_seconds is not None else QUEUE_TIMEOUT
    if not _chat_slots.acquire(timeout=queue_timeout):
        return {
            "status": "busy",
            "content": "",
            "model": settings()["model"],
            "provider": settings()["provider"],
            "detail": "模型并发已满，请稍后重试",
            "usage": {},
        }
    try:
        return _chat_unlocked(
            messages, temperature=temperature, max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    finally:
        _chat_slots.release()


def _chat_unlocked(messages: list, *, temperature: float = 0.2,
                   max_tokens: int = 400, timeout_seconds: float | None = None) -> dict:
    """返回 {status, content, model, provider, detail}。status 非 success 时由调用方回退。"""
    cfg = dict(settings())
    if timeout_seconds is not None:
        cfg["timeout"] = min(cfg["timeout"], max(0.1, timeout_seconds))
    if cfg["provider"] == "openai":
        if not cfg["api_key"]:
            return {"status": "unconfigured", "content": "", "model": cfg["model"],
                    "provider": "openai", "detail": "缺少 API Key"}
        result = _post(
            cfg["base_url"] + "/chat/completions",
            {"model": cfg["model"], "messages": messages, "temperature": temperature,
             "max_tokens": max_tokens, "stream": False},
            {"Authorization": "Bearer " + cfg["api_key"]},
            cfg["timeout"],
        )
        if not result["ok"]:
            return {"status": "error", "content": "", "model": cfg["model"],
                    "provider": "openai", "detail": result["detail"]}
        choices = result["body"].get("choices") or []
        content = (choices[0].get("message", {}).get("content") if choices else "") or ""
        usage = _usage(result["body"], "openai")
    else:
        result = _post(
            cfg["base_url"] + "/api/chat",
            {"model": cfg["model"], "messages": messages, "stream": False, "think": False,
             "keep_alive": "15m", "options": {"temperature": temperature, "num_predict": max_tokens}},
            {}, cfg["timeout"],
        )
        if not result["ok"]:
            return {"status": "error", "content": "", "model": cfg["model"],
                    "provider": "ollama", "detail": result["detail"]}
        content = (result["body"].get("message") or {}).get("content", "") or ""
        usage = _usage(result["body"], "ollama")

    if not content.strip():
        return {"status": "empty", "content": "", "model": cfg["model"],
                "provider": cfg["provider"], "detail": "模型返回空内容",
                "usage": usage}
    return {"status": "success", "content": content.strip(), "model": cfg["model"],
            "provider": cfg["provider"], "detail": "", "usage": usage}


async def chat_async(messages: list, *, temperature: float = 0.2,
                     max_tokens: int = 400, timeout_seconds: float | None = None) -> dict:
    """异步聊天入口，使用共享 httpx 连接池。"""
    cfg = dict(settings())
    if timeout_seconds is not None:
        cfg["timeout"] = min(cfg["timeout"], max(0.1, timeout_seconds))
    acquired = False
    try:
        await asyncio.wait_for(
            _async_chat_slots.acquire(), timeout=(min(QUEUE_TIMEOUT, 0.5, cfg["timeout"])
                                                  if timeout_seconds is not None else QUEUE_TIMEOUT)
        )
        acquired = True
    except asyncio.TimeoutError:
        return {
            "status": "busy",
            "content": "",
            "model": cfg["model"],
            "provider": cfg["provider"],
            "detail": "模型并发已满，请稍后重试",
            "usage": {},
        }
    try:
        if cfg["provider"] == "openai":
            if not cfg["api_key"]:
                return {
                    "status": "unconfigured",
                    "content": "",
                    "model": cfg["model"],
                    "provider": "openai",
                    "detail": "缺少 API Key",
                    "usage": {},
                }
            url = cfg["base_url"] + "/chat/completions"
            headers = {"Authorization": "Bearer " + cfg["api_key"]}
            payload = {
                "model": cfg["model"],
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
        else:
            url = cfg["base_url"] + "/api/chat"
            headers = {}
            payload = {
                "model": cfg["model"],
                "messages": messages,
                "stream": False,
                "think": False,
                "keep_alive": "15m",
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }
        try:
            response = await asyncio.wait_for(async_http.request(
                "POST", url, json=payload, headers=headers,
                timeout=cfg["timeout"],
            ), timeout=cfg["timeout"])
        except asyncio.TimeoutError:
            return {"status": "error", "content": "", "model": cfg["model"],
                    "provider": cfg["provider"], "detail": "模型请求等待超时", "usage": {}}
        except Exception as exc:
            return {
                "status": "error",
                "content": "",
                "model": cfg["model"],
                "provider": cfg["provider"],
                "detail": str(exc),
                "usage": {},
            }
        if response.status_code >= 400:
            detail = response.text[:300]
            if response.status_code == 401:
                detail = "鉴权失败（401），请检查 API Key。"
            elif response.status_code == 404:
                detail = "接口或模型不存在（404）：" + detail
            else:
                detail = "HTTP " + str(response.status_code) + " " + detail
            return {
                "status": "error",
                "content": "",
                "model": cfg["model"],
                "provider": cfg["provider"],
                "detail": detail,
                "usage": {},
            }
        try:
            body = response.json()
        except ValueError:
            return {
                "status": "error",
                "content": "",
                "model": cfg["model"],
                "provider": cfg["provider"],
                "detail": "模型返回非 JSON 内容",
                "usage": {},
            }
        if cfg["provider"] == "openai":
            choices = body.get("choices") or []
            content = (
                choices[0].get("message", {}).get("content") if choices else ""
            ) or ""
        else:
            content = (body.get("message") or {}).get("content", "") or ""
        usage = _usage(body, cfg["provider"])
        if not content.strip():
            return {
                "status": "empty",
                "content": "",
                "model": cfg["model"],
                "provider": cfg["provider"],
                "detail": "模型返回空内容",
                "usage": usage,
            }
        return {
            "status": "success",
            "content": content.strip(),
            "model": cfg["model"],
            "provider": cfg["provider"],
            "detail": "",
            "usage": usage,
        }
    finally:
        if acquired:
            _async_chat_slots.release()
