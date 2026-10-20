"""产品功能词歧义确认。

说明书没有明确对应项时，不拿一个泛化设置章节让模型猜，先让用户确认。
"""
from __future__ import annotations

AMBIGUOUS_FEATURES = {
    "定时": (
        "延时升压",
        "延迟关机",
        "自动开机/自动关机",
        "定时提醒",
    ),
}

PRODUCT_NOUNS = (
    "面罩", "鼻罩", "头带", "管路", "滤芯", "水盒", "湿化器", "加湿器",
    "呼吸机", "设备", "手机", "app", "提醒", "模式", "压力", "漏气",
    "延时升压", "延迟关机", "自动开机", "自动关机", "双水平", "单水平",
    "眼睛", "眼部", "眼干", "眼涩",
)

REPLACEMENT_TERMS = ("换", "更换", "周期", "多久")
CONSUMABLE_TERMS = ("面罩", "鼻罩", "头带", "管路", "水盒", "滤芯")


def suggest(question: str):
    """返回需要确认的功能；没有歧义时返回 None。"""
    text = question or ""
    if "定时" in text:
        if any(term in text for term in (
            "延时升压", "延迟关机", "自动开机", "自动关机", "定时关机", "提醒"
        )):
            return None
        return {"feature": "定时", "options": AMBIGUOUS_FEATURES["定时"]}
    return None


def candidate_needed(question: str, top_section: str) -> bool:
    """章节名没有包含问题中的产品名词时，认为需要候选确认。"""
    text = _normalize(question)
    section = _normalize(top_section)
    nouns = [noun for noun in PRODUCT_NOUNS if noun in text]
    return bool(nouns) and not any(noun in section for noun in nouns)


def candidate_matches(question: str, section: str) -> bool:
    """候选章节至少要包含问题中的产品名词或其明确别名。"""
    text = _normalize(question)
    target = _normalize(section)
    aliases = {
        "面罩": ("面罩", "鼻罩"),
        "鼻罩": ("鼻罩", "面罩"),
    }
    for noun in PRODUCT_NOUNS:
        if noun in text and any(alias in target for alias in aliases.get(noun, (noun,))):
            return True
    return False


def replacement_guidance(question: str, hits: list):
    """耗材更换问题没有专门章节时，返回不推测的说明。"""
    text = _normalize(question)
    if not any(term in text for term in CONSUMABLE_TERMS):
        return None
    if not any(term in text for term in REPLACEMENT_TERMS):
        return None
    for hit in hits:
        section = _normalize(hit.get("section", ""))
        if ("面罩" in section or "鼻罩" in section) and "更换" in section:
            return None
    if "面罩" in text or "鼻罩" in text:
        return (
            "说明书没有给出面罩的固定更换周期，只说明应按照通气面罩等附件"
            "制造商的要求清洁附件并确定清洁频率。建议以面罩制造商说明为准，"
            "或联系客服、售后确认更换时间。"
        )
    return None


def _normalize(text: str) -> str:
    import re
    return re.sub(r"\s+", "", text or "").lower()
