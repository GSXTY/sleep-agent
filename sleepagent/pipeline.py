"""对话管线：路由 -> 事实/检索 -> 模型 -> 校验 -> 输出。

入口层（命令行、将来的 API）只做参数解析和结果展示，
所有业务判定都在这里，保证两个入口行为一致。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sleepagent import (embeddings, knowledge, llm, nightdata, product_kb, product_models,
                        report, router, runtime_info, safety, social, validation)
from sleepagent.router import route

logger = logging.getLogger("sleepagent.pipeline")

# 每次回答都注入当前日期：让「最近 / 上周」这类相对时间可用，
# 同时避免模型凭记忆编造日期。
SYS = "【系统信息】\n" + runtime_info.facts_line() + "\n\n"

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
7. 涉及当前日期或时间时，一律以【系统信息】为准，不要凭记忆回答。
8. 用简体中文，平实简洁，控制在 200 字以内。"""

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

    def __post_init__(self):
        self.question = (self.question or "").strip()
        self.history = list(self.history or [])


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
    if base_mode in ("系统信息", "型号确认"):
        return "none"
    return VALIDATION_MODE.get(base_mode, "weak")


def _response(text: str, mode: str, detail: str = "", sources=None,
              validation=None, risk_flags=None, structured=None,
              request_id: str = "") -> AgentResponse:
    return AgentResponse(
        text=text,
        mode=mode,
        detail=detail,
        request_id=request_id,
        sources=list(sources or []),
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
TIME_REF_WORDS = ("昨晚", "昨天", "前天", "前晚", "大前天", "最近", "最新",
                  "这几天", "近几天", "这一晚", "那晚")

HOWTO_TERMS = ("多久", "怎么办", "怎么用", "怎么做", "怎么清洗", "怎么连接", "怎么设置",
               "怎么换", "如何", "怎样", "更换", "清洗", "安装", "连接", "设置",
               "操作", "说明书", "报错", "故障", "能不能", "可以吗", "哪里", "坏了",
               "换一次", "换多久", "怎么保养", "怎么维护",
               "用什么", "用多少", "要多久", "需要多久")

PRODUCT_FEATURE_TERMS = (
    "自动漏气补偿", "漏气补偿", "压力释放", "延时升压", "压力上升坡度",
    "自动开机", "自动关机", "漏气提示", "参数锁定", "加温湿化器",
    "湿化器", "加温管路", "工作模式", "产品介绍", "工作原理",
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


def _doc_fallback(hits: list) -> str:
    """没有大模型时，直接把资料原文给出来 —— 相当于一次文档检索。"""
    if not hits:
        return NO_MATERIAL_TEXT
    lines = ["我在产品资料里查到以下内容（当前未启用大模型，直接给出原文）：", ""]
    for index, hit in enumerate(hits, 1):
        lines.append("【" + str(index) + "】" + _source_label(hit))
        lines.append(hit["text"])
        lines.append("")
    return "\n".join(lines).strip()


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
             validation=None, risk_flags=None, structured=None,
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
            risk_flags=risk_flags, structured=structured, request_id=request_id,
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
    intent, _ = social.classify(question)
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
        return emit(social.reply(intent, night), "对话", "意图 " + intent,
                    intent=intent)

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
    product_priority = is_howto or is_product_feature

    # 时间指代（昨晚 / 前天 / 具体日期）说明问的是用户自己的数据，
    # 此时即使带「多久」这类产品提问词，也应该走设备数据
    has_time_ref = _has_time_ref(question)

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

    doc_hits = product_kb.retrieve(question, top_k=3)
    if not doc_hits and product_priority:
        # 「面罩多久换」这类问题只匹配到一个词，覆盖率刚好不到严格阈值。
        # 产品提问信号明确时放宽一次，避免漏检；通用问题不会走到这里。
        doc_hits = product_kb.retrieve(question, top_k=3, min_score=3.0, min_coverage=0.2)
    doc_hits = product_kb.trim_low_relevance(doc_hits, question=question)

    if has_time_ref and picked:
        mode = "设备数据"
    elif doc_hits and product_priority:
        mode = "产品文档"
    elif picked:
        mode = "设备数据"
        # 带上数据范围：否则对话侧看不到历史夜晚，会和报告给出的夜晚数打架
        if not any(item.fid == "data-coverage" for item in picked):
            picked = [nightdata.coverage_fact(request.user_id, night)] + picked
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
            material = "\n".join(item.text for item in picked) + "\n" + knowledge.render(knowledge_hits)
            system = DEVICE_PROMPT
            user_prefix = SYS + "【本晚数据与规则结论】\n" + material + "\n\n【用户问题】\n"
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
                                            mode=VALIDATION_MODE.get(mode, "strong"))
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
                intent=decision["goal"],
                recall=len(picked) + len(knowledge_hits),
                model_used=bool(use_llm),
                marker=fallback_marker)


