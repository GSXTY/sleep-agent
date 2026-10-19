"""安全边界：在生成任何回答之前先做确定性判定。

判定结果不经过大模型，命中即直接返回固定话术。
阈值与规则来源为原项目 core/rule_catalog.py 与 ai_interpretation 的边界要求，
均为产品观察规则，不是诊断或处方标准。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 需要立即就医的紧急情况
URGENT_TERMS = (
    "胸痛", "胸口疼", "胸闷", "憋醒", "喘不上气", "喘不过气", "呼吸困难",
    "窒息", "晕倒", "意识不清", "心慌", "心悸", "剧烈头痛", "呕血", "咳血",
)

# 要求自行调整治疗参数
PRESSURE_TERMS = ("压力", "调压", "参数", "模式", "epap", "ipap", "呼吸机设置")
CHANGE_VERBS = ("调高", "调低", "提高", "降低", "修改", "改变", "自己调", "自行调",
                "设置成", "改成", "调整", "自己改", "自行改", "能改", "可以改",
                "能调", "可以调", "自己设", "自行设",
                "降到", "升到", "调到", "设到",
                "改", "调")     # 单字动词：因为必须同时命中压力类词才拦截，不会误伤）

# 要求下诊断
DIAGNOSIS_TERMS = ("是不是得了", "确诊", "诊断", "算不算", "严重吗", "是不是病", "什么病", "有多严重")
SEVERITY_TERMS = ("轻度", "中度", "重度", "属于几度", "几级")

# 用药与治疗建议（PRD 4.6.1 明确要求拦截）
MEDICATION_TERMS = ("吃什么药", "吃点什么药", "用什么药", "服什么药", "什么药", "买药", "开药",
                    "用药", "吃药", "服药", "药物治疗", "药能治", "开点药",
                    "怎么治疗", "如何治疗", "治疗方法", "治疗方案", "能治好吗", "治得好吗",
                    "要不要手术", "需要手术", "动手术")

# 停止治疗 / 停用设备
STOP_TERMS = ("停用", "不用了", "不想戴", "不戴了", "停药", "停止治疗", "今晚不用", "可以不用",
              "停几天", "停一阵", "停一停", "先停", "暂停使用", "停掉", "歇几天")


# PRD 4.6.1 要求拦截后给出统一回复。各类别的话术更具体，但都以此句收尾，
# 保证「统一口径」这条要求被字面满足，同时不牺牲针对性。
STANDARD_BLOCK_SUFFIX = (
    "抱歉，我无法提供医疗诊断、参数调整与治疗建议，"
    "相关问题请咨询您的主治医生或设备工程师。"
)


@dataclass
class Verdict:
    kind: str          # urgent / pressure / diagnosis / stop / in_scope
    reply: str
    reason: str


URGENT_REPLY = (
    "您描述的这种情况需要优先处理：如果正在出现胸痛、明显呼吸困难、无法缓解的憋闷，或伴有意识改变，"
    "请立即联系医生或前往就近医疗机构，不要仅依赖设备数据自行判断。\n"
    "我可以先把这次情况记录下来，方便您就诊时说明。"
)

PRESSURE_REPLY = (
    "治疗压力属于医生处方内容，我不能给您建议值，也不能指导您自行修改设备参数。\n"
    "如果是因为戴着不舒服想调整，可以先看适配问题：面罩漏气、头带松紧、干燥刺激这几类都有明确的检查步骤，"
    "我可以带您过一遍。参数本身请通过医生随访调整。"
)

DIAGNOSIS_REPLY = (
    "我不能根据设备记录下诊断，也不能判定严重程度。设备数据只能描述治疗过程中记录到的数值，"
    "是否达到某种疾病的诊断标准需要由医生结合完整检查判断。\n"
    "我可以帮您把当晚的数值和含义解释清楚，方便您和医生沟通。"
)

MEDICATION_REPLY = (
    "我不能提供用药建议或治疗方案，这属于医疗行为。\n"
    "用药、手术和治疗方案请咨询您的主治医生；我可以帮您说明设备的使用方法，"
    "或者把您的问题转给人工客服。"
)

STOP_REPLY = (
    "是否继续使用设备需要医生评估，我不能建议您停用。\n"
    "如果是因为佩戴不适想暂停，我们可以先找原因：漏气、压痕、干燥或难以入睡都有对应的处理办法。"
)


def _with_standard(reply: str) -> str:
    """给分类话术补上统一收尾句；已经包含时不重复追加。"""
    if STANDARD_BLOCK_SUFFIX in reply:
        return reply
    return reply.rstrip() + "\n\n" + STANDARD_BLOCK_SUFFIX


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def check(question: str) -> Verdict:
    normalized = _normalize(question)

    if any(term in normalized for term in URGENT_TERMS):
        hit = next(term for term in URGENT_TERMS if term in normalized)
        return Verdict("urgent", URGENT_REPLY, "命中紧急症状关键词：" + hit)

    asks_for = any(term in normalized for term in CHANGE_VERBS)
    about_pressure = any(term in normalized for term in PRESSURE_TERMS)
    if about_pressure and asks_for:
        return Verdict("pressure", _with_standard(PRESSURE_REPLY), "请求自行调整治疗参数")

    if any(term in normalized for term in DIAGNOSIS_TERMS) or (
        any(term in normalized for term in SEVERITY_TERMS) and about_pressure is False
    ):
        hit = next((t for t in DIAGNOSIS_TERMS if t in normalized), None) or "严重程度判定"
        return Verdict("diagnosis", _with_standard(DIAGNOSIS_REPLY), "要求下诊断：" + hit)

    if any(term in normalized for term in MEDICATION_TERMS):
        hit = next(term for term in MEDICATION_TERMS if term in normalized)
        return Verdict("medication", _with_standard(MEDICATION_REPLY), "要求用药或治疗建议：" + hit)

    if any(term in normalized for term in STOP_TERMS):
        hit = next(term for term in STOP_TERMS if term in normalized)
        return Verdict("stop", _with_standard(STOP_REPLY), "要求停用设备：" + hit)

    return Verdict("in_scope", "", "")
