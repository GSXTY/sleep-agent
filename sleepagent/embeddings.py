"""向量化（embedding）客户端。

支持三种提供方：
  ollama  —— 本地模型，推荐 bge-m3（中文强、免费、离线）
  openai  —— 任何 OpenAI 兼容的 /embeddings 接口
  fake    —— 确定性哈希向量，**只用于验证流程，不具备语义能力**

为什么单独一层：向量只在这一层产生，检索逻辑不关心它从哪来。
换提供方只需要改配置，不改检索代码。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import time
from urllib.parse import urlparse
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parents[1] / "config.json"

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
OLLAMA_DEFAULT_MODEL = "bge-m3"
OPENAI_DEFAULT_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_TIMEOUT = 8.0

_probe_cache = {"at": 0.0, "ok": False}
PROBE_TTL = 60.0            # 探测成功后的缓存秒数，避免每次检索都发请求
PROBE_TTL_FAIL = 5.0        # 探测失败只缓存 5 秒：模型冷启动要十几秒，
                            # 缓存 60 秒会让人误以为装失败了


def _file_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("embedding") or {}
        except Exception:
            return {}
    return {}


def settings() -> dict:
    file_config = _file_config()
    env_provider = os.getenv("LLM_EMBEDDING_PROVIDER")
    if env_provider and env_provider.lower() != str(file_config.get("provider", "")).lower():
        file_config = {}          # 环境变量切了 provider，文件里的配套字段不再适用
    provider = (env_provider or file_config.get("provider") or "ollama").lower()

    def pick(key, env_names, default=""):
        for name in env_names:
            value = os.getenv(name)
            if value:
                return value
        return file_config.get(key) or default

    timeout_raw = pick("timeout_seconds", ["LLM_EMBEDDING_TIMEOUT"], "")
    try:
        timeout = max(1.0, float(timeout_raw)) if timeout_raw else DEFAULT_TIMEOUT
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    if provider == "openai":
        return {"provider": "openai",
                "model": pick("model", ["LLM_EMBEDDING_MODEL", "OPENAI_EMBEDDING_MODEL"], OPENAI_DEFAULT_MODEL),
                "base_url": pick("base_url", ["LLM_EMBEDDING_BASE_URL", "OPENAI_BASE_URL"], OPENAI_DEFAULT_URL).rstrip("/"),
                "api_key": pick("api_key", ["LLM_EMBEDDING_API_KEY", "OPENAI_API_KEY"], ""),
                "timeout": timeout}
    if provider == "fake":
        return {"provider": "fake", "model": "fake-hash-256", "base_url": "", "api_key": "", "timeout": timeout}
    return {"provider": "ollama",
            "model": pick("model", ["LLM_EMBEDDING_MODEL", "OLLAMA_EMBEDDING_MODEL"], OLLAMA_DEFAULT_MODEL),
            "base_url": pick("base_url", ["LLM_EMBEDDING_BASE_URL", "OLLAMA_URL"], OLLAMA_DEFAULT_URL).rstrip("/"),
            "api_key": "",
            "timeout": timeout}


def describe() -> str:
    cfg = settings()
    lines = ["当前向量模型配置：", "  提供方：" + cfg["provider"], "  模型：" + cfg["model"]]
    if cfg["provider"] == "fake":
        lines.append("  [!] fake 只用于验证流程，不具备语义能力，不要用它评估效果。")
    elif cfg["provider"] == "ollama":
        names = _list_models(cfg["base_url"]) or []
        pulled = any(name == cfg["model"] or name.startswith(cfg["model"] + ":") for name in names)
        if not _port_reachable(cfg["base_url"]):
            lines.append("  [!] Ollama 没有在运行（端口未监听）。")
        elif not pulled:
            lines.append("  [!] 模型尚未下载，执行：ollama pull " + cfg["model"])
    else:
        lines.append("  地址：" + cfg["base_url"])
    return "\n".join(lines)


def _fake_vector(text: str, dim: int = 256) -> list:
    """确定性哈希向量。

    用二元组的签名哈希累加再归一化。它能反映字面重合，但**没有语义理解**，
    所以只能用来验证「索引 -> 检索 -> 融合 -> 降级」这条链路通不通。
    """
    normalized = re.sub(r"\s+", "", str(text).lower())
    grams = [normalized[i:i + 2] for i in range(max(len(normalized) - 1, 0))] or [normalized or "空"]
    vector = [0.0] * dim
    for gram in grams:
        digest = hashlib.md5(gram.encode("utf-8")).hexdigest()
        value = int(digest, 16)
        vector[value % dim] += 1.0 if (value >> 20) & 1 else -1.0
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


def _post(url: str, payload: dict, headers: dict, timeout: float):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": True, "body": json.loads(response.read().decode("utf-8")), "detail": ""}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return {"ok": False, "body": None, "detail": "HTTP " + str(exc.code) + " " + detail}
    except Exception as exc:
        return {"ok": False, "body": None, "detail": str(exc)}


def embed(texts: list) -> list | None:
    """批量向量化。失败返回 None，由调用方降级到纯关键词检索。"""
    if not texts:
        return []
    cfg = settings()

    if cfg["provider"] == "fake":
        return [_fake_vector(text) for text in texts]

    if cfg["provider"] == "openai":
        if not cfg["api_key"]:
            return None
        result = _post(cfg["base_url"] + "/embeddings",
                       {"model": cfg["model"], "input": list(texts)},
                       {"Authorization": "Bearer " + cfg["api_key"]}, cfg["timeout"])
        if not result["ok"]:
            return None
        data = sorted(result["body"].get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in data]
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            return None
        return vectors

    result = _post(cfg["base_url"] + "/api/embed",
                   {"model": cfg["model"], "input": list(texts)}, {}, cfg["timeout"])
    if not result["ok"]:
        return None
    vectors = result["body"].get("embeddings")
    if not vectors or len(vectors) != len(texts):
        return None
    return vectors


def embed_query(text: str):
    vectors = embed([text])
    return vectors[0] if vectors else None


def _port_reachable(url: str, timeout: float = 0.3) -> bool:
    """快速判断服务是否在监听。

    直接发 HTTP 探测在 Windows 上要 2 秒才失败（连接被拒前的重试），
    而第一次提问会因此明显卡顿。先做一次 0.3 秒的 TCP 连接探测，
    端口没开就立刻判定不可用。
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _list_models(base_url: str, timeout: float = 2.0):
    """列出 Ollama 已下载的模型。

    用 /api/tags 而不是试调 /api/embed：后者会触发模型加载，
    冷启动要二三十秒，探测因此超时——状态行就会误报「不可用」，
    而实际上紧接着的检索又是成功的。
    """
    try:
        with urllib.request.urlopen(base_url + "/api/tags", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [str(item.get("name", "")) for item in (data.get("models") or [])]
    except Exception:
        return None


def available(refresh: bool = False) -> bool:
    """向量化是否可用。结果缓存 PROBE_TTL 秒。"""
    cfg = settings()
    if os.getenv("SLEEPAGENT_DISABLE_VECTORS") == "1" and cfg["provider"] != "fake":
        # 测试用：让结果不依赖开发机上是否装了 Ollama（fake 不受影响）
        return False
    if cfg["provider"] == "fake":
        return True                      # fake 永远可用，便于验证流程
    now = time.time()
    if not refresh and _probe_cache["at"]:
        ttl = PROBE_TTL if _probe_cache["ok"] else PROBE_TTL_FAIL
        if now - _probe_cache["at"] < ttl:
            return _probe_cache["ok"]
    if cfg["provider"] == "openai" and not cfg["api_key"]:
        _probe_cache["at"], _probe_cache["ok"] = now, False
        return False
    if not _port_reachable(cfg["base_url"]):
        _probe_cache["at"], _probe_cache["ok"] = now, False
        return False
    names = _list_models(cfg["base_url"])
    if names is None:
        _probe_cache["at"], _probe_cache["ok"] = now, False
        return False
    # 配置的模型名可能是 bge-m3，实际列出的是 bge-m3:latest
    ok = any(name == cfg["model"] or name.startswith(cfg["model"] + ":") for name in names)
    _probe_cache["at"], _probe_cache["ok"] = now, ok
    return ok


def reset_probe_cache() -> None:
    _probe_cache["at"] = 0.0
    _probe_cache["ok"] = False
