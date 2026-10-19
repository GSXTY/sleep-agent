"""一键全量验证：单元测试 + 检索评估。

用法：
    python run_checks.py
    python run_checks.py --verbose     # 附检索评估的未命中清单

退出码：0 表示全部通过，非 0 表示有失败项。
改动后必须跑一次，并核对用例数量——数量对不上说明有测试没被收集到。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(command: list) -> tuple:
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def main() -> int:
    verbose = "--verbose" in sys.argv
    print("=" * 56)
    print("1/2  单元测试")
    print("=" * 56)
    code, output = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    match = re.search(r"Ran (\d+) tests", output)
    count = match.group(1) if match else "?"
    if code == 0:
        print("通过：" + count + " 项")
    else:
        print("失败：" + count + " 项")
        print(output[-2000:])

    print()
    print("=" * 56)
    print("2/2  检索评估")
    print("=" * 56)
    args = [sys.executable, "eval_retrieval.py"]
    if verbose:
        args.append("--verbose")
    eval_code, eval_output = run(args)
    print(eval_output.strip())

    print()
    print("=" * 56)
    print("总体结果：" + ("全部通过" if (code == 0 and eval_code == 0) else "有失败项"))
    print("=" * 56)
    return 0 if (code == 0 and eval_code == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
