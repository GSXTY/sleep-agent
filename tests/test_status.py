"""运行状态展示测试。

状态输出必须能区分聊天模型、向量模型、索引和降级状态，并且不能泄露 API Key。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat


CHAT_CONFIG = {
    "provider": "openai",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "test-api-key-must-not-leak",
    "timeout": 60,
}
EMBEDDING_CONFIG = {
    "provider": "ollama",
    "model": "bge-m3",
    "base_url": "http://127.0.0.1:11434",
    "api_key": "",
    "timeout": 30,
}
INDEX_HEALTHY = {"built": True, "count": 98, "model": "bge-m3"}


class TestStatus(unittest.TestCase):
    def _status(self, *, use_llm=True, chat_ready=True, vector_ready=True,
                index=None):
        index = INDEX_HEALTHY if index is None else index
        with patch.object(chat.llm, "settings", return_value=CHAT_CONFIG), \
                patch.object(chat.embeddings, "settings", return_value=EMBEDDING_CONFIG), \
                patch.object(chat.vector_index, "stats", return_value=index):
            return chat.build_status(
                use_llm, chat_ready=chat_ready, vector_ready=vector_ready
            )

    def test_healthy_status_shows_both_models_and_index(self):
        text = self._status()
        self.assertIn("openai / deepseek-chat（已开启）", text)
        self.assertIn("ollama / bge-m3（可用）", text)
        self.assertIn("已建立，98 条", text)
        self.assertIn("混合检索（稀疏 0.3 / 稠密 1.0）", text)
        self.assertIn("降级状态：正常，未降级", text)

    def test_status_never_prints_api_key(self):
        text = self._status()
        self.assertNotIn(CHAT_CONFIG["api_key"], text)
        self.assertNotIn("sk-test-secret", text)

    def test_vector_failure_reports_degradation(self):
        text = self._status(vector_ready=False)
        self.assertIn("纯关键词（自动降级）", text)
        self.assertIn("降级状态：已降级", text)
        self.assertNotIn("降级状态：正常", text)

    def test_index_model_mismatch_is_visible(self):
        text = self._status(index={"built": True, "count": 10, "model": "old-model"})
        self.assertIn("与当前配置不一致，首次检索时重建", text)

    def test_llm_off_is_visible(self):
        text = self._status(use_llm=False)
        self.assertIn("已关闭，当前使用规则版", text)


if __name__ == "__main__":
    unittest.main()
