"""向量索引：切片 -> 向量，落盘、增量重建、余弦检索。

规模说明：几千到十万级切片都可以暴力余弦（纯 Python 毫秒级）。
**不要因为「做 RAG」就去上 FAISS / Milvus** —— 超过十万切片、
或需要元数据过滤加分片时再说。

增量策略：每个切片按「文档 + 章节 + 正文」算一个 hash，
只有 hash 变化（文档被改）的切片才重新向量化。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

INDEX_FILE = Path(__file__).resolve().parents[1] / ".local" / "product_index.json"


def _chunk_hash(chunk) -> str:
    raw = (chunk.doc + "\u0000" + chunk.section + "\u0000" + chunk.text).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _entry(chunk, vector) -> dict:
    return {
        "doc": chunk.doc,
        "section": chunk.section,
        "index": chunk.index,
        "visibility": chunk.visibility,
        "text": chunk.text,
        "hash": _chunk_hash(chunk),
        "vector": [round(float(value), 6) for value in vector],
    }


def load() -> dict | None:
    if not INDEX_FILE.is_file():
        return None
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(entries: list, model: str) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "count": len(entries), "entries": entries}
    INDEX_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def ensure(chunks, embed_fn, model: str) -> list | None:
    """确保索引与当前切片一致。

    返回条目列表；无法向量化（模型不可用）时返回 None，调用方降级。
    只重算 hash 变了的切片，其余直接复用落盘结果。
    """
    cached = load()
    reusable = {}
    if cached and cached.get("model") == model:
        for entry in cached.get("entries") or []:
            reusable[(entry["doc"], entry["section"], entry["index"], entry["hash"])] = entry["vector"]

    entries = [None] * len(chunks)
    pending = []
    pending_slots = []
    for slot, chunk in enumerate(chunks):
        key = (chunk.doc, chunk.section, chunk.index, _chunk_hash(chunk))
        vector = reusable.get(key)
        if vector is not None:
            entries[slot] = _entry(chunk, vector)
        else:
            pending.append(chunk)
            pending_slots.append(slot)

    if pending:
        vectors = embed_fn([chunk.section + "\n" + chunk.text for chunk in pending])
        if not vectors:
            return None
        for slot, chunk, vector in zip(pending_slots, pending, vectors):
            entries[slot] = _entry(chunk, vector)
        save([entry for entry in entries if entry], model)

    return [entry for entry in entries if entry]


def cosine(left: list, right: list) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def search(query_vector, top_k: int = 10, visibility: str = "user", entries: list = None) -> list:
    if not query_vector:
        return []
    pool = entries if entries is not None else (load() or {}).get("entries") or []
    scored = []
    for entry in pool:
        if visibility != "all" and entry.get("visibility") != visibility:
            continue
        scored.append((cosine(query_vector, entry["vector"]), entry))
    scored.sort(key=lambda item: -item[0])
    results = []
    for score, entry in scored[:top_k]:
        results.append({"doc": entry["doc"], "section": entry["section"],
                        "index": entry["index"], "text": entry["text"],
                        "visibility": entry["visibility"], "score": round(score, 6)})
    return results


def stats() -> dict:
    cached = load()
    if not cached:
        return {"built": False, "count": 0, "model": None}
    return {"built": True, "count": cached.get("count", 0), "model": cached.get("model")}
