"""大模型输出校验。

这是"接入大模型"里最关键的一环：模型说的话必须先过这道闸，过不了就回退。
校验强度跟着回答模式走：

  mode="grounded"：回答必须依据材料（产品资料或设备数据）
      → 数字白名单 + 诊断式表述 + P95 语义 全部检查
  mode="general" ：日常问答，数字本来就无法逐一追溯
      → 只保留医疗底线（调压建议、确诊类表述）

数字白名单沿用原项目 core/ai_interpretation.py 的做法：
把材料里出现过的数字做成白名单，输出里的数字必须在白名单里（允许 0.05 的舍入误差），
扫描前先把 P95 换成文字，否则里面的 95 会被当成凭空出现的数字。
"""
from __future__ import annotations

import re

from .product_models import KNOWN_MODELS

NUMERIC_TOLERANCE = 0.05      # 允许模型对数字做四舍五入，例如把 4.35 说成 4.4
# 浮点比较的兜底余量：4.4 - 4.35 在浮点下是 0.050000000000000266，不加余量会误判
_EPSILON = 1e-6
ROUND_DECIMALS = 3

# 任何模式下都必须拦住的表述（医疗设备公司的底线）
MEDICAL_GUARDS = (
    "自行调整治疗压力", "自行修改治疗压力", "你可以确诊", "一定是因为",
    "建议你调高", "建议你调低", "可以把压力调到", "应该把压力调到",
    "建议自行调", "可以自行调", "你可以调高", "你可以调低",
    "确诊为", "诊断为", "可以确诊",
)

# 只在有材料支撑的模式下检查：诊断式表述与数据误读
CLINICAL_PATTERNS = (
    "你得了", "你患有", "属于重度", "属于中度", "属于轻度", "属于几度",
)

P95_MISREADINGS = (
    "P95 是最大值", "P95是最大值", "最大值是P95",
    "95%的时间高于", "95% 的时间高于",
)

ADVICE_PATTERNS = ("建议自行调", "可以自行调", "你可以调高", "你可以调低",
                   "可以把压力调到", "建议你调高", "建议你调低")

MAX_LENGTH = 1200

# 日期要单独校验：模型复述一句"2026-10-15 的 AHI 是 5.7"不该被当成凭空数字，
# 但日期本身必须和提问或材料对得上（否则就是编错了哪一晚）。
DATE_PATTERNS = (
    re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"),
    re.compile(r"(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*[日号]?"),
)

# 我们给材料加的编号（[资料1]、[知识2]、【3】）不是数据，不能进数字白名单，
# 否则模型随口说一句"每天 1 次"也会因为材料里有个 1 而通过。
MATERIAL_MARKERS = (
    re.compile(r"\[(?:资料|知识)\s*\d+\]"),
    re.compile(r"【\s*\d+\s*】"),
)

# 引用格式："第 1 条"、"【2】"。这些是排版，不是测量值。
REFERENCE_MARKERS = (
    re.compile(r"【\s*\d+\s*】"),
    re.compile(r"第\s*\d+(?:\.\d+)*\s*[章节条点]"),
)

# 模型自己分条时的 "1. " "2、" 属于排版，不是数值。
LIST_MARKER = re.compile(r"(?m)^\s*\d{1,2}\s*[.、)）]\s+")

# 型号/错误码里的数字不是测量值：YA70、E3、S9、A20AUK2609…
# 不含小数点，避免把 "AHI5.7" 这类紧凑写法一起吞掉。
MODEL_CODE = re.compile(r"(?<![A-Za-z0-9.])([A-Za-z]{1,10}\d{1,4})(?![A-Za-z0-9.])")
SERIAL_CODE = re.compile(r"[A-Za-z]{1,6}\d{5,}[A-Za-z0-9]*")

NEGATION_MARKERS = (
    "不要", "不能", "不应", "不可", "不得", "不建议", "避免", "禁止",
    "请勿", "无需", "不用", "别",
)

# 否定只在同一分句内生效："不用担心，你可以自行调压" 里的"不用"不算否定。
CLAUSE_BREAKS = "，。；！？：、,.;!?:;\n"


def _numbers(text: str) -> set:
    return {value for value in re.findall(r"\d+(?:\.\d+)?", text)}


def _mask_dates(text: str) -> str:
    for pattern in DATE_PATTERNS:
        text = pattern.sub(" 日期 ", text)
    return text


def _strip_markers(text: str) -> str:
    for pattern in MATERIAL_MARKERS + REFERENCE_MARKERS:
        text = pattern.sub(" ", text)
    return text


def _mask_list_markers(text: str) -> str:
    return LIST_MARKER.sub(" ", text)


def _mask_codes(text: str) -> str:
    for model in sorted(KNOWN_MODELS, key=len, reverse=True):
        text = re.sub(re.escape(model), " 型号 ", text, flags=re.IGNORECASE)
    text = MODEL_CODE.sub(" 型号 ", text)
    return SERIAL_CODE.sub(" 型号 ", text)


def _date_keys(text: str) -> set:
    """把日期归一成 MM-DD，方便跨写法比较（10月15日 / 2026-10-15）。"""
    keys = set()
    for value in re.findall(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?", text):
        parts = re.findall(r"\d+", value)
        if len(parts) >= 3:
            keys.add("%02d-%02d" % (int(parts[-2]), int(parts[-1])))
    for month, day in re.findall(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text):
        keys.add("%02d-%02d" % (int(month), int(day)))
    return keys


def _allowed_numbers(material: str) -> set:
    return _numbers(material)


def _supported(value: str, allowed: set) -> bool:
    if value in allowed:
        return True
    try:
        number = round(float(value), ROUND_DECIMALS)
    except ValueError:
        return False
    for candidate in allowed:
        try:
            if abs(number - round(float(candidate), ROUND_DECIMALS)) <= NUMERIC_TOLERANCE + _EPSILON:
                return True
        except ValueError:
            continue
    return False


def _has_unsafe_phrase(text: str, phrase: str) -> bool:
    """检查短语是否出现在肯定语境中；明确否定的安全提醒不算违规。"""
    start = 0
    while True:
        position = text.find(phrase, start)
        if position < 0:
            return False
        prefix = _clause_before(text, position)
        negated = any(marker in prefix for marker in NEGATION_MARKERS)
        # 「不建议自行调…」：短语从"建议"开始，否定只剩紧贴的一个"不"字。
        # 只认紧贴的那一个，避免"不用担心，你可以自行调…"被误判成否定。
        if not negated and position > 0 and text[position - 1] == "不":
            negated = True
        if not negated:
            return True
        start = position + len(phrase)


def _clause_before(text: str, position: int) -> str:
    """取短语所在分句里、短语之前的部分。"""
    start = position
    while start > 0 and text[start - 1] not in CLAUSE_BREAKS:
        start -= 1
    return text[start:position]


# PRD 4.6.2 的三档强度。别名保留，避免调用方改动。
MODE_ALIASES = {"grounded": "strong", "general": "weak"}

LEVELS = {
    "strong": {"numbers": True, "medical": True, "clinical": True, "p95": True},
    "medium": {"numbers": True, "medical": True, "clinical": False, "p95": False},
    "weak":   {"numbers": False, "medical": True, "clinical": False, "p95": False},
}


def check_output(text: str, material: str, mode: str = "strong",
                 question: str = "", extra_numbers=None) -> dict:
    """校验模型输出，返回 {ok, problems}。

    三档强度对应 PRD 4.6.2：
      strong —— 数据查询 / 报告：数字白名单 + 医疗红线 + 诊断表述 + P95 语义
      medium —— 知识库问答：数字白名单 + 医疗红线
      weak   —— 通用对话：仅医疗红线
    """
    level = LEVELS.get(MODE_ALIASES.get(mode, mode), LEVELS["strong"])
    problems = []

    if not text or not text.strip():
        return {"ok": False, "problems": ["输出为空"]}
    if len(text) > MAX_LENGTH:
        problems.append("输出过长（" + str(len(text)) + " 字）")

    # 数字白名单只在需要「有材料可依据」的档位启用
    if level["numbers"]:
        scan_text = _mask_codes(_strip_markers(_mask_list_markers(_mask_dates(text))))
        scan_text = scan_text.replace("P95", "较高百分位").replace("p95", "较高百分位")
        allowed = _allowed_numbers(_mask_codes(_strip_markers(material)))
        if extra_numbers:
            # 语料里真实存在的章节号（1.1、6.4…）是引用，不是测量值
            allowed |= {str(item) for item in extra_numbers}
        unsupported = sorted(value for value in _numbers(scan_text)
                             if not _supported(value, allowed))
        if unsupported:
            problems.append("出现材料中没有的数字：" + "、".join(unsupported))
        allowed_dates = _date_keys(material) | _date_keys(question)
        if question or allowed_dates:
            unknown_dates = sorted(_date_keys(text) - allowed_dates)
            if unknown_dates:
                problems.append("出现材料中没有的日期：" + "、".join(unknown_dates))

    if level["medical"]:
        for phrase in MEDICAL_GUARDS:
            if _has_unsafe_phrase(text, phrase):
                problems.append("出现禁止语：" + phrase)

    if level["clinical"]:
        for pattern in CLINICAL_PATTERNS:
            if pattern in text:
                problems.append("出现诊断式表述：" + pattern)

    if level["p95"]:
        for pattern in P95_MISREADINGS:
            if pattern in text:
                problems.append("P95 语义错误：" + pattern)

    for pattern in ADVICE_PATTERNS:
        if _has_unsafe_phrase(text, pattern):
            problems.append("出现调压建议：" + pattern)

    # 同一短语被「禁止语」和「调压建议」同时命中时只报一次
    unique, seen = [], set()
    for problem in problems:
        phrase = problem.split("：", 1)[-1]
        if phrase in seen:
            continue
        seen.add(phrase)
        unique.append(problem)
    problems = unique
    return {"ok": not problems, "problems": problems}
