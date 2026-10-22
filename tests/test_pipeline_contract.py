"""Pipeline 输入输出契约测试。

入口层可以变化，但 UserQuery / AgentResponse 的字段语义必须稳定：
后续接真实后台时，只做字段映射，不把业务逻辑散回 CLI 或 HTTP 层。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline


class TestPipelineContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_user_query_defaults_and_trim(self):
        query = pipeline.UserQuery("  我昨晚用了多久  ")
        self.assertEqual(query.question, "我昨晚用了多久")
        self.assertEqual(query.history, [])
        self.assertTrue(query.use_llm)
        self.assertEqual(query.user_id, "DEMO-USER")

    def test_legacy_string_call_still_works(self):
        result = pipeline.answer("今天几号", self.night, [], use_llm=False)
        self.assertIsInstance(result, pipeline.AgentResponse)
        self.assertEqual(result.mode, "系统信息")
        self.assertEqual(result["mode"], "系统信息")

    def test_new_query_returns_standard_response(self):
        result = pipeline.answer(
            pipeline.UserQuery("我昨晚 AHI 多少", use_llm=False),
            self.night,
        )
        self.assertIsInstance(result, pipeline.AgentResponse)
        self.assertEqual(result.tier, "strong")
        self.assertIn("设备数据", result.mode)
        self.assertIn("text", result.to_dict())
        self.assertIn("validation", result.to_dict())

    def test_safety_response_carries_risk_flag(self):
        result = pipeline.answer(
            pipeline.UserQuery("帮我把压力调高一点", use_llm=True),
            self.night,
        )
        self.assertEqual(result.mode, "安全边界")
        self.assertEqual(result.tier, "blocked")
        self.assertIn("pressure", result.risk_flags)
        self.assertTrue(result.validation["ok"])

    def test_report_response_carries_structured_data(self):
        result = pipeline.answer(
            pipeline.UserQuery("生成 2026-10-15 的报告", use_llm=False),
            self.night,
        )
        self.assertEqual(result.mode, "报告生成")
        self.assertEqual(result.tier, "strong")
        self.assertEqual(result.detail, "单晚报告")
        self.assertNotIn("验收", result.detail)
        self.assertIsNotNone(result.structured)
        self.assertIn("record_date", result.structured)
        self.assertIn("acceptance", result.structured)


if __name__ == "__main__":
    unittest.main()
