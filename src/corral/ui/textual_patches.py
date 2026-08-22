"""Textual 上游缺口的运行时补丁。

在 ``CorralApp`` 导入时安装一次。补丁必须幂等：测试与多次 import 不得叠
多层包装。
"""

from __future__ import annotations

_installed = False


def install_textual_patches() -> None:
    """安装本模块登记的全部 Textual 补丁（可重复调用）。"""
    global _installed
    if _installed:
        return
    _patch_lru_cache_eviction()
    _installed = True


def _patch_lru_cache_eviction() -> None:
    """Textual ``LRUCache.set`` 驱逐时链表/dict 不同步会 ``KeyError`` 掀掉 TUI。

    真机：``corral`` 长跑（数小时）+ 双分屏 ``PaneCell`` 频繁布局后，
    ``Widget._get_box_model`` 写入 ``_box_model_cache`` 时在
    ``del self._cache[last[2]]`` 炸掉（2026-08-06，v0.24.55，
    ``~/.cache/corral`` 侧 traceback 落在 ``textual/cache.py:126``）。

    上游 8.2.8 仍是裸 ``del``；缓存 miss 只是多算一次 box model，清空后重试
    比退出整个界面划算。
    """
    from textual.cache import LRUCache

    if getattr(LRUCache.set, "_corral_safe_eviction", False):
        return

    original_set = LRUCache.set

    def set_safe(self, key, value):  # noqa: ANN001 - 与上游签名一致
        try:
            original_set(self, key, value)
        except KeyError:
            self.clear()
            original_set(self, key, value)

    set_safe._corral_safe_eviction = True  # type: ignore[attr-defined]
    LRUCache.set = set_safe  # type: ignore[method-assign]
    LRUCache.__setitem__ = set_safe  # type: ignore[method-assign]
