"""语义查询规划：规则快速路径 + LLM 受限兜底。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata, pipeline, query_planner


class QueryPlannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_rule_plan_normalizes_score_week_question(self):
        plan = query_planner.plan("我这一周的总分变化", use_llm=False)
        self.assertEqual(plan["intent"], "analyze_change")
        self.assertEqual(plan["metrics"], ["treatment_score"])
        self.assertEqual(plan["window"], "week")
        self.assertEqual(plan["window_days"], 7)
        self.assertEqual(plan["source"], "rules")

    def test_definition_and_generic_analysis_are_distinguished(self):
        definition = query_planner.plan("时长得分是什么", use_llm=False)
        self.assertEqual(definition["intent"], "explain_metric")
        self.assertEqual(definition["metrics"], ["duration_score"])

        generic = query_planner.plan("分析一下", use_llm=False)
        self.assertEqual(generic["intent"], "general")
        self.assertEqual(generic["metrics"], [])

    def test_llm_plan_handles_unseen_score_paraphrase(self):
        content = (
            '{"intent":"analyze_trend","metrics":["treatment_score"],'
            '"window":"week","confidence":0.93}'
        )
        with mock.patch.object(query_planner.llm, "available", return_value=True), \
                mock.patch.object(query_planner.llm, "chat", return_value={
                    "status": "success", "content": content,
                }):
            plan = query_planner.plan(
                "最近七天那个百分制表现怎么走", use_llm=True
            )
        self.assertEqual(plan["intent"], "analyze_trend")
        self.assertEqual(plan["metrics"], ["treatment_score"])
        self.assertEqual(plan["source"], "llm")

    def test_invalid_llm_metric_falls_back_to_rules(self):
        content = (
            '{"intent":"analyze_trend","metrics":["invented_metric"],'
            '"window":"week","confidence":0.99}'
        )
        with mock.patch.object(query_planner.llm, "available", return_value=True), \
                mock.patch.object(query_planner.llm, "chat", return_value={
                    "status": "success", "content": content,
                }):
            plan = query_planner.plan(
                "最近七天那个百分制表现怎么走", use_llm=True
            )
        self.assertEqual(plan["source"], "rules")
        self.assertEqual(plan["metrics"], [])

    def test_pipeline_uses_llm_plan_before_retrieval_fallbacks(self):
        content = (
            '{"intent":"analyze_change","metrics":["treatment_score"],'
            '"window":"week","confidence":0.94}'
        )
        with mock.patch.object(query_planner.llm, "available", return_value=True), \
                mock.patch.object(query_planner.llm, "chat", return_value={
                    "status": "success", "content": content,
                }):
            result = pipeline.answer(
                pipeline.UserQuery(
                    "最近七天那个百分制表现怎么走", use_llm=True
                ),
                self.night,
            )
        self.assertEqual(result.mode, "评分分析")
        self.assertIn("查询计划:analyze_change", result.detail)
        self.assertIn("（llm）", result.detail)
        self.assertIn("最近 7 天评分", result.text)


if __name__ == "__main__":
    unittest.main()
