"""最小 HTTP 探针：验证 pipeline 可以脱离命令行独立运行。

为什么不用 FastAPI：这个 Windows 版本的核心优势是**零第三方依赖**，
`pip install fastapi` 会把 pydantic / starlette / uvicorn 一整串带进来，
重蹈 Mac 版在环境依赖上翻车的覆辙。标准库 60 行足够验证同一件事。

用法：
    python probe_server.py            # 默认 127.0.0.1:8099
    python probe_server.py --port 9000

接口：
    GET  /health          -> {"status": "ok"}
    POST /ask             -> {"code": 0, "message": "ok", "data": {...}}
         body: {"question": "滤芯多久换一次", "use_llm": false}

注意：这是**验证性探针**，没有鉴权、没有限流、只绑本机。不要直接对外暴露。
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sleepagent import nightdata, pipeline
from sleepagent.logging_config import configure_logging

_nights = {}


def _current_night(user_id: str = nightdata.DEFAULT_USER_ID):
    """按用户按需加载；当前数据源仍是单用户，接口已预留隔离边界。"""
    if user_id not in _nights:
        _nights[user_id] = nightdata.load_night(user_id)
    return _nights[user_id]


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        server_version = "SleepAgentProbe/0.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
                return
            self._send(404, {"code": 404, "message": "not found", "data": None})

        def do_POST(self):
            if self.path != "/ask":
                self._send(404, {"code": 404, "message": "not found", "data": None})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send(400, {"code": 400, "message": "请求体不是合法 JSON", "data": None})
                return
            question = str(body.get("question") or "").strip()
            if not question:
                self._send(400, {"code": 400, "message": "缺少 question 字段", "data": None})
                return
            use_llm = bool(body.get("use_llm", False))
            user_id = str(body.get("user_id") or "DEMO-USER")
            try:
                result = pipeline.answer(
                    pipeline.UserQuery(question=question, use_llm=use_llm, user_id=user_id),
                    _current_night(user_id),
                )
            except Exception as exc:  # 探针阶段：把异常转成结构化响应，便于定位
                self._send(500, {"code": 500, "message": str(exc), "data": None})
                return
            self._send(200, {"code": 0, "message": "ok", "data": result.to_dict()})

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8099) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler())


def main() -> int:
    parser = argparse.ArgumentParser(description="pipeline HTTP 探针")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--verbose", action="store_true", help="在终端显示 INFO 请求日志")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)
    server = make_server(args.host, args.port)
    print("探针已启动: http://" + args.host + ":" + str(args.port))
    print("健康检查: GET /health    提问: POST /ask")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
