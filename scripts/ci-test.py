#!/usr/bin/env python3
"""CI 用的检查入口：先 lint，再跑全量 unittest，并解决两个只在 CI 上要命的问题。

**〇、lint 必须和 CI 同源，不能只跑单测。**
GitHub Actions 在单测前先跑 `ruff check src tests`（固定 `ruff==0.16.1`）。
以前本脚本只管 unittest，发版机只跑 `ci-test.py` 会绿、推上去却在 Lint
步全矩阵报红——2026-08-07 起 `v0.24.57`～`v0.24.65` 每次推送都因
`tests/test_cache.py` 一处 import 排序（I001）发失败邮件，单测根本没跑到。
把 ruff 收进本入口后，本机绿 ≈ CI 绿。

**一、挂死要能自曝位置，不能干等到作业上限。**
2026-07-30 macOS runner 上真实发生过：单测跑到一半卡住，GitHub 作业没有配
timeout，于是整整占着 runner 6 小时直到被平台按上限杀掉。免费额度的 macOS
并发本来就少，两个这样的僵尸作业能把后面所有排队任务拖到十几小时，连带一片
「cancelled」。这里到点用 faulthandler 把**所有线程的栈**打出来再退出——日志
里直接能看到卡在哪个用例的哪一行，而不是只剩一句「The operation was canceled」。

**二、已知的 Textual Pilot 偶发不该变成失败邮件。**
`AGENTS.md` 与 `docs/MAINTAINER_GUIDE.md` 早就写明：涉及真实 tmux 回显与 Pilot
等待的用例在负载高的机器上会假失败，判定方法是**把失败用例单独重跑**。这段
判断以前只写在文档里靠人执行，CI 仍旧一失败就发邮件。这里把它固化下来：失败
用例自动单独重跑一次，两次都失败才算真回归。真回归是确定性的，重跑照样挂，
不会被这层重试掩盖；而单次偶发（实测 `test_focusing_split_pane_highlights_
matching_sidebar_session` 约十次一遇）不再污染 CI 结论。

用法与 CI 的 Lint+Test 两步等价，退出码同语义。

``--lint-only``：只跑 ruff（推送前门禁用，几秒级）；完整入口留给发版推送与
``publish-release.sh``。

``--check-stamp``：本工作区产品代码是否刚跑过完整检查（给推送门禁和收尾脚本
用来跳过重复，不跑 lint/单测）。
"""
from __future__ import annotations

import argparse
import faulthandler
import os
import subprocess
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import ci_stamp

ROOT = _SCRIPTS_DIR.parent
_SRC = str(ROOT / "src")
# 本机 `python3` 常常没有装过 corral；子进程 `python -m corral` 也要找得到包。
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_existing = os.environ.get("PYTHONPATH", "")
if _SRC not in _existing.split(os.pathsep):
    os.environ["PYTHONPATH"] = _SRC if not _existing else _SRC + os.pathsep + _existing

# 单个作业的硬上限（秒）。取值要明显小于 CI 作业自身的 timeout-minutes，
# 才能保证「先由我们打出栈」而不是「先被平台静默杀掉」。
HANG_DUMP_SECONDS = int(os.environ.get("CORRAL_TEST_HANG_SECONDS", "1500"))
# 与 `.github/workflows/test.yml` 的 Lint 步保持同一版本，避免规则集漂移。
RUFF_VERSION = "0.16.1"


def _collect_ids(result: unittest.TestResult) -> list[str]:
    """取出本轮失败/出错的用例 id，用于精确重跑。"""
    ids: list[str] = []
    for test, _ in list(result.failures) + list(result.errors):
        test_id = getattr(test, "id", None)
        if test_id is None:
            return []  # 拿不到 id（如加载期错误）就别重跑，直接判失败
        ids.append(test_id())
    return ids


def _run_ruff() -> int:
    """跑与 CI 相同的 ruff 检查；优先 PATH / `python -m ruff`，不硬装系统包。"""
    candidates = (
        ["ruff"],
        [sys.executable, "-m", "ruff"],
    )
    for prefix in candidates:
        probe = subprocess.run(
            [*prefix, "--version"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and RUFF_VERSION in (probe.stdout or ""):
            print("=== ruff check src tests ===")
            return subprocess.run([*prefix, "check", "src", "tests"]).returncode
    print(
        f"错误：未找到 ruff=={RUFF_VERSION}（与 CI Lint 步一致）。\n"
        f"  macOS: brew install ruff\n"
        f"  其它:  python3 -m pip install ruff=={RUFF_VERSION}",
        file=sys.stderr,
    )
    return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="与 CI 同源的 lint + 单测入口")
    parser.add_argument(
        "--lint-only",
        action="store_true",
        help="只跑 ruff check（推送前门禁）；不加则再跑全量单测",
    )
    parser.add_argument(
        "--check-stamp",
        action="store_true",
        help="只判断本工作区是否刚跑过完整检查（不跑 lint/单测）",
    )
    return parser.parse_args(argv)


def _record_success() -> None:
    ci_stamp.write_stamp(ROOT)
    print("=== 已记下完整检查戳（后续推送/收尾若产品代码未改则跳过重复）===")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check_stamp:
        if ci_stamp.stamp_matches(ROOT):
            print("完整检查戳有效")
            return 0
        print("完整检查戳缺失或产品代码已改", file=sys.stderr)
        return 1

    lint_code = _run_ruff()
    if lint_code != 0:
        return lint_code
    if args.lint_only:
        return 0

    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir="tests")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        _record_success()
        return 0

    flaky = _collect_ids(result)
    if not flaky:
        return 1

    print(f"\n=== 首轮 {len(flaky)} 个用例失败，按既定判定路径单独重跑一次 ===")
    for test_id in flaky:
        print(f"  - {test_id}")
    retry = runner.run(loader.loadTestsFromNames(flaky))
    if retry.wasSuccessful():
        print("\n=== 重跑全部通过，判定为已知偶发（非回归） ===")
        _record_success()
        return 0
    print("\n=== 重跑仍失败，判定为真回归 ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
