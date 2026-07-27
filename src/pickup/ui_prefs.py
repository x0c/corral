"""壳层 UI 偏好：侧边栏显隐等，持久化到 ~/.cache/pickup/ui-prefs.json。"""

from __future__ import annotations

import json
import os
import uuid

from pickup.titles import CACHE_DIR

PREFS_FILE = os.path.join(CACHE_DIR, "ui-prefs.json")
PREFS_VERSION = 1


def load_sidebar_visible(*, default: bool = True) -> bool:
    """读取侧边栏是否可见；文件缺失或损坏时回落 default。"""
    try:
        with open(PREFS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    value = data.get("sidebar_visible")
    if isinstance(value, bool):
        return value
    return default


def save_sidebar_visible(visible: bool) -> None:
    """原子写入侧边栏可见性。"""
    payload = {
        "version": PREFS_VERSION,
        "sidebar_visible": bool(visible),
    }
    parent = os.path.dirname(PREFS_FILE) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{PREFS_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, PREFS_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
