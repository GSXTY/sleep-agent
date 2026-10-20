"""产品资料可见性与路由测试。

默认检索应覆盖 user、internal、examples 三类资料；来源标签要能区分
正式说明书、内部技术要求和演示占位资料。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import nightdata, pipeline, product_kb


class TestProductSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_all_three_source_types_are_loaded(self):
        stats = product_kb.stats()
        self.assertTrue(stats["user_documents"])
        self.assertTrue(stats["internal_documents"])
        self.assertTrue(stats["example_documents"])

    def test_examples_are_searchable(self):
        hits = product_kb.retrieve("E3 报错怎么处理", top_k=3, hybrid=False)
        self.assertTrue(hits)
        self.assertTrue(any(hit.get("visibility") == "examples" for hit in hits),
                        [hit.get("doc") for hit in hits])

    def test_internal_compensation_chunk_is_searchable(self):
        hits = product_kb.retrieve(
            "自动漏气补偿", top_k=3, hybrid=False, visibility="internal"
        )
        self.assertTrue(any(
            hit.get("visibility") == "internal" and "自动漏气补偿" in hit.get("section", "")
            for hit in hits
        ), [(hit.get("visibility"), hit.get("section")) for hit in hits])

    def test_feature_question_routes_to_product_docs(self):
        result = pipeline.answer(
            "什么是漏气补偿", self.night, [], use_llm=False
        )
        self.assertTrue(result.mode.startswith("产品文档"), result.mode)
        self.assertIn("自动漏气补偿", result.text)
        self.assertTrue(any("内部资料" in source or "11.1" in source for source in result.sources),
                        result.sources)

    def test_device_answer_does_not_cite_product_docs(self):
        result = pipeline.answer(
            "我昨晚漏气多少", self.night, [], use_llm=False
        )
        self.assertIn("设备数据", result.mode)
        self.assertTrue(result.sources)
        self.assertFalse(any(".docx" in source or ".md" in source for source in result.sources),
                         result.sources)


if __name__ == "__main__":
    unittest.main()
