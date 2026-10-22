"""评分分项定义：字段说明表不能被检索层误排除。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest

from sleepagent import knowledge, nightdata, pipeline, product_kb


class ScoreDefinitionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_knowledge_entries_exist(self):
        ids = {item["id"] for item in knowledge.load_knowledge_base()}
        for item_id in ("total-treatment-score", "duration-score",
                        "mask-score", "respiratory-event-score"):
            self.assertIn(item_id, ids)

    def test_duration_score_definition_is_answerable(self):
        result = pipeline.answer(
            "时长得分是什么", self.night, [], use_llm=False
        )
        self.assertEqual(result.mode, "指标解释（规则版）")
        self.assertIn("使用时长分项", result.text)
        self.assertIn("核对当晚上实际使用时长", result.text)
        self.assertIn("duration-score", result.sources)

    def test_score_definition_table_is_searchable(self):
        for question in ("面罩得分是什么", "呼吸事件得分是什么", "治疗评分是什么"):
            hits = product_kb.retrieve(question, top_k=3)
            self.assertTrue(hits, question)
            self.assertTrue(any("报告指标说明" in hit["doc"] for hit in hits), question)

    def test_other_score_components_explain_related_metrics(self):
        expectations = (
            ("面罩得分是什么", "漏气记录"),
            ("呼吸事件得分是什么", "AHI"),
            ("治疗评分是什么", "时长得分"),
        )
        for question, expected in expectations:
            result = pipeline.answer(question, self.night, [], use_llm=False)
            self.assertEqual(result.mode, "指标解释（规则版）", question)
            self.assertIn(expected, result.text, question)


if __name__ == "__main__":
    unittest.main()
