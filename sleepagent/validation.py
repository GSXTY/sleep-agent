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


def _numbers(text: str) -> set:
    return {value for value in re.findall(r"\d+(?:\.\d+)?", text)}


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


# PRD 4.6.2 的三档强度。别名保留，避免调用方改动。
MODE_ALIASES = {"grounded": "strong", "general": "weak"}

LEVELS = {
    "strong": {"numbers": True, "medical": True, "clinical": True, "p95": True},
    "medium": {"numbers": True, "medical": True, "clinical": False, "p95": False},
    "weak":   {"numbers": False, "medical": True, "clinical": False, "p95": False},
}


def check_output(text: str, material: str, mode: str = "strong") -> dict:
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
        scan_text = text.replace("P95", "较高百分位").replace("p95", "较高百分位")
        allowed = _allowed_numbers(material)
        unsupported = sorted(value for value in _numbers(scan_text)
                             if not _supported(value, allowed))
        if unsupported:
            problems.append("出现材料中没有的数字：" + "、".join(unsupported))

    if level["medical"]:
        for phrase in MEDICAL_GUARDS:
            if phrase in text:
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
        if pattern in text:
            problems.append("出现调压建议：" + pattern)

    return {"ok": not problems, "problems": problems}
