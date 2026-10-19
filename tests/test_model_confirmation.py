"""产品型号近似输入确认测试。

型号拼写错误不能被检索成别的主题；应先确认是否指已知型号。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline


class TestModelConfirmation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_y70_asks_for_ya70_confirmation(self):
        result = pipeline.answer("Y70是什么", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "型号确认")
        self.assertIn("YA70", result.text)
        self.assertIn("笔误", result.text)
        self.assertNotIn("P95", result.text)
        self.assertIn("model_typo", result.risk_flags)

    def test_close_variant_asks_for_specific_model(self):
        result = pipeline.answer("Y70A 有哪些功能", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "型号确认")
        self.assertIn("YA70A", result.text)

    def test_exact_model_is_not_intercepted(self):
        result = pipeline.answer("YA70是什么", self.night, [], use_llm=False)
        self.assertNotEqual(result.mode, "型号确认")

    def test_unrelated_code_is_not_intercepted(self):
        result = pipeline.answer("P95是什么", self.night, [], use_llm=False)
        self.assertNotEqual(result.mode, "型号确认")

    def test_safety_still_has_priority(self):
        result = pipeline.answer("Y70 帮我把压力调高一点", self.night, [], use_llm=True)
        self.assertEqual(result.mode, "安全边界")


if __name__ == "__main__":
    unittest.main()
