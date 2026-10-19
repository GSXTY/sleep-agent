"""统一的数值与缺失值格式化。

为什么要单独一个模块：之前 None 泄漏到报告正文的问题，根子在「每个模块各自拼字符串」。
只要有一处忘了判断，就会把 None / 空串透出去。现在所有对外数值都走这里。
"""
from __future__ import annotations

MISSING = "未提供"


def format_value(value, unit: str = "", digits: int = None) -> str:
    """把数值格式化成对外文本。缺值固定返回「未提供」，绝不返回 None 或空串。"""
    if value is None or value == "":
        return MISSING
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if digits is not None:
            text = str(round(float(value), digits))
        elif float(value).is_integer():
            text = str(int(value))
        else:
            text = str(value)
    else:
        text = str(value)
    return text + ((" " + unit) if unit else "")


def is_missing(value) -> bool:
    return value is None or value == ""


def missing_note(field_label: str, consequence: str) -> str:
    """缺失字段的标准句式：说明缺什么，以及因此无法判断什么。"""
    return "本次数据未提供" + field_label + "，" + consequence + "。"
