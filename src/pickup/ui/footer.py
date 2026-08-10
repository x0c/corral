"""主屏底栏：在 Textual Footer 右端、命令面板键左侧常驻显示本机版本号。

Textual 自带 Footer 把 `^p palette` dock 到最右；多个 `dock: right` 子控件会互相
重叠。这里关掉父类的 palette 键，改把「版本 + palette」放进同一个靠右 Horizontal，
顺序固定为 `vX.Y.Z  ^p palette`。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Label
from textual.widgets._footer import FooterKey

from pickup import __version__


class _FooterRight(Horizontal, can_focus=False, can_focus_children=False):
    """右端版本号 + palette 簇；绝不可聚焦，否则会从侧栏抢走输入蒙版所需的焦点。"""


class PickupFooter(Footer):
    """带版本号的底栏；仍是 Textual `Footer` 子类，既有 `query_one(Footer)` 测例照旧。"""

    DEFAULT_CSS = """
    PickupFooter {
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
        #footer-right FooterKey.-command-palette {
            dock: none;
            border-left: vkey $foreground 20%;
            padding-right: 1;
        }
    }
    """

    def __init__(self, *args, show_command_palette: bool = True, **kwargs) -> None:
        # 父类 compose 不再自画 palette；右侧簇由本类补上，避免双份 dock:right 叠在一起。
        super().__init__(*args, show_command_palette=False, **kwargs)
        self._want_palette = show_command_palette

    def compose(self) -> ComposeResult:
        yield from super().compose()
        if not self._bindings_ready:
            return
        with _FooterRight(id="footer-right"):
            yield Label(f"v{__version__}", id="footer-version")
            if not (self._want_palette and self.app.ENABLE_COMMAND_PALETTE):
                return
            active_bindings = self.screen.active_bindings
            try:
                _node, binding, enabled, tooltip = active_bindings[
                    self.app.COMMAND_PALETTE_BINDING
                ]
            except KeyError:
                return
            yield FooterKey(
                binding.key,
                self.app.get_key_display(binding),
                binding.description,
                binding.action,
                classes="-command-palette",
                disabled=not enabled,
                tooltip=binding.tooltip or binding.description,
            )
