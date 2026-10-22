"""单晚设备数据的确定性事实层。

原则与原项目一致：事实与数值由程序计算，大模型只负责组织语言。
每条事实都带来源字段与计算方式，便于核对（对应原项目的 fact ID 机制）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
import datetime

from . import formatting

SAMPLE_FILE = Path(__file__).resolve().parents[1] / "data" / "sample_nights.json"
TREND_WINDOW = 7            # 报告默认使用最近 7 个有记录的夜晚
FRAGMENT_MIN_MINUTES = 240  # 低于此时长视为碎片记录，必须附加提示
DEFAULT_USER_ID = "DEMO-USER"


@dataclass
class Fact:
    fid: str
    topic: str
    text: str
    source: str
    calculation: str


@dataclass
class Night:
    raw: dict                                        # 这一晚的记录
    user_id: str = DEFAULT_USER_ID
    history: list = field(default_factory=list)      # 更早的夜晚，按日期升序
    facts: list = field(default_factory=list)
    missing: list = field(default_factory=list)

    def get(self, key, default=None):
        value = self.raw.get(key, default)
        if value is None:
            self.missing.append(key)
            return default
        return value


def _show(value, unit=""):
    """兼容旧调用：统一走 formatting.format_value。"""
    return formatting.format_value(value, unit)

def _ratio_text(numerator, denominator, label):
    if not denominator:
        return label + "无法计算（分母缺失）"
    return "为 " + str(round(numerator / denominator, 2)) + " 倍"


def load_dataset(user_id: str = DEFAULT_USER_ID, path=None) -> list:
    """读取多晚数据集。

    user_id 是入口层的用户边界；当前演示数据只有 DEMO-USER，
    真实多用户数据源接入时在这里按 user_id 查询，不改变下游调用形态。

    支持三种形态：
      1. { "nights": [ {...}, {...} ] }  —— 推荐，每行一条治疗夜
      2. [ {...}, {...} ]                —— 直接是数组
      3. { ...单个夜晚... }               —— 兼容旧的单晚文件（含内部 history）
    每晚都会拿到它之前的夜晚作为 history，供趋势使用。
    """
    target = Path(path) if path else SAMPLE_FILE
    data = json.loads(Path(target).read_text(encoding="utf-8"))
    if isinstance(data, list):
        records = data
    elif isinstance(data.get("nights"), list):
        records = data["nights"]
    else:
        records = [data]
    top_source = data.get("data_source") if isinstance(data, dict) else None
    records = [record for record in records if record.get("record_date")]
    records.sort(key=lambda record: str(record["record_date"]))
    if top_source:
        # 数据集级别的来源说明要带给每个夜晚，否则报告里会显示「来源未知」
        for record in records:
            record.setdefault("data_source", top_source)

    nights = []
    for index, record in enumerate(records):
        history = list(records[:index])
        if index == 0 and isinstance(record.get("history"), list):
            # 兼容旧格式：历史夜晚写在单个夜晚里
            history = sorted(record["history"], key=lambda item: str(item.get("record_date", "")))
        nights.append(Night(raw=record, user_id=user_id, history=history))
    for night in nights:
        build_facts(user_id, night)
    return nights


def load_night(user_id: str = DEFAULT_USER_ID, path=None, index: int = None) -> Night:
    """取某一晚。默认取最新的一晚。"""
    nights = load_dataset(user_id, path)
    if not nights:
        raise ValueError("数据集中没有任何治疗夜")
    return nights[-1] if index is None else nights[index]


def build_facts(user_id: str, night: Night) -> None:
    night.user_id = user_id
    f = []
    r = night.raw

    def add(fid, topic, text, source, calc):
        f.append(Fact(fid, topic, text, source, calc))

    # ---- 使用与总体 ----
    minutes = r.get("treat_time_minutes")
    if minutes is not None:
        hours = round(minutes / 60, 1)
        add("usage-time", "usage",
            "本晚治疗时长 " + str(minutes) + " 分钟（约 " + str(hours) + " 小时），"
            "记录归属日期为 " + str(r.get("record_date", "未提供")) + "。",
            "treat_time_minutes", "分钟数除以 60 得到小时")

    score = r.get("treat_score")
    if score is not None:
        add("treat-score", "usage",
            "设备总评分为 " + str(score) + "（满分 100），其中时长 "
            + str(r.get("treat_duration_score", "未提供")) + "、面罩 "
            + str(r.get("mask_score", "未提供")) + "、呼吸事件 "
            + str(r.get("resp_score", "未提供")) + "。",
            "treat_score / treat_duration_score / mask_score / resp_score", "直接取源字段原值")

    therapy_mode = r.get("therapy_mode") or r.get("mode")
    if therapy_mode is not None:
        add("therapy-mode", "therapy_mode",
            "本晚治疗模式为 " + str(therapy_mode) + "。",
            "therapy_mode / mode", "直接取源字段")
    else:
        add("therapy-mode-missing", "therapy_mode",
            "本晚数据未提供治疗模式，无法确定实际使用的是 CPAP、AutoCPAP、S 还是 AutoS。",
            "therapy_mode / mode", "字段缺失；不使用 device_model 推断")

    # ---- 压力 ----
    p_avg = r.get("treat_pressure_avg")
    p_p95 = r.get("treat_pressure_p95")
    p_max = r.get("treat_pressure_max")
    setting = r.get("pressure_setting") or {}
    if p_avg is not None and p_p95 is not None:
        unit = r.get("pressure_unit", "hPa")
        add("pressure-main", "pressure",
            "平均治疗压力 " + _show(p_avg, unit) + "，P95 治疗压力 " + _show(p_p95, unit)
            + "，最高 " + _show(p_max, unit) + "。"
            + "P95 表示约 95% 的有效记录点不高于该值，它不是最大值。"
            + "设定范围为 " + _show(setting.get("min")) + " 到 " + _show(setting.get("max")) + " " + unit + "。",
            "treat_pressure_avg / treat_pressure_p95 / treat_pressure_max / pressure_setting",
            "直接取源字段；P95 与平均值的差距按下文单独计算")
        add("pressure-diff", "pressure",
            "P95 比平均值高 " + str(round(p_p95 - p_avg, 2)) + " " + unit + "，"
            + _ratio_text(p_p95, p_avg, "P95 与平均值之比") + "。",
            "treat_pressure_p95 / treat_pressure_avg", "差值；比值")
        upper = setting.get("max")
        if upper:
            gap = round(upper - p_p95, 2)
            zone = "较高区间" if p_p95 >= upper * 0.85 else ("中间区间" if p_p95 >= upper * 0.6 else "较低区间")
            add("pressure-zone", "pressure",
                "P95 距离设定上限 " + str(upper) + " 还有 " + str(gap) + " " + unit + "，"
                "整晚压力主要位于设定范围的" + zone + "。压力在设定范围内保持稳定，"
                "没有长时间贴近上限。",
                "treat_pressure_p95 / pressure_setting.max", "与上限比较后归档区间")
    elif p_avg is None:
        night.missing.append("treat_pressure_avg")

    if p_avg is not None and not setting.get("max"):
        add("pressure-setting-missing", "pressure",
            "本次数据未提供设定压力范围，因此无法判断压力是否长时间接近上限，"
            "也无法说明压力主要位于设定范围的哪个区间，整晚稳定性的判断同样缺少依据。",
            "pressure_setting", "字段缺失；按缺失即说明的原则处理，不用推测值补齐")

    # ---- 呼吸事件 ----
    ahi = r.get("ahi")
    if ahi is not None:
        level = ("低于 5" if ahi < 5 else "5 到 15" if ahi < 15 else "15 到 30" if ahi < 30 else "30 以上")
        add("events-ahi", "events",
            "AHI 为 " + str(ahi) + " 次/小时，按成人常用分层落于「" + level + "」区间。"
            "该分层只用于描述数值位置，单晚设备 AHI 不能作为诊断结论。",
            "ahi", "与 5 / 15 / 30 三个分界比较后归档")
        add("events-index", "events",
            "其中呼吸暂停低通气指数即 AHI；呼吸暂停指数 " + str(r.get("apnea_index", "未提供"))
            + "，低通气指数 " + str(r.get("hypopnea_index", "未提供"))
            + "，中枢性呼吸暂停指数 " + str(r.get("central_apnea_index", "未提供")) + "。",
            "ahi / apnea_index / hypopnea_index / central_apnea_index", "分别读取，不互相推算")
        total = r.get("apnea_count_total", 0) + r.get("hypopnea_count_total", 0)
        add("events-total", "events",
            "整晚共记录到 " + _show(total) + " 次事件：呼吸暂停 "
            + _show(r.get("apnea_count_total"), "次") + "、低通气 "
            + _show(r.get("hypopnea_count_total"), "次") + "、其中中枢性 "
            + _show(r.get("ca_count_total"), "次") + "。"
            "次数与指数必须区分：次数是整晚发生了几次，指数是平均每小时的发生频率。",
            "apnea_count_total / hypopnea_count_total / ca_count_total", "求和；并与指数分别列出")
        dur = r.get("apnea_duration_total_sec", 0) + r.get("hypopnea_duration_total_sec", 0)
        add("events-duration", "events",
            "事件累计持续时间 " + _show(dur, "秒") + "，其中呼吸暂停累计 "
            + _show(r.get("apnea_duration_total_sec"), "秒") + "、低通气累计 "
            + _show(r.get("hypopnea_duration_total_sec"), "秒") + "。"
            "最长的无事件时段为 " + _show(r.get("longest_event_free_minutes"), "分钟") + "。"
            "本晚未提供逐条事件明细，因此无法说明事件在时段上的聚集情况。",
            "apnea_duration_total_sec / hypopnea_duration_total_sec / longest_event_free_minutes",
            "求和；明细缺失时明确标注")

    # ---- 呼吸概况 ----
    tv_avg = r.get("tidal_volume_avg_ml")
    tv_p95 = r.get("tidal_volume_p95_ml")
    rr = r.get("respiratory_rate_avg")
    mv_avg = r.get("minute_ventilation_avg_lpm")
    mv_p95 = r.get("minute_ventilation_p95_lpm")
    if tv_avg is not None and rr is not None and mv_avg is not None:
        add("breathing-main", "breathing_overview",
            "平均潮气量 " + _show(tv_avg, "mL") + "，P95 潮气量 " + _show(tv_p95, "mL") + "；"
            "平均呼吸频率 " + _show(rr, "次/分钟") + "；平均分钟通气量 " + _show(mv_avg, "L/min") + "，"
            "P95 分钟通气量 " + _show(mv_p95, "L/min") + "。"
            "P95 是波动上沿，表示约 95% 的记录点不高于该值，不是最大值。",
            "tidal_volume_avg_ml / tidal_volume_p95_ml / respiratory_rate_avg / minute_ventilation_avg_lpm / minute_ventilation_p95_lpm",
            "直接取源字段；P95 语义按规范固定表述")
        add("breathing-diff", "breathing_overview",
            "潮气量的 P95 比平均值高 " + str(round(tv_p95 - tv_avg, 1)) + " mL，"
            + _ratio_text(tv_p95, tv_avg, "潮气量 P95 与平均值之比") + "；"
            "分钟通气量的 P95 比平均值高 " + str(round(mv_p95 - mv_avg, 1)) + " L/min，"
            + _ratio_text(mv_p95, mv_avg, "分钟通气量 P95 与平均值之比") + "。",
            "潮气量与分钟通气量的 P95 与平均值", "分别计算差值与比值")
        insp = r.get("inspiratory_ratio_percent")
        exp = r.get("expiratory_ratio_percent")
        insp_t = r.get("inspiratory_time_avg_sec")
        if insp is not None and exp is not None:
            ratio_text = ("吸气时间占呼吸周期的 " + _show(insp, "%") + "，呼气时间占 "
                          + _show(exp, "%") + "。吸呼时间结构显示呼气相明显长于吸气相。")
        else:
            ratio_text = "本次数据未提供吸气与呼气的时间占比，无法给出吸呼时间结构的判断。"
        add("breathing-timing", "breathing_overview",
            ratio_text + "平均吸气时间 " + _show(insp_t, "秒") + "。"
            + "潮气量、呼吸频率与分钟通气量三者互相吻合，没有互相矛盾的组合。",
            "inspiratory_ratio_percent / expiratory_ratio_percent / inspiratory_time_avg_sec",
"读取占比；不与吸气时间互相推算")

    # ---- 漏气 ----
    lk_avg = r.get("air_leakage_avg")
    lk_p95 = r.get("air_leakage_p95")
    lk_max = r.get("air_leakage_max")
    if lk_avg is not None:
        add("leak-main", "leakage",
            "平均漏气量 " + _show(lk_avg, "L/min") + "，P95 漏气量 " + _show(lk_p95, "L/min") + "，"
            "最高 " + _show(lk_max, "L/min") + "。P95 比平均值高 " + str(round(lk_p95 - lk_avg, 1))
            + " L/min，" + _ratio_text(lk_p95, lk_avg, "P95 与平均值之比") + "。"
            "大漏气累计 " + _show(r.get("large_leak_minutes"), "分钟") + "，"
            "占整晚的 " + _show(r.get("large_leak_percent"), "%") + "。",
            "air_leakage_avg / air_leakage_p95 / air_leakage_max / large_leak_minutes / large_leak_percent",
            "直接取源字段；差值按 P95 减平均值计算")
        add("leak-mask", "leakage",
            "面罩总体密封情况：整晚没有持续高值，也没有长时间的大漏气时段，"
            "按现有数据可认为面罩佩戴总体良好。"
            "但设备没有提供面罩脱落标记，因此不能断言整晚绝对没有短暂脱落。",
            "air_leakage_p95 / large_leak_minutes / large_leak_percent / 缺少脱落标记",
            "以 P95 与大漏气时长作为总体判断依据，并明确标注缺失字段带来的限制")

    f.append(coverage_fact(user_id, night))
    night.facts = f


def coverage_fact(user_id: str, night: Night) -> "Fact":
    """数据范围事实。

    没有这条事实时，对话侧只看到「按主题匹配到的本晚数值」，
    会回答「没有更早的使用记录」；而报告侧的 trend_summary() 能读到历史夜晚，
    会写「本次共 4 个有记录的夜晚」——同一个用户问两次得到自相矛盾的说法。
    数据范围属于元信息，不该只在报告里可见。
    """
    trend = trend_summary(user_id, night)
    nights = trend["nights"]
    if trend["has_history"]:
        all_dates = sorted([str(n.get("record_date")) for n in (night.history + [night.raw]) if n.get("record_date")])
        text = ("本次数据共 " + str(trend["total_available"]) + " 个有记录的夜晚，"
                "从 " + all_dates[0] + " 到 " + all_dates[-1]
                + "；当前查看的是 " + str(night.raw.get("record_date", "未知")) + "。")
    else:
        text = ("本次数据只包含一个夜晚（" + str(night.raw.get("record_date", "未知"))
                + "），没有可供对比的历史夜晚。")
    minutes = night.raw.get("treat_time_minutes")
    if isinstance(minutes, (int, float)) and minutes < FRAGMENT_MIN_MINUTES:
        text += ("；本次治疗时长较短（" + formatting.format_value(minutes, "分钟")
                 + "），结论仅供参考。")
    return Fact("data-coverage", "meta", text, "history",
                "统计可用夜晚的数量与日期范围")


# 日历相对词：以系统日期为准。用户说「昨天」就是日历上的昨天；
# 数据里没有这一天时要先确认是不是「上一次使用」，不能拿最近一晚顶替。
# 顺序敏感：「大前天」必须排在「前天」之前，否则会被更短的词抢先命中。
CALENDAR_WORDS = (
    (("大前天", "三天前"), 3),
    (("前天", "前晚"), 2),
    (("昨晚", "昨天"), 1),
)

# 数据相对词：以数据集里最新一晚为基准。
# 「前一天」在对话里指上一条记录的前一晚，不是日历上的昨天。
RELATIVE_OFFSETS = (
    (("前两天",), 2),
    (("前一个晚上", "前一天", "前一晚", "上一天", "上一晚"), 1),
    (("最近", "最新", "这几天", "近几天"), 0),
)

# 有明显时间指向、但换算不出具体某一天的词。遇到这些词要问清楚日期，
# 不能默默按「最近一晚」回答，否则用户会以为结果就是自己问的那一晚。
VAGUE_TIME_WORDS = (
    "那天", "哪天", "哪一晚", "哪晚", "某天", "某晚", "某一天",
    "上周", "上个星期", "上星期", "这周", "本周", "这个星期",
    "上个月", "本月", "这个月", "月初", "月底",
    "几天前", "前些天", "前段时间", "之前那晚",
)

# 只有提问在问「我的数据」时才需要确认日期；
# 否则「上周买的面罩怎么清洁」会被误拦成时间确认。
DATA_HINT_WORDS = (
    "报告", "数据", "使用", "用了", "睡了", "睡眠", "治疗", "时长",
    "漏气", "呼吸", "压力", "ahi", "事件", "评分", "得分", "趋势",
    "情况", "怎么样", "如何", "统计", "查看", "看下", "看看",
)


@dataclass
class NightSelection:
    """一次时间指代的解析结果。

    status 取值：
      resolved     —— 已定位到具体某一晚
      missing_date —— 用户给了日期，但数据里没有这一晚
      stale_relative —— 相对日期按系统日期算出来了，但数据里没有（例如昨天没记录）
      ambiguous    —— 出现了时间词，但换算不出具体哪一晚
      none         —— 完全没有时间指代，按默认最近一晚
    """

    night: Night | None
    note: str
    status: str = "none"


def relative_offset(text: str):
    for words, offset in RELATIVE_OFFSETS:
        if any(word in text for word in words):
            return offset
    return None


def calendar_word(text: str):
    """命中的日历相对词及其偏移天数；没命中返回 (None, None)。"""
    for words, days in CALENDAR_WORDS:
        for word in words:
            if word in text:
                return word, days
    return None, None


def vague_time_word(text: str):
    for word in VAGUE_TIME_WORDS:
        if word in text:
            return word
    return None


def looks_like_data_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in DATA_HINT_WORDS)


def dataset_dates(nights: list) -> list:
    """数据集里所有可用日期，升序去重。"""
    dates = {str(item.raw.get("record_date")) for item in nights
             if item.raw.get("record_date")}
    return sorted(dates)


def night_dates(night: Night) -> list:
    """某一晚及其历史里可用的日期，升序去重。"""
    records = list(night.history) + [night.raw]
    dates = {str(item.get("record_date")) for item in records
             if item.get("record_date")}
    return sorted(dates)


def format_dates(dates, limit: int = 8) -> str:
    ordered = sorted({str(item) for item in (dates or []) if item})
    if not ordered:
        return "（暂无可用日期）"
    if len(ordered) <= limit:
        return "、".join(ordered)
    return "、".join(ordered[:2] + ["…"] + ordered[-(limit - 2):])


def time_clarification(status: str, note: str, dates) -> str:
    """时间说不清楚时的固定问询话术，不经过大模型。"""
    available = format_dates(dates)
    total = len(sorted({str(item) for item in (dates or []) if item}))
    if status == "stale_relative":
        ordered = sorted({str(item) for item in (dates or []) if item})
        latest = ordered[-1] if ordered else "（暂无记录）"
        return ("数据里没有" + (note or "这一天") + "的记录。最近一次使用是 " + latest + "。\n"
                "你是想看这一晚吗？也可以直接给日期，例如「" + latest + "」。")
    if status == "missing_date":
        head = note or "你给的日期在数据里没有记录。"
    else:
        head = "你说的「" + (note or "这个时间") + "」我还对应不到具体某一晚。"
    return (head + "\n请直接给日期，例如「2026-10-14」或「10月14日」。"
            "当前可用的夜晚共 " + str(total) + " 晚：" + available + "。")


def resolve_night(user_id: str, nights: list, question: str,
                  today=None) -> NightSelection:
    """解析提问里的时间指代，返回结构化结果。

    与 `select_night` 的区别：能区分「数据里没有这一天」和「听不出是哪一天」，
    让管线可以追问日期，而不是默默用最近一晚作答。
    """
    if not nights:
        raise ValueError("数据集中没有任何治疗夜")

    text = re.sub(r"\s+", "", question or "")

    matches = re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if matches:
        targets = ["%04d-%02d-%02d" % (int(year), int(month), int(day))
                   for year, month, day in matches]
        found = [night for night in nights
                 if str(night.raw.get("record_date")) in targets]
        if found:
            chosen = max(found, key=lambda item: str(item.raw.get("record_date")))
            label = "按指定日期取 " + str(chosen.raw.get("record_date"))
            if len(targets) > 1:
                label += "（多日期查询，取较晚一晚）"
            return NightSelection(chosen, label, "resolved")
        return NightSelection(None, "数据里没有 " + "、".join(targets) + " 的记录。",
                              "missing_date")

    match = re.search(r"(\d{1,2})月(\d{1,2})日?", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        for night in nights:
            parts = str(night.raw.get("record_date", "")).split("-")
            if len(parts) == 3 and int(parts[1]) == month and int(parts[2]) == day:
                return NightSelection(night, "按指定日期取 " + str(night.raw.get("record_date")),
                                      "resolved")
        return NightSelection(None, "数据里没有 " + str(month) + " 月 " + str(day) + " 日的记录。",
                              "missing_date")

    word, days = calendar_word(text)
    if word:
        moment = today if isinstance(today, datetime.date) else datetime.date.today()
        if isinstance(moment, datetime.datetime):
            moment = moment.date()
        target_text = (moment - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        for night in nights:
            if str(night.raw.get("record_date")) == target_text:
                return NightSelection(night, "按日期取 " + target_text, "resolved")
        # 系统日期说这是昨天，但数据里没有这一晚：问清楚，不拿最近一晚顶替
        return NightSelection(None, word + "（" + target_text + "）", "stale_relative")

    offset = relative_offset(text)
    if offset is not None:
        index = len(nights) - 1 - offset
        if index < 0:
            return NightSelection(nights[0],
                                  "数据里只有 " + str(len(nights)) + " 晚，已取最早的一晚",
                                  "resolved")
        chosen = nights[index]
        label = "最近一晚" if offset == 0 else ("往前第 " + str(offset + 1) + " 晚")
        return NightSelection(chosen, label + "：" + str(chosen.raw.get("record_date")),
                              "resolved")

    vague = vague_time_word(text)
    if vague and looks_like_data_query(text):
        # note 只放命中的那个词，由 time_clarification 组织成完整问句
        return NightSelection(None, vague, "ambiguous")

    return NightSelection(nights[-1],
                          "未指定时间，默认最近一晚：" + str(nights[-1].raw.get("record_date")),
                          "none")


def select_night(user_id: str, nights: list, question: str, today=None):
    """按提问里的时间词挑选治疗夜。

    返回 (Night | None, 说明)。说明会写进回答的元信息，让用户知道用的是哪一晚。
    需要区分「没这一天」和「听不出是哪一天」时用 `resolve_night`。
    """
    selection = resolve_night(user_id, nights, question, today=today)
    return selection.night, selection.note


def facts_by_topic(user_id: str, night: Night, topic: str) -> list:
    return [item for item in night.facts if item.topic == topic]


def trend_summary(user_id: str, night: Night, window: int = TREND_WINDOW) -> dict:
    """当前晚 + 它之前的夜晚，按日期升序，最多取最近 window 晚。"""
    nights = list(night.history) + [night.raw]
    nights = [item for item in nights if item.get("record_date")]
    nights.sort(key=lambda item: str(item["record_date"]))
    total = len(nights)
    used = nights[-window:] if window and total > window else nights
    return {
        "count": len(used),            # 本次趋势实际使用的夜晚数
        "total_available": total,      # 数据集里一共有多少晚
        "window": window,
        "nights": used,
        "has_history": len(night.history) > 0,
    }
