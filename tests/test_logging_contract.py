"""请求日志契约测试。

固定字段：时间、request_id、模式、意图、召回数、模型使用、校验结果、耗时。
异常路径必须有统一前缀：SAFETY_BLOCK / VALIDATION_FAIL / DEGRADE。
"""

import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import logging_config, nightdata, pipeline


class TestLoggingContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.night = nightdata.load_night()

    def test_normal_request_has_standard_fields_and_request_id(self):
        with self.assertLogs("sleepagent.pipeline", level="INFO") as captured:
            result = pipeline.answer(
                pipeline.UserQuery("滤芯多久换一次", use_llm=False),
                self.night,
            )
        line = next(item for item in captured.output if "[产品文档" in item)
        self.assertIn("[" + result.request_id + "]", line)
        self.assertRegex(line, r"召回:\d+")
        self.assertIn("模型:否", line)
        self.assertIn("校验:未执行", line)
        self.assertRegex(line, r"耗时:\d+ms")
        self.assertTrue(result.request_id)

    def test_safety_block_has_prefix(self):
        with self.assertLogs("sleepagent.pipeline", level="WARNING") as captured:
            result = pipeline.answer(
                pipeline.UserQuery("帮我把压力调高一点", use_llm=True),
                self.night,
            )
        line = next(item for item in captured.output if "[SAFETY_BLOCK]" in item)
        self.assertIn("[安全边界]", line)
        self.assertIn("模型:否", line)
        self.assertEqual(result.risk_flags, ["pressure"])

    def test_validation_failure_has_prefix_and_falls_back(self):
        fake = {
            "status": "success",
            "content": "滤芯需要 99999 天更换。",
            "provider": "fake",
            "model": "fake",
            "detail": "",
        }
        with mock.patch.object(pipeline.llm, "chat", return_value=fake):
            with self.assertLogs("sleepagent.pipeline", level="WARNING") as captured:
                result = pipeline.answer(
                    pipeline.UserQuery("滤芯多久换一次", use_llm=True),
                    self.night,
                )
        self.assertTrue(any("[VALIDATION_FAIL]" in item for item in captured.output))
        self.assertIn("规则版", result.mode)
        self.assertFalse(result.validation["ok"])

    def test_degrade_has_prefix(self):
        with mock.patch.object(pipeline.embeddings, "available", return_value=False):
            with self.assertLogs("sleepagent.pipeline", level="WARNING") as captured:
                pipeline.answer(
                    pipeline.UserQuery("滤芯多久换一次", use_llm=False),
                    self.night,
                )
        self.assertTrue(any("[DEGRADE]" in item for item in captured.output))


class TestFileLogging(unittest.TestCase):
    def test_info_is_written_to_file_even_without_verbose(self):
        path = (Path(__file__).resolve().parents[1] / "logs"
                / ("test-agent-" + str(os.getpid()) + ".log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        try:
            logging_config.configure_logging(verbose=False, log_file=path)
            logging.getLogger("sleepagent.file-test").info("file-log-ok")
            for handler in logging.getLogger().handlers:
                handler.flush()
            text = path.read_text(encoding="utf-8")
        finally:
            logging_config.reset_logging()
            path.unlink(missing_ok=True)
        self.assertIn("file-log-ok", text)


if __name__ == "__main__":
    unittest.main()
