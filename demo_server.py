"""手机 App Demo 服务：H5 聊天页 + /api/chat + 反馈收集。

定位：内部审核 Demo。App 用 WebView 指向本服务即可；正式版原生页面
复用同一个 /api/chat 接口。

用法：
    python demo_server.py                 # http://127.0.0.1:8100
    python demo_server.py --host 0.0.0.0   # 允许手机同一局域网访问
"""
from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sleepagent import data_source, nightdata, pipeline
from sleepagent.logging_config import configure_logging

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "demo_web"
FEEDBACK_FILE = ROOT / ".local" / "demo_feedback.jsonl"
USER_ID = nightdata.DEFAULT_USER_ID
DATA_SOURCE = ""
MAX_HISTORY = 20

_sessions = {}
_session_nights = {}
_datasets = {}
_session_locks = {}
_dataset_locks = {}
_locks_guard = threading.Lock()


def keyed_lock(mapping: dict, key):
    """为单个会话或用户返回稳定的锁，避免全局串行化。"""
    with _locks_guard:
        return mapping.setdefault(key, threading.RLock())


def nights(user_id: str):
    lock = keyed_lock(_dataset_locks, user_id)
    with lock:
        if user_id not in _datasets:
            _datasets[user_id] = data_source.load_nights(
                user_id=user_id, source=DATA_SOURCE or None
            )
        return _datasets[user_id]


def public_detail(result) -> str:
    """内部模型和校验故障写入 validation/日志，不在用户界面展示。"""
    internal_flags = {"llm_error", "validation_failed", "llm_disabled"}
    if any(flag in internal_flags for flag in (result.risk_flags or [])):
        return ""
    detail = str(result.detail or "")
    internal_phrases = (
        "模型调用失败", "模型输出未通过校验", "未启用大模型",
        "验收通过", "验收未通过",
    )
    return "" if any(phrase in detail for phrase in internal_phrases) else detail


def chat(message: str, session_id: str, use_llm: bool = True,
         user_id: str | None = None):
    user_id = str(user_id or USER_ID)
    lock = keyed_lock(_session_locks, (user_id, session_id))
    with lock:
        return _chat_locked(message, session_id, use_llm, user_id)


def _chat_locked(message: str, session_id: str, use_llm: bool,
                 user_id: str):
    data = nights(user_id)
    session_key = (user_id, session_id)
    history = _sessions.setdefault(session_key, [])
    question, pending = pipeline.resolve_pending_confirmation(message, history)
    selection = nightdata.resolve_night(user_id, data, question)
    if selection.status != "none" and selection.night is not None:
        night = selection.night
    else:
        night = _session_nights.get(session_key) or data[-1]
    _session_nights[session_key] = night
    result = pipeline.answer(
        pipeline.UserQuery(
            question=question, history=history, use_llm=use_llm,
            user_id=user_id, time_status=selection.status,
            time_note=selection.note,
            available_dates=nightdata.dataset_dates(data),
        ),
        night,
    )
    if result.mode.startswith(("设备数据", "报告生成", "App报告解读")):
        extra = pending or selection.note
        if extra and extra not in result.detail:
            result.detail = " · ".join(
                part for part in (result.detail, extra) if part
            )
    result.detail = public_detail(result)

    history.extend([{"role": "user", "content": message},
                    {"role": "assistant", "content": result.text}])
    _sessions[session_key] = history[-MAX_HISTORY:]

    payload = result.to_dict()
    payload["session_id"] = session_id
    payload["user_id"] = user_id
    return payload


async def chat_async(message: str, session_id: str, use_llm: bool = True,
                     user_id: str | None = None):
    """FastAPI 使用的异步版本；调用方负责同一会话串行锁。"""
    user_id = str(user_id or USER_ID)
    data = nights(user_id)
    session_key = (user_id, session_id)
    history = _sessions.setdefault(session_key, [])
    question, pending = pipeline.resolve_pending_confirmation(message, history)
    selection = nightdata.resolve_night(user_id, data, question)
    if selection.status != "none" and selection.night is not None:
        night = selection.night
    else:
        night = _session_nights.get(session_key) or data[-1]
    _session_nights[session_key] = night
    result = await pipeline.answer_async(
        pipeline.UserQuery(
            question=question, history=history, use_llm=use_llm,
            user_id=user_id, time_status=selection.status,
            time_note=selection.note,
            available_dates=nightdata.dataset_dates(data),
        ),
        night,
    )
    if result.mode.startswith(("设备数据", "报告生成", "App报告解读")):
        extra = pending or selection.note
        if extra and extra not in result.detail:
            result.detail = " · ".join(
                part for part in (result.detail, extra) if part
            )
    result.detail = public_detail(result)

    history.extend([
        {"role": "user", "content": message},
        {"role": "assistant", "content": result.text},
    ])
    _sessions[session_key] = history[-MAX_HISTORY:]

    payload = result.to_dict()
    payload["session_id"] = session_id
    payload["user_id"] = user_id
    return payload


def save_feedback(body: dict) -> None:
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": str(body.get("request_id", "")),
        "rating": str(body.get("rating", "")),
        "comment": str(body.get("comment", ""))[:500],
        "session_id": str(body.get("session_id", "")),
        "question": str(body.get("question", ""))[:500],
        "answer": str(body.get("answer", ""))[:800],
        "mode": str(body.get("mode", ""))[:80],
    }
    with FEEDBACK_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "SleepAgentDemo/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _sse_write(self, event: str, data: dict):
        payload = (
            "event: " + event + "\n"
            + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        ).encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()

    def _sse_heartbeat(self):
        self.wfile.write(b": keep-alive\n\n")
        self.wfile.flush()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = (WEB / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._json(200, {
                "status": "ok",
                "sessions": len(_sessions),
                "data_source": DATA_SOURCE or "configured",
                "default_user_id": USER_ID,
                "streaming": True,
            })
            return
        self._json(404, {"code": 404, "message": "not found", "data": None})

    def do_POST(self):
        body = self._body()
        if self.path == "/api/chat":
            message = str(body.get("message") or "").strip()
            if not message:
                self._json(400, {"code": 400, "message": "缺少 message", "data": None})
                return
            session_id = str(body.get("session_id") or uuid.uuid4().hex[:16])
            user_id = str(body.get("user_id") or USER_ID)
            try:
                data = chat(
                    message, session_id, bool(body.get("use_llm", True)),
                    user_id=user_id,
                )
            except Exception as exc:
                self._json(500, {"code": 500, "message": str(exc), "data": None})
                return
            self._json(200, {"code": 0, "message": "ok", "data": data})
            return
        if self.path == "/api/chat/stream":
            message = str(body.get("message") or "").strip()
            if not message:
                self._json(400, {"code": 400, "message": "缺少 message", "data": None})
                return
            session_id = str(body.get("session_id") or uuid.uuid4().hex[:16])
            user_id = str(body.get("user_id") or USER_ID)
            self._stream_chat(
                message, session_id, bool(body.get("use_llm", True)), user_id
            )
            return
        if self.path == "/api/feedback":
            save_feedback(body)
            self._json(200, {"code": 0, "message": "ok", "data": None})
            return
        self._json(404, {"code": 404, "message": "not found", "data": None})

    def _stream_chat(self, message: str, session_id: str, use_llm: bool,
                     user_id: str):
        """SSE 推送处理状态和通过校验后的最终回答，不直接推送模型原文。"""
        events = queue.Queue()

        def worker():
            try:
                events.put(("status", {
                    "stage": "processing",
                    "text": "正在检索资料并分析数据…",
                }))
                data = chat(
                    message, session_id, use_llm, user_id=user_id
                )
                events.put(("message", data))
                events.put(("done", {
                    "request_id": data.get("request_id", ""),
                }))
            except Exception:
                events.put(("error", {
                    "message": "服务暂时不可用，请稍后再试。",
                }))

        self._sse_start()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    event, payload = events.get(timeout=15)
                except queue.Empty:
                    self._sse_heartbeat()
                    continue
                self._sse_write(event, payload)
                if event in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            return


def make_server(host="127.0.0.1", port=8100):
    return ThreadingHTTPServer((host, port), Handler)


def lan_ip() -> str:
    """取本机局域网 IP，用于提示手机访问地址。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
        sock.close()
        return address
    except OSError:
        return ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="App 内嵌 Demo 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--data-source", choices=("sample", "backend"), default="")
    parser.add_argument("--user-id", default="")
    args = parser.parse_args()

    global DATA_SOURCE, USER_ID
    source_settings = data_source.settings(
        source=args.data_source or None,
        user_id=args.user_id or None,
    )
    DATA_SOURCE = source_settings["source"]
    USER_ID = source_settings["user_id"]
    configure_logging(verbose=args.verbose)
    server = make_server(args.host, args.port)
    print("Demo 已启动")
    print(data_source.describe(user_id=USER_ID, source=DATA_SOURCE))
    print("本机访问: http://127.0.0.1:" + str(args.port) + "/")
    address = lan_ip()
    if address and args.host == "0.0.0.0":
        print("手机访问: http://" + address + ":" + str(args.port) + "/")
    elif args.host != "0.0.0.0":
        print("手机访问需用 --host 0.0.0.0 启动，并使用电脑局域网 IP。")
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
