"""两晚报告对比。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline


class ReportCompareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nights = nightdata.load_dataset()
        cls.night = cls.nights[-1]

    def test_last_vs_previous(self):
        result = pipeline.answer("上一次的报告和前一次相比", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "报告对比")
        self.assertIn("2026-10-14", result.text)
        self.assertIn("2026-10-15", result.text)
        self.assertIn("| AHI | 6.9", result.text)
        self.assertIn("| AHI | 6.9次/小时 | 5.7次/小时", result.text)

    def test_two_explicit_dates(self):
        result = pipeline.answer("2026-10-13 和 2026-10-15 对比",
                                 self.night, [], use_llm=False)
        self.assertEqual(result.mode, "报告对比")
        self.assertIn("2026-10-13", result.text)
        self.assertIn("2026-10-15", result.text)

    def test_first_night_has_no_previous(self):
        result = pipeline.answer("和前一次相比", self.nights[0], [], use_llm=False)
        self.assertIn("数据不足", result.mode)
        self.assertIn("没有前一晚", result.text)


if __name__ == "__main__":
    unittest.main()
