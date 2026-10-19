"""多用户最小埋点测试。

当前只有一份 DEMO-USER 数据，但所有用户数据入口都必须显式接收 user_id，
并拒绝请求用户与治疗夜数据用户不一致的调用。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline


class TestUserScope(unittest.TestCase):
    def test_default_user_is_demo_user(self):
        night = nightdata.load_night()
        self.assertEqual(night.user_id, nightdata.DEFAULT_USER_ID)

    def test_load_dataset_carries_user_id(self):
        nights = nightdata.load_dataset("USER-A")
        self.assertTrue(nights)
        self.assertTrue(all(night.user_id == "USER-A" for night in nights))

    def test_select_night_carries_user_id(self):
        nights = nightdata.load_dataset("USER-A")
        night, _ = nightdata.select_night("USER-A", nights, "昨晚")
        self.assertEqual(night.user_id, "USER-A")

    def test_data_helpers_accept_user_id(self):
        night = nightdata.load_night("USER-A")
        facts = nightdata.facts_by_topic("USER-A", night, "usage")
        coverage = nightdata.coverage_fact("USER-A", night)
        trend = nightdata.trend_summary("USER-A", night)
        self.assertTrue(facts)
        self.assertEqual(coverage.fid, "data-coverage")
        self.assertEqual(trend["count"], 7)

    def test_pipeline_rejects_user_mismatch(self):
        night = nightdata.load_night("USER-A")
        with self.assertRaisesRegex(ValueError, "用户上下文不一致"):
            pipeline.answer(
                pipeline.UserQuery("今天几号", user_id="USER-B"),
                night,
            )

    def test_pipeline_accepts_matching_user(self):
        night = nightdata.load_night("USER-A")
        result = pipeline.answer(
            pipeline.UserQuery("今天几号", user_id="USER-A"),
            night,
        )
        self.assertEqual(result.mode, "系统信息")


if __name__ == "__main__":
    unittest.main()
