"""追问与选项：意图不清、查不到、时间不清时，都给可执行的下一步。

选项统一结构 {type,title,value,source,visibility}：
  date   —— 可选的治疗夜日期
  intent —— 产品/数据/报告/指标四个方向
  topic  —— 产品资料里的近似章节
"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import knowledge, nightdata, pipeline, product_kb


def _hit(section, doc="ya70.md", visibility="user", text="x", score=0.02):
    return {"doc": doc, "section": section, "visibility": visibility,
            "text": text, "score": score, "index": 0}


class OptionTestCase(unittest.TestCase):
    def setUp(self):
        self.nights = nightdata.load_dataset()
        self.night = self.nights[-1]
        self.dates = nightdata.dataset_dates(self.nights)

    def ask(self, question, use_llm=False, time_status="none",
            time_note="", dates=None):
        request = pipeline.UserQuery(
            question=question, use_llm=use_llm,
            time_status=time_status, time_note=time_note,
            available_dates=self.dates if dates is None else dates,
        )
        return pipeline.answer(request, self.night)

    def assertOptionShape(self, candidates):
        self.assertTrue(candidates)
        for item in candidates:
            self.assertEqual(set(item), {"type", "title", "value", "source", "visibility"})
            self.assertIn(item["type"], ("date", "scope", "followup", "intent", "topic"))
            self.assertTrue(item["title"])
            self.assertTrue(item["value"])


class TestDateOptions(OptionTestCase):
    def test_time_confirmation_offers_dates(self):
        result = self.ask("那天的使用情况", time_status="ambiguous", time_note="那天")
        self.assertEqual(result.mode, "时间确认")
        self.assertEqual([item["title"] for item in result.candidates], self.dates)
        self.assertEqual({item["type"] for item in result.candidates}, {"date"})
        self.assertOptionShape(result.candidates)


class TestTopicOptions(OptionTestCase):
    def test_junk_hits_become_readable_topics(self):
        junk = [_hit("注意"), _hit("注意"), _hit("表格")]
        related = [_hit("日常清洁", doc="清洁与维护.md", visibility="examples"),
                   _hit("六、清洁和保养")]
        with mock.patch.object(product_kb, "retrieve", return_value=junk), \
                mock.patch.object(product_kb, "trim_low_relevance", return_value=junk), \
                mock.patch.object(product_kb, "related_hits", return_value=related):
            result = self.ask("面罩怎么清洁")
        self.assertEqual(result.mode, "产品文档（需要确认）")
        self.assertEqual([item["title"] for item in result.candidates],
                         ["日常清洁", "六、清洁和保养"])
        self.assertNotIn("注意", [item["title"] for item in result.candidates])
        self.assertEqual(result.candidates[0]["visibility"], "examples")
        self.assertIn("清洁与维护.md", result.candidates[0]["source"])
        self.assertOptionShape(result.candidates)

    def test_missing_answer_offers_nearby_topics(self):
        related = [_hit("6.4 更换空气滤芯"), _hit("六、清洁和保养")]
        with mock.patch.object(product_kb, "retrieve", return_value=[]), \
                mock.patch.object(product_kb, "related_hits", return_value=related), \
                mock.patch.object(knowledge, "retrieve", return_value=[]):
            result = self.ask("呼吸机能不能水洗")
        self.assertEqual(result.mode, "产品文档（未命中）")
        self.assertIn("没有和这个问题直接对应", result.text)
        self.assertEqual([item["title"] for item in result.candidates],
                         ["6.4 更换空气滤芯", "六、清洁和保养"])
        self.assertEqual(result.risk_flags, ["needs_topic_confirmation"])
        self.assertOptionShape(result.candidates)

    def test_no_material_falls_back_to_intent_menu(self):
        with mock.patch.object(product_kb, "retrieve", return_value=[]), \
                mock.patch.object(product_kb, "related_hits", return_value=[]), \
                mock.patch.object(knowledge, "retrieve", return_value=[]):
            result = self.ask("呼吸机支持远程升级吗")
        self.assertEqual(result.mode, "资料未覆盖")
        self.assertEqual({item["type"] for item in result.candidates}, {"intent"})
        self.assertEqual(len(result.candidates), 4)
        self.assertOptionShape(result.candidates)

    def test_mask_cleaning_never_falls_into_leak_data(self):
        result = self.ask("面罩怎么清洁")
        self.assertNotIn("漏气量", result.text)
        haystack = result.text + " ".join(item["title"] for item in result.candidates)
        self.assertIn("清洁", haystack)


class TestIntentOptions(OptionTestCase):
    def test_general_fallback_offers_directions(self):
        result = self.ask("帮我写一句生日祝福", use_llm=False)
        self.assertTrue(result.mode.startswith("通用对话"))
        self.assertEqual({item["type"] for item in result.candidates}, {"intent"})
        self.assertOptionShape(result.candidates)

    def test_safety_boundary_has_no_options(self):
        result = self.ask("帮我把压力调高一点")
        self.assertEqual(result.mode, "安全边界")
        self.assertEqual(result.candidates, [])


if __name__ == "__main__":
    unittest.main()
