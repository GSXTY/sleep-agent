"""Agent 数据源选择：本地样板数据或 YA70 后台接口。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path

from . import backend_ya70, nightdata

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / ".local" / "ya70_api.json"
DEFAULT_USER_ID = str(nightdata.DEFAULT_USER_ID)
DEFAULT_SOURCE = "sample"
DEFAULT_LOOKBACK_DAYS = 30


def _config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("读取 YA70 数据源配置失败：" + str(exc))
    return data if isinstance(data, dict) else {}


def settings(source: str | None = None, user_id: str | None = None) -> dict:
    config = _config()
    selected = (
        source
        or os.getenv("SLEEPAGENT_DATA_SOURCE", "")
        or str(config.get("data_source") or DEFAULT_SOURCE)
    ).strip().lower()
    if selected not in ("sample", "backend"):
        raise ValueError("SLEEPAGENT_DATA_SOURCE 只能是 sample 或 backend")

    end_date = (
        os.getenv("SLEEPAGENT_BACKEND_END_DATE", "")
        or str(config.get("end_date") or "")
        or dt.date.today().isoformat()
    )
    lookback = int(
        os.getenv("SLEEPAGENT_BACKEND_LOOKBACK_DAYS", "")
        or config.get("lookback_days")
        or DEFAULT_LOOKBACK_DAYS
    )
    start_date = (
        os.getenv("SLEEPAGENT_BACKEND_START_DATE", "")
        or str(config.get("start_date") or "")
        or (dt.date.fromisoformat(end_date) - dt.timedelta(days=lookback - 1)).isoformat()
    )
    key = (
        os.getenv("SLEEPAGENT_BACKEND_KEY", "")
        or os.getenv("YA70_API_KEY", "")
        or str(config.get("key") or config.get("api_key") or "")
    )
    return {
        "source": selected,
        "user_id": str(
            user_id
            or os.getenv("SLEEPAGENT_USER_ID", "")
            or config.get("user_id")
            or DEFAULT_USER_ID
        ),
        "url": (
            os.getenv("SLEEPAGENT_BACKEND_URL", "")
            or str(config.get("url") or "")
            or backend_ya70.DEFAULT_URL
        ),
        "key": key,
        "start_date": start_date,
        "end_date": end_date,
        "timeout": float(
            os.getenv("SLEEPAGENT_BACKEND_TIMEOUT", "")
            or config.get("timeout")
            or 20
        ),
    }


def load_nights(user_id: str | None = None, source: str | None = None) -> list:
    cfg = settings(source=source, user_id=user_id)
    if cfg["source"] == "sample":
        return nightdata.load_dataset(cfg["user_id"])
    if not cfg["key"]:
        raise RuntimeError(
            "backend 数据源缺少 key：请设置 SLEEPAGENT_BACKEND_KEY，"
            "或在 .local/ya70_api.json 中填写 key"
        )
    rows = backend_ya70.fetch_rows(
        int(cfg["user_id"]),
        cfg["start_date"],
        cfg["end_date"],
        cfg["key"],
        cfg["url"],
        cfg["timeout"],
    )
    nights = backend_ya70.rows_to_nights(rows, int(cfg["user_id"]))
    if not nights:
        raise RuntimeError(
            "后台在 " + cfg["start_date"] + " 至 " + cfg["end_date"] + " 没有数据"
        )
    return nights


def load_night(user_id: str | None = None, source: str | None = None):
    nights = load_nights(user_id=user_id, source=source)
    return nights[-1]


async def load_nights_async(user_id: str | None = None,
                            source: str | None = None) -> list:
    cfg = settings(source=source, user_id=user_id)
    if cfg["source"] == "sample":
        return await asyncio.to_thread(nightdata.load_dataset, cfg["user_id"])
    if not cfg["key"]:
        raise RuntimeError(
            "backend 数据源缺少 key：请设置 SLEEPAGENT_BACKEND_KEY，"
            "或在 .local/ya70_api.json 中填写 key"
        )
    rows = await backend_ya70.fetch_rows_async(
        int(cfg["user_id"]),
        cfg["start_date"],
        cfg["end_date"],
        cfg["key"],
        cfg["url"],
        cfg["timeout"],
    )
    nights = await asyncio.to_thread(
        backend_ya70.rows_to_nights, rows, int(cfg["user_id"])
    )
    if not nights:
        raise RuntimeError(
            "后台在 " + cfg["start_date"] + " 至 " + cfg["end_date"] + " 没有数据"
        )
    return nights


async def load_night_async(user_id: str | None = None,
                           source: str | None = None):
    nights = await load_nights_async(user_id=user_id, source=source)
    return nights[-1]


def describe(user_id: str | None = None, source: str | None = None) -> str:
    cfg = settings(source=source, user_id=user_id)
    if cfg["source"] == "sample":
        return "数据源：sample（data/sample_nights.json），用户 " + cfg["user_id"]
    return (
        "数据源：backend（" + cfg["url"] + "），用户 " + cfg["user_id"]
        + "，日期 " + cfg["start_date"] + " 至 " + cfg["end_date"]
    )
