"""把「N天报告导出」CSV 转成单晚报告。

定位：这是一层**外部数据源适配**，把报告层导出（每行一个治疗夜）映射到项目内部的
单晚契约，再交给现有的 report.py 生成报告。它不改动项目其它模块。

用法：
    python import_report_export.py --list
    python import_report_export.py --date 2026-10-16
    python import_report_export.py --latest          # 最近一个达到可用时长的夜晚

已知的数据问题（都在导出侧，不在本脚本）：
  1. 压力类字段列名写 hPa，实际是原始缩放值，必须 /100
  2. 大量记录只有几分钟治疗时长，属于碎片记录，不适合出报告
  3. 次数类字段填充率低（11%~28%），但对应的指数是 100%，不能把空当 0
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sleepagent import nightdata, report

PRESSURE_DIVISOR = 100.0     # 导出里的压力是原始缩放值
LOCAL_DIR = Path(__file__).resolve().parent / ".local"
POINTER = LOCAL_DIR / "report_export.path"
# 真实数据不进仓库（沿用原项目约定）：导出文件用 .local/report_export.path 指向外部路径
DEFAULT_EXPORT = Path(__file__).resolve().parent / "data" / "report_export.csv"
USABLE_MINUTES = 240         # 低于这个时长视为碎片记录

# 导出列名 -> 内部契约字段。（列名很长，用前缀匹配）
COLUMN_MAP = (
    ("用户ID", "user_id", None),
    ("记录时间", "record_date", None),
    ("设备ID", "device_id", None),
    ("PiCode", "pi_code", None),
    ("设备类型", "device_type", None),
    ("固件版本", "firmware_version", None),
    ("硬件版本", "hardware_version", None),
    ("开始时间", "start_time", None),
    ("结束时间", "end_time", None),
    ("治疗时长", "treat_time_minutes", "num"),
    ("治疗评分", "treat_score", "num"),
    ("治疗时长得分", "treat_duration_score", "num"),
    ("面罩得分", "mask_score", "num"),
    ("呼吸事件得分", "resp_score", "num"),
    ("AHI", "ahi", "num"),
    ("睡眠呼吸暂停指数 AI", "apnea_index", "num"),
    ("低通气指数 HI", "hypopnea_index", "num"),
    ("中枢性睡眠呼吸暂停指数 CAI", "central_apnea_index", "num"),
    ("呼吸暂停次数", "apnea_count_total", "num"),
    ("低通气次数", "hypopnea_count_total", "num"),
    ("中枢性睡眠呼吸暂停次数", "ca_count_total", "num"),
    ("呼吸暂停总持续时间", "apnea_duration_total_sec", "num"),
    ("低通气总持续时间", "hypopnea_duration_total_sec", "num"),
    ("平均治疗压力", "treat_pressure_avg", "pressure"),
    ("治疗压力P95", "treat_pressure_p95", "pressure"),
    ("平均潮气量", "tidal_volume_avg_ml", "num"),
    ("潮气量P95", "tidal_volume_p95_ml", "num"),
    ("平均呼吸频率", "respiratory_rate_avg", "num"),
    ("平均分钟通气量", "minute_ventilation_avg_lpm", "num"),
    ("分钟通气P95", "minute_ventilation_p95_lpm", "num"),
    ("平均吸气时间", "inspiratory_time_avg_sec", "num"),
    ("平均漏气", "air_leakage_avg", "num"),
    ("漏气P95", "air_leakage_p95", "num"),
    ("漏气最大持续", "large_leak_minutes", "num"),
    ("漏气时间占比", "large_leak_percent", "num"),
)

# 契约里需要但导出没有的字段，显式记录，便于报告侧标注数据不足
ABSENT_FIELDS = (
    "treat_pressure_max", "pressure_setting", "mask_type",
    "longest_event_free_minutes", "air_leakage_max",
    "minute_detail_coverage", "event_detail_coverage",
)


def load_rows(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise SystemExit("导出文件为空")
    # 导出是 UTF-8 BOM 开头的，第一列的列名会带上 \ufeff，匹配前先剥掉
    header = [name.lstrip("\ufeff") for name in rows[0]]
    data = [r for r in rows[1:] if any(x.strip() for x in r)]
    index = {}
    for key, _, _ in COLUMN_MAP:
        for i, name in enumerate(header):
            if name.startswith(key):
                index[key] = i
                break
    missing = [key for key, _, _ in COLUMN_MAP if key not in index]
    if missing:
        raise SystemExit("导出缺少这些列：" + "、".join(missing))
    return header, data, index


def convert(row: list, index: dict) -> dict:
    record = {}
    for key, field, kind in COLUMN_MAP:
        raw = (row[index[key]] or "").strip() if index[key] < len(row) else ""
        if kind is None:
            if raw:
                record[field] = raw
            continue
        if raw == "":
            continue                     # 空值不写，绝不填 0
        try:
            value = float(raw)
        except ValueError:
            continue
        if kind == "pressure":
            value = round(value / PRESSURE_DIVISOR, 2)
        record[field] = value
    record["data_source"] = "报告导出 · 用户 " + str(record.get("user_id", "?"))
    record["pressure_unit"] = "hPa"
    record["_absent_fields"] = list(ABSENT_FIELDS)
    return record


def build_night(record: dict):
    """写成临时 JSON 再交给现有事实层，避免重复实现一遍测算逻辑。"""
    user_id = str(record.get("user_id") or nightdata.DEFAULT_USER_ID)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.close()
        return nightdata.load_night(user_id, path=handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="从报告导出生成单晚报告")
    parser.add_argument("--file", default="", help="导出 CSV 路径")
    parser.add_argument("--date", default="", help="指定治疗夜，如 2026-10-16")
    parser.add_argument("--latest", action="store_true", help="自动选最近一个可用夜晚")
    parser.add_argument("--list", action="store_true", help="列出所有夜晚及其时长")
    parser.add_argument("--index", type=int, default=-1, help="直接按行号取（从 0 开始）")
    parser.add_argument("--export-json", default="", help="把转换后的内部契约写到指定文件")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
    elif POINTER.is_file():
        path = Path(POINTER.read_text(encoding="utf-8").strip())
    else:
        path = DEFAULT_EXPORT
    if not path.is_file():
        raise SystemExit("找不到导出文件。用 --file 指定，或写入 .local/report_export.path 记录常用路径")
    header, data, index = load_rows(path)

    if args.list:
        print("共 " + str(len(data)) + " 条记录：")
        for i, row in enumerate(data):
            minutes = row[index["治疗时长"]].strip() if index["治疗时长"] < len(row) else ""
            flag = "  可用" if minutes and float(minutes) >= USABLE_MINUTES else ""
            print("  [" + str(i).rjust(2) + "] " + row[index["记录时间"]][:10]
                  + "  " + (minutes or "?").rjust(6) + " 分钟  "
                  + row[index["设备ID"]][:14].ljust(15) + flag)
        return 0

    if args.index >= 0:
        target = data[args.index]
    elif args.date:
        matches = [r for r in data if index["记录时间"] < len(r) and r[index["记录时间"]][:10] == args.date]
        if not matches:
            raise SystemExit("没有找到 " + args.date + " 的记录，用 --list 查看可用日期")
        target = max(matches, key=lambda r: float(r[index["治疗时长"]] or 0))
    elif args.latest:
        usable = [r for r in data if r[index["治疗时长"]].strip() and float(r[index["治疗时长"]]) >= USABLE_MINUTES]
        if not usable:
            raise SystemExit("没有达到 " + str(USABLE_MINUTES) + " 分钟的记录")
        target = max(usable, key=lambda r: r[index["记录时间"]])
    else:
        parser.print_help()
        return 1

    record = convert(target, index)

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已导出内部契约：" + str(out_path))
        print("提示：真实数据文件不要提交到仓库，建议放在 .local/ 下。")
        print()

    minutes = record.get("treat_time_minutes", 0)
    print("=" * 60)
    print("数据来源：" + str(record.get("record_date")) + " · 设备 " + str(record.get("device_id"))
          + " · 治疗时长 " + str(minutes) + " 分钟")
    if minutes < USABLE_MINUTES:
        print("[!] 注意：本次治疗时长不到 " + str(USABLE_MINUTES) + " 分钟，属于碎片记录，")
        print("    报告里的结论不具备参考意义。")
    print("=" * 60)
    print()

    night = build_night(record)
    payload = report.render(night)
    print(payload["text"])
    print()
    print("字段映射情况：")
    print("  已映射 " + str(len([k for k in record if not k.startswith("_")])) + " 个字段")
    print("  导出未提供：" + "、".join(ABSENT_FIELDS))
    print("  因缺数据而跳过的判断已在上文标注")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
