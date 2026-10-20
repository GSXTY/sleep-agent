"""低置信度意图兜底测试。"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import intent_fallback, nightdata, pipeline


class TestIntentFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_parses_structured_intent(self):
        with mock.patch.object(intent_fallback.llm, "chat", return_value={
            "status": "success",
            "content": '{"intent":"product_docs","confidence":0.91}',
        }):
            result = intent_fallback.classify("这个东西怎么弄")
        self.assertEqual(result, {"intent": "product_docs", "confidence": 0.91})

    def test_low_confidence_is_rejected(self):
        with mock.patch.object(intent_fallback.llm, "chat", return_value={
            "status": "success",
            "content": '{"intent":"general","confidence":0.2}',
        }):
            result = intent_fallback.classify("这个东西")
        self.assertIsNone(result)

    def test_invalid_intent_is_rejected(self):
        with mock.patch.object(intent_fallback.llm, "chat", return_value={
            "status": "success",
            "content": '{"intent":"medical_diagnosis","confidence":0.99}',
        }):
            result = intent_fallback.classify("你觉得呢")
        self.assertIsNone(result)

    def test_pipeline_clarifies_when_llm_is_uncertain(self):
        with mock.patch.object(
            pipeline.intent_fallback, "classify",
            return_value={"intent": "clarify", "confidence": 0.85},
        ):
            result = pipeline.answer("这个东西怎么弄", self.night, [], use_llm=True)
        self.assertEqual(result.mode, "意图确认")
        self.assertIn("请补充", result.text)
        self.assertIn("LLM意图:clarify", result.detail)


if __name__ == "__main__":
    unittest.main()
