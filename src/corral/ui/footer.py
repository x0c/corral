"""主屏底栏：在 Textual Footer 右端常驻显示本机版本号。

Textual 自带 Footer 会把命令面板键 dock 到最右；corral 已关闭命令面板，这里只
把版本号放进靠右 Horizontal，避免以后再给右端加控件时和 `dock: right` 叠在一起。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Label

from corral import __version__


class _FooterRight(Horizontal, can_focus=False, can_focus_children=False):
    """右端版本号簇；绝不可聚焦，否则会从侧栏抢走输入蒙版所需的焦点。"""


class CorralFooter(Footer):
    """带版本号的底栏；仍是 Textual `Footer` 子类，既有 `query_one(Footer)` 测例照旧。"""

    DEFAULT_CSS = """
    CorralFooter {
        #footer-right {
            dock: right;
            width: auto;
            height: 1;
            layout: horizontal;
            align: left middle;
        }
        #footer-version {
            width: auto;
            height: 1;
            color: $footer-description-foreground;
            background: $footer-description-background;
            text-style: dim;
            padding: 0 1 0 0;
        }
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs["show_command_palette"] = False
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield from super().compose()
        if not self._bindings_ready:
            return
        with _FooterRight(id="footer-right"):
            yield Label(f"v{__version__}", id="footer-version")
