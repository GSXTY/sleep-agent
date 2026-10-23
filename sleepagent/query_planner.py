"""语义查询规划：把不同问法归一成受限的结构化查询计划。

LLM 只负责识别意图、指标、时间窗口和动作，不回答问题、不生成数字。
高置信度规则直接返回；规则不确定时才请求 LLM。LLM 不可用时保留规则结果。
"""
from __future__ import annotations

import json
import re

from . import analysis, llm

INTENTS = (
    "explain_metric",
    "device_value",
    "analyze_change",
    "analyze_trend",
    "compare_periods",
    "compare_nights",
    "report",
    "app_report_insight",
    "product_query",
    "general",
    "clarify",
    "out_of_scope",
)

METRICS = (
    "overall",
    "treatment_score",
    "duration_score",
    "mask_score",
    "resp_event_score",
    "ahi",
    "usage_time",
    "pressure",
    "leak",
    "tidal_volume",
    "respiratory_rate",
    "minute_ventilation",
)

WINDOWS = ("day", "week", "month", "context", "unspecified")

METRIC_ALIASES = (
    ("overall", (
        "使用情况", "整体情况", "总体情况", "睡眠情况", "数据情况",
        "数据怎么样", "数据正常", "情况怎么样", "睡得怎么样",
    )),
    ("treatment_score", (
        "总分", "设备总分", "治疗总分", "设备评分", "治疗评分",
        "睡眠评分", "整体评分", "百分制评分", "评分", "设备分", "治疗分",
    )),
    ("duration_score", ("时长得分", "治疗时长得分", "使用时长得分")),
    ("mask_score", ("面罩得分", "面罩密封得分")),
    ("resp_event_score", ("呼吸事件得分", "事件得分")),
    ("ahi", ("ahi", "呼吸暂停低通气指数", "呼吸事件指数")),
    ("usage_time", ("使用时长", "用了多久", "治疗时长")),
    ("pressure", ("压力", "p95压力", "治疗压力")),
    ("leak", ("漏气", "漏气量", "密封")),
    ("tidal_volume", ("潮气量",)),
    ("respiratory_rate", ("呼吸频率",)),
    ("minute_ventilation", ("分钟通气量",)),
)

DEFINITION_TERMS = (
    "是什么", "什么意思", "含义", "解释", "怎么理解", "定义", "指什么",
)
TREND_TERMS = (
    "变化", "趋势", "走势", "波动", "升了", "降了", "上升", "下降",
    "提高", "降低", "变好", "变差", "怎么走", "表现如何", "表现怎么样",
)
VALUE_TERMS = ("是多少", "多少", "数值", "具体值", "显示多少")
COMPARE_TERMS = ("相比", "对比", "比较", "比上次", "比之前", "高还是低")
REPORT_TERMS = ("报告", "周报", "月报")
APP_INSIGHT_TERMS = (
    "app报告", "app 报告", "报告解读", "解读报告", "报告里的",
    "报告为什么", "报告中的",
)
PRODUCT_TERMS = (
    "怎么用", "怎么清洁", "怎么安装", "报错", "故障", "滤芯", "面罩",
    "管路", "蓝牙", "app怎么", "售后", "保修", "工作原理", "工作模式",
)
DOMAIN_HINTS = (
    "睡眠", "数据", "设备", "机器", "呼吸", "治疗", "使用", "面罩",
    "漏气", "压力", "得分", "评分", "分数", "总分", "ahi", "报告",
    "最近", "这周", "一周", "一个月", "30天", "7天", "昨天", "昨晚",
)

SYSTEM_PROMPT = """你是睡眠呼吸设备 Agent 的查询规划器。
只输出一个 JSON 对象，不回答用户问题，不生成数据，不输出 Markdown。

intent 只能是：
explain_metric、device_value、analyze_change、analyze_trend、
compare_periods、compare_nights、report、app_report_insight、
product_query、general、clarify、out_of_scope。

metrics 只能从下列值中选择 0 到 3 个：
overall、treatment_score、duration_score、mask_score、resp_event_score、
ahi、usage_time、pressure、leak、tidal_volume、respiratory_rate、
minute_ventilation。

window 只能是：day、week、month、context、unspecified。

规则：
- “是什么意思/含义”是 explain_metric；
- “多少/数值”是 device_value；
- “变化/趋势/走势/为什么变化”是 analyze_change 或 analyze_trend；
- “和之前/上周相比”是 compare_periods；
- “两晚/上次和前一次相比”是 compare_nights；
- 明确生成报告是 report；
- 解读 App 报告板块是 app_report_insight；
- 产品操作、故障、耗材是 product_query；
- 与设备数据无关的一般请求是 general；
- 无法判断时是 clarify。

示例：
{"intent":"analyze_change","metrics":["treatment_score"],"window":"week","confidence":0.95}
{"intent":"explain_metric","metrics":["duration_score"],"window":"unspecified","confidence":0.96}
"""


def _has_latin(text: str, term: str) -> bool:
    return re.search(
        r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text
    ) is not None


def _contains(text: str, term: str) -> bool:
    return _has_latin(text, term) if term.isascii() else term in text


def _metrics(question: str) -> list:
    text = (question or "").lower()
    found = []
    for metric, aliases in METRIC_ALIASES:
        if any(_contains(text, alias.lower()) for alias in aliases):
            found.append(metric)
    return found


def _window(question: str) -> tuple:
    days = analysis.detect_window_days(question, default=0)
    if days == 7:
        return "week", 7
    if days == 14:
        return "week", 14
    if days == 30:
        return "month", 30
    text = question or ""
    if any(term in text for term in ("昨晚", "昨天", "今天", "前一晚", "上一晚")):
        return "day", 1
    if re.search(r"\d{4}-\d{1,2}-\d{1,2}", text):
        return "day", 1
    return "unspecified", 0


def _intent(question: str, metrics: list) -> str:
    text = (question or "").lower()
    # 操作任务优先于伴随的“压力 / 面罩 / 多少”等词，避免去读个人数据。
    if any(term in text for term in (
            "怎么清洗", "如何清洗", "怎么清洁", "如何清洁", "怎么安装",
            "如何安装", "怎么连接", "如何连接", "怎么配对", "怎么更换",
            "多久换", "更换周期", "保修", "售后", "报错", "故障")):
        return "product_query"
    if any(term in text for term in APP_INSIGHT_TERMS):
        return "app_report_insight"
    if any(term in text for term in REPORT_TERMS):
        if any(term in text for term in ("生成", "出个", "来一份", "做份", "周报", "月报")):
            return "report"
    if any(term in text for term in DEFINITION_TERMS) and metrics:
        return "explain_metric"
    # 比较、数值、趋势本身不是领域信号；普通任务不能借用治疗记录作答。
    data_signal = bool(metrics) or any(term in text for term in (
        "治疗", "呼吸机", "使用情况", "睡眠情况", "我的数据", "治疗数据",
        "设备数据", "睡眠数据", "百分制", "昨晚", "最近几晚",
    ))
    if not data_signal:
        return "product_query" if any(term in text for term in PRODUCT_TERMS) else "general"
    if any(term in text for term in COMPARE_TERMS):
        if (len(re.findall(r"\d{4}-\d{1,2}-\d{1,2}", text)) >= 2
                or any(term in text for term in (
                    "两晚", "两次", "前一晚", "上一晚", "上一次",
                    "前一次", "上次",
                ))):
            return "compare_nights"
        return "compare_periods"
    if any(term in text for term in TREND_TERMS) or (
            metrics and any(term in text for term in ("分析", "解读", "变化"))):
        if any(term in text for term in ("趋势", "走势")):
            return "analyze_trend"
        return "analyze_change"
    if any(term in text for term in VALUE_TERMS):
        return "device_value"
    if any(term in text for term in PRODUCT_TERMS):
        return "product_query"
    if metrics:
        return "device_value"
    return "general"


def _rule_plan(question: str) -> dict:
    metrics = _metrics(question)
    intent = _intent(question, metrics)
    window, window_days = _window(question)
    confidence = 0.25
    if metrics:
        confidence += 0.30
    if intent not in ("general", "clarify"):
        confidence += 0.30
    if window != "unspecified":
        confidence += 0.15
    if intent == "explain_metric" and metrics:
        confidence = max(confidence, 0.92)
    if intent in ("analyze_change", "analyze_trend") and metrics and window != "unspecified":
        confidence = max(confidence, 0.92)
    confidence = min(round(confidence, 2), 0.98)
    return {
        "intent": intent,
        "metrics": metrics,
        "window": window,
        "window_days": window_days,
        "confidence": confidence,
        "source": "rules",
    }


def _parse(content: str):
    match = re.search(r"\{.*\}", content or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent", "")).strip()
    window = str(data.get("window", "")).strip()
    raw_metrics = data.get("metrics", [])
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if intent not in INTENTS or window not in WINDOWS:
        return None
    if not 0 <= confidence <= 1 or confidence < 0.60:
        return None
    if not isinstance(raw_metrics, list):
        return None
    metrics = [str(item) for item in raw_metrics if str(item) in METRICS]
    if len(metrics) != len(raw_metrics) or len(metrics) > 3:
        return None
    window_days = {"day": 1, "week": 7, "month": 30}.get(window, 0)
    return {
        "intent": intent,
        "metrics": metrics,
        "window": window,
        "window_days": window_days,
        "confidence": round(confidence, 2),
        "source": "llm",
    }


def _classify_with_llm(question: str):
    result = llm.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "用户问题：\n" + question},
        ],
        temperature=0,
        max_tokens=120,
        timeout_seconds=4.0,
    )
    if result.get("status") != "success":
        return None
    return _parse(result.get("content", ""))


def plan(question: str, use_llm: bool = False) -> dict:
    """返回规范化查询计划；规则高置信时不发额外模型请求。"""
    rule = _rule_plan(question)
    text = (question or "").lower()
    domain_signal = any(term in text for term in DOMAIN_HINTS)
    should_ask_llm = (
        use_llm
        and domain_signal
        and rule["confidence"] < 0.85
        and rule["intent"] not in (
            "explain_metric", "product_query", "report", "app_report_insight",
        )
        and llm.available()
    )
    if should_ask_llm:
        llm_plan = _classify_with_llm(question)
        if llm_plan and llm_plan["confidence"] >= max(0.65, rule["confidence"]):
            return llm_plan
    rule["window_days"] = analysis.detect_window_days(
        question, default=rule["window_days"] or 0
    )
    if rule["window"] == "unspecified" and rule["window_days"]:
        rule["window"] = "week" if rule["window_days"] in (7, 14) else "month"
    return rule
