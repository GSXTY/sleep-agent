"""日期回显不算编数字，但日期本身必须和提问或材料对得上。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline, validation

MATERIAL = "AHI 为 5.7 次/小时。记录归属日期为 2026-10-15。"
QUESTION = "2026-10-15 的 AHI 是多少"


class DateAwareValidationTest(unittest.TestCase):
    def check(self, text, question=QUESTION, material=MATERIAL):
        return validation.check_output(text, material, "strong", question=question)

    def test_echoing_question_date_passes(self):
        result = self.check("2026-10-15 的 AHI 为 5.7 次/小时。")
        self.assertTrue(result["ok"], result["problems"])

    def test_chinese_date_equivalent_passes(self):
        result = self.check("10 月 15 日的 AHI 为 5.7 次/小时。")
        self.assertTrue(result["ok"], result["problems"])

    def test_material_date_can_be_restated(self):
        result = self.check("本晚记录归属日期为 2026-10-15，AHI 为 5.7。",
                            question="")
        self.assertTrue(result["ok"], result["problems"])

    def test_wrong_date_is_rejected(self):
        result = self.check("2026-10-19 的 AHI 为 5.7 次/小时。")
        self.assertFalse(result["ok"])
        self.assertTrue(any("日期" in item for item in result["problems"]),
                        result["problems"])

    def test_other_numbers_still_blocked(self):
        result = self.check("2026-10-15 的 AHI 为 7.2 次/小时。")
        self.assertFalse(result["ok"])
        self.assertTrue(any("7.2" in item for item in result["problems"]),
                        result["problems"])

    def test_small_counts_still_blocked(self):
        result = self.check("建议每天 2 次。", question="眼睛刺痛怎么办")
        self.assertFalse(result["ok"])
        self.assertTrue(any("2" in item for item in result["problems"]),
                        result["problems"])


class MarkerTest(unittest.TestCase):
    """材料编号和模型分条编号都不能污染数字校验。"""

    def test_material_marker_is_not_a_number(self):
        material = "[资料1] 眼睛刺痛 | 鼻罩尺寸不合适"
        result = validation.check_output("建议每天 1 次。", material, "strong",
                                         question="眼睛刺痛怎么办")
        self.assertFalse(result["ok"])
        self.assertTrue(any("1" in item for item in result["problems"]),
                        result["problems"])

    def test_model_numbered_list_is_layout_not_data(self):
        material = "[资料1] 眼睛刺痛 | 鼻罩尺寸不合适 | 缩小额头距离、更换鼻罩"
        text = ("可能的原因和处理：\n"
                "1. 缩小鼻罩与额头之间的距离\n"
                "2. 检查头带是否压得太紧\n"
                "3. 必要时更换鼻罩")
        result = validation.check_output(text, material, "strong",
                                         question="为什么眼睛有点痛")
        self.assertTrue(result["ok"], result["problems"])

    def test_inline_count_is_still_blocked(self):
        material = "[资料1] 眼睛刺痛 | 鼻罩尺寸不合适"
        result = validation.check_output("建议每天 2 次清洗。", material, "strong",
                                         question="为什么眼睛有点痛")
        self.assertFalse(result["ok"])
        self.assertTrue(any("2" in item for item in result["problems"]),
                        result["problems"])


class PipelineEyeCaseTest(unittest.TestCase):
    """复现"为什么眼睛有点痛"：模型分条回答不应被回退。"""

    def test_numbered_product_answer_passes_end_to_end(self):
        night = nightdata.load_dataset()[-1]
        answer = ("眼睛刺痛通常和鼻罩漏气或压迫有关。\n"
                  "1. 缩小鼻罩与额头之间的距离\n"
                  "2. 检查头带是否压得太紧\n"
                  "3. 必要时更换鼻罩")
        with mock.patch.object(pipeline.llm, "chat", return_value={
                "status": "success", "content": answer,
                "provider": "fake", "model": "fake"}):
            result = pipeline.answer("为什么眼睛有点痛", night, [], use_llm=True)
        self.assertIn("已校验", result.mode)
        self.assertIn("缩小鼻罩", result.text)


class ReferenceNumberTest(unittest.TestCase):
    """章节号与引用格式是排版，不是测量值。"""

    MATERIAL = "设备通过正压气流保持上气道开放。"

    def test_section_number_allowed_when_declared(self):
        result = validation.check_output(
            "根据 1.1 工作原理，设备通过正压气流保持上气道开放。",
            self.MATERIAL, "strong", question="产品工作原理",
            extra_numbers={"1.1"})
        self.assertTrue(result["ok"], result["problems"])

    def test_section_number_blocked_without_declaration(self):
        result = validation.check_output(
            "根据 1.1 工作原理，设备通过正压气流保持上气道开放。",
            self.MATERIAL, "strong", question="产品工作原理")
        self.assertFalse(result["ok"])

    def test_reference_markers_are_layout(self):
        text = "根据【1】的资料，第 2 条写明：设备通过正压气流保持上气道开放。"
        result = validation.check_output(text, self.MATERIAL, "strong",
                                         question="产品工作原理")
        self.assertTrue(result["ok"], result["problems"])

    def test_unknown_measurement_still_blocked(self):
        result = validation.check_output(
            "AHI 为 5.7，压力 6.5 hPa。", "AHI 为 5.7 次/小时。",
            "strong", question="AHI 多少", extra_numbers={"1.1"})
        self.assertFalse(result["ok"])
        self.assertTrue(any("6.5" in item for item in result["problems"]),
                        result["problems"])

    def test_single_level_numbers_are_not_whitelisted(self):
        # 语料里有"1 概述""2 安全使用原则"，但单层数字不能当章节号放行
        from sleepagent import product_kb
        with unittest.mock.patch.object(product_kb, "load_chunks", return_value=(
            product_kb.Chunk("test.md", "1.1工作原理", "正文", 0),
            product_kb.Chunk("test.md", "1 概述", "正文", 1),
        )):
            product_kb.section_numbers.cache_clear()
            numbers = product_kb.section_numbers()
        product_kb.section_numbers.cache_clear()
        self.assertIn("1.1", numbers)
        self.assertNotIn("1", numbers)
        result = validation.check_output(
            "建议每天 2 次。", self.MATERIAL, "strong",
            question="产品工作原理", extra_numbers=numbers)
        self.assertFalse(result["ok"], result["problems"])


if __name__ == "__main__":
    unittest.main()
