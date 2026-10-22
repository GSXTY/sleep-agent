"""多晚设备数据的确定性统计分析。

这里只计算可复核的描述性统计，不把趋势自动解释成疾病变化。
所有结论都保留窗口、样本量、日期和指标证据，供规则层或大模型组织语言。
"""
from __future__ import annotations

import datetime as dt
import math
import statistics

DEFAULT_WINDOW_DAYS = 30
RECENT_PERIOD_DAYS = 7
FRAGMENT_MINUTES = 240

METRICS = (
    ("使用时长", "treat_time_minutes", "分钟"),
    ("AHI", "ahi", "次/小时"),
    ("平均治疗压力", "treat_pressure_avg", "hPa"),
    ("P95 漏气", "air_leakage_p95", "L/min"),
    ("平均潮气量", "tidal_volume_avg_ml", "mL"),
    ("平均呼吸频率", "respiratory_rate_avg", "次/分钟"),
    ("平均分钟通气量", "minute_ventilation_avg_lpm", "L/min"),
)


def detect_window_days(question: str, default: int = DEFAULT_WINDOW_DAYS) -> int:
    """从问法里识别回看窗口；默认按 30 天设计，数据不足时如实降级。"""
    text = (question or "").replace(" ", "")
    if any(term in text for term in
           ("30天", "三十天", "30晚", "三十晚", "一个月", "近一个月",
            "最近一月", "近一月", "本月", "这个月")):
        return 30
    if any(term in text for term in ("14天", "十四天", "两周", "近两周", "最近两周")):
        return 14
    if any(term in text for term in
           ("7天", "七天", "7晚", "七晚", "一周", "近一周", "最近一周",
            "最近几晚", "最近几天", "这几天", "近几天")):
        return 7
    return default


def _date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value, digits=2):
    return round(float(value), digits) if value is not None else None


def _direction(change, mean):
    threshold = max(abs(mean) * 0.03, 1e-9)
    if change > threshold:
        return "上升"
    if change < -threshold:
        return "下降"
    return "平稳"


def _stability(cv):
    if cv is None:
        return "样本不足"
    if cv <= 0.08:
        return "较稳定"
    if cv <= 0.20:
        return "有波动"
    return "波动较明显"


def _linear_change(values: list, dates: list):
    """用日期做自变量的最小二乘斜率，返回整段窗口的折算变化。"""
    if len(values) < 2:
        return 0.0
    origin = dates[0]
    xs = [float((day - origin).days) for day in dates]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(values) / len(values)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator <= 0:
        return 0.0
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denominator
    return slope * (xs[-1] - xs[0])


def _window_records(night, days: int) -> tuple[list, dt.date | None, dt.date | None]:
    raw_records = list(night.history) + [night.raw]
    by_date = {}
    for raw in raw_records:
        day = _date(raw.get("record_date"))
        if day:
            by_date[day] = raw
    ordered = sorted(by_date.items())
    end = _date(night.raw.get("record_date"))
    if end is None:
        return [], None, None
    start = end - dt.timedelta(days=max(1, days) - 1)
    selected = [raw for day, raw in ordered if start <= day <= end]
    return selected, start, end


def _metric_summary(records: list, label: str, key: str, unit: str) -> dict:
    points = []
    missing = 0
    for raw in records:
        value = _number(raw.get(key))
        day = _date(raw.get("record_date"))
        if value is None or day is None:
            missing += 1
            continue
        points.append((day, value))
    if not points:
        return {
            "label": label, "key": key, "unit": unit, "count": 0,
            "missing": missing, "mean": None, "median": None,
            "minimum": None, "maximum": None, "first": None, "last": None,
            "change": None, "change_percent": None, "direction": "样本不足",
            "volatility": None, "stability": "样本不足",
        }
    values = [value for _, value in points]
    dates = [day for day, _ in points]
    mean = statistics.fmean(values)
    change = _linear_change(values, dates)
    change_percent = (change / abs(mean) * 100) if mean else None
    cv = statistics.pstdev(values) / abs(mean) if len(values) >= 2 and mean else None
    return {
        "label": label,
        "key": key,
        "unit": unit,
        "count": len(values),
        "missing": missing,
        "mean": _round(mean),
        "median": _round(statistics.median(values)),
        "minimum": _round(min(values)),
        "maximum": _round(max(values)),
        "first": _round(values[0]),
        "last": _round(values[-1]),
        "change": _round(change),
        "change_percent": _round(change_percent),
        "direction": _direction(change, mean),
        "volatility": _round(cv, 3),
        "stability": _stability(cv),
        "points": [(day.isoformat(), value) for day, value in points],
    }


def _period_comparison(records: list, summaries: dict) -> dict:
    if len(records) < 6:
        return {"available": False, "reason": "少于 6 个有记录夜晚，暂不做阶段对比",
                "metrics": []}
    recent_count = min(RECENT_PERIOD_DAYS, max(3, (len(records) + 1) // 2))
    prior_count = min(RECENT_PERIOD_DAYS, len(records) - recent_count)
    recent = records[-recent_count:]
    prior = records[-recent_count - prior_count:-recent_count]
    compared = []
    for label, key, unit in METRICS:
        old_values = [_number(item.get(key)) for item in prior]
        new_values = [_number(item.get(key)) for item in recent]
        old_values = [value for value in old_values if value is not None]
        new_values = [value for value in new_values if value is not None]
        if not old_values or not new_values:
            continue
        old_mean = statistics.fmean(old_values)
        new_mean = statistics.fmean(new_values)
        delta = new_mean - old_mean
        percent = (delta / abs(old_mean) * 100) if old_mean else None
        compared.append({
            "label": label,
            "key": key,
            "unit": unit,
            "prior_mean": _round(old_mean),
            "recent_mean": _round(new_mean),
            "delta": _round(delta),
            "change_percent": _round(percent),
            "direction": _direction(delta, old_mean),
            "importance": abs(percent) if percent is not None else 0.0,
        })
    compared.sort(key=lambda item: item["importance"], reverse=True)
    return {
        "available": bool(compared),
        "prior_count": len(prior),
        "recent_count": len(recent),
        "prior_start": str(prior[0].get("record_date")) if prior else None,
        "prior_end": str(prior[-1].get("record_date")) if prior else None,
        "recent_start": str(recent[0].get("record_date")) if recent else None,
        "recent_end": str(recent[-1].get("record_date")) if recent else None,
        "metrics": compared,
    }


def _outliers(records: list, summaries: dict) -> list:
    found = []
    if len(records) < 8:
        return found
    for label, key, unit in METRICS:
        summary = summaries.get(key)
        if not summary or not summary.get("points"):
            continue
        values = [value for _, value in summary["points"]]
        center = statistics.median(values)
        deviations = [abs(value - center) for value in values]
        mad = statistics.median(deviations)
        scored = []
        if mad > 0:
            for day, value in summary["points"]:
                score = 0.6745 * abs(value - center) / mad
                if score >= 3.5:
                    scored.append((score, day, value))
        elif len(set(values)) > 1:
            threshold = max(abs(center) * 0.25, 1e-9)
            for day, value in summary["points"]:
                score = abs(value - center) / threshold
                if score >= 1.0:
                    scored.append((score, day, value))
        for score, day, value in sorted(scored, reverse=True)[:2]:
            found.append({
                "date": day, "label": label, "key": key, "unit": unit,
                "value": _round(value), "score": _round(score),
            })
    found.sort(key=lambda item: item["score"], reverse=True)
    return found[:6]


def _data_quality(records: list, start, end) -> dict:
    dates = sorted(day for day in (_date(raw.get("record_date")) for raw in records) if day)
    gaps = []
    for previous, current in zip(dates, dates[1:]):
        missing = (current - previous).days - 1
        if missing > 0:
            gaps.append({
                "after": previous.isoformat(),
                "before": current.isoformat(),
                "days": missing,
            })
    fragments = []
    for raw in records:
        minutes = _number(raw.get("treat_time_minutes"))
        if minutes is not None and minutes < FRAGMENT_MINUTES:
            fragments.append(raw)
    return {
        "record_count": len(records),
        "calendar_days": (end - start).days + 1 if start and end else 0,
        "observed_days": ((dates[-1] - dates[0]).days + 1) if dates else 0,
        "first_date": dates[0].isoformat() if dates else None,
        "last_date": dates[-1].isoformat() if dates else None,
        "gap_count": len(gaps),
        "missing_days": sum(item["days"] for item in gaps),
        "gaps": gaps,
        "fragment_count": len(fragments),
        "fragment_dates": [str(raw.get("record_date")) for raw in fragments],
    }


def _conclusions(result: dict) -> list:
    quality = result["quality"]
    summaries = result["summaries"]
    period = result["period"]
    conclusions = []

    if quality["observed_days"] < result["window_days"]:
        conclusions.append(
            "这次请求按最近 " + str(result["window_days"]) + " 天分析，但实际只有 "
            + str(quality["observed_days"]) + " 个日历日的记录（"
            + str(quality["record_count"]) + " 个有记录夜晚，"
            + str(quality["first_date"]) + " 至 " + str(quality["last_date"])
            + "）。因此当前结论只能代表已有记录，不能当作完整 "
            + str(result["window_days"]) + " 天基线。"
        )
    else:
        conclusions.append(
            "本次请求最近 " + str(result["window_days"]) + " 天，窗口内有 "
            + str(quality["record_count"]) + " 个有记录夜晚，时间范围 "
            + str(quality["first_date"]) + " 至 " + str(quality["last_date"]) + "。"
        )

    trend_items = [
        item for item in summaries.values()
        if item.get("count", 0) >= 4
        and item.get("direction") in ("上升", "下降")
        and item.get("change_percent") is not None
        and abs(item["change_percent"]) >= 5
    ]
    trend_items.sort(key=lambda item: abs(item["change_percent"]), reverse=True)
    for item in trend_items[:3]:
        conclusions.append(
            item["label"] + " 在窗口内整体" + item["direction"] + "：首晚 "
            + str(item["first"]) + item["unit"] + "，末晚 "
            + str(item["last"]) + item["unit"] + "，趋势折算约 "
            + ("%+.1f" % item["change_percent"]) + "%。"
        )
    if not trend_items:
        conclusions.append("已有记录中，主要指标没有超过 5% 的持续单向变化。")

    if period["available"]:
        shifts = [
            item for item in period["metrics"]
            if item["change_percent"] is not None
            and abs(item["change_percent"]) >= 5
        ][:2]
        label = ("最近 " + str(period["recent_count"]) + " 晚与前 "
                 + str(period["prior_count"]) + " 晚相比")
        if shifts:
            parts = [
                item["label"] + " " + str(item["prior_mean"]) + " → "
                + str(item["recent_mean"]) + item["unit"]
                + "（" + ("%+.1f" % item["change_percent"]) + "%）"
                for item in shifts
            ]
            conclusions.append(label + "，" + "；".join(parts) + "。")
        else:
            conclusions.append(label + "，主要指标均值变化均小于 5%，近期阶段相对平稳。")

    valid = [item for item in summaries.values() if item.get("volatility") is not None]
    if valid:
        most = max(valid, key=lambda item: item["volatility"])
        conclusions.append(
            "波动最大的是" + most["label"] + "，变异系数约 "
            + ("%.1f" % (most["volatility"] * 100))
            + "%，属于“" + most["stability"] + "”；这表示记录值有起伏，不等于病情变化。"
        )

    if quality["gap_count"]:
        conclusions.append(
            "窗口内有 " + str(quality["gap_count"]) + " 段无记录间隔，共 "
            + str(quality["missing_days"])
            + " 个日历日；无法区分是未使用还是未上传，因此阶段平均值只按有记录夜晚计算。"
        )
    if quality["fragment_count"]:
        conclusions.append(
            "有 " + str(quality["fragment_count"])
            + " 个夜晚治疗时长不足 4 小时，已保留在记录中；解读平均值时要同时看样本量和时长。"
        )
    return conclusions[:7]


def analyze(night, window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """分析当前夜及此前最多 N 个日历日，返回可渲染的结构化结果。"""
    records, start, end = _window_records(night, window_days)
    if not records:
        records = [night.raw]
    summaries = {
        key: _metric_summary(records, label, key, unit)
        for label, key, unit in METRICS
    }
    quality = _data_quality(records, start, end)
    period = _period_comparison(records, summaries)
    outliers = _outliers(records, summaries)
    result = {
        "window_days": window_days,
        "requested_start": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else str(night.raw.get("record_date", "")),
        "records": records,
        "summaries": summaries,
        "quality": quality,
        "period": period,
        "outliers": outliers,
    }
    result["conclusions"] = _conclusions(result)
    return result


def metric_summary_text(night, key: str, window_days: int) -> str:
    """给单指标趋势问题提供简洁的统计结论。"""
    result = analyze(night, window_days)
    item = None
    for label, candidate_key, unit in METRICS:
        if candidate_key == key:
            item = result["summaries"][candidate_key]
            break
    if not item or not item.get("count"):
        return "当前窗口没有足够的该指标记录，无法给出趋势。"
    if item["count"] < 2:
        return (item["label"] + " 当前只有 1 个有效记录，无法判断变化。")
    return (
        "最近 " + str(result["window_days"]) + " 天窗口中，" + item["label"]
        + " 有 " + str(item["count"]) + " 个有效夜晚；平均值 "
        + str(item["mean"]) + item["unit"] + "，最低 "
        + str(item["minimum"]) + item["unit"] + "，最高 "
        + str(item["maximum"]) + item["unit"] + "。首晚到末晚由 "
        + str(item["first"]) + " 变为 " + str(item["last"]) + item["unit"]
        + "，趋势判断为" + item["direction"] + "，波动情况为"
        + item["stability"] + "。单晚波动不代表长期趋势，也不构成医疗诊断。"
    )
