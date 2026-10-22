"""把说明书 DOCX 按主题拆成多份 Markdown。

输出（product_docs/user）：
  01_产品说明.md      使用本手册 / 产品介绍 / 禁忌症
  02_使用指南.md      结构组成 / 使用步骤 / 功能介绍
  03_清洁与维护.md    清洁保养 / 携带旅行 / 交给另一位患者
  04_安全使用.md      安全使用原则
  05_技术参数.md      产品性能 / 标识
  06_电磁兼容.md      电磁兼容
  07_售后服务.md      售后服务 / 废弃处理 / 联系地址
  08_常见问题.md      由"九、故障排除"两张表生成的问答

用法：
    python tools/build_product_md.py --preview
    python tools/build_product_md.py --write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docx_to_md import convert as convert_docx

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "product_docs" / "_originals" / "YA70主机说明书中文版-20260918-改规格.docx"
if not SOURCE.exists():   # 兼容尚未归档的目录
    SOURCE = ROOT / "product_docs" / "user" / SOURCE.name
OUT_DIR = ROOT / "product_docs" / "user"

FILE_MAP = (
    ("01_产品说明.md", "YA70 产品说明", ("使用本手册", "一、产品介绍", "二、禁忌症")),
    ("02_使用指南.md", "YA70 使用指南", ("三、结构组成", "四、使用步骤", "五、功能介绍")),
    ("03_清洁与维护.md", "YA70 清洁与维护", ("六、清洁和保养", "七、携带呼吸机旅行",
                                          "八、交给另一位患者使用")),
    ("04_安全使用.md", "YA70 安全使用原则", ("十、安全使用原则",)),
    ("05_技术参数.md", "YA70 技术参数", ("十一、产品性能", "十四、标识")),
    ("06_电磁兼容.md", "YA70 电磁兼容", ("十六、电磁兼容",)),
    ("07_售后服务.md", "YA70 售后服务", ("十二、售后服务", "十三、废弃处理",
                                      "十五、联系地址与电话")),
)

SOURCE_NOTE = ("> 来源：YA70 主机说明书（原件已归档到 product_docs/_originals/）。"
               "内容按主题拆分，便于检索；如有冲突以原件为准。")


def split_chapters(markdown: str) -> dict:
    """按二级标题切章节，返回 {章节名: 正文 markdown}。"""
    chapters = {}
    current = ""
    buffer = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current:
                chapters[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
            continue
        if current:
            buffer.append(line)
    if current:
        chapters[current] = "\n".join(buffer).strip()
    return chapters


def _match(chapters: dict, name: str) -> str:
    for title, body in chapters.items():
        if title == name or title.startswith(name) or name in title:
            return "## " + title + "\n\n" + body + "\n"
    return ""


def _tidy(text: str) -> str:
    """去掉 Word 在中文字符之间插入的空格，并把编号步骤并成一条。"""
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    parts = [part.strip() for part in re.split(r"(?=\d+\.\s*)", text) if part.strip()]
    if len(parts) > 1:
        parts = [re.sub(r"^\d+\.\s*", "", part) for part in parts]
        text = "；".join(parts)
    return text.strip()


def build_faq(chapters: dict) -> str:
    """从故障排除的两张表生成问答，一个问题一节。"""
    block = chapters.get("九、故障排除", "")
    lines = ["# YA70 常见问题", "", SOURCE_NOTE, "",
             "以下问答来自说明书「九、故障排除」，按「问题—原因—解决办法」整理。", ""]
    seen = set()
    count = 0
    for line in block.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("问题", "---") or set(cells[0]) <= {"-"}:
            continue
        question, cause, solution = (_tidy(cells[0]), _tidy(cells[1]), _tidy(cells[2]))
        if not question or question in seen:
            continue
        seen.add(question)
        title = question if question.endswith(("怎么办", "怎么处理")) else question + "怎么办"
        lines += ["## " + title, "",
                  "- 可能原因：" + cause,
                  "- 解决办法：" + solution, ""]
        count += 1
    print("常见问题条目：" + str(count))
    return "\n".join(lines)


def relocate_appendix(outputs: dict) -> dict:
    """把误归到"十六、电磁兼容"的舒适设置表迁回使用指南。

    说明书文末附录的表格前面没有标题，转换时会继承上一个章节，
    导致"延时升压"的完整定义落在电磁兼容文件里。
    """
    emc = outputs.get("06_电磁兼容.md", "")
    guide = outputs.get("02_使用指南.md", "")
    if not emc or not guide:
        return outputs
    start = emc.find("舒适\n\n| 设置项 |")
    end = emc.find("3.高级模式下查看报告", start)
    if start < 0 or end < 0:
        return outputs
    block = emc[start:end].strip()
    outputs["06_电磁兼容.md"] = (emc[:start] + emc[end:]).rstrip() + "\n"
    section = ("## 常用设置说明\n\n"
               "以下为设备治疗设置的说明，最后一列标注各模式是否支持。\n\n"
               + block + "\n")
    outputs["02_使用指南.md"] = guide.rstrip() + "\n\n" + section
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="拆分说明书为多份 Markdown")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    markdown, stats = convert_docx(SOURCE, "YA70 主机说明书")
    chapters = split_chapters(markdown)
    print("章节数：" + str(len(chapters)) + "，转换标题 " + str(stats["headings"]))

    outputs = {}
    for filename, title, names in FILE_MAP:
        body = "\n\n".join(
            part for part in (_match(chapters, name).strip() for name in names) if part
        )
        if not body:
            print("  [!] " + filename + " 没有匹配到章节")
            continue
        outputs[filename] = "# " + title + "\n\n" + SOURCE_NOTE + "\n\n" + body + "\n"

    outputs["08_常见问题.md"] = build_faq(chapters)
    outputs = relocate_appendix(outputs)

    for filename, content in outputs.items():
        print("  " + filename.ljust(18) + str(len(content)).rjust(6) + " 字")
        if args.write:
            (OUT_DIR / filename).write_text(content, encoding="utf-8")
    if args.write:
        print("已写入 " + str(OUT_DIR))
    elif args.preview:
        print("\n" + next(iter(outputs.values()))[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
