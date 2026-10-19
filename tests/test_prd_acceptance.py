"""PRD 第七章「验收标准」的集成测试。

每一条对应 PRD 7.1 功能验收要点表中的一行，改动后直接跑这个文件就能知道
哪一条 PRD 承诺被破坏了。
"""

# 测试固定关闭向量：结果不能依赖开发机上是否装了 Ollama。
# 想验证真实向量效果请跑 eval_retrieval.py / tune_weights.py。
import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat
from sleepagent import embeddings, nightdata, report, runtime_info, safety


class TestPRDAcceptance(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_night()

    # PRD 7.1 · 安全边界：输入「帮我把压力调高一点」→ 返回合规拒绝话术，不调用大模型
    def test_safety_boundary_does_not_call_model(self):
        from unittest import mock
        calls = []
        with mock.patch.object(chat.llm, "chat", side_effect=lambda *a, **k: calls.append(a)):
            result = chat.answer("帮我把压力调高一点", self.night, [], use_llm=True)
        self.assertEqual(calls, [], "安全边界请求不应调用大模型")
        self.assertEqual(result["mode"], "安全边界")
        self.assertIn("不能", result["text"])

    # PRD 7.1 · 时间查询：输入「今天几号」→ 返回正确当前日期，由程序直接生成
    def test_current_date_from_program(self):
        result = chat.answer("今天几号", self.night, [], use_llm=True)
        self.assertEqual(result["mode"], "系统信息")
        self.assertIn(runtime_info.now().strftime("%Y年%m月%d日"), result["text"])

    # PRD 7.1 · 数据查询：输入「我昨晚 AHI 多少」→ 返回数值，来自事实层
    def test_data_query_returns_fact_value(self):
        result = chat.answer("我昨晚 AHI 多少", self.night, [], use_llm=False)
        self.assertIn("设备数据", result["mode"])
        self.assertIn(str(self.night.raw["ahi"]), result["text"])

    # PRD 7.1 · 缺失数据：不出现 None / 0 或推测值
    def test_missing_fields_do_not_leak_none(self):
        partial = dict(self.night.raw)
        for key in ("treat_pressure_max", "air_leakage_max", "longest_event_free_minutes",
                    "inspiratory_ratio_percent", "expiratory_ratio_percent"):
            partial.pop(key, None)
        night = nightdata.Night(raw=partial, user_id=self.night.user_id,
                                history=self.night.history)
        nightdata.build_facts(self.night.user_id, night)
        text = report.render(night)["text"]
        self.assertNotIn("None", text)
        self.assertIn("未提供", text)

    # PRD 7.1 · 知识库问答：正确返回知识库内容，并标注来源
    def test_knowledge_answer_cites_source(self):
        result = chat.answer("滤芯多久换一次", self.night, [], use_llm=False)
        self.assertIn("产品文档", result["mode"])
        self.assertTrue(result["sources"], "知识库回答必须带出处")

    # PRD 7.1 · 语义匹配：口语说法要能匹配到文档用词
    def test_synonym_matching(self):
        result = chat.answer("加湿器怎么清洁", self.night, [], use_llm=False)
        self.assertIn("产品文档", result["mode"])
        self.assertIn("湿化器", result["text"])

    # PRD 7.1 · 报告生成：结构完整，首尾带免责声明
    def test_report_structure_and_disclaimers(self):
        text = report.render(self.night)["text"]
        for section in ("总体评级：", "核心指标：", "一、治疗压力", "二、呼吸事件",
                        "三、呼吸概况", "四、漏气与面罩密封", "五、总结建议"):
            self.assertIn(section, text, section)
        self.assertIn("不构成医学诊断", text)
        self.assertIn("参数请遵医嘱", text)

    # PRD 7.1 · 降级能力：关闭向量服务后问答仍可用
    def test_works_without_vector_service(self):
        from unittest import mock
        with mock.patch.object(embeddings, "available", return_value=False):
            result = chat.answer("滤芯多久换一次", self.night, [], use_llm=False)
        self.assertIn("产品文档", result["mode"])

    # PRD 4.6.1 · 用药 / 治疗建议拦截
    def test_medication_advice_blocked(self):
        self.assertEqual(safety.check("我应该吃什么药").kind, "medication")


class TestAcceptanceIsNonBlocking(unittest.TestCase):
    """验收未通过时不阻断报告，但必须列出问题清单。"""

    def _night_missing_leak(self):
        night = nightdata.load_night()
        raw = dict(night.raw)
        for key in ("air_leakage_avg", "air_leakage_p95", "air_leakage_max",
                    "large_leak_minutes", "large_leak_percent"):
            raw.pop(key, None)
        partial = nightdata.Night(raw=raw, user_id=night.user_id, history=night.history)
        nightdata.build_facts(night.user_id, partial)
        return partial

    def test_report_still_generated_when_acceptance_fails(self):
        payload = report.render(self._night_missing_leak())
        self.assertFalse(payload["acceptance"]["passed"])
        self.assertGreater(len(payload["text"]), 300, "验收未通过时报告仍应生成")

    def test_acceptance_failures_are_listed(self):
        text = report.render(self._night_missing_leak())["text"]
        self.assertIn("验收结果：未通过", text)
        self.assertIn("  · ", text, "未通过时必须逐条列出问题")

    def test_acceptance_listing_is_capped(self):
        # 单模块缺数据会连报多项，输出要折叠，避免刷屏
        text = report.render(self._night_missing_leak())["text"]
        listing = [line for line in text.split("\n") if line.startswith("  · ")]
        self.assertLessEqual(len(listing), 8, str(listing))


if __name__ == "__main__":
    unittest.main()
