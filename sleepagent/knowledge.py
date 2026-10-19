"""知识库加载与检索。

移植自原项目 core/rag_retriever.py，保持相同的接口与打分方式，
不引入任何第三方依赖（原实现同样只用标准库）。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

KNOWLEDGE_FILE = Path(__file__).resolve().parents[1] / "knowledge" / "base.json"
REQUIRED_FIELDS = {"id", "title", "category", "keywords", "content", "limitations", "safe_actions", "source"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _tokens(text: str) -> set:
    normalized = _normalize(text)
    latin = set(re.findall(r"[a-z]+|\d+(?:\.\d+)?", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    bigrams = {chinese[i:i + 2] for i in range(max(len(chinese) - 1, 0))}
    return latin | bigrams


@lru_cache(maxsize=1)
def load_knowledge_base() -> tuple:
    raw = json.loads(KNOWLEDGE_FILE.read_text(encoding="utf-8"))
    seen = set()
    documents = []
    for item in raw:
        missing = REQUIRED_FIELDS - set(item)
        if missing:
            raise ValueError("知识条目缺少字段：" + str(item.get("id", "unknown")) + " " + str(sorted(missing)))
        if item["id"] in seen:
            raise ValueError("知识条目 ID 重复：" + item["id"])
        seen.add(item["id"])
        documents.append(item)
    return tuple(documents)


def _score(question: str, document: dict) -> float:
    normalized_question = _normalize(question)
    question_tokens = _tokens(question)
    score = 0.0
    title = _normalize(document["title"])
    if title and (title in normalized_question or normalized_question in title):
        score += 8.0
    for keyword in document["keywords"]:
        normalized_keyword = _normalize(str(keyword))
        if normalized_keyword and normalized_keyword in normalized_question:
            score += 5.0
    document_tokens = _tokens(" ".join([
        document["title"], document["category"],
        *[str(item) for item in document["keywords"]],
        document["content"],
    ]))
    overlap = question_tokens & document_tokens
    if question_tokens:
        score += len(overlap) / len(question_tokens) * 6.0
    return round(score, 4)


def retrieve(question: str, top_k: int = 3, categories=None, min_score: float = 1.5) -> list:
    """min_score 调低可以得到"勉强相关"的条目，用于给用户提供猜测性的引导。"""
    if not question.strip() or top_k <= 0:
        return []
    allowed = set(categories) if categories else None
    ranked = []
    for document in load_knowledge_base():
        if allowed is not None and document["category"] not in allowed:
            continue
        score = _score(question, document)
        if score < min_score:
            continue
        ranked.append({**document, "score": score})
    ranked.sort(key=lambda item: (-item["score"], item["id"]))
    return ranked[:top_k]


def render(documents: list) -> str:
    if not documents:
        return "未检索到与当前问题直接相关的知识条目。"
    blocks = []
    for index, item in enumerate(documents, 1):
        blocks.append("\n".join([
            "[知识" + str(index) + "] " + item["title"] + "（" + item["id"] + "）",
            "可用知识：" + item["content"],
            "解释限制：" + item["limitations"],
            "低风险建议：" + item["safe_actions"],
            "来源：" + item["source"],
        ]))
    return "\n\n".join(blocks)


def list_categories() -> list:
    seen = []
    for item in load_knowledge_base():
        if item["category"] not in seen:
            seen.append(item["category"])
    return seen


def all_titles() -> list:
    return [(item["id"], item["title"], item["category"]) for item in load_knowledge_base()]
