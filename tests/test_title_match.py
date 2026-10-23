"""提问里原样出现章节标题时，该章节应排在前面。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline, product_kb


class TitleMatchTest(unittest.TestCase):
    def test_section_core_strips_numbering(self):
        self.assertEqual(product_kb._section_core("1.1工作原理"), "工作原理")
        self.assertEqual(product_kb._section_core("六、清洁和保养"), "清洁和保养")
        self.assertEqual(product_kb._section_core("5.2.1.3系统设置"), "系统设置")

    def test_promotes_section_quoted_in_question(self):
        hits = [{"section": "1.3工作模式"},
                {"section": "1.1工作原理"},
                {"section": "3.1主机"}]
        ordered = product_kb.prefer_title_match("产品工作原理", hits)
        self.assertEqual(ordered[0]["section"], "1.1工作原理")

    def test_no_quote_keeps_order(self):
        hits = [{"section": "1.3工作模式"}, {"section": "3.1主机"}]
        self.assertEqual(product_kb.prefer_title_match("滤芯多久换一次", hits), hits)

    def test_short_title_does_not_trigger(self):
        # 标题太短（如"注意"）不作为精确命中信号
        hits = [{"section": "注意", "index": 1}, {"section": "3.1主机"}]
        self.assertEqual(product_kb.prefer_title_match("注意什么", hits), hits)


class PipelineTitleTest(unittest.TestCase):
    def test_product_principle_uses_deterministic_section(self):
        """产品工作原理直接使用确定性章节，不依赖模型改写。"""
        night = nightdata.load_dataset()[-1]
        with mock.patch.object(pipeline.llm, "chat") as chat:
            result = pipeline.answer("产品工作原理", night, [], use_llm=True)
        chat.assert_not_called()
        self.assertEqual(result.mode, "产品文档（规则版）")
        self.assertIn("正压", result.text)
        self.assertIn("01_产品说明.md · 工作原理", result.sources)


if __name__ == "__main__":
    unittest.main()
