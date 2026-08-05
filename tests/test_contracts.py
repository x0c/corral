"""跨模块散落约定的自动化守卫。

这里守的三条约定，`AGENTS.md` 都明确写过「改错了不报错，只会在运行期/发布后
才暴露」：包顶层兼容导出映射表、四处版本号必须一致、扫描器产出的会话字典字段
契约。三条都不属于任何单个模块的单测范畴，因此单独成文件。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import pickup
from pickup import updater
from pickup.models import SessionInfo, make_session_info


class CompatExportsTests(unittest.TestCase):
    """`pickup/__init__.py` 的 `__getattr__` 兼容导出表：45 条全手工维护，
    移动/重命名符号时同步漏改不会报错，只会让 `pickup.X` 形式的老调用方在运行期
    才抛 `AttributeError`（见 `cli/AGENTS.md`「入口分层与包顶层的兼容导出」）。
    这里逐条实际取一次属性，代替只读表不求值的静态检查。
    """

    def test_all_module_exports_resolve(self) -> None:
        for name in sorted(pickup._MODULE_EXPORTS):
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(pickup, name))

    def test_all_standard_module_exports_resolve(self) -> None:
        for name in sorted(pickup._STANDARD_MODULE_EXPORTS):
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(pickup, name))

    def test_all_standard_symbol_exports_resolve(self) -> None:
        for name in sorted(pickup._STANDARD_SYMBOL_EXPORTS):
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(pickup, name))

    def test_all_symbol_exports_resolve(self) -> None:
        for name in sorted(pickup._SYMBOL_EXPORTS):
            with self.subTest(name=name):
                # 不用 assertIsNotNone：导出目标本身可能合法地是 None 兜底值
                # （目前没有，但不应该因未来出现而误判失败）；关键断言是
                # getattr 不抛异常，且 __dir__ 里确实登记了这个名字。
                getattr(pickup, name)
        self.assertTrue(set(pickup._SYMBOL_EXPORTS).issubset(set(dir(pickup))))

    def test_unknown_attribute_still_raises(self) -> None:
        with self.assertRaises(AttributeError):
            pickup._does_not_exist_anywhere  # noqa: B018 - 刻意求值触发 __getattr__


class VersionConsistencyTests(unittest.TestCase):
    """版本号散落 `pyproject.toml` / `Cargo.toml` / `Cargo.lock` / `__init__.py`
    四处，发布脚本只读取 `pyproject.toml` 一处、不做交叉校验；Python 公共规范
    要求「多处版本号必须一次改齐，并有测试或脚本兜住一致性」（见 `~/Codes/_standards/python.md`）。

    只在源码检出树里跑：装好的发行版副本没有 `Cargo.toml`/`Cargo.lock`，
    `find_checkout_root()` 会返回 None，此时跳过而不是误判失败。
    """

    def setUp(self) -> None:
        root = updater.find_checkout_root()
        if root is None:
            self.skipTest("当前不是源码检出树（没有 pyproject.toml + src/pickup），跳过版本一致性校验")
        self.root = Path(root)

    @staticmethod
    def _extract(text: str, pattern: str) -> str:
        match = re.search(pattern, text, re.M)
        assert match is not None, f"未匹配到版本号：pattern={pattern!r}"
        return match.group(1)

    def test_versions_match_across_files(self) -> None:
        pyproject_version = self._extract(
            (self.root / "pyproject.toml").read_text(encoding="utf-8"),
            r'^version = "([^"]+)"',
        )
        cargo_toml_version = self._extract(
            (self.root / "Cargo.toml").read_text(encoding="utf-8"),
            r'^version = "([^"]+)"',
        )
        cargo_lock_text = (self.root / "Cargo.lock").read_text(encoding="utf-8")
        cargo_lock_version = self._extract(
            cargo_lock_text,
            r'name = "pickup-native"\nversion = "([^"]+)"',
        )

        self.assertEqual(pickup.__version__, pyproject_version, "__init__.py 与 pyproject.toml 版本不一致")
        self.assertEqual(pickup.__version__, cargo_toml_version, "__init__.py 与 Cargo.toml 版本不一致")
        self.assertEqual(
            pickup.__version__, cargo_lock_version,
            "__init__.py 与 Cargo.lock 里 pickup-native 版本不一致",
        )


class SessionFieldContractTests(unittest.TestCase):
    """会话字段契约现在由 `models.make_session_info` 工厂统一收口（5 个扫描器
    的 `_build_session_info` 都改为调用它，见 `scan/{claude,codex,kimi,cursor,
    opencode}.py`），这里直接断言工厂输出的键集合，不再依赖本机是否恰好有
    某个运行时的真实历史——工厂本身就是全部扫描器共用的唯一产出路径，测它
    等价于测全部 5 个扫描器的字段契约。
    """

    _MINIMAL_KWARGS = dict(
        source="claude",
        id="s1",
        short_id="s1",
        cwd="/tmp/proj",
        mtime=1000.0,
        time_source="file_mtime",
        event_time=999.0,
        file_mtime=1000.0,
        size_bytes=100,
        native_title=None,
        fallback_title="fallback",
        status_tag="pending",
        path="/tmp/proj/s1.jsonl",
    )

    def test_factory_output_covers_exactly_the_required_fields(self) -> None:
        session = make_session_info(**self._MINIMAL_KWARGS)
        self.assertEqual(set(session), SessionInfo.__required_keys__)

    def test_factory_extra_kwargs_land_in_optional_fields(self) -> None:
        """`**extra` 收的运行时私有字段（如 codex 的 thread_source）必须落在
        `SessionInfo` 声明的可选字段集合内，不能是随手拼错的野字段。"""
        session = make_session_info(**self._MINIMAL_KWARGS, thread_source="subagent")
        self.assertEqual(set(session), SessionInfo.__required_keys__ | {"thread_source"})
        self.assertIn("thread_source", SessionInfo.__optional_keys__)

    def test_first_user_last_user_last_agent_msg_truncated_to_300_chars(self) -> None:
        session = make_session_info(
            **self._MINIMAL_KWARGS,
            first_user_msg="a" * 400,
            last_user_msg="b" * 400,
            last_agent_msg="c" * 400,
        )
        self.assertEqual(len(session["first_user_msg"]), 300)
        self.assertEqual(len(session["last_user_msg"]), 300)
        self.assertEqual(len(session["last_agent_msg"]), 300)

    def test_live_and_pid_default_to_scan_time_placeholders(self) -> None:
        """`live`/`pid` 由各运行时的 `scan_sessions()` 事后按判活逻辑回填，
        工厂只负责给出安全占位值。"""
        session = make_session_info(**self._MINIMAL_KWARGS)
        self.assertIs(session["live"], False)
        self.assertIsNone(session["pid"])


if __name__ == "__main__":
    unittest.main()
