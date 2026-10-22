"""客服服务意图与子库检索。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline, service_intent


class ServiceIntentTest(unittest.TestCase):
    def test_intents_are_detected(self):
        cases = {
            "E3 报错怎么处理": "troubleshooting",
            "面罩怎么清洁": "consumables",
            "AHI 是什么意思": "metric",
            "保修多久": "after_sales",
            "主机也能生成报告吗": "device_operation",
            "YA70A 是什么产品": "product_overview",
            "我昨晚用了多久": "data",
        }
        for question, expected in cases.items():
            self.assertEqual(service_intent.detect(question)["intent"], expected, question)

    def test_unknown_does_not_narrow_scope(self):
        result = service_intent.detect("这个东西怎么弄")
        self.assertIsNone(result["scope"])


class SubLibraryRetrievalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_no_official_material_for_e3(self):
        result = pipeline.answer("E3 报错怎么处理", self.night, [], use_llm=False)
        self.assertEqual(result.mode, "资料未覆盖")
        self.assertIn("正式产品资料里没有", result.text)
        self.assertNotIn("屏幕显示 E3", result.text)

    def test_consumable_question_stays_in_consumable_docs(self):
        result = pipeline.answer("面罩怎么清洁", self.night, [], use_llm=False)
        self.assertIn("产品文档", result.mode)
        self.assertTrue(all(
            source.startswith(("03_清洁与维护.md", "10_YA70A常见问题.md",
                               "11_常见操作补充.md"))
            for source in result.sources
        ), result.sources)


if __name__ == "__main__":
    unittest.main()
