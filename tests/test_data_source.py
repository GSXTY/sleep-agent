"""Agent 数据源选择与 backend 切换。"""
import os
import unittest
from pathlib import Path
from unittest import mock

from sleepagent import data_source, nightdata


class DataSourceTest(unittest.TestCase):
    def test_defaults_to_sample(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SLEEPAGENT_DATA_SOURCE", None)
            with mock.patch.object(data_source, "CONFIG_FILE", Path("missing.json")):
                cfg = data_source.settings()
        self.assertEqual(cfg["source"], "sample")

    def test_invalid_source_is_rejected(self):
        with mock.patch.object(data_source, "CONFIG_FILE", Path("missing.json")):
            with self.assertRaises(ValueError):
                data_source.settings(source="unknown")

    def test_backend_config_and_mock_fetch(self):
        config = {
            "data_source": "backend",
            "user_id": 10001,
            "key": "test-key",
            "url": "http://example.test/api",
            "start_date": "2026-09-23",
            "end_date": "2026-10-22",
        }
        nights = nightdata.load_dataset()
        with mock.patch.object(data_source, "_config", return_value=config), \
                mock.patch.object(data_source.backend_ya70, "fetch_rows",
                                  return_value=[{"id": 1}]) as fetch, \
                mock.patch.object(data_source.backend_ya70, "rows_to_nights",
                                  return_value=nights) as mapper:
            loaded = data_source.load_nights()
        self.assertEqual(len(loaded), 7)
        fetch.assert_called_once_with(
            10001, "2026-09-23", "2026-10-22", "test-key",
            "http://example.test/api", 20.0,
        )
        mapper.assert_called_once_with([{"id": 1}], 10001)

    def test_backend_requires_key(self):
        with mock.patch.object(data_source, "CONFIG_FILE", Path("missing.json")):
            with self.assertRaises(RuntimeError):
                data_source.load_nights(user_id="10001", source="backend")


if __name__ == "__main__":
    unittest.main()
