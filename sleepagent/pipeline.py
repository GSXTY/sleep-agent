"""对话管线：路由 -> 事实/检索 -> 模型 -> 校验 -> 输出。

入口层（命令行、将来的 API）只做参数解析和结果展示，
所有业务判定都在这里，保证两个入口行为一致。
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sleepagent import (analysis, embeddings, intent_fallback, knowledge, llm, nightdata,
                        product_features, product_kb, product_models, report,
                        report_insight, query_planner, router, runtime_info, safety,
                        service_intent, social, validation)
from sleepagent.router import route
from sleepagent.document_text import clean_heading

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
1. 只依据【产品资料】回答产品相关问题。正文不要写资料文件名、章节号、
   “根据《…》”或“出处为…”等来源说明；来源由客户端在“查看来源”中单独展示。
2. 资料里没有写的内容，直接说资料未覆盖，并建议联系客服或售后；绝不允许自己补全。
3. 不要编造型号、部件更换周期、错误码含义、保修政策或任何产品参数。
4. 如果资料与问题明显无关，就说明没有查到相关资料，不要牵强引用。
5. 不给出疾病诊断，不建议用户自行调整治疗压力或设备参数；涉及这类问题应引导联系医生。
6. 涉及当前日期或时间时，一律以【系统信息】为准，不要凭记忆回答。
7. 先给出用户所需的结论或操作步骤；多个问题逐项回答，资料缺失的那一项单独说明，不遗漏其他可回答的问题。
8. 用简体中文，条理清楚，控制在 300 字以内。"""

PRODUCT_MODEL_SCOPE_PROMPT = """

【型号范围说明】
用户问题中提到了 YA70 系列型号。只要资料文件或章节属于 YA70 系列，
正文中的“本系列设备”可以视为对该 YA70 系列的一般说明，不要因为正文没有
逐字重复型号而回答“资料未覆盖 YA70”。
同时必须保留系列口径：不能声称所有 YA70 子型号完全相同；资料没有区分具体
子型号时，说明“资料按 YA70 系列描述，具体子型号以说明书或设备界面为准”。
"""

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
    "App报告解读": "strong",
    "产品文档": "medium",
    "指标解释": "medium",
    "通用对话": "weak",
}

MAX_CONTEXT_TURNS = 10
CONTEXT_TURNS_BY_MODE = {
    "产品文档": 2,
    "设备数据": 3,
    "指标解释": 1,
    "通用对话": 4,
}
LLM_MAX_TOKENS_BY_MODE = {
    "产品文档": 240,
    "设备数据": 280,
    "指标解释": 240,
    "通用对话": 320,
}
PUBLIC_CACHE_TTL_SECONDS = 3600
PUBLIC_CACHE_MAX_ENTRIES = 256
PRODUCT_LLM_TIMEOUT_SECONDS = 8.0
_public_cache = {}
_public_cache_lock = threading.Lock()


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


def _log_model_failure(request_id: str, result: dict) -> None:
    """记录可定位的失败类别，不把用户材料、凭据或上游响应正文写入日志。"""
    status = result.get("status", "unknown")
    detail = str(result.get("detail", "")).lower()
    reason = {"empty": "empty_content", "busy": "queue_full",
              "unconfigured": "missing_config"}.get(status, "upstream_error")
    if any(word in detail for word in ("timeout", "timed out", "超时")):
        reason = "timeout"
    elif "401" in detail:
        reason = "authentication"
    elif "429" in detail:
        reason = "rate_limit"
    elif "404" in detail:
        reason = "endpoint_or_model_missing"
    elif any(word in detail for word in ("无法连接", "refused", "connection", "ssl")):
        reason = "connection_error"
    logger.warning("[%s] [MODEL_FAILURE] status=%s reason=%s", request_id, status, reason)


def _log_request(request_id: str, mode: str, intent: str, recall: int,
                 model_used: bool, validation_status: str, elapsed_ms: int,
                 marker: str = "", token_usage=None) -> None:
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
    if token_usage:
        parts.append(
            "Tokens:" + str(token_usage.get("input_tokens", 0))
            + "/" + str(token_usage.get("output_tokens", 0))
        )
    if marker in ("SAFETY_BLOCK", "VALIDATION_FAIL", "DEGRADE"):
        logger.warning(" ".join(parts))
    else:
        logger.info(" ".join(parts))


def _context_messages(history: list, max_turns: int = MAX_CONTEXT_TURNS) -> list:
    """只把指定轮数的最近对话转成模型消息。"""
    if not history or max_turns <= 0:
        return []
    recent = history[-max_turns * 2:]
    return [{"role": item["role"], "content": item["content"]}
            for item in recent if item.get("role") and item.get("content")]


CONTEXT_DEPENDENT_TERMS = (
    "它", "这个", "那个", "这些", "那些", "上述", "前面", "刚才",
    "继续", "还有", "再问", "再说", "同样", "这款", "那款", "刚才的",
)


def _needs_history(question: str) -> bool:
    return any(term in (question or "") for term in CONTEXT_DEPENDENT_TERMS)


def _public_cache_key(question: str):
    """公共问题缓存键；带上下文指代的问题不缓存。"""
    text = re.sub(r"\s+", "", (question or "").casefold())
    text = re.sub(r"[，。！？、,.!?;；:：()（）\[\]【】]", "", text)
    if len(text) < 4 or _needs_history(text):
        return None
    return text


def clear_public_cache() -> None:
    with _public_cache_lock:
        _public_cache.clear()


def _public_cache_get(question: str):
    key = _public_cache_key(question)
    if not key:
        return None
    now = time.monotonic()
    with _public_cache_lock:
        item = _public_cache.get(key)
        if not item:
            return None
        if item["expires_at"] <= now:
            _public_cache.pop(key, None)
            return None
        return copy.deepcopy(item["payload"])


def _public_cache_put(question: str, payload: dict) -> None:
    key = _public_cache_key(question)
    if not key:
        return
    now = time.monotonic()
    with _public_cache_lock:
        if len(_public_cache) >= PUBLIC_CACHE_MAX_ENTRIES:
            oldest = next(iter(_public_cache), None)
            if oldest is not None:
                _public_cache.pop(oldest, None)
        _public_cache[key] = {
            "expires_at": now + PUBLIC_CACHE_TTL_SECONDS,
            "payload": copy.deepcopy(payload),
        }


GENERAL_PROMPT = """你是这家睡眠呼吸设备公司的智能助手，既能回答日常问题，也能解答产品使用问题。

回答规则：
1. 日常问题正常回答，简洁、准确、用简体中文。
2. 不得编造产品细节：部件名称、更换周期、错误码含义、保修政策、具体参数等，
   如果问题涉及这些而你没有拿到产品资料，就说明资料未覆盖并建议联系客服。
3. 不给出疾病诊断，不判定病情严重程度，不建议自行调整治疗压力或设备参数。
4. 如果用户描述的是急性身体不适（胸痛、呼吸困难、憋醒等），优先建议就医。
5. 涉及当前日期或时间时，一律以【系统信息】为准，不要凭记忆回答。
6. 先识别本轮要完成的任务并直接交付结果：要求写作就给出成稿，要求比较就给出比较，要求步骤就给出步骤，不只说“可以帮你”。
7. 多个问题逐项覆盖；遵循用户指定的格式、语言和长度。只在缺少完成任务所必需的信息时追问，其他细节作合理假设并简短说明。
8. 历史只用于理解指代；用户切换任务或纠正要求时，以本轮为准。不要声称已经完成没有工具或执行证据的操作。
9. 默认控制在 250 字以内；用户明确要求的篇幅或完整任务优先。"""


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
    if any(term in question for term in ANALYSIS_TERMS):
        return True
    return re.search(r"(?:^|请|帮我|想|需要)分析", question or "") is not None


def _is_generic_analysis_request(question: str) -> bool:
    if not _wants_analysis(question):
        return False
    if any(term in question.lower() for term in ANALYSIS_METRIC_TERMS):
        return False
    if report_insight.should_handle(question) or _wants_report_compare(question):
        return False
    return True


def _analysis_followup_options() -> tuple:
    return (
        _option("followup", "和前一天比较", "和前一天比较"),
        _option("followup", "最近7天有什么变化", "最近7天有什么变化"),
    )


DAY_CHANGE_FOLLOWUPS = ("有什么变化", "哪里变了", "变化呢", "有什么不同")


def _last_was_day_analysis(history: list) -> bool:
    for item in reversed(history or []):
        if item.get("role") != "assistant":
            continue
        return str(item.get("content") or "").startswith("单日数据分析")
    return False


def _day_analysis_text(night: nightdata.Night) -> str:
    """只分析指定一天，不混入历史对比或趋势。"""
    facts = {item.fid: item.text for item in night.facts}
    lines = [
        "单日数据分析（" + str(night.raw.get("record_date", "未知")) + "）",
        "",
        "【数据概览】",
    ]
    for fid in ("usage-time", "treat-score", "pressure-main",
                "events-ahi", "breathing-main", "leak-main", "leak-mask-seal"):
        if fid in facts:
            lines.append("- " + facts[fid])

    raw = night.raw
    component_lines = []
    for label, key, meaning in (
        ("时长得分", "treat_duration_score", "反映使用时长"),
        ("面罩得分", "mask_score", "反映面罩佩戴和漏气"),
        ("呼吸事件得分", "resp_score", "反映 AHI 和呼吸事件记录"),
    ):
        value = raw.get(key)
        if value is not None:
            component_lines.append(
                "- " + label + " " + str(value) + " 分，" + meaning + "。"
            )
    if raw.get("treat_score") is not None or component_lines:
        lines += ["", "【评分构成】"]
        if raw.get("treat_score") is not None:
            lines.append("- 设备总分 " + str(raw.get("treat_score")) + " 分。")
        lines += component_lines

    advice = report.summary_advice(night)
    findings = advice["positives"] + advice["issues"]
    if findings:
        lines += ["", "【主要发现】"]
        lines += ["- " + item for item in findings]
    if advice["actions"]:
        lines += ["", "【建议】"]
        lines += ["- " + item for item in advice["actions"]]
    lines += [
        "",
        "如需继续分析，可以问“和前一天比较”或“最近7天有什么变化”。",
        "",
        "【边界】以上只描述这一晚的设备记录，不构成医疗诊断。",
    ]
    return "\n".join(lines)


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


def _analysis_text(night: nightdata.Night, question: str = "",
                   as_report: bool = False, window_days: int | None = None) -> str:
    """综合分析：先给结论，再用对比和统计表提供可复核证据。"""
    facts = {item.fid: item.text for item in night.facts}
    date = str(night.raw.get("record_date", "未知"))
    window_days = window_days or analysis.detect_window_days(question)
    result = analysis.analyze(night, window_days)
    quality = result["quality"]
    if as_report:
        title = (
            _period_label(window_days) + "治疗分析报告（按已有记录："
            + str(quality["record_count"]) + " 个有记录夜晚，"
            + str(quality["first_date"]) + " 至 " + str(quality["last_date"]) + "）"
        )
    else:
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

    score = result.get("score") or {}
    if score.get("current") is not None:
        lines += ["", "【评分构成分析】", "- " + score["conclusion"]]
        for item in score.get("components", []):
            lines.append("- " + item["explanation"])

    if night.history:
        previous = sorted(night.history,
                          key=lambda item: str(item.get("record_date")))[-1]
        current_date = str(night.raw.get("record_date") or "")
        previous_date = str(previous.get("record_date") or "")
        try:
            consecutive = (
                dt.date.fromisoformat(current_date)
                - dt.date.fromisoformat(previous_date)
            ).days == 1
        except ValueError:
            consecutive = False
        comparison_title = (
            "【与上一晚对比】" if consecutive
            else "【与上一条有记录夜晚（" + previous_date + "）对比】"
        )
        lines += ["", comparison_title]
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
    for recommendation in result.get("recommendations", []):
        if ("面罩" in recommendation
                and any("面罩" in item or "头带" in item for item in actions)):
            continue
        actions.append(recommendation)
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


SCORE_ANALYSIS_TERMS = (
    "评分", "分数", "得分", "总分", "设备总分", "治疗总分",
)
SCORE_DEFINITION_TERMS = (
    "治疗评分", "设备评分", "总评分", "总分",
    "时长得分", "治疗时长得分", "面罩得分", "呼吸事件得分",
)
SCORE_ANALYSIS_HINTS = (
    "变化", "分析", "为什么", "原因", "下降", "上升", "降低", "提高",
    "怎么样", "趋势", "解读", "构成", "扣分",
)
PLAN_SCORE_METRICS = {
    "treatment_score", "duration_score", "mask_score", "resp_event_score",
}
def _wants_score_analysis(question: str) -> bool:
    text = question or ""
    return (
        any(term in text for term in SCORE_ANALYSIS_TERMS)
        and any(term in text for term in SCORE_ANALYSIS_HINTS)
    )


def _score_analysis_text(night: nightdata.Night, question: str = "") -> str:
    window_days = analysis.detect_window_days(question, default=7)
    result = analysis.analyze(night, window_days)
    score = result["score"]
    summary = score.get("window_summary") or {}
    period = score.get("period_comparison")
    conclusion = score["conclusion"]
    if "下降" in (question or "") and score.get("delta") is not None \
            and score["delta"] >= 0:
        conclusion = "记录显示评分没有下降。" + conclusion
    elif "上升" in (question or "") and score.get("delta") is not None \
            and score["delta"] <= 0:
        conclusion = "记录显示评分没有上升。" + conclusion
    lines = [
        "设备评分分析（" + str(night.raw.get("record_date", "未知"))
        + "；最近 " + str(window_days) + " 天窗口）",
        "",
        "【结论】",
        "- " + conclusion,
    ]
    if summary:
        lines += [
            "",
            "【最近 " + str(window_days) + " 天评分】",
            "- 有效夜晚 " + str(summary["count"]) + " 个，平均值 "
            + str(summary["mean"]) + " 分，最低 " + str(summary["minimum"])
            + " 分，最高 " + str(summary["maximum"]) + " 分。",
            "- 首晚到末晚 " + str(summary["first"]) + " → " + str(summary["last"])
            + " 分，走势为" + summary["direction"] + "，波动"
            + summary["stability"] + "。",
        ]
    if period:
        lines.append(
            "- 最近 " + str(result["period"]["recent_count"])
            + " 晚平均 " + str(period["recent_mean"]) + " 分，此前 "
            + str(result["period"]["prior_count"]) + " 晚平均 "
            + str(period["prior_mean"]) + " 分，阶段变化 "
            + str(period["delta"]) + " 分。"
        )

    lines += [
        "",
        "【评分分项解释】",
    ]
    for item in score.get("components", []):
        lines += [
            "- " + item["label"] + "：",
            "  " + item["explanation"],
            "  建议：" + item["advice"],
        ]

    lines += ["", "【建议】"]
    recommendations = result.get("recommendations") or score.get("suggestions") or [
        "继续观察连续多晚评分和三个分项的变化。"
    ]
    lines += ["- " + item for item in recommendations]
    lines += [
        "",
        "【边界】评分分项和同晚指标用于解释记录变化，不把相关性写成因果，"
        "也不构成医疗诊断或参数调整建议。",
    ]
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
                   "前一次", "前一晚", "前一天")
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


def _trend_summary_text(night: nightdata.Night, question: str = "",
                        window_days: int | None = None) -> str:
    window_days = window_days or analysis.detect_window_days(question, default=7)
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


def _knowledge_definition_answer(item: dict) -> str:
    """把指标定义合成一段完整回答，避免定义、限制、建议被拆成多次结果。"""
    parts = [str(item.get("content") or "").strip()]
    limitation = str(item.get("limitations") or "").strip()
    if limitation and limitation not in parts[0]:
        parts.append(limitation)
    safe_actions = str(item.get("safe_actions") or "").strip()
    if safe_actions and safe_actions not in " ".join(parts):
        parts.append(safe_actions)
    return " ".join(part for part in parts if part)


def _split_compound_product_question(question: str) -> list:
    """拆分一个输入里的多个独立产品问题，最多处理 3 项。"""
    text = str(question or "").strip()
    text = re.sub(r"(顺便问一下|另外|还有|以及)", "，", text)
    raw_parts = re.split(r"[，,；;？?]\s*", text)
    parts = []
    for part in raw_parts:
        value = part.strip().strip("。.!！ ")
        if len(value) >= 3 and value not in parts:
            parts.append(value)
    return parts if 2 <= len(parts) <= 3 else []


def _compound_product_groups(question: str) -> list:
    groups = []
    for clause in _split_compound_product_question(question):
        service = service_intent.detect(clause)
        preferred = service["scope"] if service["confidence"] >= 0.7 else None
        hits = product_kb.retrieve(clause, top_k=4)
        hits = _prefer_scope(hits, preferred)
        hits = product_kb.trim_low_relevance(hits, question=clause)
        if not hits:
            return []
        groups.append((clause, hits[:2]))
    return groups


def _asks_product_modes(question: str) -> bool:
    """问产品有哪些模式（设备功能），而不是本晚用了什么模式。"""
    if "模式" not in question or _has_time_ref(question):
        return False
    if "产品" in question or "型号" in question:
        return True
    if PRODUCT_MODE_SUFFIX.search(question):
        return True
    return bool(PRODUCT_MODE_PATTERN.search(question))


def _asks_product_principle(question: str) -> bool:
    """问产品工作原理，而不是某一晚的设备数据。"""
    return "工作原理" in (question or "") and not _has_time_ref(question)


def _direct_product_section(question: str) -> str | None:
    """只有完整的单一章节问题才能直读，复合任务和个人数据仍走原管线。"""
    if _has_time_ref(question):
        return None
    text = product_models.strip_known_models(question)
    text = re.sub(r"[\s？?。！!，,]", "", text)
    prefix = (
        r"(?:请问|请介绍一下|介绍一下|帮我查一下|请告诉我)?"
        r"(?:这个|这款|本系列)?(?:设备|产品|呼吸机)?的?"
    )
    modes = (
        r"(?:(?:有|支持)?(?:哪些|什么|几种)(?:工作|治疗|通气)?模式|"
        r"(?:工作|治疗|通气)?模式(?:都?有(?:哪些|什么|几种)|有哪些|是什么))"
    )
    if re.fullmatch(prefix + modes + r"(?:呢|吗)?", text):
        return "工作模式"
    if re.fullmatch(prefix + r"工作原理(?:是什么|是怎样的|是怎么样的)?(?:呢|吗)?", text):
        return "工作原理"
    return None


def _direct_product_hits(section: str) -> list:
    """直读正式说明书，不探测向量服务，不触发索引更新。缺少正文时交回检索。"""
    return [
        {"doc": chunk.doc, "section": chunk.section, "text": chunk.text,
         "index": chunk.index, "visibility": chunk.visibility}
        for chunk in product_kb.load_chunks()
        if chunk.doc == "01_产品说明.md" and chunk.visibility == "user"
        and clean_heading(chunk.section) == section and chunk.text.strip()
    ]


def _mode_comparison_key(question: str) -> str:
    text = re.sub(r"[\s？?。]", "", clean_heading(question).lower())
    text = re.sub(r"^(?:请问|帮我解释一下|解释一下)", "", text)
    text = text.replace("模式", "").replace("与", "和")
    text = re.sub(r"(?:有什么不同|有什么差别|有啥区别|区别是什么|区别在哪)$", "有什么区别", text)
    return text


def _direct_mode_comparison(question: str) -> dict | None:
    """只匹配已审阅的模式比较 FAQ，不用模糊相似度代替用户的其他任务。"""
    key = _mode_comparison_key(question)
    if key not in ("单水平和双水平有什么区别", "双水平和单水平有什么区别",
                   "cpap和autocpap有什么区别", "autocpap和cpap有什么区别"):
        return None
    if key.startswith("双水平"):
        key = "单水平和双水平有什么区别"
    elif key.startswith("autocpap"):
        key = "cpap和autocpap有什么区别"
    for chunk in product_kb.load_chunks():
        if (chunk.doc != "10_YA70A常见问题.md" or chunk.visibility != "user"
                or _mode_comparison_key(chunk.section) != key):
            continue
        # 只提取标准答案；缺失时交回完整检索，避免输出维护说明。
        answer = re.fullmatch(r"\s*-\s*\*\*标准答案\*\*\s*[：:]\s*(.+?)\s*", chunk.text, re.S)
        if answer:
            return {"doc": chunk.doc, "section": chunk.section,
                    "text": answer.group(1), "index": chunk.index,
                    "visibility": chunk.visibility}
    return None


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

WEEK_SCOPE_TERMS = (
    "一周", "1周", "7天", "七天", "14天", "十四天", "两周",
    "这一周", "本周", "这周", "上周", "最近一周", "近一周",
    "近7天", "近七天", "最近7天", "周报", "这几天", "最近几天",
)
MONTH_SCOPE_TERMS = (
    "一个月", "1个月", "30天", "三十天", "这一月", "本月", "这月",
    "上个月", "最近一月", "近一月", "最近一个月", "近一个月",
    "近30天", "近三十天", "月报",
)
DAY_SCOPE_TERMS = (
    "单晚", "一晚", "昨晚", "昨天", "今天", "今日", "前天", "前晚",
    "大前天", "前一天", "前一晚", "上一晚", "上一天", "最近一晚",
    "最新一晚", "最近一次",
)
SITUATION_TERMS = (
    "我的情况", "使用情况", "睡眠情况", "治疗情况", "整体情况",
    "总体情况", "数据情况", "最近情况", "我的数据怎么样",
    "数据怎么样", "数据正常吗", "数据有问题吗", "数据好吗",
    "睡得怎么样", "睡眠怎么样", "最近怎么样",
)


# 产品"怎么做"的提问信号：出现这些词时优先查产品资料，而不是走设备数据
# 注意不要放裸的「怎么」——「今天天气怎么样」会因此被误判成产品提问
# 时间指代：出现这些词说明问的是用户自己的数据
TIME_REF_WORDS = ("昨晚", "昨天", "今天", "今日", "前天", "前晚", "大前天",
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

    type=date/scope/intent/topic，value 是用户点选后回传的内容；
    旧字段 title/source/visibility 保留，前端不改也能继续用。
    """
    return {
        "type": kind,
        "title": _section_heading(title) if kind == "topic" else title,
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

GREETING_OPTIONS = (
    _option("intent", "生成单晚报告", "生成最近一晚的治疗报告"),
    _option("intent", "什么是 AHI", "什么是 AHI"),
    _option("intent", "滤芯多久换一次", "滤芯多久换一次"),
    _option("intent", "查看工作模式", "YA70有什么工作模式"),
)


def _explicit_report_scope(question: str):
    """返回 day/week/month；只认明确范围，不把“最近”猜成 30 天。"""
    text = re.sub(r"\s+", "", question or "")
    if (any(term in text for term in MONTH_SCOPE_TERMS)
            or re.search(r"(?:最近|近)\d{1,2}天", text)
            and any(int(days) >= 28 for days in re.findall(r"(\d{1,2})天", text))):
        return "month"
    if any(term in text for term in WEEK_SCOPE_TERMS):
        return "week"
    if (re.search(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日?", text)
            or any(term in text for term in DAY_SCOPE_TERMS)):
        return "day"
    return None


def _is_generic_report_request(question: str) -> bool:
    if (_asks_device_report_capability(question)
            or _asks_report_content(question)
            or _wants_report_compare(question)
            or report_insight.should_handle(question)):
        return False
    intent, _ = social.classify(question)
    return intent == "report"


def _is_situation_request(question: str) -> bool:
    text = re.sub(r"\s+", "", question or "")
    if any(term in text for term in SITUATION_TERMS):
        return True
    context = ("我的", "使用", "睡眠", "治疗", "数据", "整体", "总体",
               "漏气", "压力", "ahi", "呼吸", "这周", "本周", "本月",
               "最近")
    if "情况" in text and any(term in text for term in context):
        return True
    if "怎么样" in text and any(term in text for term in context):
        return True
    return False


def _scope_confirmation_options(kind: str) -> tuple:
    if kind == "report":
        values = (
            ("单晚报告", "生成最近一晚的治疗报告"),
            ("近 7 天报告", "生成最近7天的治疗报告"),
            ("近 30 天报告", "生成最近30天的治疗报告"),
        )
    elif kind == "analysis":
        values = (
            ("单日分析", "分析最近一晚"),
            ("近 7 天分析", "分析最近7天"),
            ("近 30 天分析", "分析最近30天"),
        )
    else:
        values = (
            ("最近一晚", "查看最近一晚的使用情况"),
            ("近 7 天", "查看最近7天的使用情况"),
            ("近 30 天", "查看最近30天的使用情况"),
        )
    return tuple(_option("scope", title, value) for title, value in values)


def _period_days(question: str, scope: str) -> int:
    text = re.sub(r"\s+", "", question or "")
    if "两周" in text or "14天" in text or "十四天" in text:
        return 14
    if scope == "week":
        return 7
    return 30


def _period_label(days: int) -> str:
    return "近 " + str(days) + " 天"


def _is_period_report_request(question: str, scope) -> bool:
    if scope not in ("week", "month"):
        return False
    return _is_generic_report_request(question) or _is_situation_request(question)


def _scope_from_history(history: list):
    """追问没有新时间词时，沿用紧邻上一轮用户明确说过的单晚/周/月。"""
    for item in reversed(history or []):
        if item.get("role") != "user":
            continue
        return _explicit_report_scope(str(item.get("content") or ""))
    return None


def _scope_only_value(question: str):
    text = re.sub(r"[\s，。！？、,.!?]", "", question or "").lower()
    mapping = {
        "单晚": "day", "一晚": "day", "一天": "day", "当天": "day",
        "最近一晚": "day",
        "一周": "week", "7天": "week", "七天": "week",
        "两周": "14", "14天": "14", "十四天": "14",
        "一个月": "month", "30天": "month", "三十天": "month",
        "最近一个月": "month",
    }
    return mapping.get(text)


def _pending_scope_mode(history: list):
    """上一轮是否是范围确认，以及当时问的是报告还是情况。"""
    for item in reversed(history or []):
        if item.get("role") != "assistant":
            continue
        content = str(item.get("content") or "")
        if "你希望生成哪一类报告" in content:
            return "report"
        if "你想查看哪个时间范围" in content:
            return "situation"
        return None
    return None


def _rewrite_scope_only_answer(question: str, history: list) -> str:
    scope = _scope_only_value(question)
    mode = _pending_scope_mode(history)
    if not scope or not mode:
        return question
    if scope == "day":
        period = "最近一晚"
    elif scope == "14":
        period = "最近14天"
    elif scope == "week":
        period = "最近7天"
    else:
        period = "最近30天"
    if mode == "report":
        return "生成" + period + "的治疗报告"
    return "查看" + period + "的使用情况"


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


def _section_heading(section: str) -> str:
    """从内部章节标识中提取面向用户的主题名，不暴露文件名和章节编号。"""
    heading = str(section or "").split(" · ")[-1].strip()
    heading = clean_heading(heading)
    return heading.strip(" ：:·-")


def _doc_fallback(hits: list) -> str:
    """没有大模型时，直接把资料原文给出来 —— 相当于一次文档检索。"""
    if not hits:
        return NO_MATERIAL_TEXT
    lines = []
    seen_headings = set()
    for hit in hits:
        heading = _section_heading(hit.get("section", ""))
        if (heading and heading not in ("注意", "表格")
                and heading not in seen_headings):
            lines.append("【" + heading + "】")
            seen_headings.add(heading)
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
    llm_usage = {}

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
                     validation_status, elapsed_ms, marker, llm_usage)
        response = _response(
            text, mode, detail, sources=sources, validation=validation,
            risk_flags=risk_flags, structured=structured, candidates=candidates,
            request_id=request_id,
        )
        if (use_llm
                and str(mode).split("（", 1)[0] == "产品文档"
                and isinstance(validation, dict)
                and validation.get("ok") is True
                and not risk_flags):
            _public_cache_put(question, response.to_dict())
        return response

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

    question = _rewrite_scope_only_answer(question, history)
    if use_llm:
        cached = _public_cache_get(question)
        if cached:
            cached["request_id"] = request_id
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            _log_request(
                request_id, str(cached.get("mode", "产品文档")), "cache",
                len(cached.get("sources") or []), False, "通过", elapsed_ms,
                "CACHE_HIT",
            )
            return AgentResponse(**cached)

    compound_groups = []
    if (not _has_time_ref(question)
            and not _is_generic_report_request(question)
            and not _is_generic_analysis_request(question)
            and not _is_situation_request(question)):
        compound_groups = _compound_product_groups(question)
    if compound_groups:
        material_lines = []
        source_hits = []
        for index, (clause, hits) in enumerate(compound_groups, 1):
            material_lines.append("【子问题" + str(index) + "】" + clause)
            material_lines.append(product_kb.render(hits))
            source_hits.extend(hits)
        material = "\n\n".join(material_lines)
        fallback_reason = ""
        fallback_validation = {"ok": None, "problems": []}
        fallback_flags = ["llm_disabled"]
        fallback_marker = ""
        if use_llm:
            system = (
                PRODUCT_PROMPT
                + (PRODUCT_MODEL_SCOPE_PROMPT
                   if product_models.find_known_model(question) else "")
                + "\n\n用户本轮包含多个独立问题。必须按【子问题】顺序逐项回答，"
                  "不能只回答第一个；资料未覆盖的子问题要明确说明。"
            )
            messages = [{"role": "system", "content": system}]
            context_turns = 2 if _needs_history(question) else 0
            messages.extend(_context_messages(history, max_turns=context_turns))
            messages.append({
                "role": "user",
                "content": SYS + "【产品资料】\n" + material
                           + "\n\n【用户问题】\n" + question + "\n\n请逐项回答。",
            })
            result = llm.chat(messages, max_tokens=360,
                              timeout_seconds=PRODUCT_LLM_TIMEOUT_SECONDS)
            llm_usage = result.get("usage") or {}
            if result["status"] == "success":
                check = validation.check_output(
                    result["content"], material, mode="medium",
                    question=question,
                    extra_numbers=product_kb.section_numbers(),
                )
                if check["ok"]:
                    return emit(
                        result["content"],
                        "产品文档（" + result["provider"] + " / "
                        + result["model"] + "，已校验）",
                        "复合问题：" + str(len(compound_groups)) + " 项",
                        sources=[
                            _source_label(hit) for hit in source_hits
                        ],
                        validation=check,
                        intent="compound_product",
                        recall=len(source_hits),
                        model_used=True,
                    )
                logger.warning(
                    "validation_failed problems=%s", "；".join(check["problems"])
                )
                dump_rejected(result["content"], check["problems"])
                fallback_reason = (
                    "模型输出未通过校验，已回退：" + "；".join(check["problems"])
                )
                fallback_validation = check
                fallback_flags = ["validation_failed"]
                fallback_marker = "VALIDATION_FAIL"
            else:
                _log_model_failure(request_id, result)
                fallback_reason = (
                    "模型调用失败（" + result["status"] + "）：" + result["detail"]
                )
                fallback_validation = {
                    "ok": None,
                    "problems": [fallback_reason],
                }
                fallback_flags = ["llm_error"]
                fallback_marker = "DEGRADE"

        lines = []
        for clause, hits in compound_groups:
            lines.append("【" + clause + "】")
            lines.append(_plain_markdown(_excerpt(hits[0]["text"], 600)))
            lines.append("")
        return emit(
            "\n".join(lines).strip(),
            "产品文档（规则版）",
            fallback_reason or "未启用大模型",
            sources=[_source_label(hit) for hit in source_hits],
            validation=fallback_validation,
            risk_flags=fallback_flags,
            intent="compound_product",
            recall=len(source_hits),
            model_used=bool(use_llm),
            marker=fallback_marker,
        )

    scope = _explicit_report_scope(question)
    report_request = _is_generic_report_request(question)
    situation_request = _is_situation_request(question)
    analysis_request = _is_generic_analysis_request(question)
    # “生成报告”必须只认当前句子的明确范围；上一轮日期只用于追问“情况”。
    if scope is None and situation_request and not report_request:
        scope = _scope_from_history(history)

    # 问“情况”或让 Agent 生成报告时，不默认猜单晚。
    if (scope is None
            and request.time_status not in ("ambiguous", "missing_date", "stale_relative")
            and (report_request or situation_request or analysis_request)):
        if report_request:
            prompt = (
                "你希望生成哪一类报告？\n"
                "1. 单晚报告\n"
                "2. 近 7 天报告\n"
                "3. 近 30 天报告"
            )
        elif analysis_request:
            prompt = (
                "你想分析哪个范围？\n"
                "1. 单日分析\n"
                "2. 近 7 天分析\n"
                "3. 近 30 天分析"
            )
        else:
            prompt = (
                "你想查看哪个时间范围？\n"
                "1. 最近一晚\n"
                "2. 近 7 天\n"
                "3. 近 30 天"
            )
        return emit(
            prompt,
            "范围确认",
            "未明确单晚、近7天或近30天",
            validation={"ok": True, "problems": []},
            risk_flags=["needs_scope_confirmation"],
            candidates=list(_scope_confirmation_options(
                "report" if report_request else "analysis" if analysis_request else "situation"
            )),
            intent="scope_clarify",
        )

    if _is_period_report_request(question, scope):
        period_days = _period_days(question, scope)
        payload = report_insight.build(night, period_days, question=question)
        structured = payload["structured"]
        return emit(
            _analysis_text(night, question, as_report=True, window_days=period_days),
            "报告生成（周期）",
            _period_label(period_days) + "；实际使用 "
            + str(structured["window"]["record_count"]) + " 个有记录夜晚",
            sources=[key for _, key, _ in TREND_METRICS],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="period_report",
        )

    if (analysis_request and scope == "day"
            and request.time_status not in ("ambiguous", "missing_date", "stale_relative")):
        return emit(
            _day_analysis_text(night),
            "单日分析",
            "仅分析当前选定日期，不自动比较历史",
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            candidates=list(_analysis_followup_options()),
            intent="day_analysis",
        )

    if analysis_request and scope in ("week", "month"):
        window_days = _period_days(question, scope)
        structured = report_insight.build(
            night, window_days
        )["structured"]
        return emit(
            _analysis_text(night, question, window_days=window_days),
            "数据分析",
            _period_label(window_days) + "综合数据分析",
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="analysis",
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
        if request.time_status in ("stale_relative", "missing_date") and dates:
            # 缺日时同时提供“最近一次使用”和附近日期，用户不用重新描述整句。
            latest = dates[-1]
            target = ""
            date_match = re.search(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日?",
                                   question)
            if date_match:
                target = date_match.group(0)
            elif request.time_status == "stale_relative":
                target = (request.time_note or "").split("（")[0]
            value = question.replace(target, latest, 1) if target else latest
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

    if (_last_was_day_analysis(history)
            and any(term in question for term in DAY_CHANGE_FOLLOWUPS)
            and _explicit_report_scope(question) is None):
        pair = _compare_pair(night, "和前一天比较")
        if pair:
            return emit(
                _compare_text(pair[0], pair[1]),
                "报告对比",
                "延续上一轮单日分析，仅回答与上一条有记录夜晚的变化",
                sources=[key for _, key, _ in COMPARE_FIELDS],
                validation={"ok": True, "problems": []},
                intent="day_change_followup",
            )

    # App 报告解读：直接读取与 App 同源的字段，返回可由前端渲染的板块洞察。
    # 它不生成图表、不识别截图，也不另算一套趋势口径。
    if report_insight.should_handle(question):
        window_days = analysis.detect_window_days(question)
        payload = report_insight.build(
            night, window_days, report_insight.detect_module(question), question
        )
        return emit(
            payload["text"],
            "App报告解读",
            "结构化板块解读：评分 / 呼吸事件 / 呼吸概况 / 漏气 / 压力",
            sources=payload["structured"]["source_fields"],
            validation={"ok": True, "problems": []},
            structured=payload["structured"],
            intent="app_report_insight",
        )

    comparison_hit = _direct_mode_comparison(question)
    if comparison_hit:
        return emit(
            comparison_hit["text"], "产品文档（规则版）", "模式比较：标准答案直读",
            sources=[_source_label(comparison_hit)],
            validation={"ok": True, "problems": []},
            intent="mode_comparison", recall=1,
        )

    direct_section = _direct_product_section(question)
    if direct_section:
        direct_hits = _direct_product_hits(direct_section)
        if direct_hits:
            return emit(
                _doc_fallback(direct_hits), "产品文档（规则版）",
                "说明书章节直读：" + direct_section,
                sources=list(dict.fromkeys(_source_label(hit) for hit in direct_hits)),
                validation={"ok": True, "problems": []},
                intent="product_section", recall=len(direct_hits),
            )

    semantic_plan = query_planner.plan(question, use_llm=use_llm)
    plan_metrics = set(semantic_plan.get("metrics") or [])
    plan_intent = semantic_plan.get("intent")
    plan_days = semantic_plan.get("window_days") or 0
    plan_detail = (
        "查询计划:" + str(plan_intent)
        + ("/" + ",".join(sorted(plan_metrics)) if plan_metrics else "")
        + ("/" + str(plan_days) + "天" if plan_days else "")
        + "（" + str(semantic_plan.get("source", "rules")) + "）"
    )

    if (plan_intent in ("analyze_change", "analyze_trend")
            and plan_metrics & PLAN_SCORE_METRICS):
        window_days = plan_days or 7
        structured = report_insight.build(
            night, window_days, "score", question
        )["structured"]
        return emit(
            _score_analysis_text(night, question),
            "评分分析",
            plan_detail + " · 总分变化 + 分项与同晚记录",
            sources=[
                "treat_score", "treat_duration_score", "mask_score", "resp_score",
                "treat_time_minutes", "air_leakage_p95", "large_leak_minutes", "ahi",
            ],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="score_analysis",
        )

    if (plan_intent in ("analyze_change", "analyze_trend", "compare_periods")
            and (not plan_metrics or plan_metrics == {"overall"})
            and not _wants_analysis(question)
            and (plan_intent != "compare_periods"
                 or semantic_plan.get("window") in ("week", "month"))):
        window_days = plan_days or 7
        structured = report_insight.build(
            night, window_days
        )["structured"]
        return emit(
            _trend_summary_text(night, question, window_days=window_days),
            "趋势分析",
            plan_detail + " · 多晚趋势与阶段对比",
            sources=[key for _, key, _ in TREND_METRICS],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="planned_trend",
        )

    if _wants_score_analysis(question):
        window_days = analysis.detect_window_days(question, default=7)
        structured = report_insight.build(
            night, window_days, "score", question
        )["structured"]
        return emit(
            _score_analysis_text(night, question),
            "评分分析",
            "总分变化 + 时长/面罩/呼吸事件分项 + 同晚对应记录",
            sources=[
                "treat_score", "treat_duration_score", "mask_score", "resp_score",
                "treat_time_minutes", "air_leakage_p95", "large_leak_minutes", "ahi",
            ],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="score_analysis",
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
        structured = report_insight.build(
            night, analysis.detect_window_days(question)
        )["structured"]
        return emit(
            _analysis_text(night, question),
            "数据分析",
            "综合分析：趋势结论 + 两晚对比 + 阶段对比 + 波动与偏离日",
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="analysis",
        )
    if _wants_trend_summary(question):
        structured = report_insight.build(
            night, analysis.detect_window_days(question, default=7)
        )["structured"]
        return emit(
            _trend_summary_text(night, question),
            "趋势分析",
            "多晚趋势结论 + 阶段对比 + 统计证据",
            sources=[key for _, key, _ in TREND_METRICS],
            validation={"ok": True, "problems": []},
            structured=structured,
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
        structured = report_insight.build(
            night, analysis.detect_window_days(question)
        )["structured"]
        return emit(
            _analysis_text(night, question),
            "数据分析",
            "按当前夜晚评估：" + str(night.raw.get("record_date", "未知")),
            sources=[item.fid for item in night.facts],
            validation={"ok": True, "problems": []},
            structured=structured,
            intent="assessment",
        )
    if intent == "report":
        payload = report.render(night)
        acceptance = payload["acceptance"]
        problems = [failure for item in acceptance.get("results", [])
                    for failure in item.get("failures", [])]
        return emit(payload["text"], "报告生成",
                    "单晚报告",
                    validation={"ok": acceptance["passed"], "problems": problems},
                    risk_flags=[] if acceptance["passed"] else ["report_acceptance_failed"],
                    structured=report.render_structured(night),
                    intent="report",
                    marker="" if acceptance["passed"] else "VALIDATION_FAIL")
    if intent in ("greeting", "capability", "identity", "thanks", "unclear", "empty"):
        if intent == "greeting":
            menu = list(GREETING_OPTIONS)
        elif intent in ("capability", "unclear", "empty"):
            menu = list(INTENT_OPTIONS)
        else:
            menu = []
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
    doc_hits = product_kb.retrieve(search_question, top_k=4)
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
    score_definition_query = (
        any(term in question for term in DEFINITION_TERMS)
        and any(term in question for term in SCORE_DEFINITION_TERMS)
    )
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

    if _asks_product_principle(question):
        principle_hits = [
            hit for hit in raw_doc_hits
            if "工作原理" in str(hit.get("section", ""))
            and str(hit.get("visibility")) != "examples"
        ]
        principle_hits.sort(
            key=lambda hit: (
                0 if str(hit.get("doc", "")).startswith("01_") else 1,
                -float(hit.get("score") or 0),
            )
        )
        if principle_hits:
            top = principle_hits[0]
            return emit(
                _doc_fallback([top]),
                "产品文档（规则版）",
                "产品工作原理：确定性资料回答",
                sources=[_source_label(top)],
                validation={"ok": True, "problems": []},
                intent=decision["goal"],
                recall=len(principle_hits),
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

    if _asks_product_modes(question) and doc_hits:
        top = doc_hits[0]
        return emit(
            _doc_fallback([top]),
            "产品文档（规则版）",
            "产品工作模式：确定性资料回答",
            sources=[_source_label(top)],
            validation={"ok": True, "problems": []},
            intent=decision["goal"],
            recall=len(doc_hits),
        )

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
    elif score_definition_query and knowledge_hits:
        # 评分分项优先用面向用户的指标解释，而不是直接把字段表原文倒出来。
        mode = "指标解释"
    elif is_definition_query and _is_metric_definition(question, knowledge_hits):
        # 指标定义优先使用本地知识库，避免被说明书中顺带提到指标的段落抢走。
        mode = "指标解释"
    elif is_definition_query and doc_hits:
        # 双水平等产品功能定义仍使用产品资料。
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

    if mode == "指标解释" and _is_metric_definition(question, knowledge_hits):
        top = knowledge_hits[0]
        return emit(
            _knowledge_definition_answer(top),
            "指标解释（规则版）",
            "本地指标知识库",
            sources=[top["id"]],
            validation={"ok": True, "problems": []},
            intent=decision["goal"],
            recall=len(knowledge_hits),
        )

    vector_degraded = (
        mode in ("产品文档", "设备数据", "指标解释")
        and not embeddings.available()
    )
    fallback_reason = ""
    if use_llm:
        if mode == "产品文档":
            material = product_kb.render(doc_hits)
            system = PRODUCT_PROMPT
            if product_models.find_known_model(question):
                system += PRODUCT_MODEL_SCOPE_PROMPT
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
        context_turns = CONTEXT_TURNS_BY_MODE.get(mode, 2)
        if mode == "产品文档" and not _needs_history(question):
            context_turns = 0
        messages.extend(_context_messages(history, max_turns=context_turns))
        messages.append({"role": "user", "content": user_prefix + question + "\n\n请据此回答。"})
        chat_options = {"max_tokens": LLM_MAX_TOKENS_BY_MODE.get(mode, 300),
                        "timeout_seconds": 15.0}
        if mode == "产品文档":
            chat_options["timeout_seconds"] = PRODUCT_LLM_TIMEOUT_SECONDS
        result = llm.chat(messages, **chat_options)
        llm_usage = result.get("usage") or {}
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
            _log_model_failure(request_id, result)
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


async def answer_async(query: UserQuery | str, night: nightdata.Night,
                       history: list | None = None, use_llm: bool = True,
                       user_id: str = "DEMO-USER") -> AgentResponse:
    """在保持同步业务结构的同时，将模型网络调用桥接到异步 httpx。"""
    bridge = llm.AsyncLLMBridge(asyncio.get_running_loop())
    token = llm.set_async_bridge(bridge)
    try:
        return await asyncio.to_thread(
            answer, query, night, history, use_llm, user_id
        )
    except asyncio.CancelledError:
        bridge.cancel()
        raise
    finally:
        llm.reset_async_bridge(token)


