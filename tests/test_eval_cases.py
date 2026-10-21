"""Bad case 注册表与诊断辅助函数。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import json
import shutil
import unittest
import uuid
from pathlib import Path

import eval_cases
import eval_retrieval


class EvalCasesTest(unittest.TestCase):
    def setUp(self):
        base = Path(eval_cases.CASES_FILE).parent / ("test-" + uuid.uuid4().hex[:8])
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "cases.jsonl"

    def tearDown(self):
        shutil.rmtree(self.path.parent, ignore_errors=True)

    def test_record_and_load(self):
        case = eval_cases.record_case(
            "氧气瓶能带上飞机吗", mode="范围外", section="",
            contains=["无法"], note="越界问题", path=self.path)
        self.assertEqual(case["query"], "氧气瓶能带上飞机吗")
        loaded = eval_cases.load_cases(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["expected_contains"], ["无法"])

    def test_duplicate_query_is_not_recorded_twice(self):
        first = eval_cases.record_case("重复问题", mode="设备数据", path=self.path)
        second = eval_cases.record_case("重复问题", mode="产品文档", path=self.path)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(eval_cases.load_cases(self.path)), 1)

    def test_empty_expectation_is_rejected(self):
        with self.assertRaises(ValueError):
            eval_cases.record_case("只有问题", path=self.path)

    def test_bad_lines_are_skipped(self):
        self.path.write_text(
            '{"query":"正常","expected_mode":"产品文档"}\n'
            'not json\n'
            '{"expected_mode":"x"}\n',
            encoding="utf-8")
        loaded = eval_cases.load_cases(self.path)
        self.assertEqual([item["query"] for item in loaded], ["正常"])

    def test_repo_registry_contains_real_badcases(self):
        loaded = eval_cases.load_cases()
        queries = {item["query"] for item in loaded}
        self.assertGreaterEqual(len(loaded), 7)
        self.assertIn("我有气胸可以使用吗", queries)
        self.assertIn("产品工作原理", queries)


class DiagnoseHelperTest(unittest.TestCase):
    def test_rank_of_matches_section_substring(self):
        hits = [{"section": "1.4产品型号"}, {"section": "1.1工作原理"}]
        self.assertEqual(eval_retrieval._rank_of(hits, ["工作原理"]), 2)
        self.assertIsNone(eval_retrieval._rank_of(hits, ["禁忌症"]))

    def test_all_cases_merges_registry(self):
        questions = {question for question, _ in eval_retrieval.all_cases()}
        self.assertIn("产品工作原理", questions)
        self.assertIn("双水平是什么意思", questions)
        # 安全边界用例没有 expected_section，不进入检索评测
        self.assertNotIn("我有气胸可以使用吗", questions)


if __name__ == "__main__":
    unittest.main()
