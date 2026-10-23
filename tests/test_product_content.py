"""说明书拆分后的关键内容不能丢。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER = ROOT / "product_docs" / "user"


class ProductContentTest(unittest.TestCase):
    def test_delay_ramp_definition_is_complete(self):
        text = (USER / "00_产品规格.md").read_text(encoding="utf-8")
        start = text.index("## 延时升压")
        section = text[start:text.index("## ", start + 3)]
        self.assertIn("设置治疗开始后", section)
        self.assertIn("45", section)
        self.assertIn("初始压力", section)

    def test_comfort_table_moved_to_guide(self):
        guide = (USER / "02_使用指南.md").read_text(encoding="utf-8")
        self.assertIn("## 常用设置说明", guide)
        self.assertIn("| 延时升压 | 设置治疗开始后", guide)

    def test_comfort_table_not_in_emc(self):
        emc = (USER / "06_电磁兼容.md").read_text(encoding="utf-8")
        self.assertNotIn("| 延时升压 | 设置治疗开始后", emc)

    def test_humidity_level_row_is_complete(self):
        from sleepagent import product_kb
        chunk = next(c for c in product_kb.load_chunks()
                     if c.section == "主界面 · 加湿等级")
        self.assertIn("0为关闭", chunk.text)
        self.assertIn("8为最高加湿等级", chunk.text)

    def test_user_faq_excludes_sales_policy_questions(self):
        faq = (USER / "10_YA70A常见问题.md").read_text(encoding="utf-8")
        self.assertIn("Q001", faq)
        for question_id in ("Q008", "Q009", "Q092", "Q093"):
            self.assertNotIn(question_id, faq)

    def test_sales_faq_is_archived_outside_retrieval(self):
        sales = ROOT / "product_docs" / "_sales" / "YA70A销售百问百答.md"
        self.assertTrue(sales.exists())
        text = sales.read_text(encoding="utf-8")
        self.assertIn("Q008", text)
        self.assertIn("Q009", text)

    def test_manual_keeps_official_noise_value(self):
        manual = (USER / "05_技术参数.md").read_text(encoding="utf-8")
        self.assertIn("28dB", manual)


if __name__ == "__main__":
    unittest.main()
