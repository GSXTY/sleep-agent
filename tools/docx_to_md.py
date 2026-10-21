"""把产品 DOCX 转成便于切片的 Markdown。

特点：
  · 去掉封面页和目录（目录标题带页码，会导致重复章节）
  · 只把"像标题"的行当标题：编号标题 / 短的独立标题
  · 表格转成标准 Markdown 表，表头只出现一次
  · 警告 / 注意 / 说明 转成引用块，不再单独成节
  · 章节之间留空行，正文一行一段，便于按标题切片

用法：
    python tools/docx_to_md.py --src a.docx --out a.md
    python tools/docx_to_md.py --src a.docx --preview
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sleepagent import product_kb

NOTE_LABELS = ("警告", "注意", "说明", "提示", "警示")
NUMBER_HEADING = re.compile(r"^(?:[一二三四五六七八九十]+、|\d+(?:\.\d+)*[、.\s]?)")
TOC_TAIL = re.compile(r"\d+$")

# 无编号的中文标题：必须是"像章节名"的词，避免把示意图标签、版本号当标题
SECTION_SUFFIXES = (
    "型号", "指标", "要求", "信息", "规则", "说明", "组成", "结构", "功能",
    "步骤", "保养", "维护", "性能", "安全", "附录", "包装", "运输", "贮存",
    "标识", "售后", "服务", "处理", "兼容", "试验", "方法", "软件", "模式",
    "参数", "连接", "安装", "操作", "清洁", "消毒", "故障", "排除", "注意",
)
LATIN_HEADING = re.compile(r"^[A-Za-z][A-Za-z0-9/+.-]{1,11}$")
FIG_REF = re.compile(r"(?:如|见)?图(\d+[-–]\d+)(?:所示)?")
SEC_REF = re.compile(r"(见|参见|详见|查阅|参考)\s*(\d+(?:\.\d+)+)")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace("\u200b", "")).strip()


def _is_numbered(text: str) -> bool:
    return bool(re.match(r"^[一二三四五六七八九十]+、", text)
                or re.match(r"^\d+(?:\.\d+)+", text)
                or re.match(r"^\d+[、.\s]", text))


def _looks_like_heading(text: str, next_length: int = 0) -> bool:
    """比 DOCX 的样式判断更严格，避免把 V1.0.00 这类值当标题。"""
    text = _clean(text)
    if not text or len(text) > 40:
        return False
    if text.endswith(("。", "；", "，", "：", "！", "!", ":", ".", ",")):
        return False
    if " | " in text:
        return False
    if text in NOTE_LABELS:
        return False
    if _is_numbered(text):
        return True
    # 无编号标题：短、无句末标点、不含版本号样式
    if re.search(r"\d+\.\s*\d", text):
        return False
    if LATIN_HEADING.match(text):
        return True
    if len(text) > 12:
        return False
    if any(char in text for char in ("—", "—", "□", "~")):
        return False
    if any(text.endswith(suffix) for suffix in SECTION_SUFFIXES):
        return True
    # 后面紧跟一段正文（而不是另一个短标签）时，才认为这是无编号标题
    return next_length >= 15


def _depth(text: str, current: int) -> int:
    text = _clean(text)
    if re.match(r"^[一二三四五六七八九十]+、", text):
        return 2
    match = re.match(r"^(\d+(?:\.\d+)*)", text)
    if match:
        return min(6, 1 + match.group(1).count(".") + 1)
    return min(4, current + 1)


def _parse_table_row(line: str) -> list:
    text = line.strip()
    # 以 "| " 开头的行是合并单元格的续行：第一列应为空，不能把后面的值顶到第一列
    leading_empty = text.startswith("|")
    if leading_empty:
        text = text[1:]
    cells = [cell.strip() for cell in text.split("|")]
    if leading_empty:
        cells = [""] + cells
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _render_table(rows: list) -> str:
    parsed = [_parse_table_row(row) for row in rows]
    width = max(len(cells) for cells in parsed)
    parsed = [cells + [""] * (width - len(cells)) for cells in parsed]
    header, body = parsed[0], parsed[1:]
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * width) + "|"]
    for cells in body:
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _apply_references(markdown: str, title: str, headings: dict, captions: dict) -> str:
    """把脱离上下文的"图5-1 / 见4.2"改写成自解释引用。

    拆分后单独一份 MD 里，读者看不到原图的图注，也不知道 4.2 是什么章节。
    """
    output = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if re.match(r"^(图|表)\d+[-–]\d+", stripped):
            output.append(line)          # 图注/表注本身，保持原样
            continue

        def figure(match):
            fid = "图" + match.group(1)
            caption = captions.get(fid, "")
            rest = match.string[match.end():].lstrip()
            if caption and rest.startswith(caption):
                return "见《" + title + "》" + fid + "："
            return "见《" + title + "》" + fid + (
                "（" + caption + "）" if caption else "（需对照说明书原件查看）")

        line = FIG_REF.sub(figure, line)

        def section(match):
            verb, number = match.group(1), match.group(2)
            heading = headings.get(number, "")
            label = "《" + title + "》" + number
            if heading:
                label += "「" + heading + "」"
            return verb + label

        line = SEC_REF.sub(section, line)
        output.append(line)
    return "\n".join(output)


def convert(path: Path, title: str = "") -> tuple:
    lines = list(product_kb._read_docx(path))
    title = title or path.stem

    out = ["# " + title, ""]
    buffer = []
    table = []
    depth = 1
    in_toc = False
    pending_note = ""
    headings = {}
    captions = {}
    stats = {"headings": 0, "tables": 0, "paragraphs": 0, "notes": 0}

    for raw in lines:
        text = _clean(str(raw).lstrip("#"))
        match = re.match(r"^(图|表)(\d+[-–]\d+)\s*(.*)$", text)
        if match:
            caption = re.split(r"\s+|（\d+）", match.group(3).strip())[0].strip()
            captions[match.group(1) + match.group(2)] = caption[:24]

    def flush_paragraph():
        text = _clean(" ".join(buffer))
        buffer.clear()
        if not text:
            return
        if text in NOTE_LABELS:
            out.append("> **" + text + "**")
            out.append("")
            stats["notes"] += 1
            return
        nonlocal pending_note
        if pending_note:
            out.append("> **" + pending_note + "** " + text)
            out.append("")
            pending_note = ""
            stats["notes"] += 1
            return
        out.append(text)
        out.append("")
        stats["paragraphs"] += 1

    def flush_table():
        if table:
            out.append(_render_table(table))
            out.append("")
            stats["tables"] += 1
            table.clear()

    for index, raw in enumerate(lines):
        line = str(raw)
        heading = line.startswith("#")
        text = _clean(line[1:] if heading else line)
        if not text:
            continue

        # 目录：标题带页码，直到出现不带页码的同名标题
        if text == "目录":
            in_toc = True
            continue
        if in_toc:
            if heading and TOC_TAIL.search(text) and len(text) <= 30:
                continue
            in_toc = False

        if " | " in text:
            flush_paragraph()
            table.append(text)
            continue
        flush_table()

        if text in NOTE_LABELS:
            flush_paragraph()
            pending_note = text
            continue

        next_length = 0
        for follower in lines[index + 1:]:
            candidate = _clean(str(follower).lstrip("#"))
            if candidate:
                next_length = len(candidate)
                break

        if heading and _looks_like_heading(text, next_length):
            flush_paragraph()
            depth = _depth(text, depth)
            out.append("#" * depth + " " + text)
            out.append("")
            number = re.match(r"^(\d+(?:\.\d+)*)", text)
            if number:
                headings[number.group(1)] = re.sub(
                    r"^\d+(?:\.\d+)*\s*", "", text).strip()
            stats["headings"] += 1
            continue

        buffer.append(text)
        if sum(len(item) for item in buffer) >= 300:
            flush_paragraph()

    flush_table()
    flush_paragraph()
    markdown = _apply_references("\n".join(out).rstrip(), title, headings, captions)
    return markdown + "\n", stats


def main() -> int:
    parser = argparse.ArgumentParser(description="DOCX → Markdown")
    parser.add_argument("--src", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--preview", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args()

    src = Path(args.src)
    markdown, stats = convert(src, args.title)
    print(src.name + " → 标题 " + str(stats["headings"])
          + " / 表格 " + str(stats["tables"])
          + " / 段落 " + str(stats["paragraphs"])
          + " / 注意块 " + str(stats["notes"])
          + " / 字符 " + str(len(markdown)))
    if args.preview:
        print("\n" + markdown[:1500])
        return 0
    out = Path(args.out) if args.out else src.with_suffix(".md")
    out.write_text(markdown, encoding="utf-8")
    print("已写入 " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
