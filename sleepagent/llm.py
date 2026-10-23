"""大模型客户端：支持本地 Ollama 和任意 OpenAI 兼容接口。

设计要点（沿用原项目）：
  1. 只用标准库 urllib，不引入 requests/openai 等依赖
  2. 任何失败都返回 status != success，由调用方回退到规则版，绝不中断对话
  3. API Key 只从环境变量或 config.json 读取，任何输出里都做掩码

配置优先级：环境变量 > config.json > 默认值
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parents[1] / "config.json"

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
OLLAMA_DEFAULT_MODEL = "qwen3:1.7b"
OPENAI_DEFAULT_URL = "https://api.openai.com/v1"

DEFAULT_TIMEOUT = 60.0
MAX_CONCURRENCY = max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "8")))
QUEUE_TIMEOUT = max(0.1, float(os.getenv("LLM_QUEUE_TIMEOUT_SECONDS", "15")))
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


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
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).lower()
        if not any(marker in reason for marker in ("refused", "10061")):
            raise
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return direct.open(request, timeout=timeout)


def chat(messages: list, *, temperature: float = 0.2, max_tokens: int = 400) -> dict:
    """带进程内并发上限的聊天入口。"""
    if not _chat_slots.acquire(timeout=QUEUE_TIMEOUT):
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
            messages, temperature=temperature, max_tokens=max_tokens
        )
    finally:
        _chat_slots.release()


def _chat_unlocked(messages: list, *, temperature: float = 0.2,
                   max_tokens: int = 400) -> dict:
    """返回 {status, content, model, provider, detail}。status 非 success 时由调用方回退。"""
    cfg = settings()
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
