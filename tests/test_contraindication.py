"""禁忌症属于安全边界：命中即固定话术，不依赖检索、不调用模型。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline, safety


class ContraindicationRuleTest(unittest.TestCase):
    def test_pneumothorax_is_absolute(self):
        verdict = safety.check("我有气胸可以使用吗")
        self.assertEqual(verdict.kind, "contraindication_absolute")
        self.assertIn("绝对禁忌症", verdict.reply)
        self.assertIn("气胸", verdict.reply)
        self.assertIn("医生", verdict.reply)

    def test_other_absolute_terms(self):
        for question in ("我得了纵隔气肿还能用吗",
                         "昏迷病人能用呼吸机吗",
                         "颅内积气可以使用吗"):
            self.assertEqual(safety.check(question).kind,
                             "contraindication_absolute", question)

    def test_relative_contraindication(self):
        verdict = safety.check("我有肺大泡可以使用吗")
        self.assertEqual(verdict.kind, "contraindication_relative")
        self.assertIn("相对禁忌症", verdict.reply)

    def test_normal_question_is_not_blocked(self):
        for question in ("2026-10-15 用的是什么模式", "滤芯多久换一次", "关机怎么做"):
            self.assertEqual(safety.check(question).kind, "in_scope", question)


class ContraindicationPipelineTest(unittest.TestCase):
    def test_pneumothorax_never_reaches_model_or_retrieval(self):
        night = nightdata.load_dataset()[-1]
        with mock.patch.object(pipeline.llm, "chat",
                               side_effect=AssertionError("不应调用模型")), \
                mock.patch.object(pipeline.product_kb, "retrieve",
                                  side_effect=AssertionError("不应触发检索")):
            result = pipeline.answer("我有气胸可以使用吗", night, [], use_llm=True)
        self.assertEqual(result.mode, "安全边界")
        self.assertIn("绝对禁忌症", result.text)
        self.assertIn("气胸", result.text)
        self.assertEqual(result.risk_flags, ["contraindication_absolute"])
        self.assertTrue(result.validation["ok"])


if __name__ == "__main__":
    unittest.main()
