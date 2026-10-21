"""对话历史卫生：报告正文不进历史，别的轮次数字不得复用到本轮。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import shutil
import unittest
import uuid
from unittest import mock

import chat
from sleepagent import nightdata, pipeline, report, runtime_info
from sleepagent.pipeline import AgentResponse


class TestHistoryCompaction(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_dataset()[-1]
        self.report = report.render(self.night)
        self.structured = report.render_structured(self.night)
        self.result = AgentResponse(
            text=self.report["text"], mode="报告生成",
            structured=self.structured, detail="验收通过")

    def test_report_history_is_compact(self):
        messages = chat.history_messages("帮我生成报告", self.result, self.night)
        self.assertEqual(len(messages), 2)
        note = messages[1]["content"]
        self.assertIn("2026-10-15", note)
        self.assertLess(len(note), 120)
        for number in ("402", "5.7", "38", "372", "84"):
            self.assertNotIn(number, note)

    def test_plain_answer_history_keeps_text(self):
        result = AgentResponse(text="本晚治疗时长 402 分钟。", mode="设备数据（规则版）")
        messages = chat.history_messages("昨晚用多久", result, self.night)
        self.assertEqual(messages[1]["content"], "本晚治疗时长 402 分钟。")

    def test_report_mode_without_structured_is_also_compacted(self):
        result = AgentResponse(text=self.report["text"], mode="报告生成（规则版）")
        messages = chat.history_messages("出个报告", result, self.night)
        self.assertNotIn("AHI", messages[1]["content"])
        self.assertIn("2026-10-15", messages[1]["content"])

    def test_device_prompt_forbids_reusing_history_numbers(self):
        self.assertIn("历史轮次", pipeline.DEVICE_PROMPT)
        self.assertIn("不得复用", pipeline.DEVICE_PROMPT)


class TestCrossTurnNumbers(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_dataset()[-1]
        self.stale_history = [
            {"role": "user", "content": "提供报告"},
            {"role": "assistant",
             "content": "AHI 5.7 次/小时，共 38 次事件，累计 372 秒，中枢性 5 次。"},
        ]

    def _ask(self, answer_text, history):
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return {"status": "success", "content": answer_text,
                    "provider": "fake", "model": "fake"}

        with mock.patch.object(pipeline.llm, "chat", side_effect=fake_chat):
            result = pipeline.answer("昨晚使用情况", self.night, history, use_llm=True)
        return result, captured

    def test_reused_history_numbers_fail_validation(self):
        result, _ = self._ask(
            "本晚治疗时长 402 分钟，设备总评分 84 分，AHI 5.7 次/小时。",
            self.stale_history,
        )
        self.assertIn("规则版", result.mode)
        self.assertIn("未通过校验", result.detail)
        self.assertNotIn("5.7", result.text)

    def test_clean_answer_passes_validation(self):
        result, _ = self._ask(
            "本晚治疗时长 402 分钟（约 6.7 小时），设备总评分 84 分。",
            self.stale_history,
        )
        self.assertIn("已校验", result.mode)
        self.assertIn("402", result.text)

    def test_compact_history_keeps_old_numbers_out_of_prompt(self):
        compact = chat.history_messages(
            "提供报告",
            AgentResponse(text="x", mode="报告生成",
                          structured={"record_date": "2026-10-15"}),
            self.night,
        )
        result, captured = self._ask("本晚治疗时长 402 分钟。", compact)
        joined = " ".join(str(item["content"]) for item in captured["messages"])
        self.assertNotIn("5.7", joined)
        self.assertNotIn("372", joined)
        self.assertIn("已校验", result.mode)


class TestDeviceDateIsolation(unittest.TestCase):
    """设备模式不注入系统日期：否则模型会把「昨晚」推成别的日期再被拦下。"""

    def setUp(self):
        self.night = nightdata.load_dataset()[-1]
        # 放在项目 .local 下：已 gitignore，且不受系统临时目录权限限制
        self.tmp = pipeline.DEBUG_DIR.parent / ("test-debug-" + uuid.uuid4().hex[:8])
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, answer_text):
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return {"status": "success", "content": answer_text,
                    "provider": "fake", "model": "fake"}

        with mock.patch.object(pipeline.llm, "chat", side_effect=fake_chat):
            result = pipeline.answer("昨晚使用情况", self.night, [], use_llm=True)
        return result, captured["messages"]

    def test_no_system_date_in_device_prompt(self):
        _, messages = self._run("本晚治疗时长 402 分钟。")
        joined = "\n".join(str(item["content"]) for item in messages)
        self.assertNotIn(runtime_info.facts_line(), joined)
        self.assertIn("不要引用系统日期", joined)

    def test_answer_that_dates_the_night_is_rejected(self):
        result, _ = self._run("记录归属日期为 2026-10-19，治疗时长 402 分钟。")
        self.assertIn("规则版", result.mode)
        self.assertIn("19", result.detail)
        self.assertNotIn("2026-10-19", result.text)

    def test_debug_dump_is_opt_in(self):
        with mock.patch.dict(os.environ, {"SLEEPAGENT_DEBUG_LLM": "0"}):
            self.assertIsNone(
                pipeline.dump_rejected("x", ["p"], directory=self.tmp))

    def test_debug_dump_writes_rejected_output(self):
        with mock.patch.dict(os.environ, {"SLEEPAGENT_DEBUG_LLM": "1"}):
            path = pipeline.dump_rejected(
                "AHI 5.7 次/小时", ["出现材料中没有的数字：5.7"],
                directory=self.tmp)
        self.assertIsNotNone(path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("5.7", text)
        self.assertIn("problems", text)


if __name__ == "__main__":
    unittest.main()
