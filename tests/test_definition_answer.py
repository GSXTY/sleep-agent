"""产品定义问题的精准回答测试。"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline, product_kb, validation


class TestDefinitionAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_bilevel_definition_only_keeps_relevant_section(self):
        result = pipeline.answer("双水平是什么意思", self.night, [], use_llm=False)
        self.assertTrue(result.mode.startswith("产品文档"), result.mode)
        self.assertIn("1.3.2", result.text)
        self.assertIn("IPAP", result.text)
        self.assertIn("EPAP", result.text)
        self.assertNotIn("电磁兼容", result.text)

    def test_trim_keeps_close_scores(self):
        hits = [
            {"score": 0.02, "section": "A"},
            {"score": 0.015, "section": "B"},
            {"score": 0.004, "section": "C"},
        ]
        trimmed = product_kb.trim_low_relevance(hits)
        self.assertEqual([hit["section"] for hit in trimmed], ["A", "B"])

    def test_negated_pressure_advice_is_safe(self):
        result = validation.check_output(
            "不要自行调整治疗压力，请遵医嘱。", "材料", mode="medium"
        )
        self.assertTrue(result["ok"], result["problems"])

    def test_affirmative_pressure_advice_is_blocked(self):
        result = validation.check_output(
            "建议自行调整治疗压力。", "材料", mode="medium"
        )
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
