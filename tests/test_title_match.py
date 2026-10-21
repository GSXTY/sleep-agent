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
    def test_quoted_section_number_passes_validation(self):
        """模型引用【1】和 1.1 章节号时，整条链路应显示"已校验"。"""
        night = nightdata.load_dataset()[-1]
        answer = "根据【1】1.1 工作原理：设备持续输出正压气流，保持上气道开放。"
        with mock.patch.object(pipeline.llm, "chat", return_value={
                "status": "success", "content": answer,
                "provider": "fake", "model": "fake"}):
            result = pipeline.answer("产品工作原理", night, [], use_llm=True)
        self.assertIn("已校验", result.mode)
        self.assertIn("正压", result.text)


if __name__ == "__main__":
    unittest.main()
