"""对话管线：路由 -> 事实/检索 -> 模型 -> 校验 -> 输出。

入口层（命令行、将来的 API）只做参数解析和结果展示，
所有业务判定都在这里，保证两个入口行为一致。
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sleepagent import (analysis, embeddings, intent_fallback, knowledge, llm, nightdata,
                        product_features, product_kb, product_models, report, router,
                        runtime_info, safety, service_intent, social, validation)
from sleepagent.router import route

logger = logging.getLogger("sleepagent.pipeline")

# 每次回答都注入当前日期：让「最近 / 上周」这类相对时间可用，
# 同时避免模型凭记忆编造日期。
SYS = "【系统信息】\n" + runtime_info.facts_line() + "\n\n"

# 设备数据模式**不**注入系统日期时间。
# 一旦给了「今天是 2026-10-20」，模型就会把「昨晚」推成 10 月 19 日，
# 写进答案后被数字白名单拦下（19、20 都不在今晚材料里）。
# 数据解读只需要材料里的记录归属日期。
DEVICE_SYS = (
    "【系统信息】\n"
    "本轮只解读材料里的数据夜，不要引用系统日期或时间；\n"
    "需要说明数据归属时，照抄材料里的“记录归属日期”。\n\n"
)

PRODUCT_PROMPT = """你在为一家睡眠呼吸设备公司做客服助手。

回答规则：
1. 只依据【产品资料】回答产品相关问题，并注明出处（资料文件名与章节）。
2. 资料里没有写的内容，直接说资料未覆盖，并建议联系客服或售后；绝不允许自己补全。
3. 不要编造型号、部件更换周期、错误码含义、保修政策或任何产品参数。
4. 如果资料与问题明显无关，就说明没有查到相关资料，不要牵强引用。
5. 不给出疾病诊断，不建议用户自行调整治疗压力或设备参数；涉及这类问题应引导联系医生。
6. 涉及当前日期或时间时，一律以【系统信息】为准，不要凭记忆回答。
7. 用简体中文，条理清楚，控制在 300 字以内。"""

DEVICE_PROMPT = """你是睡眠呼吸机数据解读助手。

回答规则：
1. 用户数据和规则结论是事实来源，知识库只补充解释、限制和低风险建议。
2. 材料里没有的数值、事件类型、症状、睡眠阶段一律不得编造。
3. 不做疾病诊断，不判定严重程度，不建议自行调整治疗压力或设备参数。
4. 单晚数据不能代表长期水平；时间相邻只能说时间关联，不能说因果。
5. P95 表示约 95% 的记录点不高于该值，不是最大值。
6. 数据缺失时必须说明缺失以及因此无法完成的判断。
7. 不要用系统日期去推算「昨晚 / 昨天」是哪一天，也不要在回答里写日期或时间点；
   需要说明数据归属时，照抄材料里的「记录归属日期」。
8. 只能使用【本晚数据与规则结论】里的数字。历史轮次里出现过的数字属于之前的问题，
   不是本轮证据，一律不得复用；本轮材料没有的指标就说未提供，不要从历史里补。
9. 用简体中文，平实简洁，控制在 200 字以内。"""

# 路由模式 -> 校验强度（PRD 4.6.2）


VALIDATION_MODE = {
    "设备数据": "strong",
    "报告生成": "strong",
    "产品文档": "medium",
    "指标解释": "medium",
    "通用对话": "weak",
}

MAX_CONTEXT_TURNS = 10        # PRD 4.1：保留最近 10 轮对话


@dataclass
class UserQuery:
    """Pipeline 的标准输入。

    `user_id` 先作为多用户埋点保留，当前演示环境默认 DEMO-USER；
    真实后台的用户标识、权限和字段映射由入口适配层负责。
    """

    question: str
    history: list = field(default_factory=list)
    use_llm: bool = True
    user_id: str = "DEMO-USER"
    # 入口层解析好的时间指代状态（见 nightdata.resolve_night）：
    # ambiguous / missing_date 时管线要先追问日期，不能默认拿最近一晚作答。
    time_status: str = "none"
    time_note: str = ""
    available_dates: list = field(default_factory=list)

    def __post_init__(self):
        self.question = (self.question or "").strip()
        self.history = list(self.history or [])
        self.time_note = (self.time_note or "").strip()
        self.available_dates = sorted({str(item) for item in (self.available_dates or []) if item})


@dataclass
class AgentResponse(Mapping):
    """Pipeline 的标准输出。

    Mapping 兼容旧版 dict 调用；新入口应优先使用属性和 `to_dict()`。
    `structured` 目前由报告模式填充，后续可承载 App 需要的结构化卡片。
    """

    text: str
    mode: str
    detail: str = ""
    request_id: str = ""
    sources: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    tier: str = "weak"
    validation: dict = field(default_factory=dict)
    risk_flags: list = field(default_factory=list)
    structured: dict | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "mode": self.mode,
            "detail": self.detail,
            "request_id": self.request_id,
            "sources": list(self.sources),
            "candidates": list(self.candidates),
            "tier": self.tier,
            "validation": dict(self.validation),
            "risk_flags": list(self.risk_flags),
            "structured": self.structured,
        }

    def __getitem__(self, key):
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())


def _tier_for_mode(mode: str) -> str:
    base_mode = str(mode).split("（", 1)[0]
    if base_mode in ("安全边界",):
        return "blocked"
    if base_mode in ("系统信息", "型号确认", "意图确认", "功能确认"):
        return "none"
    return VALIDATION_MODE.get(base_mode, "weak")


def _response(text: str, mode: str, detail: str = "", sources=None,
              validation=None, risk_flags=None, structured=None, candidates=None,
              request_id: str = "") -> AgentResponse:
    return AgentResponse(
        text=text,
        mode=mode,
        detail=detail,
        request_id=request_id,
        sources=list(sources or []),
        candidates=list(candidates or []),
        tier=_tier_for_mode(mode),
        validation=validation if validation is not None else {"ok": None, "problems": []},
        risk_flags=list(risk_flags or []),
        structured=structured,
    )


def _log_request(request_id: str, mode: str, intent: str, recall: int,
                 model_used: bool, validation_status: str, elapsed_ms: int,
                 marker: str = "") -> None:
    parts = [
        "[" + time.strftime("%Y-%m-%d %H:%M:%S") + "]",
        "[" + request_id + "]",
    ]
    if marker:
        parts.append("[" + marker + "]")
    parts.extend([
        "[" + mode + "]",
        "[" + intent + "]",
        "召回:" + str(recall),
        "模型:" + ("是" if model_used else "否"),
        "校验:" + validation_status,
        "耗时:" + str(elapsed_ms) + "ms",
    ])
    if marker in ("SAFETY_BLOCK", "VALIDATION_FAIL", "DEGRADE"):
        logger.warning(" ".join(parts))
    else:
        logger.info(" ".join(parts))


def _context_messages(history: list) -> list:
    """把最近 10 轮对话转成模型消息。超出部分自动遗忘最早轮次。"""
    if not history:
        return []
    recent = history[-MAX_CONTEXT_TURNS * 2:]
    return [{"role": item["role"], "content": item["content"]}
            for item in recent if item.get("role") and item.get("content")]


GENERAL_PROMPT = """你是这家睡眠呼吸设备公司的智能助手，既能回答日常问题，也能解答产品使用问题。

回答规则：
1. 日常问题正常回答，简洁、准确、用简体中文。
2. 不得编造产品细节：部件名称、更换周期、错误码含义、保修政策、具体参数等，
   如果问题涉及这些而你没有拿到产品资料，就说明资料未覆盖并建议联系客服。
3. 不给出疾病诊断，不判定病情严重程度，不建议自行调整治疗压力或设备参数。
4. 如果用户描述的是急性身体不适（胸痛、呼吸困难、憋醒等），优先建议就医。
5. 涉及当前日期或时间时，一律以【系统信息】为准，不要凭记忆回答。
6. 控制在 250 字以内。"""


# 路由识别出的主题名与事实层使用的话题名并不一致，这里做一次映射。
# 漏了这一步，「我的 AHI 是多少」会因为找不到 ahi 话题的事实而掉到别的模式去。
FACT_TOPIC_ALIASES = {
    "ahi": "events",
    "respiratory_rate": "breathing_overview",
    "minute_ventilation": "breathing_overview",
}


def _has_time_ref(question: str) -> bool:
    """提问里是否出现时间指代（相对时间或具体日期）。

    出现时间指代说明问的是用户自己的数据；即使句子带「多久」这类产品提问词，
    也应该走设备数据，而不是去查产品资料。
    """
    if any(word in question for word in TIME_REF_WORDS):
        return True
    return bool(re.search(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日?", question))


def _wants_trend(question: str) -> bool:
    return any(term in question for term in TREND_REQUEST_TERMS)


def _asks_device_report_capability(question: str) -> bool:
    """问"主机能不能生成/查看报告"，不是让 Agent 生成报告。"""
    if "报告" not in question:
        return False
    if not any(term in question for term in ("主机", "设备", "屏幕", "机器")):
        return False
    asks = any(term in question for term in
               ("能", "可以", "支持", "有", "是否", "怎么", "在哪", "哪里"))
    question_form = ("吗" in question or "?" in question or "？" in question
                     or "怎么" in question or "在哪" in question or "哪里" in question)
    return asks and question_form


REPORT_CONTENT_TERMS = ("哪些指标", "什么指标", "显示什么", "显示哪些",
                        "包含什么", "包含哪些", "包括什么", "包括哪些",
                        "报告内容", "看什么")
REPORT_GENERATE_TERMS = ("生成", "出个", "给我", "帮我", "提供", "来一份")


def _asks_report_content(question: str) -> bool:
    """问报告里显示什么内容，不是让 Agent 生成报告。"""
    if "报告" not in question:
        return False
    if any(term in question for term in REPORT_GENERATE_TERMS):
        return False
    return any(term in question for term in REPORT_CONTENT_TERMS)


ANALYSIS_TERMS = ("分析一下", "分析下", "帮我分析", "解读一下", "解读下",
                  "评估一下", "分析这份", "分析这个", "看看数据",
                  "综合分析", "对比分析", "趋势分析", "分析最近",
                  "分析过去", "帮我看看数据", "总结一下数据")
ANALYSIS_METRIC_TERMS = ("ahi", "漏气", "压力", "潮气量", "呼吸频率",
                         "分钟通气量", "事件", "模式", "使用时长", "评分")


def _wants_analysis(question: str) -> bool:
    return any(term in question for term in ANALYSIS_TERMS)


def _analysis_metric_table(result: dict) -> list:
    """分析证据表：保留平均值，同时给出中位数、范围和波动。"""
    lines = ["| 指标 | 有效夜晚 | 平均值 | 中位数 | 最低 | 最高 | 首晚→末晚 | 走势 | 波动 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, key, _ in TREND_METRICS:
        item = result["summaries"].get(key)
        if not item or not item.get("count"):
            continue
        unit = item["unit"]
        change_percent = item.get("change_percent")
        percent = ("，%+.1f%%" % change_percent) if change_percent is not None else ""
        lines.append(
            "| " + item["label"] + " | " + str(item["count"])
            + " | " + str(item["mean"]) + unit
            + " | " + str(item["median"]) + unit
            + " | " + str(item["minimum"]) + unit
            + " | " + str(item["maximum"]) + unit
            + " | " + str(item["first"]) + " → " + str(item["last"])
            + "（" + item["direction"] + percent + "）"
            + " | " + item["direction"]
            + " | " + item["stability"]
            + "（" + ("%.1f%%" % (item["volatility"] * 100)
                       if item.get("volatility") is not None else "样本不足") + "） |"
        )
    return lines


def _period_table(result: dict) -> list:
    period = result["period"]
    if not period.get("available"):
        return ["- " + period.get("reason", "暂不做阶段对比")]
    lines = [
        "对比最近 " + str(period["recent_count"]) + " 晚（"
        + str(period["recent_start"]) + " ~ " + str(period["recent_end"])
        + "）与此前 " + str(period["prior_count"]) + " 晚（"
        + str(period["prior_start"]) + " ~ " + str(period["prior_end"]) + "）。",
        "",
        "| 指标 | 前期平均 | 近期平均 | 变化 | 变化% |",
        "|---|---|---|---|---|",
    ]
    for item in period["metrics"]:
        unit = item["unit"]
        percent = item.get("change_percent")
        lines.append(
            "| " + item["label"] + " | " + str(item["prior_mean"]) + unit
            + " | " + str(item["recent_mean"]) + unit
            + " | " + ("+" if (item.get("delta") or 0) > 0 else "")
            + str(item["delta"]) + unit
            + " | " + (("%+.1f%%" % percent) if percent is not None else "—")
            + " |"
        )
    return lines


def _comparison_table_lines(older: dict, newer: dict) -> list:
    """两晚指标对照表，供综合分析和独立报告对比共用。"""
    left = str(older.get("record_date", "未知"))
    right = str(newer.get("record_date", "未知"))
    lines = ["| 指标 | " + left + " | " + right + " | 变化 |",
             "|---|---|---|---|"]
    for label, key, unit in COMPARE_FIELDS:
        lines.append("| " + label + " | " + _fmt_compare(older.get(key), unit)
                     + " | " + _fmt_compare(newer.get(key), unit)
                     + " | " + _diff_compare(older.get(key), newer.get(key), unit) + " |")
    return lines


def _analysis_text(night: nightdata.Night, question: str = "") -> str:
    """综合分析：先给结论，再用对比和统计表提供可复核证据。"""
    facts = {item.fid: item.text for item in night.facts}
    date = str(night.raw.get("record_date", "未知"))
    window_days = analysis.detect_window_days(question)
    result = analysis.analyze(night, window_days)
    quality = result["quality"]
    title = (
        "设备数据分析（请求最近 " + str(window_days) + " 天；当前 "
        + str(quality["record_count"]) + " 个有记录夜晚，"
        + str(quality["first_date"]) + " 至 " + str(quality["last_date"]) + "）"
    )
    lines = [title, "", "【先给结论】"]
    lines += ["- " + item for item in result["conclusions"]]
    lines += ["", "【数据概览】"]
    for fid in ("usage-time", "treat-score", "pressure-main",
                "events-ahi", "leak-main", "leak-mask-seal"):
        if fid in facts:
            lines.append("- " + facts[fid])

    advice = report.summary_advice(night)
    if advice["positives"] or advice["issues"]:
        lines += ["", "【主要发现】"]
        lines += ["- " + item for item in advice["positives"]]
        lines += ["- " + item for item in advice["issues"]]

    if night.history:
        previous = sorted(night.history,
                          key=lambda item: str(item.get("record_date")))[-1]
        lines += ["", "【与上一晚对比】"]
        lines += _comparison_table_lines(previous, night.raw)
    else:
        lines += ["", "【与上一晚对比】", "- 当前数据没有更早的夜晚，暂时无法对比。"]

    lines += ["", "【阶段对比】"]
    lines += _period_table(result)

    lines += ["", "【窗口统计】"]
    lines += _analysis_metric_table(result)

    lines += ["", "【值得回看】"]
    if result["outliers"]:
        for item in result["outliers"]:
            lines.append(
                "- " + item["date"] + " 的" + item["label"] + "为 "
                + str(item["value"]) + item["unit"] + "，相对该窗口常见范围有偏离。"
            )
    else:
        lines.append("- 当前样本中未发现明显偏离窗口常见范围的单个夜晚。")

    actions = list(advice["actions"])
    if quality["observed_days"] < window_days:
        actions.append(
            "继续积累完整记录后再判断长期趋势，当前不把短期结果外推为 "
            + str(window_days) + " 天结论"
        )
    if quality["gap_count"]:
        actions.append("确认无记录日是否由未使用或数据未上传造成，补齐后再看阶段平均值")
    if quality["fragment_count"]:
        actions.append("核对碎片夜是否由分段使用或上传不完整造成")
    if result["period"].get("available"):
        leak = next((item for item in result["period"]["metrics"]
                     if item["key"] == "air_leakage_p95"), None)
        if leak and leak.get("change_percent") is not None \
                and leak["change_percent"] >= 5:
            actions.append("近期 P95 漏气均值上升，优先检查面罩贴合、头带和管路接口")
    actions = list(dict.fromkeys(item for item in actions if item))
    if actions:
        lines += ["", "【建议】"]
        lines += ["- " + item for item in actions]

    lines += ["", "【边界】以上是设备记录的趋势和波动分析，不判断病情严重程度，"
              "不构成医疗诊断，也不建议自行调整治疗参数。"]
    return "\n".join(lines)


ADVICE_TERMS = ("有什么建议", "建议呢", "给点建议", "有什么改进", "怎么改善",
                "有什么可以改进", "下一步建议", "有什么办法", "那建议")
ASSESSMENT_TERMS = ("正常吗", "是否正常", "数据好吗", "数据怎么样",
                   "数据有问题吗", "数据正常吗")


def _wants_advice(question: str) -> bool:
    return any(term in question for term in ADVICE_TERMS)


def _wants_data_assessment(question: str) -> bool:
    return "数据" in question and any(term in question for term in ASSESSMENT_TERMS)


COMPARE_TERMS = ("相比", "对比", "比较", "比上次", "比前一次", "比起来", "变化")
COMPARE_CONTEXT = ("报告", "数据", "两晚", "两次", "上次", "上一次",
                   "前一次", "前一晚")
COMPARE_FIELDS = (
    ("治疗时长", "treat_time_minutes", "分钟"),
    ("设备评分", "treat_score", "分"),
    ("AHI", "ahi", "次/小时"),
    ("平均治疗压力", "treat_pressure_avg", "hPa"),
    ("P95 治疗压力", "treat_pressure_p95", "hPa"),
    ("平均漏气", "air_leakage_avg", "L/min"),
    ("P95 漏气", "air_leakage_p95", "L/min"),
    ("大漏气时长", "large_leak_minutes", "分钟"),
)


def _wants_report_compare(question: str) -> bool:
    dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", question)
    return (any(term in question for term in COMPARE_TERMS)
            and (any(term in question for term in COMPARE_CONTEXT) or len(dates) >= 2))


TREND_INTENT_TERMS = ("这几天", "最近几晚", "最近几天", "最近一周", "最近7天",
                      "近7天", "最近14天", "近两周", "最近30天", "近30天",
                      "最近一个月", "近一个月", "一个月", "趋势", "走势",
                      "变化趋势", "有什么变化", "波动")
TREND_METRICS = analysis.METRICS


def _wants_trend_summary(question: str) -> bool:
    # 带具体指标的"最近7晚 AHI 趋势"仍交给设备数据模式
    if any(term in question.lower() for term in
           ("ahi", "漏气", "压力", "潮气量", "呼吸频率", "分钟通气量")):
        return False
    if any(term in question for term in TREND_INTENT_TERMS):
        return True
    return ("平均" in question
            and any(term in question for term in
                    ("最近", "这几", "几天", "一周", "7天", "14天", "30天", "一个月")))


def _trend_summary_text(night: nightdata.Night, question: str = "") -> str:
    window_days = analysis.detect_window_days(question, default=7)
    result = analysis.analyze(night, window_days)
    quality = result["quality"]
    if not quality["record_count"]:
        return "这几晚没有可用于趋势比较的指标。"
    lines = [
        "多晚数据分析（请求最近 " + str(window_days) + " 天；当前 "
        + str(quality["record_count"]) + " 个有记录夜晚，"
        + str(quality["first_date"]) + " 至 " + str(quality["last_date"]) + "）",
        "",
        "【先给结论】",
    ]
    lines += ["- " + item for item in result["conclusions"]]
    lines += ["", "【阶段对比】"]
    lines += _period_table(result)
    lines += ["", "【窗口统计】"]
    lines += _analysis_metric_table(result)
    lines += ["", "说明：趋势来自设备记录，单晚波动不代表因果，也不构成医疗诊断。"]
    return "\n".join(lines)


def _fmt_compare(value, unit=""):
    if value is None:
        return "未提供"
    if isinstance(value, float):
        value = round(value, 2)
    return str(value) + unit


def _diff_compare(older, newer, unit=""):
    if older is None or newer is None:
        return "—"
    diff = round(float(newer) - float(older), 2)
    return ("+" if diff > 0 else "") + str(diff) + unit


def _compare_text(older: dict, newer: dict) -> str:
    left = str(older.get("record_date", "未知"))
    right = str(newer.get("record_date", "未知"))
    lines = ["报告对比：" + left + "（前一次） → " + right + "（上一次）", ""]
    lines += _comparison_table_lines(older, newer)
    lines += ["", "说明：以上只是两晚设备记录的差异，单晚变化不能代表长期趋势，也不构成医疗诊断。"]
    return "\n".join(lines)


def _compare_pair(night: nightdata.Night, question: str):
    raws = list(night.history) + [night.raw]
    dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", question)
    if len(dates) >= 2:
        picked = []
        for target in dates[:2]:
            parts = [str(int(item)) for item in target.split("-")]
            target = "-".join([parts[0], parts[1].zfill(2), parts[2].zfill(2)])
            for raw in raws:
                if str(raw.get("record_date")) == target:
                    picked.append(raw)
                    break
        if len(picked) == 2:
            picked.sort(key=lambda item: str(item.get("record_date")))
            return picked[0], picked[1]
    history = [raw for raw in night.history if raw.get("record_date")]
    if history:
        history.sort(key=lambda item: str(item.get("record_date")))
        return history[-1], night.raw
    return None


def _prefer_scope(hits: list, scope) -> list:
    """意图子库只做软排序：分数接近第一名时才提前，避免硬过滤漏内容。"""
    if not hits or not scope:
        return hits
    allowed = {str(name) for name in scope}
    top = max(float(hit.get("score") or 0) for hit in hits) or 1.0
    preferred = [
        hit for hit in hits
        if str(hit.get("doc", "")) in allowed
        and float(hit.get("score") or 0) >= top * 0.75
    ]
    if not preferred:
        return hits
    rest = [hit for hit in hits if hit not in preferred]
    return preferred + rest


def _advice_text(night: nightdata.Night) -> str:
    """分析之后的建议：只针对本晚数据，不涉及治疗参数。"""
    advice = report.summary_advice(night)
    raw = night.raw
    basis = []
    if raw.get("treat_time_minutes") is not None:
        basis.append("使用时长 " + str(raw.get("treat_time_minutes")) + " 分钟")
    if raw.get("ahi") is not None:
        basis.append("AHI " + str(raw.get("ahi")))
    if raw.get("air_leakage_p95") is not None:
        basis.append("P95 漏气 " + str(raw.get("air_leakage_p95")) + " L/min")

    lines = ["基于本晚数据的建议", ""]
    if basis:
        lines.append("依据：" + "、".join(basis) + "。")
    if advice["actions"]:
        lines += ["", "建议："] + ["- " + item for item in advice["actions"]]
    else:
        lines += ["", "本晚数据没有触发需要特别调整的规则，保持规律使用并继续观察即可。"]
    if advice["issues"]:
        lines += ["", "需要关注："] + ["- " + item for item in advice["issues"]]
    lines += ["", "以上建议只针对本晚设备和佩戴情况，不涉及治疗参数调整，也不构成医疗诊断。"]
    return "\n".join(lines)


def _is_affirmation(text: str) -> bool:
    cleaned = re.sub(r"[\s，。！？、,.!?]", "", text or "")
    if not cleaned:
        return False
    return cleaned in AFFIRM_TERMS or (
        len(cleaned) <= 4 and cleaned.startswith(("是", "对", "好", "可以", "行", "嗯")))


def resolve_pending_confirmation(question: str, history: list):
    """把"是的"接回上一轮的时间确认。

    上一轮问"是否看最近一次使用 2026-10-15"，用户回"是的"时，
    恢复成原问题加具体日期，例如"我2026-10-15的ahi是多少"。
    """
    if not history or not _is_affirmation(question):
        return question, ""
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if item.get("role") != "assistant":
            continue
        match = LAST_USE_PATTERN.search(str(item.get("content") or ""))
        if not match:
            continue
        day = match.group(1)
        previous = ""
        for cursor in range(index - 1, -1, -1):
            if history[cursor].get("role") == "user":
                previous = str(history[cursor].get("content") or "")
                break
        if not previous:
            return question, ""
        rewritten = re.sub(r"(昨晚|昨天|前天|前一天)", day, previous, count=1)
        if rewritten == previous:
            rewritten = day + " " + previous
        return rewritten, "按上一轮确认取 " + day
    return question, ""


def _trend_text(night: nightdata.Night, decision: dict, question: str) -> str:
    """跨晚单指标趋势：按用户要求的 7/14/30 天窗口计算。"""
    topics = decision.get("topics") or ["overall"]
    key_map = {
        "leakage": "air_leakage_p95",
        "pressure": "treat_pressure_avg",
        "events": "ahi",
        "ahi": "ahi",
        "breathing_overview": "minute_ventilation_avg_lpm",
        "respiratory_rate": "respiratory_rate_avg",
        "minute_ventilation": "minute_ventilation_avg_lpm",
        "overall": "ahi",
    }
    key = key_map.get(topics[0], "ahi")
    return analysis.metric_summary_text(
        night, key, analysis.detect_window_days(question, default=7)
    )


def _is_metric_definition(question: str, knowledge_hits: list) -> bool:
    """「X 是什么意思」这类问题，优先给指标解释，而不是产品文档原文。"""
    if not knowledge_hits:
        return False
    if not any(term in question for term in DEFINITION_TERMS):
        return False
    title = str(knowledge_hits[0].get("title", ""))
    lowered = question.lower()
    for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", title):
        if len(token) >= 2 and token.lower() in lowered:
            return True
    return False


def _asks_product_modes(question: str) -> bool:
    """问产品有哪些模式（设备功能），而不是本晚用了什么模式。"""
    if "模式" not in question or _has_time_ref(question):
        return False
    if "产品" in question or "型号" in question:
        return True
    if PRODUCT_MODE_SUFFIX.search(question):
        return True
    return bool(PRODUCT_MODE_PATTERN.search(question))


def _facts_for(user_id: str, night: nightdata.Night, decision: dict, question: str) -> list:
    if decision["topics"] != ["overall"]:
        picked = []
        seen = set()
        for topic in decision["topics"]:
            fact_topic = FACT_TOPIC_ALIASES.get(topic, topic)
            for item in nightdata.facts_by_topic(user_id, night, fact_topic):
                if item.fid not in seen:
                    seen.add(item.fid)
                    picked.append(item)
        if picked:
            return picked
    # 整体性提问，或者指明了某一晚，都给全部事实
    if any(term in question for term in OVERALL_TERMS) or _has_time_ref(question):
        return list(night.facts)
    return []


# 整体性提问的信号。注意不要放裸的「数据」——
# 「登录后别人能看到我的数据吗」是产品问题，不是要看本晚数值。
OVERALL_TERMS = ("整体", "总结", "概览", "总体", "复盘", "全部", "所有", "本晚", "这晚",
                 "数据怎么样", "看数据", "看看数据", "数据如何")

PERIOD_REPORT_TERMS = (
    "这一周", "本周", "这周", "最近一周", "近7天", "近七天", "近一周",
    "上周", "这一月", "本月", "这月", "最近一月", "近30天", "近三十天",
    "近一个月",
)


# 产品"怎么做"的提问信号：出现这些词时优先查产品资料，而不是走设备数据
# 注意不要放裸的「怎么」——「今天天气怎么样」会因此被误判成产品提问
# 时间指代：出现这些词说明问的是用户自己的数据
TIME_REF_WORDS = ("昨晚", "昨天", "前天", "前晚", "大前天",
                  "前一天", "前一晚", "上一晚", "上一天",
                  "最近", "最新", "这几天", "近几天", "这一晚", "那晚",
                  "上次", "上一次", "上回")

TREND_REQUEST_TERMS = ("趋势", "走势", "最近几晚", "连续几晚", "这几天",
                       "最近7晚", "最近七晚", "近7晚", "近七晚",
                       "最近30晚", "最近30天")

DEFINITION_TERMS = ("什么是", "是什么", "什么意思", "含义", "怎么理解", "指什么", "代表什么")
PERSONAL_TERMS = ("我的", "我昨晚", "我最近", "我这", "昨晚", "昨天", "本晚", "当前", "今天")

AFFIRM_TERMS = ("是的", "是", "对", "对的", "好", "好的", "可以", "行", "嗯",
                "确认", "看", "看看", "看一下", "是的看这一晚")
LAST_USE_PATTERN = re.compile(r"最近一次使用是\s*(\d{4}-\d{2}-\d{2})")

# "产品模式有哪些""有哪些工作模式"：问的是产品能力，不是本晚用了什么模式
PRODUCT_MODE_PATTERN = re.compile(r"(哪些|哪几种|几种|什么|支持).{0,4}模式")
# 倒装说法："模式有哪些""模式包括哪几种"
PRODUCT_MODE_SUFFIX = re.compile(r"模式.{0,6}(有哪些|有哪几种|有什么|包括|支持|列出)")

HOWTO_TERMS = ("多久", "怎么办", "怎么用", "怎么做", "怎么清洗", "怎么连接", "怎么设置",
               "怎么换", "如何", "怎样", "更换", "清洗", "清洁", "安装", "连接", "设置",
               "操作", "说明书", "报错", "故障", "能不能", "可以吗", "哪里", "坏了",
               "换一次", "换多久", "怎么保养", "怎么维护",
               "用什么", "用多少", "要多久", "需要多久",
               "什么时候换", "什么时候更换", "该换", "需要换", "要不要换", "需不需要换")

PRODUCT_FEATURE_TERMS = (
    "自动漏气补偿", "漏气补偿", "压力释放", "延时升压", "压力上升坡度",
    "自动开机", "自动关机", "漏气提示", "参数锁定", "加温湿化器",
    "湿化器", "加温管路", "工作模式", "产品介绍", "工作原理",
)

# 产品/设备名词：出现这些词但资料没有命中时，给用户「你是指」候选，
# 而不是硬套通用对话或拿无关的设备数据顶上。
PRODUCT_SIGNAL_TERMS = (
    "呼吸机", "机器", "设备", "面罩", "鼻罩", "口鼻罩", "管路", "管子",
    "滤芯", "过滤棉", "湿化", "水盒", "加温", "app", "蓝牙", "报错", "故障",
    "报警", "噪音", "声音", "耗材", "硅胶", "头带", "电池", "屏幕", "按键",
    "模式", "参数", "延时升压", "消毒", "保修", "售后", "配件",
)


def _option(kind: str, title: str, value: str = "", source: str = "",
            visibility: str = "") -> dict:
    """统一的「下一步可以点什么」选项。

    type=date/intent/topic，value 是用户点选后回传的内容；
    旧字段 title/source/visibility 保留，前端不改也能继续用。
    """
    return {
        "type": kind,
        "title": title,
        "value": value or title,
        "source": source,
        "visibility": visibility,
    }


# 意图不明确时的固定选项：每条都能被自己消化，不依赖上下文。
INTENT_OPTIONS = (
    _option("intent", "产品使用与故障排查", "我想咨询产品使用或故障排查的问题"),
    _option("intent", "看我的治疗数据", "查看我最近一晚的治疗数据"),
    _option("intent", "生成单晚报告", "生成最近一晚的治疗报告"),
    _option("intent", "解释指标含义", "解释 AHI、漏气量这类指标的含义"),
)


VISIBILITY_LABELS = {
    "internal": "内部资料",
    "examples": "示例资料",
}


def _source_label(hit: dict) -> str:
    prefix = VISIBILITY_LABELS.get(hit.get("visibility"), "")
    label = hit["doc"] + " · " + hit["section"]
    return prefix + " · " + label if prefix else label


def _sources_for_mode(mode: str, doc_hits: list, knowledge_hits: list, picked: list) -> list:
    base_mode = str(mode).split("（", 1)[0]
    if base_mode == "产品文档":
        return [_source_label(hit) for hit in doc_hits]
    if base_mode == "设备数据":
        return [item.fid for item in picked] + [item["id"] for item in knowledge_hits]
    if base_mode == "指标解释":
        return [item["id"] for item in knowledge_hits]
    return []


def _excerpt(text: str, max_chars: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = max(cut.rfind("\n"), cut.rfind("。"), cut.rfind("；"))
    if boundary >= max_chars // 2:
        cut = cut[:boundary + 1]
    return cut.rstrip() + "\n……（后续内容已省略）"


def _doc_fallback(hits: list) -> str:
    """没有大模型时，直接把资料原文给出来 —— 相当于一次文档检索。"""
    if not hits:
        return NO_MATERIAL_TEXT
    lines = ["我在产品资料里查到以下内容（当前未启用大模型，直接给出原文）：", ""]
    for index, hit in enumerate(hits, 1):
        lines.append("【" + str(index) + "】" + _source_label(hit))
        lines.append(_plain_markdown(_excerpt(hit["text"])))
        lines.append("")
    return "\n".join(lines).strip()


def _plain_markdown(text: str) -> str:
    """规则版直接展示原文时去掉 Markdown 标记，避免终端里出现 > 和 **。"""
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*>\s?", "", line)
        line = line.replace("**", "")
        lines.append(line)
    return "\n".join(lines)


DEBUG_DIR = Path(__file__).resolve().parents[1] / ".local" / "debug"


def dump_rejected(text: str, problems: list, directory=None):
    """把被校验拒绝的模型输出落到本机 .local/debug，便于排查。

    只在 SLEEPAGENT_DEBUG_LLM=1 时启用，且不写 logs/agent.log——
    日志里长期留存治疗数据不合适，调试文件默认也不进仓库。
    """
    if os.getenv("SLEEPAGENT_DEBUG_LLM") != "1":
        return None
    target_dir = Path(directory) if directory else DEBUG_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / ("rejected-" + time.strftime("%Y%m%d-%H%M%S") + ".txt")
        path.write_text("problems: " + "；".join(problems) + "\n\n" + (text or ""),
                        encoding="utf-8")
        return path
    except OSError:
        return None


NO_MATERIAL_TEXT = (
    "我这边没有查到相关资料，如实说不知道，就不猜了。\n"
    "你可以换一种说法再问，或者联系客服获取准确答复。"
)

GENERAL_FALLBACK = (
    "抱歉，这次我没能给出可靠的回答。\n"
    "可以换个说法再问一次；如果是产品使用相关的问题，我会去查产品资料。\n"
    "也可以直接联系人工客服，把您的问题转过去。"
)


def answer(query: UserQuery | str, night: nightdata.Night,
           history: list | None = None, use_llm: bool = True,
           user_id: str = "DEMO-USER") -> AgentResponse:
    """运行对话管线。

    新入口传 `UserQuery`；旧入口传字符串时自动包装，保持既有调用兼容。
    """
    if isinstance(query, UserQuery):
        request = query
    else:
        request = UserQuery(question=str(query), history=history or [],
                            use_llm=use_llm, user_id=user_id)
    question = request.question
    history = request.history
    use_llm = request.use_llm
    if night.user_id != request.user_id:
        raise ValueError("用户上下文不一致：请求用户与治疗夜数据用户不同")
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()

    def emit(text: str, mode: str, detail: str = "", sources=None,
             validation=None, risk_flags=None, structured=None, candidates=None,
             intent: str = "", recall: int = 0, model_used: bool = False,
             marker: str = "") -> AgentResponse:
        validation_status = "未执行"
        if isinstance(validation, dict):
            if validation.get("ok") is True:
                validation_status = "通过"
            elif validation.get("ok") is False:
                validation_status = "未通过"
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        _log_request(request_id, mode, intent, recall, model_used,
                     validation_status, elapsed_ms, marker)
        return _response(
            text, mode, detail, sources=sources, validation=validation,
            risk_flags=risk_flags, structured=structured, candidates=candidates,
            request_id=request_id,
        )

    # ---------- 1) 安全边界：固定话术，不经过大模型 ----------
    verdict = safety.check(question)
    if verdict.kind != "in_scope":
        return emit(verdict.reply, "安全边界", verdict.reason,
                    validation={"ok": True, "problems": []},
                    risk_flags=[verdict.kind], intent=verdict.kind,
                    marker="SAFETY_BLOCK")

    model_hint = product_models.suggest(question)
    if model_hint:
        observed = model_hint["observed"]
        canonical = model_hint["canonical"]
        if canonical == "YA70":
            reply = (
                "你输入的型号“" + observed + "”可能是笔误。你是指 YA70 系列吗？"
                "YA70 系列包含 YA70A、YA70B20、YA70B25、YA70Pro。"
                "请确认具体型号后，我再帮你查对应资料。"
            )
        else:
            reply = (
                "你输入的型号“" + observed + "”可能是笔误。你是指 " + canonical + " 吗？"
                "确认型号后，我再帮你查对应资料。"
            )
        return emit(
            reply,
            "型号确认",
            "近似型号：" + observed + " -> " + canonical,
            validation={"ok": True, "problems": []},
            risk_flags=["model_typo"],
            intent="model_confirm",
        )

    feature_hint = product_features.suggest(question)
    if feature_hint:
        options = "、".join(feature_hint["options"])
        return emit(
            "说明书没有明确提供名为“" + feature_hint["feature"] + "”的设置项。"
            "你是指 " + options + " 中的哪一个？请确认具体功能后我再查。",
            "功能确认",
            "功能词需要确认：" + feature_hint["feature"],
            validation={"ok": True, "problems": []},
            risk_flags=["feature_ambiguity"],
            intent="feature_confirm",
        )

    # ---------- 2) 对话层：问候、能力询问、报告触发 ----------
    had_greeting, rest = social.split_leading_greeting(question)
    if had_greeting and len(rest) >= 4:
        question = rest
    period_report = (
        any(term in question for term in PERIOD_REPORT_TERMS)
        and ("报告" in question or "周报" in question or "月报" in question)
    )
    if period_report:
        return emit(
            "当前版本只支持单晚治疗报告，近 7 天/近 30 天的周期报告暂未支持。"
            "你可以指定某一晚，例如“生成 2026-10-15 的报告”，"
            "我会按该晚及之前的数据生成单晚报告。",
            "周期报告（未支持）",
            "周期报告功能未实现",
            validation={"ok": True, "problems": []},
            risk_flags=["period_report_unsupported"],
            intent="report",
        )

    # ---------- 2.1) 时间说不清楚就问，不拿"最近一晚"顶替 ----------
    if request.time_status in ("ambiguous", "missing_date", "stale_relative"):
        dates = list(request.available_dates)
        if request.time_status == "missing_date":
            time_detail = request.time_note or "数据里没有该日期"
        elif request.time_status == "stale_relative":
            time_detail = "系统日期与数据不一致：" + (request.time_note or "相对日期")
        else:
            time_detail = "时间指代不明确：" + (request.time_note or "未给出时间")
        candidates = [_option("date", day, day) for day in dates]
        if request.time_status == "stale_relative" and dates:
            # 把「昨天」这类词换成最近一次记录日期，用户点一下就能直接看到那一晚
            latest = dates[-1]
            word = (request.time_note or "").split("（")[0]
            value = (question.replace(word, latest, 1)
                     if word and word in question else latest)
            others = [day for day in dates if day != latest][-2:]
            candidates = ([_option("date", "上一次使用（" + latest + "）", value)]
                          + [_option("date", day, day) for day in reversed(others)])
        return emit(
            nightdata.time_clarification(request.time_status,
                                         request.time_note,
                                         dates),
            "时间确认",
            time_detail,
            validation={"ok": True, "problems": []},
            risk_flags=["needs_date_confirmation"],
            candidates=candidates,
            intent="time_clarify",
        )

    intent, _ = social.classify(question)
    social_intent = intent
    if intent == "report" and (_asks_device_report_capability(question)
                               or _asks_report_content(question)):
        # "主机能生成报告吗 / 报告显示哪些指标"是产品功能问题，交给下面的检索
        intent = "product_query"
    # 综合分析是总入口：对比、平均、变化和趋势都要在同一份结果里出现。
    # 带具体指标的“分析一下漏气”仍交给设备数据，避免扩大成整份报告。
    if _wants_analysis(question) and not any(
            term in question for term in ANALYSIS_METRIC_TERMS):
        return emit(
            _analysis_text(night, question),
            "数据分析",
            "综合分析：趋势结论 + 两晚对比 + 阶段对比 + 波动与偏离日",
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            intent="analysis",
        )
    if _wants_trend_summary(question):
        return emit(
            _trend_summary_text(night, question),
            "趋势分析",
            "多晚趋势结论 + 阶段对比 + 统计证据",
            sources=[key for _, key, _ in TREND_METRICS],
            validation={"ok": True, "problems": []},
            intent="trend_summary",
        )
    if _wants_report_compare(question):
        pair = _compare_pair(night, question)
        if pair:
            return emit(
                _compare_text(pair[0], pair[1]),
                "报告对比",
                "两晚指标对比",
                sources=[key for _, key, _ in COMPARE_FIELDS],
                validation={"ok": True, "problems": []},
                intent="compare",
            )
        return emit(
            "当前只有这一晚的记录，没有前一晚可以对比。",
            "报告对比（数据不足）",
            "缺少可对比的夜晚",
            validation={"ok": True, "problems": []},
            risk_flags=["missing_field"],
            intent="compare",
        )
    if _wants_advice(question) and not any(
            term in question for term in ANALYSIS_METRIC_TERMS):
        return emit(
            _advice_text(night),
            "数据建议",
            "基于本晚确定性事实",
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            intent="advice",
        )
    if _wants_data_assessment(question):
        # 沿用入口层当前选定的夜晚（没有新时间词时不会重置到最近一晚）
        return emit(
            _analysis_text(night, question),
            "数据分析",
            "按当前夜晚评估：" + str(night.raw.get("record_date", "未知")),
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            intent="assessment",
        )
    if intent == "report":
        payload = report.render(night)
        acceptance = payload["acceptance"]
        problems = [failure for item in acceptance.get("results", [])
                    for failure in item.get("failures", [])]
        return emit(payload["text"], "报告生成",
                    "验收" + ("通过" if acceptance["passed"] else "未通过"),
                    validation={"ok": acceptance["passed"], "problems": problems},
                    risk_flags=[] if acceptance["passed"] else ["report_acceptance_failed"],
                    structured=report.render_structured(night),
                    intent="report",
                    marker="" if acceptance["passed"] else "VALIDATION_FAIL")
    if intent in ("greeting", "capability", "identity", "thanks", "unclear", "empty"):
        menu = list(INTENT_OPTIONS) if intent in ("capability", "unclear", "empty") else []
        return emit(social.reply(intent, night), "对话", "意图 " + intent,
                    candidates=menu, intent=intent)

    if runtime_info.is_datetime_question(question):
        return emit(runtime_info.answer(question), "系统信息",
                    "本机时间，未经过模型", intent="datetime")

    decision = route(question)
    if decision["goal"] == "out_of_scope":
        return emit(social.OUT_OF_SCOPE_TEXT, "范围外", "命中范围外关键词",
                    intent=decision["goal"])

    # ---------- 3) 选模式 ----------
    #   设备数据 / 产品文档 / 指标解释 / 通用对话
    #   判定顺序说明：
    #     · 有"怎么做"这类产品提问信号且查到资料 → 产品文档优先
    #       （否则「面罩多久换」会被设备数据抢走，答成漏气数值）
    #     · 其余情况下如果事实层有对应指标 → 设备数据
    #     · 只命中知识条目 → 指标解释
    #     · 都没有 → 通用对话
    picked = _facts_for(request.user_id, night, decision, question)
    knowledge_hits = knowledge.retrieve(question, top_k=2)
    is_howto = any(term in question for term in HOWTO_TERMS)
    is_product_feature = any(term in question for term in PRODUCT_FEATURE_TERMS)
    product_priority = is_howto or is_product_feature or _asks_product_modes(question)

    # 时间指代（昨晚 / 前天 / 具体日期）说明问的是用户自己的数据，
    # 此时即使带「多久」这类产品提问词，也应该走设备数据
    has_time_ref = _has_time_ref(question)

    # 问「趋势」时要给跨晚对比，不然只回单晚数值，用户会以为趋势就是当晚
    trend_text = ""
    if _wants_trend(question) and (picked or has_time_ref):
        trend_text = _trend_text(night, decision, question)

    if "therapy_mode" in decision["topics"] and has_time_ref:
        actual_mode = night.raw.get("therapy_mode") or night.raw.get("mode")
        if not actual_mode:
            return emit(
                "本晚数据没有提供治疗模式，无法确定昨晚实际使用的是 "
                "CPAP、AutoCPAP、S 还是 AutoS。设备型号不能替代治疗模式记录。",
                "设备数据（缺字段）",
                "未提供 therapy_mode",
                sources=["therapy_mode"],
                validation={"ok": True, "problems": []},
                risk_flags=["missing_field"],
                intent=decision["goal"],
            )

    # 问「产品模式有哪些」时用「工作模式」检索：说明书里的章节叫「1.3工作模式」
    search_question = "工作模式有哪些" if _asks_product_modes(question) else question
    service = service_intent.detect(search_question)
    preferred_docs = service["scope"] if service["confidence"] >= 0.7 else None
    doc_hits = product_kb.retrieve(search_question, top_k=6)
    doc_hits = _prefer_scope(doc_hits, preferred_docs)[:3]
    raw_doc_hits = list(doc_hits)
    if not doc_hits and product_priority:
        # 「面罩多久换」这类问题只匹配到一个词，覆盖率刚好不到严格阈值。
        # 产品提问信号明确时放宽一次，避免漏检；通用问题不会走到这里。
        doc_hits = product_kb.retrieve(search_question, top_k=3,
                                       min_score=3.0, min_coverage=0.2)

    # 错误码/型号类实体必须真实出现在命中内容里，否则视为没有资料
    codes = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]\d{1,3}(?![A-Za-z0-9])", question)
    if codes and doc_hits:
        # 示例资料不能作为错误码的正式依据
        doc_hits = [hit for hit in doc_hits
                    if str(hit.get("visibility")) != "examples"]
        joined = "\n".join(
            str(hit.get("section", "")) + " " + str(hit.get("text", ""))
            for hit in doc_hits
        ).lower()
        if not doc_hits or not all(code.lower() in joined for code in codes):
            doc_hits = []
            raw_doc_hits = []
    doc_hits = product_kb.trim_low_relevance(doc_hits, question=question)

    # 定义类问题优先用字段说明（09_报告指标说明.md），而不是说明书里顺带提到该词的段落
    is_definition_query = (any(term in question for term in DEFINITION_TERMS)
                           and not any(term in question for term in PERSONAL_TERMS))
    if is_definition_query and doc_hits:
        # trim_low_relevance 可能已经把字段说明裁掉，从原始命中里补回来
        glossary = [hit for hit in raw_doc_hits
                    if "报告指标说明" in str(hit.get("doc", ""))]
        if glossary:
            doc_hits = (glossary + [hit for hit in doc_hits if hit not in glossary])[:3]
    elif doc_hits:
        # 非定义问题（如"面罩怎么清洁"）不要被字段表的"面罩得分"抢走
        trimmed = [hit for hit in doc_hits
                   if "报告指标说明" not in str(hit.get("doc", ""))]
        if trimmed:
            # 保留 trim_low_relevance 的裁剪结果，避免把无关章节又加回来
            doc_hits = trimmed
        else:
            product_hits = [hit for hit in raw_doc_hits
                            if "报告指标说明" not in str(hit.get("doc", ""))]
            if product_hits:
                doc_hits = product_hits[:3]

    primary_hits = [hit for hit in raw_doc_hits
                    if not str(hit.get("doc", "")).startswith("10_")]
    replacement_guidance = product_features.replacement_guidance(question, primary_hits)
    if replacement_guidance:
        return emit(
            replacement_guidance,
            "产品文档（未覆盖）",
            "未找到面罩固定更换周期",
            validation={"ok": True, "problems": []},
            risk_flags=["missing_field"],
            intent=decision["goal"],
        )

    if (doc_hits and product_priority
            and product_features.candidate_needed(
        question, doc_hits[0].get("section", "")
    )):
        candidate_hits = [
            hit for hit in raw_doc_hits
            if hit.get("section") not in ("注意", "表格")
            and product_features.candidate_matches(question, hit.get("section", ""))
        ]
        if not candidate_hits:
            # 只检索到「注意 / 表格」这类碎片时，改用近似章节做选项，
            # 否则用户看到的候选就是两条一模一样的「注意」。
            candidate_hits = product_kb.related_hits(question, top_k=3)
        candidates, seen_sections = [], set()
        for hit in candidate_hits:
            section = str(hit.get("section", "")).strip()
            if not section or section in seen_sections or section in ("注意", "表格"):
                continue
            seen_sections.add(section)
            candidates.append(
                _option("topic", section, section,
                        source=_source_label(hit),
                        visibility=hit.get("visibility", ""))
            )
            if len(candidates) >= 3:
                break
        if candidates:
            options = "\n".join(
                str(index) + ". " + item["title"]
                for index, item in enumerate(candidates, 1)
            )
            return emit(
                "我不确定你具体指哪一项。你是指下面这些吗？\n" + options
                + "\n直接回复主题名称（或点选上面的选项），我再按那一条查。",
                "产品文档（需要确认）",
                "章节名未直接匹配，返回候选主题",
                sources=[item["source"] for item in candidates],
                validation={"ok": True, "problems": []},
                risk_flags=["needs_confirmation"],
                candidates=candidates,
                intent=decision["goal"],
            )
        # 连候选都凑不出来，说明这次检索没有可用结果
        doc_hits = []

    llm_intent = None
    intent_detail = "意图 " + decision["goal"]
    if use_llm and social_intent is None and not doc_hits and not picked and not knowledge_hits:
        llm_intent = intent_fallback.classify(question)
        if llm_intent:
            intent_detail += " / LLM意图:" + llm_intent["intent"]

    if llm_intent and llm_intent["intent"] == "clarify":
        return emit(
            "我还不能确定你想问哪一类，你需要的是？\n"
            "产品使用与故障排查 / 看治疗数据 / 生成单晚报告 / 解释指标含义，"
            "说一个方向，或者换个说法问我。",
            "意图确认",
            intent_detail,
            validation={"ok": True, "problems": []},
            risk_flags=["intent_clarify"],
            candidates=list(INTENT_OPTIONS),
            intent=llm_intent["intent"],
        )
    if llm_intent and llm_intent["intent"] == "out_of_scope":
        return emit(social.OUT_OF_SCOPE_TEXT, "范围外", intent_detail,
                    intent=llm_intent["intent"])

    # ---------- 3.0) 查不到 → 给近似候选，而不是硬答或硬套设备数据 ----------
    wants_product_help = product_priority or any(
        term in question.lower() for term in PRODUCT_SIGNAL_TERMS)
    if (wants_product_help and not doc_hits and not knowledge_hits
            and not (has_time_ref and picked)):
        related = product_kb.related_hits(question, top_k=3)
        if codes:
            # 错误码只认真实产品资料，示例资料里的 E3 是占位内容
            related = [
                hit for hit in related
                if str(hit.get("visibility")) != "examples"
                and all(code.lower() in (
                    str(hit.get("section", "")) + " " + str(hit.get("text", ""))
                ).lower() for code in codes)
            ]
        if related:
            candidates = [
                _option("topic", hit.get("section", ""), hit.get("section", ""),
                        source=_source_label(hit),
                        visibility=hit.get("visibility", ""))
                for hit in related
            ]
            options = "\n".join(
                str(index) + ". " + item["title"]
                for index, item in enumerate(candidates, 1)
            )
            return emit(
                "正式产品资料里没有和这个问题直接对应的内容。你是指下面这些吗？\n" + options
                + "\n直接回复主题名称（或点选上面的选项），我再按那一条查；"
                  "也可以把问题说得更具体些。",
                "产品文档（未命中）",
                "检索未命中，返回近似候选",
                sources=[item["source"] for item in candidates],
                validation={"ok": True, "problems": []},
                risk_flags=["needs_topic_confirmation"],
                candidates=candidates,
                intent=decision["goal"],
                recall=len(related),
            )
        return emit(
            "正式产品资料里没有找到和这个问题相关的内容，我不会猜。"
            "如果是设备报错或故障，建议联系客服或售后确认。你需要的是？\n"
            "产品使用与故障排查 / 看治疗数据 / 生成单晚报告 / 解释指标含义，"
            "说一个方向，或者换个说法再问。",
            "资料未覆盖",
            "检索与近似候选均为空",
            validation={"ok": True, "problems": []},
            risk_flags=["no_material"],
            candidates=list(INTENT_OPTIONS),
            intent=decision["goal"],
        )

    if llm_intent and llm_intent["intent"] == "product_docs":
        mode = "产品文档"
    elif llm_intent and llm_intent["intent"] == "device_data":
        mode = "设备数据"
    elif llm_intent and llm_intent["intent"] == "metric_explain":
        mode = "指标解释"
    elif is_definition_query and doc_hits:
        # "什么是 AHI" 问的是定义，不是我这晚的数值
        mode = "产品文档"
    elif is_definition_query and knowledge_hits:
        mode = "指标解释"
    elif has_time_ref and picked:
        mode = "设备数据"
        if not any(item.fid == "data-coverage" for item in picked):
            picked = [nightdata.coverage_fact(request.user_id, night)] + picked
    elif doc_hits and product_priority:
        mode = "产品文档"
    elif picked:
        mode = "设备数据"
        # 带上数据范围：否则对话侧看不到历史夜晚，会和报告给出的夜晚数打架
        if not any(item.fid == "data-coverage" for item in picked):
            picked = [nightdata.coverage_fact(request.user_id, night)] + picked
    elif _is_metric_definition(question, knowledge_hits):
        mode = "指标解释"
    elif doc_hits:
        mode = "产品文档"
    elif knowledge_hits:
        mode = "指标解释"
    else:
        mode = "通用对话"

    vector_degraded = (
        mode in ("产品文档", "设备数据", "指标解释")
        and not embeddings.available()
    )
    fallback_reason = ""
    if use_llm:
        if mode == "产品文档":
            material = product_kb.render(doc_hits)
            system = PRODUCT_PROMPT
            user_prefix = SYS + "【产品资料】\n" + material + "\n\n【用户问题】\n"
        elif mode == "设备数据":
            material = "\n".join(item.text for item in picked)
            if trend_text:
                material += "\n" + trend_text
            material += "\n" + knowledge.render(knowledge_hits)
            system = DEVICE_PROMPT
            user_prefix = DEVICE_SYS + "【本晚数据与规则结论】\n" + material + "\n\n【用户问题】\n"
        elif mode == "指标解释":
            material = knowledge.render(knowledge_hits)
            system = DEVICE_PROMPT
            user_prefix = SYS + "【指标解释资料】\n" + material + "\n\n【用户问题】\n"
        else:
            material = question
            system = GENERAL_PROMPT
            user_prefix = SYS + "【用户问题】\n"

        messages = [{"role": "system", "content": system}]
        messages.extend(_context_messages(history))
        messages.append({"role": "user", "content": user_prefix + question + "\n\n请据此回答。"})
        result = llm.chat(messages)
        if result["status"] == "success":
            check = validation.check_output(result["content"], material,
                                            mode=VALIDATION_MODE.get(mode, "strong"),
                                            question=question,
                                            extra_numbers=(
                                                product_kb.section_numbers()
                                                if mode == "产品文档" else None))
            logger.info("request mode=%s goal=%s hits=%d llm=ok",
                        mode, decision["goal"], len(doc_hits) + len(knowledge_hits))
            if check["ok"]:
                return emit(
                    result["content"],
                    mode + "（" + result["provider"] + " / " + result["model"] + "，已校验）",
                    "意图 " + decision["goal"],
                    sources=_sources_for_mode(mode, doc_hits, knowledge_hits, picked),
                    validation=check,
                    intent=decision["goal"],
                    recall=len(doc_hits) + len(knowledge_hits),
                    model_used=True,
                    marker="DEGRADE" if vector_degraded else "",
                )
            logger.warning("validation_failed problems=%s", "；".join(check["problems"]))
            dump_rejected(result["content"], check["problems"])
            fallback_reason = "模型输出未通过校验，已回退：" + "；".join(check["problems"])
            fallback_validation = check
            fallback_flags = ["validation_failed"]
            fallback_marker = "VALIDATION_FAIL"
        else:
            fallback_reason = "模型调用失败（" + result["status"] + "）：" + result["detail"]
            fallback_validation = {"ok": None, "problems": [fallback_reason]}
            fallback_flags = ["llm_error"]
            fallback_marker = "DEGRADE"
    else:
        fallback_validation = {"ok": None, "problems": []}
        fallback_flags = ["llm_disabled"]
        fallback_marker = ""

    # ---------- 4) 回退 ----------
    if mode == "产品文档":
        return emit(
            _doc_fallback(doc_hits), "产品文档（规则版）",
            fallback_reason or "未启用大模型",
            sources=[_source_label(hit) for hit in doc_hits],
            validation=fallback_validation,
            risk_flags=fallback_flags,
            intent=decision["goal"],
            recall=len(doc_hits) + len(knowledge_hits),
            model_used=bool(use_llm),
            marker=fallback_marker or ("DEGRADE" if vector_degraded else ""),
        )
    if mode == "设备数据":
        parts = ["\n".join(item.text for item in picked)]
        if trend_text:
            parts.append(trend_text)
        if knowledge_hits:
            top = knowledge_hits[0]
            parts.append("\n".join([top["content"], "限制：" + top["limitations"], "建议：" + top["safe_actions"]]))
        return emit(
            "\n\n".join(parts), "设备数据（规则版）",
            fallback_reason or "未启用大模型",
            sources=[item.fid for item in picked] + [item["id"] for item in knowledge_hits],
            validation=fallback_validation,
            risk_flags=fallback_flags,
            intent=decision["goal"],
            recall=len(picked) + len(knowledge_hits),
            model_used=bool(use_llm),
            marker=fallback_marker or ("DEGRADE" if vector_degraded else ""),
        )
    if mode == "指标解释":
        top = knowledge_hits[0]
        return emit(
            "\n".join([top["content"], "限制：" + top["limitations"], "建议：" + top["safe_actions"]]),
            "指标解释（规则版）",
            fallback_reason or "未启用大模型",
            sources=[item["id"] for item in knowledge_hits],
            validation=fallback_validation,
            risk_flags=fallback_flags,
            intent=decision["goal"],
            recall=len(picked) + len(knowledge_hits),
            model_used=bool(use_llm),
            marker=fallback_marker or ("DEGRADE" if vector_degraded else ""),
        )
    return emit(GENERAL_FALLBACK, "通用对话（回退）",
                fallback_reason or "未启用大模型",
                validation=fallback_validation,
                risk_flags=fallback_flags,
                candidates=list(INTENT_OPTIONS),
                intent=decision["goal"],
                recall=len(picked) + len(knowledge_hits),
                model_used=bool(use_llm),
                marker=fallback_marker)


