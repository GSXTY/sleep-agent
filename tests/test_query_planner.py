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

    def test_general_tasks_do_not_become_therapy_queries(self):
        for question in ("比较一下这两个手机", "最近一周股票变化", "一加一是多少",
                         "帮我写一份工作报告", "为什么气温下降了"):
            with self.subTest(question=question):
                result = query_planner.plan(question, use_llm=False)
                self.assertEqual(result["intent"], "general")

    def test_product_actions_are_not_personal_metric_requests(self):
        for question in ("面罩怎么清洗", "压力管路怎么安装", "滤芯多久换一次",
                         "面罩保修多少天"):
            with self.subTest(question=question):
                self.assertEqual(query_planner.plan(question)["intent"], "product_query")

    def test_general_change_does_not_return_personal_trend(self):
        result = pipeline.answer("为什么气温下降了", self.night, [], use_llm=False)
        self.assertNotEqual(result.mode, "趋势分析")
        self.assertNotIn("treat_score", result.sources)

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
