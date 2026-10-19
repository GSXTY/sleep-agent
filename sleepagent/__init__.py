"""Windows 命令行版睡眠数据解读助手（基于原项目的设计原则重构，仅使用标准库）。"""

__version__ = "0.1.0"

import logging

# 库代码默认不输出日志；使用方通过 logging.basicConfig 决定级别（如 --verbose）
logging.getLogger(__name__).addHandler(logging.NullHandler())
