"""运行时信息：当前日期与时间。

为什么单独做一个模块：
大模型没有实时时钟，问它"今天几号"它只能靠训练数据猜，会编出一个日期。
这类"程序本来就知道的事实"不应该交给模型回答 —— 直接由程序给出，既准确又不用调用模型。

同时把日期注入到系统提示里，让模型能正确处理"最近""上周"这类相对时间。
"""
from __future__ import annotations

import re
from datetime import datetime

WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 触发日期时间问答的关键词。注意不要放裸的"今天" ——
# 「今天天气怎么样」不是问日期。
DATE_TERMS = ("今天是几", "今天是", "几号", "几月几", "日期", "星期", "礼拜",
              "几点", "现在时间", "当前时间", "今天多少号", "多少号")

# 出现这些词说明问的是设备/产品语境，不是系统时间
CONTEXT_EXCLUSIONS = ("昨晚", "今晚", "本晚", "数据", "记录", "治疗", "压力", "漏气",
                      "面罩", "设备", "报告", "睡眠", "呼吸", "滤芯", "保修", "天气",
                      "故障", "错误", "app", "发生")


def now() -> datetime:
    return datetime.now()


def weekday_cn(moment: datetime = None) -> str:
    return WEEKDAY_CN[(moment or now()).weekday()]


def facts_line() -> str:
    """注入到系统提示里的系统信息。"""
    moment = now()
    return ("当前日期：" + moment.strftime("%Y-%m-%d") + "（" + weekday_cn(moment) + "）"
            + "｜当前时间：" + moment.strftime("%H:%M")
            + "｜时区：本机时间（中国标准时间）")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def is_datetime_question(question: str) -> bool:
    n = _normalize(question)
    if not any(term in n for term in DATE_TERMS):
        return False
    return not any(term in n for term in CONTEXT_EXCLUSIONS)


def answer(question: str) -> str:
    """直接由程序回答日期时间，不经过模型。"""
    n = _normalize(question)
    moment = now()
    date_text = moment.strftime("%Y年%m月%d日")
    time_text = moment.strftime("%H:%M")
    if "星期" in n or "礼拜" in n:
        return "今天是 " + weekday_cn(moment) + "（" + date_text + "）。"
    if "几点" in n or "时间" in n:
        return "现在是 " + time_text + "，" + date_text + "，" + weekday_cn(moment) + "。"
    return "今天是 " + date_text + "，" + weekday_cn(moment) + "。当前时间 " + time_text + "。"
