"""清理结构编号不能破坏参数、步骤、代码和章节合并。"""
import unittest
from pathlib import Path

from sleepagent import product_kb
from sleepagent.document_text import clean_heading, normalize_markdown_headings


class DocumentHeadingsTest(unittest.TestCase):
    def test_numbered_heading_formats(self):
        for raw, expected in (
            ("1.3.2双水平模式", "双水平模式"),
            ("6.4 更换空气滤芯", "更换空气滤芯"),
            ("十二、售后服务", "售后服务"),
            ("3. 型号与模式", "型号与模式"),
            ("第一章 产品说明", "产品说明"),
            ("Q026 单水平模式和双水平模式有什么区别？", "单水平模式和双水平模式有什么区别？"),
        ):
            self.assertEqual(clean_heading(raw), expected)

    def test_actual_values_and_identifiers_are_kept(self):
        for title in ("4.35 hPa", "28dB", "24小时连续运行", "YA70A", "CPAP"):
            self.assertEqual(clean_heading(title), title)

    def test_faq_id_is_hidden_metadata_not_answer_material(self):
        text = "### Q026 模式区别\n\n标准答案正文。\n"
        normalized = normalize_markdown_headings(text)
        self.assertIn("### 模式区别\n<!-- faq-id: Q026 -->", normalized)
        self.assertEqual(normalize_markdown_headings(normalized), normalized)
        chunks = product_kb._chunk("faq.md", normalized.splitlines())
        self.assertEqual(chunks[0].section, "模式区别")
        self.assertNotIn("Q026", chunks[0].text)

    def test_only_headings_are_changed(self):
        text = ("### 1.3.2双水平模式\r\n"
                "压力为4.35 hPa，参见2.3.1。\r\n"
                "1. 连接电源。\r\n| 型号 | 4~20 |\r\n"
                "```md\r\n## 1.2 示例\r\n```\r\n")
        expected = text.replace("### 1.3.2双水平模式", "### 双水平模式")
        result = normalize_markdown_headings(text)
        self.assertEqual(result, expected)
        self.assertEqual(normalize_markdown_headings(result), result)

    def test_markdown_hierarchy_merges_only_descendants(self):
        chunks = product_kb._chunk("test.md", [
            "## 工作模式", "本设备具备以下模式：", "### CPAP", "固定压力。",
            "### 双水平模式", "两个压力。", "## 保修", "保修条款。",
        ])
        self.assertIn("CPAP：固定压力", chunks[0].text)
        self.assertIn("双水平模式：两个压力", chunks[0].text)
        self.assertNotIn("保修", chunks[0].text)

    def test_active_documents_have_no_numbered_headings(self):
        root = product_kb.docs_dir()
        for folder in (root, root / "user", root / "internal", root / "examples"):
            for path in folder.glob("*.md"):
                with self.subTest(path=str(path)):
                    text = path.read_bytes().decode("utf-8")
                    self.assertEqual(text, normalize_markdown_headings(text))
