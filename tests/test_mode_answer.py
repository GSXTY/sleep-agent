"""治疗模式缺失时的回答测试。

没有 therapy_mode 字段时，只能明确说无法确定，不能回退成整套设备指标。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline


class TestModeAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_missing_mode_returns_only_unavailable(self):
        result = pipeline.answer(
            "昨晚用的是什么模式", self.night, [], use_llm=True
        )
        self.assertTrue(result.mode.startswith("设备数据"), result.mode)
        self.assertIn("没有提供治疗模式", result.text)
        self.assertIn("无法确定", result.text)
        self.assertNotIn("平均治疗压力", result.text)
        self.assertEqual(result.risk_flags, ["missing_field"])

    def test_mode_field_is_used_when_available(self):
        raw = dict(self.night.raw)
        raw["therapy_mode"] = "AutoCPAP"
        night = nightdata.Night(
            raw=raw, user_id=self.night.user_id, history=self.night.history
        )
        nightdata.build_facts(self.night.user_id, night)
        result = pipeline.answer(
            "昨晚用的是什么模式", night, [], use_llm=False
        )
        self.assertIn("AutoCPAP", result.text)
        self.assertNotEqual(result.risk_flags, ["missing_field"])


if __name__ == "__main__":
    unittest.main()
