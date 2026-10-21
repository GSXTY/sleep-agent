"""产品有哪些模式 → 产品文档；本晚用的什么模式 → 设备数据。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import nightdata, pipeline, product_kb, validation


class ModeChunkTest(unittest.TestCase):
    def test_work_mode_section_includes_all_modes(self):
        """1.3 工作模式的正文原本只有一句引导语，子章节要合并进来。"""
        chunk = next(c for c in product_kb.load_chunks()
                     if c.section == "1.3工作模式")
        for mode in ("CPAP", "AutoCPAP", "S", "AutoS"):
            self.assertIn(mode, chunk.text)
        self.assertIn("双水平", chunk.text)


class ModeRoutingTest(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_dataset()[-1]

    def ask(self, question):
        return pipeline.answer(
            pipeline.UserQuery(question=question, use_llm=False), self.night)

    def test_product_modes_go_to_docs(self):
        for question in ("产品模式有哪些", "有哪些工作模式"):
            result = self.ask(question)
            self.assertIn("产品文档", result.mode, question)
            self.assertIn("AutoCPAP", result.text, question)

    def test_model_name_is_ignored_for_retrieval(self):
        result = self.ask("YA70有哪些模式")
        self.assertIn("产品文档", result.mode)
        self.assertIn("AutoS", result.text)

    def test_inverted_word_order_is_understood(self):
        # "模式有哪些" 是倒装，之前的规则只认"哪些…模式"
        for question in ("YA70模式有哪些", "模式有哪些"):
            result = self.ask(question)
            self.assertIn("产品文档", result.mode, question)
            self.assertIn("AutoCPAP", result.text, question)

    def test_personal_mode_still_goes_to_device_data(self):
        result = self.ask("2026-10-15 用的是什么模式")
        self.assertIn("设备数据", result.mode)
        self.assertIn("没有提供治疗模式", result.text)


class ModelCodeNumberTest(unittest.TestCase):
    MATERIAL = "本系列设备具备以下几种工作模式：CPAP、AutoCPAP、S、AutoS。"

    def test_model_name_digits_are_not_measurements(self):
        result = validation.check_output(
            "YA70 支持 CPAP、AutoCPAP、S、AutoS 四种模式。",
            self.MATERIAL, "strong", question="YA70有哪些模式")
        self.assertTrue(result["ok"], result["problems"])

    def test_error_code_digits_are_not_measurements(self):
        result = validation.check_output(
            "E3 报错时请联系售后。", self.MATERIAL, "strong",
            question="E3 报错怎么处理")
        self.assertTrue(result["ok"], result["problems"])

    def test_standalone_count_still_blocked(self):
        result = validation.check_output(
            "建议每天 2 次。", self.MATERIAL, "strong", question="怎么清洁")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
