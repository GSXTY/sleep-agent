"""使用 YA70 本地接口真实数据跑一组 Agent 端到端问法。"""
import datetime as dt
import os
import sys
from pathlib import Path

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from sleepagent import backend_ya70, nightdata, pipeline
from test_ya70_api import API_KEY, URL

USER_ID = 10001
START_DATE = "2026-09-23"
END_DATE = "2026-10-22"

QUESTIONS = (
    "最近一次使用情况",
    "我的评分有什么变化",
    "为什么我的评分变化",
    "最近7天数据有什么变化",
    "最近一个月的报告",
    "生成报告",
    "时长得分是什么",
    "最近一次漏气怎么样",
    "帮我把压力调高一点",
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows = backend_ya70.fetch_rows(
        USER_ID, START_DATE, END_DATE, API_KEY, URL
    )
    nights = backend_ya70.rows_to_nights(rows, USER_ID)
    print("接口记录数：" + str(len(rows)), flush=True)
    print("内部治疗夜：" + str(len(nights)), flush=True)
    print("日期：" + "、".join(item.raw["record_date"] for item in nights), flush=True)

    today = dt.date.today()
    dates = nightdata.dataset_dates(nights)
    for question in QUESTIONS:
        selection = nightdata.resolve_night(
            str(USER_ID), nights, question, today=today
        )
        night = selection.night or nights[-1]
        result = pipeline.answer(
            pipeline.UserQuery(
                question=question,
                use_llm=False,
                user_id=str(USER_ID),
                time_status=selection.status,
                time_note=selection.note,
                available_dates=dates,
            ),
            night,
        )
        print("\n" + "=" * 72)
        print("问：" + question)
        print("模式：" + result.mode)
        print("说明：" + result.detail)
        print(result.text)
        if result.candidates:
            print("候选：" + " / ".join(
                item.get("title", "") for item in result.candidates
            ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
