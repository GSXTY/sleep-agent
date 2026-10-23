"""一个输入包含多个产品问题时，必须逐项回答。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline


class CompoundProductQuestionTest(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_night()
        pipeline.clear_public_cache()

    def tearDown(self):
        pipeline.clear_public_cache()

    def test_rule_answer_contains_leak_and_warranty(self):
        result = pipeline.answer(
            pipeline.UserQuery(
                "漏气了怎么办，可以保修吗",
                use_llm=False,
            ),
            self.night,
        )
        self.assertEqual(result.mode, "产品文档（规则版）")
        self.assertIn("【漏气了怎么办】", result.text)
        self.assertIn("【可以保修吗】", result.text)
        self.assertIn("鼻罩", result.text)
        self.assertIn("保修", result.text)
        self.assertTrue(any("07_售后服务" in item for item in result.sources))
        self.assertNotIn("07_售后服务.md", result.text)
        self.assertNotIn("08_常见问题.md", result.text)

    def test_model_gets_both_question_materials_in_one_call(self):
        captured = []

        def fake_chat(messages, **kwargs):
            captured.append(messages)
            return {
                "status": "success",
                "provider": "fake",
                "model": "fake",
                "detail": "",
                "content": (
                    "漏气时先检查面罩佩戴、头带和管路连接。"
                    "保修方面，非人为的材料或制造故障可按保修条款维修。"
                ),
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 40,
                    "total_tokens": 240,
                },
            }

        with mock.patch.object(pipeline.llm, "chat", side_effect=fake_chat):
            result = pipeline.answer(
                pipeline.UserQuery(
                    "漏气了怎么办，可以保修吗",
                    use_llm=True,
                ),
                self.night,
            )

        self.assertEqual(len(captured), 1)
        prompt = "\n".join(item["content"] for item in captured[0])
        self.assertIn("【子问题1】漏气了怎么办", prompt)
        self.assertIn("【子问题2】可以保修吗", prompt)
        self.assertIn("12.1保修时间与条件", prompt)
        self.assertIn("逐项回答", prompt)
        self.assertIn("漏气", result.text)
        self.assertIn("保修", result.text)


if __name__ == "__main__":
    unittest.main()
