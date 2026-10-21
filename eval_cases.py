"""Bad case 注册表：读取和追加 eval/cases.jsonl。

一条 case 可以同时服务两个评测：
  eval_retrieval.py 用 expected_section
  eval_agent.py     用 expected_mode / expected_contains

字段：
  query             用户原话（必填）
  expected_mode     期望模式前缀，如 产品文档 / 安全边界 / 设备数据
  expected_section  期望命中的章节名（子串匹配）
  expected_contains 回答里必须出现的内容
  source            线上 / 测试 / 评审
  note              踩坑说明
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

CASES_FILE = Path(__file__).resolve().parent / "eval" / "cases.jsonl"


def load_cases(path=None) -> list:
    """读取 JSONL；坏行跳过，不让一条脏数据阻断评测。"""
    target = Path(path) if path else CASES_FILE
    if not target.exists():
        return []
    cases = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or not str(item.get("query", "")).strip():
            continue
        item["query"] = str(item["query"]).strip()
        cases.append(item)
    return cases


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^\w]+", "-", text.strip().lower()).strip("-")
    return cleaned[:24] or "case"


def record_case(query: str, mode: str = "", section: str = "",
                 contains=None, source: str = "线上", note: str = "",
                 path=None, today=None) -> dict:
    """追加一条 case；同一天同问题只记一次。"""
    target = Path(path) if path else CASES_FILE
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    if not mode and not section and not contains:
        raise ValueError("expected_mode / expected_section / expected_contains 至少给一个")

    existing = load_cases(target)
    for item in existing:
        if item["query"] == query:
            return item

    day = today or date.today()
    stamp = datetime.now().strftime("%H%M%S")
    case = {
        "id": "bc-" + day.strftime("%Y%m%d") + "-" + _slug(query),
        "query": query,
        "expected_mode": mode,
        "expected_section": section,
        "expected_contains": list(contains or []),
        "source": source,
        "date": day.isoformat(),
        "note": note,
    }
    case["id"] += "-" + stamp

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    return case
