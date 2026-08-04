"""项目发现与快捷启动 `pickup <runtime> <project>` 分流。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pickup
from pickup import projects
from pickup.models import LaunchPlan


def _temp_root(td: str) -> Path:
    """把临时目录解析成真实路径再用作断言基准。

    **macOS 上不解析会全线假失败**：那里 `/var` 是指向 `/private/var` 的软链，
    `tempfile` 交回来的是 `/var/folders/...`，而 `projects.scan_git_roots` 会
    如实解析成 `/private/var/folders/...`（解析软链是它的既定行为，另有
    `test_scan_resolves_symlink_root` 专门守着），两边字符串对不上。Linux 上
    `/tmp` 不是软链，所以本机怎么跑都不复现。2026-07-31 macOS runner 上一次暴露
    7 个用例，此前一直被 macOS 作业挂死掩盖着没人看见。
    """
    return Path(td).resolve()


def _touch_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)


class GitScanTests(unittest.TestCase):
    def setUp(self) -> None:
        projects.clear_filesystem_cache()

    def tearDown(self) -> None:
        projects.clear_filesystem_cache()

    def test_scan_finds_git_roots_and_skips_nested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "subswap")
            _touch_git(root / "a" / "b" / "LingoWeave")
            _touch_git(root / "subswap" / "vendor" / "nested")  # 应被剪枝

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(
                set(found),
                {str(root / "subswap"), str(root / "a" / "b" / "LingoWeave")},
            )

    def test_scan_skips_stversions_syncthing_snapshots(self) -> None:
        """回归：Syncthing `.stversions` 里的 `.git` 不得当成项目。"""
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            real = root / "Codes" / "subswap"
            snap = root / "Codes" / ".stversions" / "subswap"
            _touch_git(real)
            _touch_git(snap)

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str(real)])

    def test_scan_skips_dot_dirs_and_node_modules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "ok")
            _touch_git(root / "node_modules" / "pkg")
            _touch_git(root / ".cache" / "hidden")

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str(root / "ok")])

    def test_scan_resolves_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            real = root / "real"
            link = root / "link"
            _touch_git(real / "pickup")
            link.symlink_to(real)

            found = projects.scan_git_roots([str(link)], depth=4, use_cache=False)
            self.assertEqual(found, [str((real / "pickup").resolve())])

    def test_scan_follows_symlinked_subdir(self) -> None:
        """回归：`~/Codes -> /elsewhere/Codes` 这类软链接子目录必须照样扫到项目。"""
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            home = root / "home"
            home.mkdir()
            elsewhere = root / "elsewhere" / "Codes"
            _touch_git(elsewhere / "AlphaForge" / "backend")
            (home / "Codes").symlink_to(elsewhere)

            found = projects.scan_git_roots([str(home)], depth=4, use_cache=False)
            # 收录的是真实路径，便于与会话 cwd 去重。
            self.assertEqual(found, [str((elsewhere / "AlphaForge" / "backend").resolve())])

    def test_scan_symlink_cycle_terminates(self) -> None:
        """软链接成环不得死循环。"""
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "repo")
            (root / "loop").symlink_to(root)

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str((root / "repo").resolve())])

    def test_pickup_project_roots_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "only")
            with mock.patch.dict(os.environ, {"PICKUP_PROJECT_ROOTS": str(root)}):
                projects.clear_filesystem_cache()
                found = projects.scan_git_roots(depth=2, use_cache=False)
            self.assertEqual(found, [str(root / "only")])

    def test_empty_pickup_project_roots_skips_filesystem(self) -> None:
        with mock.patch.dict(os.environ, {"PICKUP_PROJECT_ROOTS": ""}):
            self.assertEqual(projects.configured_roots(), [])
            self.assertEqual(projects.scan_git_roots(use_cache=False), [])


class MatchResolveTests(unittest.TestCase):
    def _projects(self, *paths: str) -> list[projects.Project]:
        return projects.discover(paths, scan_filesystem=False)

    def test_fuzzy_case_insensitive_unique(self) -> None:
        items = self._projects("/Codes/SubSwap", "/Codes/pickup")
        matched = projects.match_projects("subswap", items)
        self.assertEqual([p.path for p in matched], ["/Codes/SubSwap"])
        self.assertEqual(projects.resolve_query("SUB", items), "/Codes/SubSwap")

    def test_substring_hits_suppress_subsequence_noise(self) -> None:
        """回归：有子串命中时，不能再把 `java-platform` 这类子序列命中掺进候选。"""
        items = self._projects(
            "/Codes/AlphaForge/backend",
            "/Codes/AlphaForge/client-web",
            "/Codes/LLMPlatform/archive/java-platform",
        )
        matched = projects.match_projects("alpha", items)
        self.assertEqual(
            [p.path for p in matched],
            ["/Codes/AlphaForge/backend", "/Codes/AlphaForge/client-web"],
        )

    def test_subsequence_still_works_without_substring_hit(self) -> None:
        items = self._projects("/Codes/SubSwap")
        self.assertEqual(
            [p.path for p in projects.match_projects("sbswp", items)],
            ["/Codes/SubSwap"],
        )

    def test_zero_matches_raises(self) -> None:
        items = self._projects("/Codes/pickup")
        with self.assertRaises(projects.ProjectResolveError) as ctx:
            projects.resolve_query("nope", items)
        self.assertIn("未找到", str(ctx.exception))

    def test_multiple_matches_interactive_pick(self) -> None:
        items = self._projects("/a/cli", "/b/cli")
        stdin = io.StringIO("2\n")
        stdout = io.StringIO()
        cwd = projects.resolve_query(
            "cli", items, stdin=stdin, stdout=stdout, interactive=True,
        )
        self.assertEqual(cwd, "/b/cli")
        self.assertIn("多个项目匹配", stdout.getvalue())

    def test_multiple_matches_noninteractive_raises(self) -> None:
        items = self._projects("/a/cli", "/b/cli")
        with self.assertRaises(projects.ProjectResolveError) as ctx:
            projects.resolve_query("cli", items, interactive=False)
        self.assertIn("多个项目", str(ctx.exception))


class ProjectEntriesTests(unittest.TestCase):
    def test_merges_git_and_session_stats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "diskonly")
            sessions = {
                "claude": [
                    {"cwd": str(root / "diskonly"), "mtime": 9},
                    {"cwd": str(root / "sessiononly"), "mtime": 5},
                ],
            }
            entries = projects.project_entries(
                sessions, roots=[str(root)], depth=3, use_cache=False,
            )
            by_key = {e["cwd_key"]: e for e in entries}
            self.assertEqual(by_key[str(root / "diskonly")]["count"], 1)
            self.assertEqual(by_key[str(root / "sessiononly")]["count"], 1)
            # 纯 git、无会话的项目 count=0（本例 diskonly 有会话；再造一个）
            _touch_git(root / "puregit")
            entries2 = projects.project_entries(
                sessions, roots=[str(root)], depth=3, use_cache=False,
            )
            pure = next(e for e in entries2 if e["cwd_key"] == str(root / "puregit"))
            self.assertEqual(pure["count"], 0)


class DirectLaunchProjectTests(unittest.TestCase):
    """`pickup claude <project>` 与透传分流。"""

    def setUp(self) -> None:
        projects.clear_filesystem_cache()

    def tearDown(self) -> None:
        projects.clear_filesystem_cache()

    def test_passthrough_when_first_arg_is_flag(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan
        registry.ids = ("claude",)

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            pickup._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        execute_launch.assert_called_once_with(plan)

    def test_project_mode_builds_new_session_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            proj = root / "subswap"
            _touch_git(proj)
            new_plan = LaunchPlan(("claude", "--dangerously-skip-permissions"), str(proj))

            runtime = mock.Mock()
            runtime.build_new_session_plan.return_value = new_plan
            registry = mock.Mock()
            # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
            registry.resolve_id.side_effect = lambda token: token
            registry.get.return_value = runtime
            registry.scan_all.return_value = {"claude": []}
            registry.ids = ("claude",)

            with (
                mock.patch.dict(os.environ, {"PICKUP_PROJECT_ROOTS": str(root)}),
                mock.patch.object(pickup, "keepalive") as keepalive_mock,
                mock.patch.object(pickup, "execute_launch") as execute_launch,
                mock.patch.object(pickup, "_require_tmux"),
                mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
                mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
            ):
                projects.clear_filesystem_cache()
                keepalive_mock.enabled.return_value = False
                keepalive_mock.new_session_ident.return_value = "xxxx"
                pickup._dispatch_direct_launch(["claude", "subswap"], registry)

            registry.get.assert_called_with("claude")
            runtime.build_new_session_plan.assert_called_once_with(str(proj))
            registry.build_passthrough_plan.assert_not_called()
            execute_launch.assert_called_once_with(new_plan)

    def test_opencode_subcommand_is_passthrough_not_project_mode(self) -> None:
        """`pickup opencode run …` 的首词是子命令，必须走透传而不是项目名匹配。

        回归：子命令曾把 run/stats 当成项目名去模糊匹配（实测会匹配出十几个项目、
        要求交互选择），导致 `pickup opencode run 提示` 这种日常用法被拦死。
        """
        plan = LaunchPlan(("opencode", "run", "--auto", "提示"), None)
        registry = mock.Mock()
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            pickup._dispatch_direct_launch(
                ["opencode", "run", "提示"], registry,
            )

        registry.build_passthrough_plan.assert_called_once_with("opencode", ["run", "提示"])
        execute_launch.assert_called_once_with(plan)

    def test_opencode_other_subcommand_is_passthrough_too(self) -> None:
        """`pickup opencode stats` 同样是透传（不带 --auto），不能被当成项目名。"""
        plan = LaunchPlan(("opencode", "stats"), None)
        registry = mock.Mock()
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            pickup._dispatch_direct_launch(["opencode", "stats"], registry)

        registry.build_passthrough_plan.assert_called_once_with("opencode", ["stats"])
        execute_launch.assert_called_once_with(plan)

    def test_project_mode_rejects_extra_args(self) -> None:
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        with (
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys, "stderr", new_callable=io.StringIO) as err,
        ):
            with self.assertRaises(SystemExit) as ctx:
                pickup._dispatch_direct_launch(["claude", "subswap", "extra"], registry)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("不接受额外参数", err.getvalue())
        registry.build_passthrough_plan.assert_not_called()

    def test_no_keepalive_passthrough_with_flag_args(self) -> None:
        plan = LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox", "--resume", "x"), None)
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            pickup._dispatch_direct_launch(
                ["--no-keepalive", "codex", "--resume", "x"], registry,
            )

        registry.build_passthrough_plan.assert_called_once_with("codex", ["--resume", "x"])
        execute_launch.assert_called_once_with(plan)


class LegacyDirectLaunchTestsUpdate(unittest.TestCase):
    """原 DirectLaunchTests 中依赖「裸位置参数透传」的用例改为 flag 透传。"""

    def test_passes_through_flag_args_and_wraps_with_keepalive(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        wrapped = LaunchPlan(("tmux", "-L", "pickup-keepalive", "new-session"), None)
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"
            keepalive_mock.wrap_plan.return_value = wrapped
            pickup._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        keepalive_mock.wrap_plan.assert_called_once_with(plan, "claude", "xxxx")
        execute_launch.assert_called_once_with(wrapped)


if __name__ == "__main__":
    unittest.main()
