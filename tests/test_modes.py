"""产品有哪些模式 → 产品文档；本晚用的什么模式 → 设备数据。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline, product_kb, product_models, validation


class ModeChunkTest(unittest.TestCase):
    def test_work_mode_section_includes_all_modes(self):
        """1.3 工作模式的正文原本只有一句引导语，子章节要合并进来。"""
        chunk = next(c for c in product_kb.load_chunks()
                     if c.section == "工作模式")
        for mode in ("CPAP", "AutoCPAP", "S", "AutoS"):
            self.assertIn(mode, chunk.text)
        self.assertIn("双水平", chunk.text)
        self.assertNotIn("1.3.2", chunk.text)

    def test_child_section_is_kept_for_retrieval(self):
        self.assertTrue(any(c.section == "双水平模式"
                            for c in product_kb.load_chunks()))


class ModeRoutingTest(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_dataset()[-1]
        pipeline.clear_public_cache()

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
        self.assertIn("双水平模式", result.text)
        self.assertNotIn("1.3.2", result.text)

    def test_known_model_is_detected_with_series_scope(self):
        self.assertEqual(product_models.find_known_model("YA70有什么工作模式"), "YA70")
        self.assertEqual(product_models.find_known_model("YA70A有什么模式"), "YA70A")

    def test_product_mode_answer_is_deterministic_without_llm(self):
        with mock.patch.object(pipeline.llm, "chat") as chat:
            first = pipeline.answer(
                pipeline.UserQuery("YA70有什么工作模式？", use_llm=True),
                self.night,
            )
            second = pipeline.answer(
                pipeline.UserQuery("有什么工作模式？", use_llm=True),
                self.night,
            )
        chat.assert_not_called()
        self.assertEqual(first.mode, "产品文档（规则版）")
        self.assertEqual(first.text, second.text)
        self.assertIn("CPAP", first.text)
        self.assertIn("AutoS", first.text)

    def test_product_principle_answer_is_deterministic(self):
        with mock.patch.object(pipeline.llm, "chat") as chat:
            first = pipeline.answer(
                pipeline.UserQuery("YA70工作原理是什么？", use_llm=True),
                self.night,
            )
            second = pipeline.answer(
                pipeline.UserQuery("设备工作原理是什么？", use_llm=True),
                self.night,
            )
        chat.assert_not_called()
        self.assertEqual(first.mode, "产品文档（规则版）")
        self.assertEqual(first.text, second.text)
        self.assertIn("正压", first.text)
        self.assertIn("01_产品说明.md · 工作原理", first.sources)

    def test_first_section_question_needs_no_network_or_query_planner(self):
        for question in ("YA70工作模式有哪些", "YA70有哪些模式", "YA70工作原理是什么"):
            with self.subTest(question=question):
                pipeline.clear_public_cache()
                with mock.patch.object(pipeline.llm, "chat", side_effect=AssertionError("LLM called")), \
                        mock.patch.object(pipeline.embeddings, "available", side_effect=AssertionError("embedding probe")), \
                        mock.patch.object(product_kb, "retrieve", side_effect=AssertionError("hybrid retrieval")), \
                        mock.patch.object(pipeline.query_planner, "plan", side_effect=AssertionError("query planning")):
                    result = pipeline.answer(question, self.night, [], use_llm=True)
                self.assertEqual(result.mode, "产品文档（规则版）")
                self.assertIn("说明书章节直读", result.detail)
                self.assertTrue(result.sources)

    def test_direct_section_does_not_swallow_other_tasks(self):
        for question in ("昨晚用的是什么模式", "我的工作模式是什么",
                         "YA70工作模式有哪些，滤芯多久换一次",
                         "YA70工作模式有哪些以及怎么清洗面罩",
                         "YA70A和YA70B20工作模式有哪些区别"):
            self.assertIsNone(pipeline._direct_product_section(question), question)

    def test_missing_direct_section_uses_existing_retrieval(self):
        with mock.patch.object(pipeline, "_direct_product_hits", return_value=[]), \
                mock.patch.object(product_kb, "retrieve", wraps=product_kb.retrieve) as retrieve:
            result = self.ask("YA70工作模式有哪些")
        retrieve.assert_called()
        self.assertIn("AutoCPAP", result.text)
        self.assertNotIn("说明书章节直读", result.detail)

    def test_mode_comparison_needs_no_model_or_vectors(self):
        for question in ("单水平和双水平有什么区别", "单水平模式和双水平模式有什么区别？",
                         "双水平与单水平有什么不同", "CPAP 和 AutoCPAP 有什么区别"):
            with self.subTest(question=question):
                pipeline.clear_public_cache()
                with mock.patch.object(pipeline.llm, "chat", side_effect=AssertionError("LLM called")), \
                        mock.patch.object(pipeline.embeddings, "available", side_effect=AssertionError("embedding probe")), \
                        mock.patch.object(product_kb, "retrieve", side_effect=AssertionError("hybrid retrieval")), \
                        mock.patch.object(pipeline.query_planner, "plan", side_effect=AssertionError("query planning")):
                    result = pipeline.answer(question, self.night, [], use_llm=True)
                self.assertIn("标准答案直读", result.detail)
                self.assertIn("CPAP", result.text)
                self.assertNotRegex(result.text, r"Q\d{3}")
                self.assertNotIn("标准答案", result.text)

    def test_comparison_fast_path_does_not_swallow_other_tasks(self):
        for question in ("我适合单水平还是双水平", "单水平和双水平有什么区别，怎么清洁",
                         "昨晚单水平和双水平有什么区别", "单水平和双水平有什么区别，帮我调一下压力"):
            self.assertIsNone(pipeline._direct_mode_comparison(question), question)

    def test_legacy_faq_id_is_removed_from_fallback_and_option_title(self):
        hit = {"section": "Q026 单水平模式和双水平模式有什么区别？", "text": "标准答案正文。"}
        self.assertNotIn("Q026", pipeline._doc_fallback([hit]))
        option = pipeline._option("topic", hit["section"], hit["section"])
        self.assertNotIn("Q026", option["title"])
        self.assertIn("Q026", option["value"])

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
