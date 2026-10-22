"""成本优化：公共问答缓存、上下文预算和输出 token 预算。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline


class CostOptimizationTest(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_night()
        pipeline.clear_public_cache()

    def tearDown(self):
        pipeline.clear_public_cache()

    @staticmethod
    def _fake_product_answer(messages, **kwargs):
        return {
            "status": "success",
            "content": "空气滤芯建议在 30 天内至少更换一次，滤芯不可清洗。",
            "provider": "fake",
            "model": "fake",
            "detail": "",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 24,
                "total_tokens": 144,
            },
        }

    def test_repeated_public_question_uses_cache(self):
        with mock.patch.object(
            pipeline.llm, "chat", side_effect=self._fake_product_answer
        ) as chat:
            first = pipeline.answer(
                pipeline.UserQuery("滤芯多久换一次", use_llm=True),
                self.night,
            )
            second = pipeline.answer(
                pipeline.UserQuery("滤芯多久换一次", use_llm=True),
                self.night,
            )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(first.text, second.text)
        self.assertNotEqual(first.request_id, second.request_id)

    def test_standalone_product_question_skips_unrelated_history(self):
        history = [
            {"role": "user", "content": "OLD-HISTORY-USER"},
            {"role": "assistant", "content": "OLD-HISTORY-ASSISTANT"},
        ]
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return self._fake_product_answer(messages, **kwargs)

        with mock.patch.object(pipeline.llm, "chat", side_effect=fake_chat):
            pipeline.answer(
                pipeline.UserQuery("滤芯多久换一次", history=history, use_llm=True),
                self.night,
            )
        joined = "\n".join(item["content"] for item in captured["messages"])
        self.assertNotIn("OLD-HISTORY", joined)

    def test_product_mode_uses_smaller_context_and_token_budget(self):
        history, expected = [], []
        for index in range(8):
            history.extend([
                {"role": "user", "content": "历史问题 " + str(index)},
                {"role": "assistant", "content": "历史回答 " + str(index)},
            ])
            expected.extend(["历史问题 " + str(index), "历史回答 " + str(index)])

        limited = pipeline._context_messages(history, max_turns=2)
        self.assertEqual(
            [item["content"] for item in limited],
            expected[-4:],
        )

        captured = {}

        def fake_chat(messages, **kwargs):
            captured["max_tokens"] = kwargs.get("max_tokens")
            return self._fake_product_answer(messages, **kwargs)

        with mock.patch.object(pipeline.llm, "chat", side_effect=fake_chat):
            pipeline.answer(
                pipeline.UserQuery("这个滤芯多久换一次", history=history, use_llm=True),
                self.night,
            )
        self.assertEqual(captured["max_tokens"], 240)

    def test_model_usage_is_written_to_log(self):
        with mock.patch.object(
            pipeline.llm, "chat", side_effect=self._fake_product_answer
        ):
            with self.assertLogs("sleepagent.pipeline", level="INFO") as captured:
                pipeline.answer(
                    pipeline.UserQuery("滤芯多久换一次", use_llm=True),
                    self.night,
                )
        self.assertTrue(any("Tokens:120/24" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
