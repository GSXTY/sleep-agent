"""报告范围测试：单晚和周报必须区分。"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline


class TestReportScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_weekly_report_is_explicitly_unsupported(self):
        result = pipeline.answer("这一周的报告", self.night, [], use_llm=True)
        self.assertTrue(result.mode.startswith("周期报告"), result.mode)
        self.assertIn("暂未支持", result.text)
        self.assertIn("单晚治疗报告", result.text)
        self.assertNotIn("单晚治疗报告（", result.text)
        self.assertEqual(result.risk_flags, ["period_report_unsupported"])

    def test_single_night_report_still_works(self):
        result = pipeline.answer("生成昨晚报告", self.night, [], use_llm=False)
        self.assertIn("单晚治疗报告（", result.text)
        self.assertIn("截至本晚共", result.text)


if __name__ == "__main__":
    unittest.main()
