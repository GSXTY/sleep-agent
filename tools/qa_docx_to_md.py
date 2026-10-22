"""把"百问百答"格式的 DOCX 转成按问题切片的 Markdown。

格式约定：
  # 1. 分类名        -> ## 分类
  # Q001 问题？       -> ### Q001 问题？
  标准答案：...       -> 列表项
  销售简答：...       -> 可选列表项（内部销售版保留）

用法：
    python tools/qa_docx_to_md.py --src 百问百答.docx --out user.md
    python tools/qa_docx_to_md.py --src 百问百答.docx --out sales.md --sales
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sleepagent import product_kb

QUESTION = re.compile(r"^Q(\d{3})\s*(.+)$")


def convert(path: Path, title: str, include_sales: bool = False,
            skip_prefixes=(), exclude_questions=(), note: str = "") -> tuple:
    lines = list(product_kb._read_docx(path))
    out = ["# " + title, ""]
    if note:
        out += ["> " + note, ""]
    stats = {"questions": 0, "categories": 0}
    started = False
    skipping = False
    skipping_question = False
    excluded = {str(item).strip().upper() for item in exclude_questions}

    for raw in lines:
        text = str(raw).strip()
        if not text:
            continue
        heading = text.startswith("#")
        body = text.lstrip("# ").strip() if heading else text

        is_question = bool(QUESTION.match(body)) if heading else False
        if heading and not is_question:
            # 只在分类标题处更新跳过状态；Q 标题不要重置
            skipping_question = False
            skipping = any(body.startswith(prefix) for prefix in skip_prefixes)
            if skipping:
                continue
        elif skipping:
            continue

        match = QUESTION.match(body) if is_question else None
        if match:
            question_id = "Q" + match.group(1)
            if question_id in excluded:
                skipping_question = True
                continue
            skipping_question = False
            if not started:
                out.insert(2, "")
                started = True
            out.append("### " + question_id + " " + match.group(2).strip())
            out.append("")
            stats["questions"] += 1
            continue

        if skipping_question:
            continue

        if heading:
            out.append("## " + body)
            out.append("")
            stats["categories"] += 1
            started = True
            continue

        if body.startswith("标准答案："):
            out.append("- **标准答案**：" + body[len("标准答案："):].strip())
            out.append("")
            continue
        if body.startswith("销售简答："):
            if include_sales:
                out.append("- **销售简答**：" + body[len("销售简答："):].strip())
                out.append("")
            continue

        if not started:
            # 封面与使用提示
            out.append("> " + body)
        else:
            out.append(body)
            out.append("")

    return "\n".join(out).rstrip() + "\n", stats


def main() -> int:
    parser = argparse.ArgumentParser(description="百问百答 DOCX → Markdown")
    parser.add_argument("--src", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--sales", action="store_true", help="保留销售简答")
    parser.add_argument("--skip-prefix", action="append", default=[],
                        help="跳过指定编号开头的分类，如 9.")
    parser.add_argument("--exclude", action="append", default=[],
                        help="排除指定问题编号，如 Q008")
    parser.add_argument("--note", default="", help="标题下的口径提示")
    args = parser.parse_args()

    src = Path(args.src)
    markdown, stats = convert(src, args.title or src.stem, args.sales,
                              args.skip_prefix, args.exclude, args.note)
    Path(args.out).write_text(markdown, encoding="utf-8")
    print(src.name + " → 问题 " + str(stats["questions"])
          + " / 分类 " + str(stats["categories"])
          + " / 字符 " + str(len(markdown)))
    print("已写入 " + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
