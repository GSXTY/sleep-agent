"""报告验收检查。

直接复用原项目的 contracts/report_analysis_acceptance.json，
检查必讲项、必用术语、禁止语与弱化风险的表述。

说明：合同里的 required_topics 是抽象主题名，原项目在代码中把它们映射到具体检查。
本文件的 TOPIC_EXPECTATIONS 就是这层映射，属于实现方的解释，需要人工复核。
"""
from __future__ import annotations

import json
from pathlib import Path

CONTRACT_FILE = Path(__file__).resolve().parents[1] / "contracts" / "report_analysis_acceptance.json"

# 主题名 -> 该主题必须出现的短语（至少命中一个）
TOPIC_EXPECTATIONS = {
    "pressure": {
        "average_pressure": ["平均"],
        "p95_pressure": ["P95"],
        "maximum_pressure": ["最高"],
        "pressure_settings": ["设定"],
        "pressure_comparison": ["差", "倍"],
        "trend_peak": ["趋势"],
        "trend_duration": ["持续"],
        "trend_direction": ["走势"],
        "near_upper_limit": ["上限"],
        "distribution_zone": ["区间"],
        "stability": ["稳定"],
    },
    "respiratory-events": {
        "total_events": ["总"],
        "ahi": ["AHI"],
        "ahi_level": ["级别", "分层"],
        "apnea_hypopnea_index": ["呼吸暂停低通气指数"],
        "apnea_index": ["呼吸暂停指数"],
        "hypopnea_index": ["低通气指数"],
        "central_apnea_index": ["中枢"],
        "event_comparison": ["占比", "区分", "比较"],
        "peak_period": ["时段"],
        "clustering": ["聚集"],
        "duration": ["持续时间"],
        "longest_event_free_period": ["无事件"],
        "classification_limit": ["明细"],
    },
    "breathing-overview": {
        "average_tidal_volume": ["潮气量"],
        "p95_tidal_volume": ["P95"],
        "average_respiratory_rate": ["呼吸频率"],
        "average_minute_ventilation": ["分钟通气量"],
        "p95_minute_ventilation": ["P95"],
        "inspiratory_ratio": ["吸气"],
        "expiratory_ratio": ["呼气"],
        "average_inspiratory_time": ["吸气时间"],
        "tidal_comparison": ["倍", "差"],
        "ventilation_comparison": ["倍", "差"],
        "timing_comparison": ["吸呼"],
        "tidal_trend": ["趋势"],
        "respiratory_rate_trend": ["趋势"],
        "minute_ventilation_trend": ["趋势"],
        "p95_semantics": ["不是最大值"],
    },
    "leakage": {
        "average_leak": ["平均"],
        "p95_leak": ["P95"],
        "maximum_leak": ["最高"],
        "large_leak_duration": ["大漏气"],
        "large_leak_ratio": ["占比", "%"],
        "leak_comparison": ["倍", "差"],
        "trend_peak": ["趋势"],
        "trend_duration": ["持续"],
        "mask_status": ["面罩"],
        "mask_off_limitation": ["脱落"],
    },
}


def load_contract() -> dict:
    return json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))


def check_module(module: str, text: str) -> dict:
    contract = load_contract()
    spec = contract["modules"][module]
    failures = []

    for term in spec["required_terms"]:
        if term not in text:
            failures.append("缺少必用术语：" + term)

    for phrase in spec["prohibited_phrases"]:
        if phrase in text:
            failures.append("出现禁止语：" + phrase)

    for phrase in contract.get("global_prohibited_phrases", []):
        if phrase in text:
            failures.append("出现全局禁止语：" + phrase)

    for phrase in contract.get("risk_weakening_phrases", []):
        if phrase in text:
            failures.append("出现弱化风险表述：" + phrase)

    for topic, options in TOPIC_EXPECTATIONS.get(module, {}).items():
        if not any(option in text for option in options):
            failures.append("未覆盖必讲主题：" + topic + "（期望包含 " + " 或 ".join(options) + "）")

    return {"module": module, "passed": not failures, "failures": failures}


def check_report(modules: dict) -> dict:
    results = [check_module(name, text) for name, text in modules.items()]
    return {
        "passed": all(item["passed"] for item in results),
        "results": results,
        "version": load_contract().get("version"),
    }
