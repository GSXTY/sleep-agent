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
        self.assertIn("IPAP", result.text)
        self.assertIn("EPAP", result.text)
        self.assertNotIn("电磁兼容", result.text)

    def test_eye_pain_uses_troubleshooting_row_only(self):
        result = pipeline.answer("为什么眼睛有点痛", self.night, [], use_llm=False)
        self.assertIn("眼睛刺痛", result.text)
        self.assertNotIn("胸部不适", result.text)
        self.assertNotIn("电磁兼容", result.text)

    def test_mask_replacement_without_section_returns_guidance(self):
        result = pipeline.answer("面罩什么时候更换", self.night, [], use_llm=True)
        self.assertEqual(result.mode, "产品文档（未覆盖）")
        self.assertIn("没有给出面罩的固定更换周期", result.text)
        self.assertNotIn("更换空气滤芯", result.text)
        self.assertNotIn("单晚治疗报告（", result.text)

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

    def test_doc_fallback_is_excerpted(self):
        text = pipeline._doc_fallback([{
            "doc": "说明.docx",
            "section": "系统设置",
            "visibility": "user",
            "text": "设置项。" * 300,
        }])
        self.assertLess(len(text), 900)
        self.assertIn("后续内容已省略", text)


if __name__ == "__main__":
    unittest.main()
