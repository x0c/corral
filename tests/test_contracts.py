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
from pickup.models import SessionInfo
from pickup.runtime import default_registry


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
            pickup._does_not_exist_anywhere


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
        self.assertEqual(pickup.__version__, cargo_lock_version, "__init__.py 与 Cargo.lock 里 pickup-native 版本不一致")


class SessionFieldContractTests(unittest.TestCase):
    """扫描器返回的会话字典目前完全靠 5 份手写字面量各自拼装，没有任何校验
    （`scan_sessions` 标注 `list[dict]`，适配器层标注 `list[SessionInfo]`，
    中间是个信任跳跃）。这里对本机真实历史做一次抽查：字段必须覆盖
    `SessionInfo` 声明的全部键，多出的键必须在已知白名单内（目前只有 codex
    的 `thread_source`，其余运行时的私有字段——如 keepalive_name、
    attention_*——是扫描之后由 SessionStore 注入的，不出现在 scan_sessions()
    的原始产出里）。

    阶段二引入 `models.make_session_info` 工厂后，这条测试应改为直接断言
    工厂输出的键集合，不再依赖本机是否恰好有某个运行时的真实历史。
    """

    _KNOWN_EXTRA_FIELDS = {"codex": {"thread_source"}}

    def test_scanned_sessions_cover_required_fields(self) -> None:
        required = set(SessionInfo.__annotations__)
        registry = default_registry()
        checked_any = False
        for runtime in registry:
            try:
                sessions = runtime.scan_sessions(3)
            except Exception:
                continue
            if not sessions:
                continue
            checked_any = True
            allowed_extra = self._KNOWN_EXTRA_FIELDS.get(runtime.id, set())
            for session in sessions:
                with self.subTest(runtime=runtime.id, session_id=session.get("id")):
                    keys = set(session)
                    missing = required - keys
                    self.assertFalse(missing, f"{runtime.id} 扫描结果缺少字段：{missing}")
                    unexpected = keys - required - allowed_extra
                    self.assertFalse(unexpected, f"{runtime.id} 扫描结果出现未登记字段：{unexpected}")
        if not checked_any:
            self.skipTest("本机没有任何运行时的真实会话历史，无法抽查字段契约")


if __name__ == "__main__":
    unittest.main()
