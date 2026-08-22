"""可选原生加速层；不可用或被禁用时自动回退纯 Python。

只加速「大量输入压缩成少量结果」的场景——目前仅终端画面解析（一屏文本 →
若干紧凑行元组，实测约 27 倍）。**不要再往这里塞 JSON 解析之类产出物本身
就是一大棵 Python 对象树的活**：Rust 侧得先解析成中间对象树、再逐节点转成
Python 对象，同一份数据构建两遍，实测比标准库 C 实现的 json 慢约 2.5 倍。
历史教训与实测数据见 docs/PERFORMANCE_KNOWLEDGE_BASE.md。
"""

from __future__ import annotations

from corral.legacy_names import getenv

_disabled = (getenv("NATIVE", "1") or "1").strip().lower() in {"0", "false", "no", "off"}
try:
    if _disabled:
        raise ImportError("已通过 CORRAL_NATIVE 禁用")
    from corral import _native as _extension
except ImportError:
    _extension = None


def available() -> bool:
    return _extension is not None


def parse_ansi_rows(text: str, width: int, height: int):
    if _extension is None:
        return None
    return _extension.parse_ansi_rows(text, width, height)
