"""“使用情况”是概览，不能只答使用时长。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline, router


class SummaryScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_usage_overview_maps_to_overall(self):
        for question in ("昨晚使用情况", "2026-10-15 的使用情况", "整体情况怎么样"):
            self.assertEqual(router.route(question)["topics"], ["overall"], question)

    def test_specific_metric_keeps_its_topic(self):
        self.assertEqual(router.route("2026-10-15 的漏气情况")["topics"], ["leakage"])
        self.assertEqual(router.route("2026-10-15 的 AHI 是多少")["topics"], ["ahi"])

    def test_overview_includes_ahi_and_leak(self):
        result = pipeline.answer("2026-10-15 的使用情况", self.night, [], use_llm=False)
        self.assertIn("设备数据", result.mode)
        self.assertIn("5.7", result.text)        # AHI
        self.assertIn("18.1", result.text)       # P95 漏气


if __name__ == "__main__":
    unittest.main()
