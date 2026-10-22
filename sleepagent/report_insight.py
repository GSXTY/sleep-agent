"""App 报告板块的结构化解读层。

只消费现有确定性数据与分析结果，不重新计算另一套趋势，也不做医学判断。
App 可以用 module 字段把结果直接放到评分、呼吸事件、呼吸概况、漏气、压力卡片。
"""
from __future__ import annotations

import re

from . import analysis, formatting

SCHEMA_VERSION = "1.0"
REPORT_TYPE = "app_report_insight"

MODULE_TITLES = {
    "score": "睡眠评分",
    "events": "呼吸事件",
    "breathing": "呼吸概况",
    "leak": "漏气与面罩密封",
    "pressure": "治疗压力",
}

DISCLAIMER = (
    "以上只描述设备记录、历史变化和数据质量，不判断病情严重程度，"
    "不构成医疗诊断，也不建议自行调整治疗参数。"
)

MODULE_HINTS = (
    ("events", ("ahi", "ai", "hi", "cai", "呼吸事件", "呼吸暂停",
                "低通气", "中枢性")),
    ("breathing", ("呼吸概况", "潮气量", "呼吸频率", "分钟通气量",
                   "吸呼占比", "吸气时间")),
    ("leak", ("漏气", "面罩密封", "大漏气")),
    ("pressure", ("压力", "p95 压力", "压力上限")),
    ("score", ("评分", "得分", "睡眠质量", "分数", "扣分")),
)

EXPLICIT_HINTS = (
    "app报告", "app 报告", "应用报告", "报告解读", "解读报告",
    "报告分析", "分析报告", "报告里的", "报告模块", "报告板块",
    "这份报告", "解读一下这份报告", "解读这份报告",
)

EXCLUDE_HINTS = (
    "生成报告", "出个报告", "来一份报告", "帮我生成", "主机报告",
)


def detect_module(question: str) -> str:
    text = (question or "").lower()
    for module, terms in MODULE_HINTS:
        for term in terms:
            if term.isascii():
                if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text):
                    return module
            elif term in text:
                return module
    return "overall"


def should_handle(question: str) -> bool:
    """只接管明确的 App/报告解读问法，避免抢走事实查询和报告生成。"""
    text = (question or "").lower()
    if not text:
        return False
    if "报告" in text and any(term in text for term in ("生成", "出个", "来一份")):
        return False
    if any(term in text for term in EXCLUDE_HINTS):
        return False
    if any(term in text for term in EXPLICIT_HINTS):
        return True
    return False


def _display(value, unit=""):
    if value is None:
        return "未提供"
    return formatting.format_value(value, unit)


def _evidence(raw: dict, fields: tuple) -> list:
    result = []
    for key, label, unit in fields:
        value = raw.get(key)
        result.append({
            "source_field": key,
            "label": label,
            "value": value,
            "unit": unit,
            "display": _display(value, unit),
            "provided": value is not None,
        })
    return result


def _period_delta(result: dict, key: str):
    for item in result["period"].get("metrics", []):
        if item["key"] == key:
            return item
    return None


def _level_from_fields(evidence: list) -> str:
    return "data_gap" if not any(item["provided"] for item in evidence) else "info"


def _score_insight(night, result: dict, question: str = "") -> dict:
    raw = night.raw
    fields = (
        ("treat_score", "设备总分", "分"),
        ("treat_duration_score", "时长得分", "分"),
        ("mask_score", "面罩得分", "分"),
        ("resp_score", "呼吸事件得分", "分"),
    )
    evidence = _evidence(raw, fields)
    breakdown = result.get("score") or {}
    if raw.get("treat_score") is None:
        conclusion = "本晚没有提供设备评分，无法说明评分及其构成。"
        level = "data_gap"
    else:
        conclusion = breakdown.get("conclusion") or (
            "本晚设备评分为 " + _display(raw.get("treat_score"), "分") + "。"
        )
        delta = breakdown.get("delta")
        if delta is not None:
            if "下降" in (question or "") and delta >= 0:
                conclusion = "记录显示评分没有下降。" + conclusion
            elif "上升" in (question or "") and delta <= 0:
                conclusion = "记录显示评分没有上升。" + conclusion
        components = [
            item["label"] + " " + item["display"]
            for item in evidence[1:] if item["provided"]
        ]
        if components:
            conclusion += "评分构成中，记录到" + "、".join(components) + "。"
        level = "info"

    suggestion = (
        "；".join(breakdown.get("suggestions", []))
        if breakdown.get("suggestions") else None
    )
    minutes = raw.get("treat_time_minutes")
    if not suggestion and minutes is not None and minutes < analysis.FRAGMENT_MINUTES:
        suggestion = "本次治疗时长不足 4 小时；可核对是否分段使用或数据上传不完整。"
        level = "attention"
    return {
        "module": "score",
        "title": MODULE_TITLES["score"],
        "definition": "设备评分由使用时长、面罩和呼吸事件等板块构成，具体计分口径以后端为准。",
        "level": level,
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestion": suggestion,
        "breakdown": breakdown,
        "source_fields": [item["source_field"] for item in evidence],
    }


def _events_insight(night, result: dict) -> dict:
    raw = night.raw
    fields = (
        ("ahi", "AHI", "次/小时"),
        ("apnea_index", "呼吸暂停指数 AI", "次/小时"),
        ("hypopnea_index", "低通气指数 HI", "次/小时"),
        ("central_apnea_index", "中枢性指数 CAI", "次/小时"),
        ("apnea_count_total", "呼吸暂停次数", "次"),
        ("hypopnea_count_total", "低通气次数", "次"),
    )
    evidence = _evidence(raw, fields)
    level = _level_from_fields(evidence)
    if level == "data_gap":
        conclusion = "本晚没有提供可解读的呼吸事件记录。"
    else:
        ahi = raw.get("ahi")
        conclusion = "本晚记录 AHI 为 " + _display(ahi, "次/小时") + "。"
        counts = [
            item["label"] + " " + item["display"]
            for item in evidence[4:] if item["provided"]
        ]
        if counts:
            conclusion += "整晚记录到" + "、".join(counts) + "。"
        period = _period_delta(result, "ahi")
        if period and period.get("change_percent") is not None:
            conclusion += (
                "最近 " + str(result["period"]["recent_count"])
                + " 晚 AHI 均值 " + _display(period["recent_mean"], "次/小时")
                + "，较此前 " + _display(period["prior_mean"], "次/小时")
                + "变化为 " + ("%+.1f" % period["change_percent"]) + "%。"
            )
        if ahi is not None and ahi >= 15:
            level = "attention"

    suggestion = None
    if level == "attention":
        suggestion = "建议结合连续多晚记录咨询睡眠呼吸专业人员，不根据单晚记录自行调整参数。"
    return {
        "module": "events",
        "title": MODULE_TITLES["events"],
        "definition": "AHI 是平均每小时记录到的呼吸暂停与低通气事件数。",
        "level": level,
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestion": suggestion,
        "source_fields": [item["source_field"] for item in evidence],
    }


def _breathing_insight(night, result: dict) -> dict:
    raw = night.raw
    fields = (
        ("tidal_volume_avg_ml", "平均潮气量", "mL"),
        ("respiratory_rate_avg", "平均呼吸频率", "次/分钟"),
        ("minute_ventilation_avg_lpm", "平均分钟通气量", "L/min"),
        ("inspiratory_ratio_percent", "吸气时间占比", "%"),
        ("expiratory_ratio_percent", "呼气时间占比", "%"),
    )
    evidence = _evidence(raw, fields)
    level = _level_from_fields(evidence)
    if level == "data_gap":
        conclusion = "本晚没有提供可解读的呼吸概况记录。"
    else:
        provided = [item for item in evidence if item["provided"]]
        conclusion = "本晚记录到" + "、".join(
            item["label"] + " " + item["display"] for item in provided
        ) + "。"
        changes = []
        for key in ("tidal_volume_avg_ml", "respiratory_rate_avg",
                    "minute_ventilation_avg_lpm"):
            item = _period_delta(result, key)
            if item and item.get("change_percent") is not None \
                    and abs(item["change_percent"]) >= 10:
                changes.append(
                    item["label"] + "阶段均值"
                    + ("%+.1f" % item["change_percent"]) + "%"
                )
                level = "attention"
        if changes:
            conclusion += "与自身此前阶段相比，" + "；".join(changes) + "。"
        else:
            conclusion += "与前一段自身记录相比，没有单项阶段均值变化达到 10%。"

    return {
        "module": "breathing",
        "title": MODULE_TITLES["breathing"],
        "definition": "呼吸概况描述潮气量、呼吸频率和分钟通气量等设备记录。",
        "level": level,
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestion": (
            "继续观察连续多晚记录；如对呼吸感受有疑问，请咨询专业人员。"
            if level == "attention" else None
        ),
        "source_fields": [item["source_field"] for item in evidence],
    }


def _leak_insight(night, result: dict) -> dict:
    raw = night.raw
    fields = (
        ("air_leakage_avg", "平均漏气", "L/min"),
        ("air_leakage_p95", "P95 漏气", "L/min"),
        ("air_leakage_max", "最高漏气", "L/min"),
        ("large_leak_minutes", "大漏气累计", "分钟"),
    )
    evidence = _evidence(raw, fields)
    level = _level_from_fields(evidence)
    if level == "data_gap":
        conclusion = "本晚没有提供可解读的漏气记录。"
    else:
        conclusion = (
            "本晚平均漏气 " + _display(raw.get("air_leakage_avg"), "L/min")
            + "，P95 漏气 " + _display(raw.get("air_leakage_p95"), "L/min")
            + "。"
        )
        large = raw.get("large_leak_minutes")
        if large is not None:
            conclusion += "大漏气累计 " + _display(large, "分钟") + "。"
        period = _period_delta(result, "air_leakage_p95")
        if period and period.get("change_percent") is not None:
            conclusion += (
                "最近 " + str(result["period"]["recent_count"])
                + " 晚 P95 漏气均值 " + _display(period["recent_mean"], "L/min")
                + "，较此前 " + _display(period["prior_mean"], "L/min")
                + "变化为 " + ("%+.1f" % period["change_percent"]) + "%。"
            )
            if period["change_percent"] >= 5:
                level = "attention"
        if large is not None and large > 0:
            level = "attention"
        avg = raw.get("air_leakage_avg")
        p95 = raw.get("air_leakage_p95")
        if avg is not None and p95 is not None and avg > 0 and p95 >= avg * 2:
            level = "attention"

    suggestion = None
    if level == "attention":
        suggestion = "可检查面罩贴合、头带松紧和管路接口；若持续升高，请联系售后确认。"
    return {
        "module": "leak",
        "title": MODULE_TITLES["leak"],
        "definition": "漏气指标反映面罩与管路在治疗过程中的漏气记录。",
        "level": level,
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestion": suggestion,
        "source_fields": [item["source_field"] for item in evidence],
    }


def _pressure_insight(night, result: dict) -> dict:
    raw = night.raw
    setting = raw.get("pressure_setting") if isinstance(raw.get("pressure_setting"), dict) else {}
    fields = (
        ("treat_pressure_avg", "平均治疗压力", "hPa"),
        ("treat_pressure_p95", "P95 治疗压力", "hPa"),
        ("treat_pressure_max", "最高治疗压力", "hPa"),
    )
    evidence = _evidence(raw, fields)
    level = _level_from_fields(evidence)
    if level == "data_gap":
        conclusion = "本晚没有提供可解读的治疗压力记录。"
    else:
        conclusion = (
            "本晚平均治疗压力 " + _display(raw.get("treat_pressure_avg"), "hPa")
            + "，P95 治疗压力 " + _display(raw.get("treat_pressure_p95"), "hPa")
            + "，最高 " + _display(raw.get("treat_pressure_max"), "hPa") + "。"
        )
        if setting.get("min") is not None and setting.get("max") is not None:
            conclusion += (
                "当前记录到的设定范围为 " + str(setting.get("min")) + " 到 "
                + str(setting.get("max")) + " hPa。"
            )
            pressure_max = raw.get("treat_pressure_max")
            if pressure_max is not None and float(pressure_max) >= float(setting["max"]):
                level = "attention"
        period = _period_delta(result, "treat_pressure_avg")
        if period and period.get("change_percent") is not None:
            conclusion += (
                "平均压力阶段变化 " + ("%+.1f" % period["change_percent"]) + "%。"
            )
    return {
        "module": "pressure",
        "title": MODULE_TITLES["pressure"],
        "definition": "治疗压力是设备在治疗过程中记录的输出压力。",
        "level": level,
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestion": (
            "如果设备多次达到设定上限，可将记录反馈给专业人员评估，不要自行改参数。"
            if level == "attention" else None
        ),
        "source_fields": [item["source_field"] for item in evidence],
    }


def build(night, window_days: int = analysis.DEFAULT_WINDOW_DAYS,
          module: str = "overall", question: str = "") -> dict:
    """生成 App 可直接渲染的结构化报告解读。"""
    result = analysis.analyze(night, window_days)
    selected = module if module in MODULE_TITLES else "overall"
    insights = [
        _score_insight(night, result, question),
        _events_insight(night, result),
        _breathing_insight(night, result),
        _leak_insight(night, result),
        _pressure_insight(night, result),
    ]
    if selected != "overall":
        insights = [item for item in insights if item["module"] == selected]

    attention = [item["title"] for item in insights if item["level"] == "attention"]
    missing = [item["title"] for item in insights if item["level"] == "data_gap"]
    if attention:
        summary = "需要回看的板块：" + "、".join(attention) + "。"
    elif missing:
        summary = "以下板块材料不足：" + "、".join(missing) + "。"
    else:
        summary = "当前选中的报告板块没有触发需要特别回看的变化或数据缺口。"

    sources = []
    for item in insights:
        for field in item["source_fields"]:
            if field not in sources:
                sources.append(field)

    quality = result["quality"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "record_date": night.raw.get("record_date"),
        "data_source": night.raw.get("data_source"),
        "selected_module": selected,
        "window": {
            "days": window_days,
            "first_date": quality["first_date"],
            "last_date": quality["last_date"],
            "record_count": quality["record_count"],
            "observed_days": quality["observed_days"],
        },
        "summary": {
            "level": (
                "attention" if attention else "data_gap" if missing else "info"
            ),
            "conclusion": summary,
        },
        "insights": insights,
        "data_quality": {
            "gap_count": quality["gap_count"],
            "missing_days": quality["missing_days"],
            "fragment_count": quality["fragment_count"],
        },
        "source_fields": sources,
        "disclaimer": DISCLAIMER,
    }
    show_definitions = any(
        term in (question or "") for term in
        ("什么意思", "是什么", "含义", "解释", "怎么理解")
    )
    return {
        "text": _render_text(payload, show_definitions=show_definitions),
        "structured": payload,
    }


def _render_text(payload: dict, show_definitions: bool = False) -> str:
    window = payload["window"]
    scope_note = ""
    if window["observed_days"] < window["days"]:
        scope_note = "；数据不足完整 " + str(window["days"]) + " 天"
    lines = [
        "App 报告解读（" + str(payload["record_date"]) + "；最近 "
        + str(window["days"]) + " 天窗口，"
        + str(window["record_count"]) + " 个有记录夜晚" + scope_note + "）",
        "",
        "【结论】",
        payload["summary"]["conclusion"],
    ]
    for item in payload["insights"]:
        lines += ["", "【" + item["title"] + "】"]
        if show_definitions:
            lines.append("定义：" + item["definition"])
        lines.append(item["conclusion"])
        if item["suggestion"]:
            lines.append("建议：" + item["suggestion"])
    lines += ["", "【边界】" + payload["disclaimer"]]
    return "\n".join(lines)
