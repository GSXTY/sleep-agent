"""检索常量的默认值锁定。

这些数值是实测调出来的；改动它们必须同时跑 eval_retrieval.py 并更新本测试。
测试失败不等于代码错——先确认评估指标没有退步，再改这里的期望值。
"""

# 测试固定关闭向量：结果不能依赖开发机上是否装了 Ollama。
# 想验证真实向量效果请跑 eval_retrieval.py / tune_weights.py。
import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import retrieval_constants as rc


class TestRetrievalConstants(unittest.TestCase):
    def test_section_bonus(self):
        self.assertEqual(rc.SECTION_BONUS, 6.0)

    def test_coverage_gate(self):
        self.assertEqual(rc.MIN_COVERAGE_DEFAULT, 0.4)

    def test_no_sparse_score_floor(self):
        self.assertEqual(rc.SPARSE_MIN_SCORE, 0.0)

    def test_pool_size(self):
        self.assertEqual(rc.RETRIEVAL_POOL, 60)

    def test_dense_score_floor(self):
        # 实测：无关问题最高 0.476，合法问题最低 0.545
        self.assertGreater(rc.DENSE_MIN_SCORE, 0.476)
        self.assertLess(rc.DENSE_MIN_SCORE, 0.545)

    def test_rrf_parameters(self):
        self.assertEqual(rc.RRF_K, 60)
        self.assertEqual(rc.RRF_WEIGHTS, (0.3, 1.0))

    def test_dense_weight_above_sparse_is_intentional(self):
        # PRD 4.2.2 要求「关键词权重高于向量」，但实测 bge-m3 明显更强
        # （稀疏为主的 82% vs 稠密为主的 95%）。当前按实测取稠密为主，
        # 这处与 PRD 的偏差需要在产品侧确认后再决定是否维持。
        sparse, dense = rc.RRF_WEIGHTS
        self.assertGreater(dense, sparse)
        self.assertGreater(sparse, 0, "稀疏仍要保留一点权重：它负责精确匹配")

