"""把一条 bad case 记进 eval/cases.jsonl。

用法：
    python record_badcase.py --query "YA70模式有哪些" --mode 产品文档 \
        --section 1.3工作模式 --contains AutoCPAP --note "倒装问法"

    python record_badcase.py --list        # 查看已登记的 case
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_cases import CASES_FILE, load_cases, record_case


def main() -> int:
    parser = argparse.ArgumentParser(description="记录 bad case")
    parser.add_argument("--query", default="", help="用户原话")
    parser.add_argument("--mode", default="", help="期望模式前缀，如 产品文档")
    parser.add_argument("--section", default="", help="期望命中的章节名")
    parser.add_argument("--contains", action="append", default=[],
                        help="回答里必须出现的内容，可重复")
    parser.add_argument("--source", default="线上", help="线上 / 测试 / 评审")
    parser.add_argument("--note", default="", help="踩坑说明")
    parser.add_argument("--list", action="store_true", help="列出现有 case")
    args = parser.parse_args()

    if args.list:
        cases = load_cases()
        print("共 " + str(len(cases)) + " 条（" + str(CASES_FILE) + "）")
        for item in cases:
            print("  " + item.get("id", "?") + " | " + item["query"]
                  + " | mode=" + (item.get("expected_mode") or "-")
                  + " | section=" + (item.get("expected_section") or "-"))
        return 0

    if not args.query:
        parser.print_help()
        return 2
    case = record_case(args.query, args.mode, args.section, args.contains,
                       args.source, args.note)
    print("已记录（或已存在）：")
    print(json.dumps(case, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
