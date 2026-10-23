"""产品型号的近似匹配和确认。

型号是精确标识，不能让向量检索把 `Y70` 猜成别的主题。
这里只处理“看起来像型号、但与已知型号差一个字符”的情况，
确认真实型号后再进入产品资料检索。
"""
from __future__ import annotations

import re

KNOWN_MODELS = ("YA70", "YA70A", "YA70B20", "YA70B25", "YA70Pro")
MODEL_PATTERN = re.compile(r"[A-Za-z]{1,6}[- ]?\d{1,4}(?:[- ]?[A-Za-z0-9]{1,8})?")


def _canonical(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def strip_known_models(text: str) -> str:
    """检索前去掉已知型号。

    型号只是文档范围，不是内容关键词："YA70有哪些模式" 里，
    "YA70" 会把"产品型号"章节顶到前面，反而找不到"1.3 工作模式"。
    """
    cleaned = text or ""
    for model in sorted(KNOWN_MODELS, key=len, reverse=True):
        cleaned = re.sub(re.escape(model), " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def find_known_model(text: str):
    """返回问题中提到的已知型号；优先匹配较长型号。"""
    for model in sorted(KNOWN_MODELS, key=len, reverse=True):
        if re.search(re.escape(model), text or "", flags=re.IGNORECASE):
            return model
    return None


def _edit_distance(left: str, right: str) -> int:
    """小型编辑距离；型号字符串很短，不需要额外依赖。"""
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (char_left != char_right),
            ))
        previous = current
    return previous[-1]


def suggest(question: str):
    """返回 {observed, canonical}；没有近似型号时返回 None。"""
    known = {_canonical(model): model for model in KNOWN_MODELS}
    for match in MODEL_PATTERN.findall(question or ""):
        observed = _canonical(match)
        if len(observed) < 3 or not any(char.isdigit() for char in observed):
            continue
        if observed in known:
            continue
        candidates = [
            (distance, model)
            for canonical, model in known.items()
            if (distance := _edit_distance(observed, canonical)) <= 1
        ]
        if candidates:
            _, model = min(candidates)
            return {"observed": match.strip(), "canonical": model}
    return None
