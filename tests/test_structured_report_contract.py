"""结构化报告契约测试。

样例文件是前端对接的稳定形状；这里比较字段名、层级和类型，
不把样本文案当作固定值。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, report


SAMPLE_FILE = Path(__file__).resolve().parents[1] / "contracts" / "structured_report_sample.json"


class TestStructuredReportContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
        cls.actual = report.render_structured(nightdata.load_night())

    def _assert_same_shape(self, expected, actual, path="root"):
        self.assertIs(type(expected), type(actual), path)
        if isinstance(expected, dict):
            self.assertEqual(set(expected), set(actual), path)
            for key in expected:
                self._assert_same_shape(expected[key], actual[key], path + "." + key)
        elif isinstance(expected, list) and expected:
            self.assertTrue(actual, path)
            self._assert_same_shape(expected[0], actual[0], path + "[0]")

    def test_sample_matches_runtime_shape(self):
        self._assert_same_shape(self.sample, self.actual)

    def test_schema_identity_is_fixed(self):
        self.assertEqual(self.actual["schema_version"], "1.0.0")
        self.assertEqual(self.actual["report_type"], "single_night")

    def test_core_metric_cards_are_renderable(self):
        cards = self.actual["core_metric_cards"]
        self.assertEqual(len(cards), 4)
        self.assertEqual(
            [card["key"] for card in cards],
            ["ahi", "treat_time", "air_leakage", "treat_pressure"],
        )
        for card in cards:
            self.assertEqual(
                set(card),
                {"key", "source_field", "label", "value", "unit", "display", "provided"},
            )
            self.assertIsInstance(card["provided"], bool)
            if card["provided"]:
                self.assertIsNotNone(card["value"])
                self.assertNotEqual(card["display"], "未提供")

    def test_modules_have_titles_and_text(self):
        modules = self.actual["modules"]
        self.assertEqual(
            list(modules),
            ["pressure", "respiratory-events", "breathing-overview", "leakage"],
        )
        for module in modules.values():
            self.assertEqual(set(module), {"title", "text"})
            self.assertTrue(module["title"])
            self.assertTrue(module["text"])

    def test_summary_acceptance_and_disclaimer_exist(self):
        self.assertEqual(set(self.actual["summary"]), {"positives", "issues", "actions"})
        self.assertEqual(set(self.actual["acceptance"]),
                         {"passed", "results", "version"})
        self.assertIsInstance(self.actual["missing_fields"], list)
        self.assertIn("不构成医学诊断", self.actual["disclaimer"])


if __name__ == "__main__":
    unittest.main()
