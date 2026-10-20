"""对话层：问候、能力询问、身份、致谢，以及自然语言触发报告。

这一层解决"助手不会打招呼"的问题，同时保持边界：
社交与元问题用固定话术自然应答；范围外问题礼貌引导回能力范围。
"""
from __future__ import annotations

import re

GREETING_TERMS = ("你好", "您好", "哈喽", "嗨", "在吗", "在么", "早上好", "中午好",
                  "下午好", "晚上好", "hello", "hi", "hey", "yo")
CAPABILITY_TERMS = ("你能做什么", "你能帮我做什么", "你会什么", "有什么功能", "能干什么",
                    "可以做什么", "能做什么", "帮我做什么", "怎么用你", "使用说明",
                    "有哪些能力", "帮助", "help", "功能有哪些")
IDENTITY_TERMS = ("你是谁", "你是什么", "你叫什么", "介绍下自己", "自我介绍", "你是什么模型")
THANKS_TERMS = ("谢谢", "感谢", "多谢", "thank", "thanks")
UNKNOWN_TERMS = ("嗯", "哦", "啊", "好吧", "额", "呃")
REPORT_TERMS = ("出报告", "生成报告", "看报告", "来个报告", "打印报告", "导出报告",
                "报告发我", "总结一下", "整体总结", "总体情况", "整体情况", "总览",
                "复盘", "出个报告", "做份报告", "报告")

EXPLANATION_GUARDS = ("是什么意思", "什么意思", "怎么理解", "如何理解", "怎么看")
TOPIC_GUARDS = ("漏气", "压力", "AHI", "呼吸事件", "潮气量", "呼吸频率", "分钟通气量", "趋势", "面罩")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _has_latin_word(text: str, word: str) -> bool:
    """英文词必须按词边界匹配，否则 hi 会命中 ahi。"""
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text) is not None


def _hit(text: str, terms) -> bool:
    for term in terms:
        if term.isascii():
            if _has_latin_word(text, term):
                return True
        elif term in text:
            return True
    return False


def split_leading_greeting(question: str):
    """把开头的问候语剥掉，返回 (是否有问候, 剩余内容)。
    用于处理「你好，滤芯多久换一次」这种问候加真实问题的输入。"""
    n = _normalize(question)
    for term in GREETING_TERMS:
        if term.isascii():
            match = re.match(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])[\s,，。!！~、]*", n)
            if match:
                return True, n[match.end():]
        elif n.startswith(term):
            return True, n[len(term):].lstrip("，。!！~、, ")
    return False, n


def classify(question: str):
    """返回 (intent, payload) 或 (None, None)。顺序即优先级。"""
    n = _normalize(question)
    if not n:
        return ("empty", None)

    if _hit(n, GREETING_TERMS) and len(n) <= 14:
        return ("greeting", None)
    if _hit(n, CAPABILITY_TERMS):
        return ("capability", None)
    if _hit(n, IDENTITY_TERMS):
        return ("identity", None)
    if _hit(n, THANKS_TERMS) and len(n) <= 16:
        return ("thanks", None)
    if n in UNKNOWN_TERMS:
        return ("unclear", None)

    if any(t in n for t in REPORT_TERMS):
        blocked = any(g in n for g in EXPLANATION_GUARDS) or any(g in n for g in TOPIC_GUARDS)
        if not blocked:
            return ("report", None)

    return (None, None)


OUT_OF_SCOPE_TEXT = (
    "这个问题我这边帮不上忙。\n"
    "我可以回答一般的日常问题，也熟悉我们产品的使用、清洁维护、错误码和 App 操作——"
    "这些产品问题我会去查产品资料再回答。"
)

CAPABILITY_TEXT = (
    "我可以做这几件事：\n"
    "\n"
    "  1. 日常问题：一般常识、计算、写作等，直接回答。\n"
    "  2. 产品问题：查产品资料后回答并注明出处 —— 使用安装、佩戴、清洁维护、耗材更换、\n"
    "     错误码、App 操作。把文档放进 product_docs/ 目录即可扩充。\n"
    "  3. 指标解释：AHI、呼吸事件、治疗压力、漏气量、潮气量、呼吸频率、分钟通气量、P95。\n"
    "  4. 单晚治疗报告：输入 /report 或直接说「出个报告」。\n"
    "\n"
    "我不会做的：下疾病诊断、判断病情严重程度、建议自行调整治疗压力或设备参数。\n"
    "\n"
    "试几个：\n"
    "  帮我生成报告\n"
    "  什么是 AHI\n"
    "  滤芯多久换一次\n"
    "  E3 报错怎么处理"
)

GREETING_TEXT = (
    "你好。我是睡眠呼吸产品助手，可以回答日常问题，也能解答产品使用方面的疑问。\n"
    "\n"
    "想直接开始的话，可以问：\n"
    "  · 帮我生成报告\n"
    "  · 什么是 AHI\n"
    "  · 滤芯多久换一次\n"
    "\n"
    "（涉及疾病诊断和调压的问题我会建议你联系医生。）"
)

IDENTITY_TEXT = (
    "我是这家睡眠呼吸设备公司的智能助手，运行在你的本机上。\n"
    "一般问题可以直接问；产品相关的问题我会检索产品资料后回答，并告诉你出处。\n"
    "产品资料放在 product_docs/ 目录，我已经加载了你放进去的文档。\n"
    "我不是医生，不能下诊断，也不能替你调整设备参数。"
)

THANKS_TEXT = "不客气。还想问什么直接说，产品使用、清洁维护、错误码都可以。"
UNCLEAR_TEXT = "抱歉，这次我没太理解。你可以换个说法，比如：帮我生成报告 / 什么是 AHI / 滤芯多久换一次。"


def reply(intent: str, night=None, report_text: str = "") -> str:
    if intent == "greeting":
        return GREETING_TEXT
    if intent == "capability":
        return CAPABILITY_TEXT
    if intent == "identity":
        return IDENTITY_TEXT
    if intent == "thanks":
        return THANKS_TEXT
    if intent == "unclear" or intent == "empty":
        return UNCLEAR_TEXT
    if intent == "report":
        return report_text
    return ""
