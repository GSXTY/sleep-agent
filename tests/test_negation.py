"""否定式安全提醒不算违规；肯定式调压建议必须拦住。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline, validation

MATERIAL = "双水平模式通过吸气压 IPAP 和呼气压 EPAP 两个压力工作。"


class NegationTest(unittest.TestCase):
    def check(self, text):
        return validation.check_output(text, MATERIAL, "strong",
                                       question="双水平是什么意思")

    def test_negative_advice_is_allowed(self):
        for text in ("不建议自行调整治疗压力。",
                     "不建议自行调压。",
                     "请勿自行调整设备参数。"):
            result = self.check(text)
            self.assertTrue(result["ok"], (text, result["problems"]))

    def test_positive_advice_is_blocked(self):
        for text in ("建议自行调高压力。", "可以把压力调到 8。"):
            result = self.check(text)
            self.assertFalse(result["ok"], text)

    def test_negation_must_be_adjacent(self):
        # 否定词在别的分句里，不构成"不要自行调压"的意思
        result = self.check("不用担心，你可以自行调高压力。")
        self.assertFalse(result["ok"])

    def test_same_phrase_reported_once(self):
        result = self.check("建议自行调高压力。")
        self.assertEqual(len(result["problems"]), 1, result["problems"])

    def test_pipeline_accepts_negative_reminder(self):
        night = nightdata.load_dataset()[-1]
        answer = ("双水平模式通过吸气压 IPAP 和呼气压 EPAP 两个压力工作。"
                  "不建议自行调整治疗压力，参数请遵医嘱。")
        with mock.patch.object(pipeline.llm, "chat", return_value={
                "status": "success", "content": answer,
                "provider": "fake", "model": "fake"}):
            result = pipeline.answer("双水平是什么意思", night, [], use_llm=True)
        self.assertIn("已校验", result.mode)
        self.assertIn("不建议自行调整", result.text)


if __name__ == "__main__":
    unittest.main()
