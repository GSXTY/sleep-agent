"""产品资料标题规范化：只清理结构编号，保留正文和实际数值。"""
import re


def clean_heading(title: str) -> str:
    title = title.strip()
    title = re.sub(r"^Q\d{3,}\s+", "", title, flags=re.I)
    # 数字后紧接单位时是实际数值，不是章节号。
    if re.match(r"^\d+(?:\.\d+)*\s*(?:hpa|pa|cm|mm|kg|hz|db|l/min|%|小时|分钟|天|年)(?:\b|$)", title, re.I):
        return title
    return re.sub(
        r"^(?:第[一二三四五六七八九十百零\d]+[章节]\s*|"
        r"[一二三四五六七八九十百]+[、．.]\s*|"
        r"\d+(?:\.\d+)+(?:[.、．])?\s*|\d+[.、．]\s*)",
        "", title,
    ).strip()


def normalize_markdown_headings(markdown: str) -> str:
    """保留 Markdown 层级、换行和非标题内容；代码块不处理。"""
    output = []
    fence = None
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        if fence is None and not marker:
            heading = re.match(r"^(#{1,6}[ \t]+)([^\r\n]+)(\r?\n)?$", line)
            if heading:
                line = heading.group(1) + clean_heading(heading.group(2)) + (heading.group(3) or "")
                faq_id = re.match(r"^(Q\d{3,})\s+", heading.group(2), re.I)
                if faq_id:
                    newline = heading.group(3) or "\n"
                    if not heading.group(3):
                        line += newline
                    line += "<!-- faq-id: " + faq_id.group(1) + " -->" + newline
        output.append(line)
    return "".join(output)
