"""意图与主题识别。

移植自原项目 core/intent_router.py 的确定性路由部分（去掉 pydantic 依赖）。
语义路由交给大模型会引入不确定性，命令行版只保留可预测的规则路由。
"""
from __future__ import annotations

import re

ACTION_PHRASES = (
    "怎么办", "怎么做", "应该做什么", "该做什么", "需要做什么", "可以做什么",
    "需要注意", "注意什么", "如何改善", "怎么改善", "怎样改善", "怎么调整",
    "如何调整", "应该调整", "需要调整", "有什么调整", "怎么优化", "如何优化",
    "下一步", "怎么处理", "如何处理", "怎么解决", "如何解决", "具体建议", "怎么改",
)
CAUSE_PHRASES = ("为什么", "什么原因", "导致", "造成", "是不是因为", "有关吗")
EXPLANATION_PHRASES = ("是什么", "什么意思", "怎么理解", "如何理解", "怎么看")
ASSESSMENT_PHRASES = ("正常吗", "好吗", "怎么样", "是否稳定", "是否平稳", "靠谱吗")
TREND_PHRASES = ("趋势", "连续几晚", "最近几晚", "持续偏", "一直偏")
COMPARISON_PHRASES = ("相比", "比较", "区别", "哪个更", "之间的关系")

TOPIC_TERMS = (
    ("leakage", ("漏气", "面罩", "密封")),
    ("usage", ("使用时长", "使用时间", "使用情况", "使用状况", "佩戴时长", "佩戴时间", "时间太短", "整晚佩戴",
               "用了多久", "用了几个小时", "用了多少", "用了多长时间", "每晚用")),
    ("ahi", ("ahi", "呼吸暂停低通气指数")),
    ("events", ("呼吸事件", "呼吸暂停", "低通气", "中枢")),
    ("pressure", ("压力", "调压", "参数")),
    ("respiratory_rate", ("呼吸频率", "呼吸次数")),
    ("minute_ventilation", ("分钟通气量",)),
    ("breathing_overview", ("潮气量", "吸呼比", "吸气时间", "呼气时间", "呼吸概况")),
    ("therapy_mode", ("治疗模式", "工作模式", "什么模式", "哪种模式", "模式")),
)

OUT_OF_SCOPE_TERMS = (
    "天气", "股票", "写代码", "翻译", "讲个笑话", "推荐电影", "买什么手机",
    "美国总统", "历史事件", "数学题",
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def topics_of(question: str) -> list:
    normalized = normalize(question)
    found = [topic for topic, terms in TOPIC_TERMS if any(t in normalized for t in terms)]
    return found or ["overall"]


def route(question: str) -> dict:
    normalized = normalize(question)
    topics = topics_of(question)
    # "使用情况 / 整体怎么样"是概览，不是只看使用时长。
    # 若同时出现具体指标（漏气、AHI、压力等），topics 会多于 usage，不受影响。
    if topics == ["usage"] and any(
            term in normalized for term in ("情况", "状况", "整体", "总结", "怎么样")):
        topics = ["overall"]
    if any(t in normalized for t in OUT_OF_SCOPE_TERMS):
        return {"goal": "out_of_scope", "topics": ["overall"], "time_scope": "unspecified"}
    if any(p in normalized for p in ACTION_PHRASES):
        goal = "action_advice"
    elif any(p in normalized for p in CAUSE_PHRASES):
        goal = "cause"
    elif any(p in normalized for p in TREND_PHRASES):
        goal = "trend"
    elif any(p in normalized for p in COMPARISON_PHRASES):
        goal = "comparison"
    elif any(p in normalized for p in EXPLANATION_PHRASES):
        goal = "explanation"
    elif any(p in normalized for p in ASSESSMENT_PHRASES):
        goal = "assessment"
    elif any(p in normalized for p in ("总结", "整体", "报告")):
        goal = "summary"
    else:
        goal = "summary"
    time_scope = (
        "history" if any(t in normalized for t in ("趋势", "最近", "连续", "上周", "上个月"))
        else "current_night" if any(t in normalized for t in ("今晚", "昨晚", "本晚", "这晚", "当前"))
        else "unspecified"
    )
    return {"goal": goal, "topics": topics, "time_scope": time_scope}
