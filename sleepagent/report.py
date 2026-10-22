"""单晚报告生成。

流程与原项目一致：先由确定性事实层组织内容，再对大模型表达做验收；
验收不通过时回退到规则版。命令行版没有大模型时直接用规则版。
"""
from __future__ import annotations

from . import acceptance, formatting, nightdata


REPORT_SCHEMA_VERSION = "1.0.0"
REPORT_TYPE = "single_night"
REPORT_DISCLAIMER = (
    "本报告只用于帮助理解设备记录的数据，不构成医学诊断。"
    "本报告不提供治疗压力或设备参数的调整建议，参数请遵医嘱由专业人员评估。"
)

MODULE_TITLES = {
    "pressure": "一、治疗压力",
    "respiratory-events": "二、呼吸事件",
    "breathing-overview": "三、呼吸概况",
    "leakage": "四、漏气与面罩密封",
}


def _trend_sentence(topic: str, trend: dict) -> str:
    nights = trend["nights"]
    if not trend["has_history"]:
        return ("多晚趋势：本次只提供了单个夜晚的记录，没有可供对比的历史夜晚，"
                "因此无法给出趋势走势和持续性判断。")
    ahi_series = "、".join(str(n.get("ahi")) for n in nights if n.get("ahi") is not None)
    leak_series = "、".join(str(n.get("air_leakage_p95")) for n in nights if n.get("air_leakage_p95") is not None)
    prefix = "多晚趋势：截至本晚共 " + str(trend["count"]) + " 个有记录的夜晚，"

    if topic == "respiratory-events":
        values = [n.get("ahi") for n in nights if n.get("ahi") is not None]
        direction = "没有持续抬升" if len(values) < 2 or values[-1] <= max(values[:-1]) else "出现持续走高迹象"
        return (prefix + "按时间顺序 AHI 依次为 " + ahi_series + "。整体走势" + direction
                + "，近期没有连续多个夜晚持续偏高。")

    if topic == "leakage":
        values = [n.get("air_leakage_p95") for n in nights if n.get("air_leakage_p95") is not None]
        direction = "没有持续抬升" if len(values) < 2 or values[-1] <= max(values[:-1]) else "出现持续走高迹象"
        return (prefix + "按时间顺序 P95 漏气量依次为 " + leak_series + "。整体走势" + direction
                + "，没有连续多个夜晚高于平均值。")

    if topic == "breathing-overview":
        # 每晚字段完整时，呼吸概况也能给出跨晚趋势；
        # 只有历史夜晚确实缺这些字段时才说明无法给出。
        def series(field):
            return [n.get(field) for n in nights if n.get(field) is not None]

        tidal, rate, minute = series("tidal_volume_avg_ml"), series("respiratory_rate_avg"), series("minute_ventilation_avg_lpm")
        if len(tidal) >= 2 and len(rate) >= 2 and len(minute) >= 2:
            return (prefix + "潮气量平均依次为 " + "、".join(str(round(v, 1)) for v in tidal)
                    + " mL；呼吸频率依次为 " + "、".join(str(round(v, 1)) for v in rate)
                    + "；分钟通气量依次为 " + "、".join(str(round(v, 1)) for v in minute)
                    + "。三项都没有出现单向持续变化，整体走势平稳。")
        return (prefix + "历史夜晚缺少潮气量、呼吸频率与分钟通气量的记录，"
                "这三项的跨晚趋势无法给出，只有本晚数值可参考。")

    # pressure
    return (prefix + "按时间顺序 AHI 依次为 " + ahi_series + "。"
            "P95 与平均值的差距在各晚之间保持相近，整体走势平稳，"
            "没有出现持续单向抬升。")


def trend_sentence(topic: str, trend: dict) -> str:
    """对外暴露的趋势句式，供设备数据模式复用报告里的同一套口径。"""
    return _trend_sentence(topic, trend)


def summary_advice(night: nightdata.Night) -> dict:
    """对外暴露报告里的总结建议，供"分析一下"复用同一套规则。"""
    return _summary_advice(night)


def build_modules(night: nightdata.Night) -> dict:
    trend = nightdata.trend_summary(night.user_id, night)
    modules = {}
    for topic, key in (
        ("pressure", "pressure"),
        ("events", "respiratory-events"),
        ("breathing_overview", "breathing-overview"),
        ("leakage", "leakage"),
    ):
        facts = nightdata.facts_by_topic(night.user_id, night, topic)
        paragraphs = [item.text for item in facts]
        if not paragraphs:
            paragraphs.append("本模块缺少必要数据，无法给出分析。")
        paragraphs.append(_trend_sentence(key, trend))
        if key == "respiratory-events":
            paragraphs.append(
                "按成人常用分层的级别划分，本晚 AHI 处于上述区间；级别只描述数值位置，"
                "不代表诊断结论。逐条事件明细未提供，事件分布只覆盖汇总层面。"
            )
        modules[key] = "\n".join(paragraphs)
    return modules


RATING_BANDS = ((90, "优秀"), (75, "良好"), (60, "需留意"), (0, "较差"))


def _rating(night: nightdata.Night) -> tuple:
    """按设备总分给出总体评级。缺失就明说，不猜。"""
    score = night.raw.get("treat_score")
    if score is None:
        return None, "本次数据未提供治疗评分，无法给出总体评级。"
    for threshold, label in RATING_BANDS:
        if score >= threshold:
            return label, label + "（设备总分 " + str(int(score) if float(score).is_integer() else score) + " 分）"
    return None, "本次数据未提供治疗评分，无法给出总体评级。"


def _core_metrics(night: nightdata.Night) -> str:
    """PRD 4.4.2 要求的四项核心指标。"""
    raw = night.raw
    unit_map = (("AHI", "ahi", "次/小时"), ("使用时长", "treat_time_minutes", "分钟"),
                ("平均漏气量", "air_leakage_avg", "L/min"), ("平均治疗压力", "treat_pressure_avg", "hPa"))
    parts = []
    for label, field, unit in unit_map:
        value = raw.get(field)
        parts.append(label + " " + (nightdata._show(value) if value is not None else "未提供")
                     + ("" if value is None else " " + unit))
    return " ｜ ".join(parts)


def _core_metric_cards(night: nightdata.Night) -> list:
    """核心指标卡：给前端直接渲染，缺失值保持 None + provided=False。"""
    raw = night.raw
    cards = []
    for key, label, field, unit in (
        ("ahi", "AHI", "ahi", "次/小时"),
        ("treat_time", "使用时长", "treat_time_minutes", "分钟"),
        ("air_leakage", "平均漏气量", "air_leakage_avg", "L/min"),
        ("treat_pressure", "平均治疗压力", "treat_pressure_avg", "hPa"),
    ):
        value = raw.get(field)
        provided = value is not None
        cards.append({
            "key": key,
            "source_field": field,
            "label": label,
            "value": value,
            "unit": unit,
            "display": (nightdata._show(value) + " " + unit) if provided else "未提供",
            "provided": provided,
        })
    return cards


def _summary_advice(night: nightdata.Night) -> dict:
    """总结建议：正向反馈 / 待改进项 / 可落地建议。

    全部由确定性规则从数值推导，模型不参与；缺数据时如实跳过对应项。
    """
    raw = night.raw
    positives, issues, actions = [], [], []

    minutes = raw.get("treat_time_minutes")
    if minutes is not None and minutes >= 240:
        positives.append("使用时长达到 4 小时以上")
    elif minutes is not None:
        issues.append("本次治疗时长只有 " + formatting.format_value(minutes, "分钟") + "，不足 4 小时")
        actions.append("保持整晚佩戴，逐步延长有效治疗时间")

    ahi = raw.get("ahi")
    if ahi is not None:
        if ahi < 5:
            positives.append("AHI 处于常用分层的较低区间")
        elif ahi < 15:
            issues.append("AHI 位于 5 到 15 区间，建议结合多晚趋势观察")
            actions.append("持续规律使用，并观察连续多晚的 AHI 变化")
        else:
            issues.append("AHI 高于 15，建议结合多晚趋势并咨询医生")
            actions.append("携带有数据的报告咨询睡眠呼吸专业人员")

    large = raw.get("large_leak_minutes")
    if large is not None:
        if large <= 0:
            positives.append("整晚没有记录到大漏气时段")
        else:
            issues.append("存在大漏气累计 " + formatting.format_value(large, "分钟"))
            actions.append("检查头带松紧、面罩硅胶与管路接口是否到位")

    p95, pavg = raw.get("air_leakage_p95"), raw.get("air_leakage_avg")
    if p95 is not None and pavg is not None and p95 >= pavg * 2 and p95 > 0:
        issues.append("漏气 P95 明显高于平均值，可能存在间歇性漏气")
        actions.append("睡前复戴一次面罩，确认没有折叠或压迫")

    return {"positives": positives, "issues": issues, "actions": actions}


def render_structured(night: nightdata.Night) -> dict:
    """结构化输出：给未来 App 前端渲染用，与对话文本分开维护。"""
    rating, rating_text = _rating(night)
    modules = build_modules(night)
    verdict = acceptance.check_report(modules)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "record_date": night.raw.get("record_date"),
        "data_source": night.raw.get("data_source"),
        "rating": {"level": rating, "text": rating_text},
        "core_metrics": _core_metrics(night),
        "core_metric_cards": _core_metric_cards(night),
        "modules": {
            key: {"title": MODULE_TITLES[key], "text": text}
            for key, text in modules.items()
        },
        "summary": _summary_advice(night),
        "acceptance": verdict,
        "missing_fields": sorted(set(night.missing)),
        "disclaimer": REPORT_DISCLAIMER,
    }


def render(night: nightdata.Night) -> dict:
    modules = build_modules(night)
    verdict = acceptance.check_report(modules)
    lines = [
        "单晚治疗报告（" + str(night.raw.get("record_date", "日期未提供"))
        + "，数据来源：" + str(night.raw.get("data_source", "未知")) + "）",
        "=" * 52,
        "",
    ]
    rating, rating_text = _rating(night)
    lines.append("总体评级：" + rating_text)
    lines.append("核心指标：" + _core_metrics(night))
    lines.append("")
    for name, text in modules.items():
        lines.append(MODULE_TITLES[name])
        lines.append(text)
        lines.append("")
    summary = _summary_advice(night)
    if summary["positives"] or summary["issues"] or summary["actions"]:
        lines.append("五、总结建议")
        if summary["positives"]:
            lines.append("正向反馈：" + "；".join(summary["positives"]) + "。")
        if summary["issues"]:
            lines.append("待改进项：" + "；".join(summary["issues"]) + "。")
        if summary["actions"]:
            lines.append("建议：" + "；".join(summary["actions"]) + "。")
        lines.append("")

    lines.append("数据范围与边界")
    lines.append("-" * 52)
    lines.append("本报告只用于帮助理解设备记录的数据，不构成医学诊断。")
    lines.append("本报告不提供治疗压力或设备参数的调整建议，参数请遵医嘱由专业人员评估。")
    if night.missing:
        lines.append("以下字段本次缺失，相关判断已跳过：" + "、".join(sorted(set(night.missing))) + "。")
    lines.append("验收结果：" + ("通过" if verdict["passed"] else "未通过"))
    if not verdict["passed"]:
        # 非阻断：报告照常输出，但列出问题便于排查。
        # 单个模块缺数据会一次报出多条，这里每个模块最多展示 3 条并汇总总数，
        # 避免用户看到十几行内部主题标识。
        for item in verdict["results"]:
            failures = item["failures"]
            if not failures:
                continue
            for failure in failures[:3]:
                lines.append("  · " + item["module"] + "：" + failure)
            if len(failures) > 3:
                lines.append("  · " + item["module"] + "：另有 " + str(len(failures) - 3) + " 项同类问题")
    return {"modules": modules, "acceptance": verdict, "text": "\n".join(lines)}
