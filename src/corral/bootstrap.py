"""极轻量命令分发：快速命令不加载 Textual、网络更新器或会话扫描器。"""

from __future__ import annotations

import os
import sys

from corral import __version__

_AGENT_ROOTS = {"list", "search", "show", "export", "share", "context", "plan", "describe", "diagnose"}


def _fast_version() -> None:
    import corral

    package_file = os.path.abspath(corral.__file__ or "")
    print(f"corral {__version__}")
    print(f"  package_file: {package_file}")
    print(f"  python:       {sys.executable}")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in {"--version", "-V", "-v"}:
        _fast_version()
        return
    if argv[:1] == ["cache"]:
        from corral.cache_cli import main as cache_main

        raise SystemExit(cache_main(argv[1:]))
    if argv and argv[0] in _AGENT_ROOTS:
        from corral.agent_api import dispatch

        raise SystemExit(dispatch(argv))
    if argv[:1] == ["update"]:
        from corral.updater import cli_update

        raise SystemExit(cli_update())
    if argv[:1] == ["remote"]:
        # 手机端接力的常驻服务与配对命令：不进 TUI，也不碰只读接口。
        from corral.remote.cli import main as remote_main

        raise SystemExit(remote_main(argv[1:]))
    if argv and argv[0] in {"login", "logout", "whoami"}:
        from corral.remote.cli import main as remote_main

        raise SystemExit(remote_main(argv))
    if argv[:1] == ["shim"]:
        # 命令拦截的安装/卸载/检查只读写 shell 配置，不碰扫描器、Textual 和 tmux。
        from corral.shim import cli_main as shim_main

        raise SystemExit(shim_main(argv[1:]))
    # 只有真人正在终端里使用 corral 时才静默补齐拦截；Agent 只读接口、管道和版本查询不写配置。
    if sys.stdin.isatty() and sys.stdout.isatty():
        from corral.shim import auto_install

        auto_install()
    from corral.cli import main as cli_main

    cli_main()
