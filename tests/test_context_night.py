"""没有新时间词时，沿用上一轮选定的夜晚。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import builtins
import contextlib
import io
import unittest

import chat
from sleepagent import nightdata, pipeline


class ContextNightTest(unittest.TestCase):
    def test_follow_up_keeps_previous_night(self):
        seq = iter(["2026-10-14 的使用情况", "我的数据正常吗", "/quit"])
        original = builtins.input
        builtins.input = lambda prompt="": next(seq)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                chat.main(["chat.py", "--no-llm"])
        finally:
            builtins.input = original
        output = buffer.getvalue()
        self.assertIn("按当前夜晚评估：2026-10-14", output)
        self.assertNotIn("单晚数据分析（2026-10-15）", output)

    def test_assessment_uses_passed_night(self):
        night = next(n for n in nightdata.load_dataset()
                     if str(n.raw.get("record_date")) == "2026-10-14")
        result = pipeline.answer("我的数据正常吗", night, [], use_llm=False)
        self.assertEqual(result.mode, "范围确认")

    def test_follow_up_inherits_previous_scope(self):
        night = next(n for n in nightdata.load_dataset()
                     if str(n.raw.get("record_date")) == "2026-10-14")
        history = [
            {"role": "user", "content": "2026-10-14 的使用情况"},
            {"role": "assistant", "content": "已读取该晚数据。"},
        ]
        result = pipeline.answer(
            "我的数据正常吗", night, history, use_llm=False
        )
        self.assertEqual(result.mode, "数据分析")
        self.assertIn("2026-10-14", result.text)


if __name__ == "__main__":
    unittest.main()
