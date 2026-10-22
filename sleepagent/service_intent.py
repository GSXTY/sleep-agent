"""客服服务意图：先判断用户想干什么，再决定检索哪个子库。

这一层不影响安全边界，只用于把检索范围收窄，减少跨类噪音。
规则高置信时直接使用；低置信时 scope=None，退回全库检索。
"""
from __future__ import annotations

import re

INTENTS = {
    "product_overview": {
        "terms": ("是什么产品", "产品介绍", "工作原理", "型号", "规格", "配件", "装箱"),
        "docs": ("00_产品规格.md", "01_产品说明.md", "05_技术参数.md",
                 "10_YA70A常见问题.md"),
    },
    "device_operation": {
        "terms": ("怎么开机", "怎么关机", "怎么启动", "怎么连接", "怎么设置", "怎么操作",
                  "按键", "屏幕", "主界面", "报告", "蓝牙", "app", "配对", "延时升压",
                  "加湿等级", "自动开机", "自动关机", "水箱"),
        "docs": ("02_使用指南.md", "11_常见操作补充.md", "10_YA70A常见问题.md"),
    },
    "troubleshooting": {
        "terms": ("报错", "故障", "不出气", "无法开机", "开不了机", "连不上", "断开",
                  "没反应", "漏水", "噪音大", "提示", "e3", "e0", "异常"),
        "docs": ("08_常见问题.md", "10_YA70A常见问题.md"),
    },
    "consumables": {
        "terms": ("滤芯", "面罩", "鼻罩", "管路", "头带", "耗材", "清洁", "清洗",
                  "消毒", "保养", "多久换", "更换周期", "硅胶"),
        "docs": ("03_清洁与维护.md", "10_YA70A常见问题.md", "11_常见操作补充.md"),
    },
    "metric": {
        "terms": ("ahi", "p95", "指标", "漏气量", "潮气量", "分钟通气量",
                  "呼吸频率", "评分", "压力值"),
        "docs": ("09_报告指标说明.md",),
    },
    "after_sales": {
        "terms": ("保修", "售后", "客服", "维修", "退货", "换货", "使用期限", "联系方式"),
        "docs": ("07_售后服务.md", "10_YA70A常见问题.md"),
    },
}

DATA_TERMS = ("我的数据", "昨晚", "使用情况", "数据分析", "有什么建议",
              "正常吗", "趋势", "最近几晚", "上一次")


def _score(question: str, intent: dict):
    text = (question or "").lower()
    hits = [term for term in intent["terms"] if term.lower() in text]
    return len(hits), hits


def detect(question: str) -> dict:
    """返回 {intent, scope, confidence, hits}。"""
    text = (question or "").lower()
    if any(term in text for term in DATA_TERMS):
        return {"intent": "data", "scope": None, "confidence": 0.9, "hits": []}

    ranked = []
    for name, intent in INTENTS.items():
        score, hits = _score(text, intent)
        if score:
            ranked.append((score, name, intent, hits))
    if not ranked:
        return {"intent": "unknown", "scope": None, "confidence": 0.0, "hits": []}

    ranked.sort(key=lambda item: (-item[0], item[1]))
    top_score, name, intent, hits = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == top_score:
        # 多个意图同分，交给全库检索，避免误收窄
        return {"intent": "unknown", "scope": None, "confidence": 0.4, "hits": hits}
    confidence = 0.9 if top_score >= 2 else 0.7
    return {"intent": name, "scope": intent["docs"], "confidence": confidence, "hits": hits}
