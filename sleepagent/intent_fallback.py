"""低置信度意图兜底。

只在规则没有明确命中、且没有可靠事实/资料时调用。
模型只能从固定集合选择意图，并返回 JSON；安全边界不经过这里。
"""
from __future__ import annotations

import json
import re

from . import llm

ALLOWED_INTENTS = (
    "product_docs",
    "device_data",
    "metric_explain",
    "general",
    "clarify",
    "out_of_scope",
)
MIN_CONFIDENCE = 0.60

SYSTEM_PROMPT = """你是睡眠呼吸产品助手的意图分类器。
只输出一个 JSON 对象，不要输出解释、Markdown 或代码块。
intent 只能是：
product_docs、device_data、metric_explain、general、clarify、out_of_scope。

分类规则：
- product_docs：产品使用、安装、佩戴、清洁、耗材、错误码、App 操作
- device_data：用户自己的治疗数据、昨晚/最近数据、个人指标数值
- metric_explain：指标或术语的含义，不是用户个人数值
- general：一般常识、计算、写作、闲聊
- clarify：信息不足，不能判断用户具体想做什么
- out_of_scope：仅当用户明确要求超出助手能力的实际操作，且不能通过文字协助完成
- 写作、翻译、计算、编程、比较和推荐等可用文字完成的日常任务都属于 general，不能因与产品无关而拒答
- 识别用户当前真正要完成的任务；不要仅凭“多少、变化、比较”等词判断为 device_data

返回格式：
{"intent":"product_docs","confidence":0.86}
"""


def _parse(content: str):
    match = re.search(r"\{.*\}", content or "", re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            return None
        intent = str(payload.get("intent", "")).strip()
        confidence = float(payload.get("confidence", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if intent not in ALLOWED_INTENTS or not 0 <= confidence <= 1:
        return None
    if confidence < MIN_CONFIDENCE:
        return None
    return {"intent": intent, "confidence": round(confidence, 2)}


def classify(question: str):
    """调用聊天模型做受限意图分类；失败或低置信度返回 None。"""
    result = llm.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "用户问题：\n" + question},
        ],
        temperature=0,
        max_tokens=80,
        timeout_seconds=4.0,
    )
    if result.get("status") != "success":
        return None
    return _parse(result.get("content", ""))
