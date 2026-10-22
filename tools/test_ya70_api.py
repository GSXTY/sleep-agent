"""YA70 日期区间统计接口测试。凭据优先从环境变量或 .local 配置读取。"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / ".local" / "ya70_api.json"
LOCAL_CONFIG = {}
if CONFIG_FILE.is_file():
    try:
        LOCAL_CONFIG = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOCAL_CONFIG = {}

URL = (
    os.getenv("SLEEPAGENT_BACKEND_URL")
    or LOCAL_CONFIG.get("url")
    or "http://127.0.0.1:8081/infc/respStatisticsYa70/listByDateRange"
)
API_KEY = (
    os.getenv("SLEEPAGENT_BACKEND_KEY")
    or LOCAL_CONFIG.get("key")
    or "replace-with-your-api-key"
)
USER_ID = int(
    os.getenv("SLEEPAGENT_USER_ID")
    or LOCAL_CONFIG.get("user_id")
    or 10001
)

REQUEST_BODY = {
    "userid": USER_ID,
    "startDate": "2026-09-23",
    "endDate": "2026-10-22",
}


def main() -> int:
    if API_KEY == "replace-with-your-api-key":
        print("请先设置 SLEEPAGENT_BACKEND_KEY 或在 .local/ya70_api.json 中配置 key。")
        return 2
    request = urllib.request.Request(
        URL,
        data=json.dumps(REQUEST_BODY, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "key": API_KEY,
        },
        method="POST",
    )

    print("POST " + URL)
    print(json.dumps(REQUEST_BODY, ensure_ascii=False, indent=2))

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=20) as response:
            status = response.status
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", "replace")
    except Exception as exc:
        print("请求失败：" + str(exc))
        return 2

    print("HTTP " + str(status))
    try:
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(body)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
