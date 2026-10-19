"""日志初始化：终端 + 轮转文件。

文件固定写入项目根目录的 `logs/agent.log`，启动时自动创建目录。
终端默认只显示 WARNING；加 `--verbose` 后显示 INFO 请求日志。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "agent.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


def configure_logging(verbose: bool = False, log_file=None) -> Path:
    """配置根日志器，并返回实际日志文件路径。

    重复调用时只替换本模块创建的 handler，不影响测试或宿主程序的 handler。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_sleepagent_managed", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(formatter)
    console._sleepagent_managed = True
    root.addHandler(console)

    path = Path(log_file) if log_file else LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        file_handler._sleepagent_managed = True
        root.addHandler(file_handler)
    except OSError:
        # 日志目录不可写时不能影响主流程，终端 handler 仍然有效。
        pass
    return path


def reset_logging() -> None:
    """移除本模块创建的 handler，供测试和进程内重配置使用。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_sleepagent_managed", False):
            root.removeHandler(handler)
            handler.close()
