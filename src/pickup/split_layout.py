"""会话组与侧边栏置顶状态：同一项目下最多三格，切走再回来自动恢复同伴。

持久化到 ~/.cache/pickup/split-layout.json，原子写（模式同 titles.save_cache）。
会话结束后仍保留分组；启动恢复时只重新打开仍在运行的成员。
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from pickup.titles import CACHE_DIR

LAYOUT_FILE = os.path.join(CACHE_DIR, "split-layout.json")
LAYOUT_VERSION = 2
MAX_PANES = 3

_FRUIT_NAMES = (
    "Apple",
    "Apricot",
    "Avocado",
    "Banana",
    "Cherry",
    "Coconut",
    "Fig",
    "Grape",
    "Guava",
    "Kiwi",
    "Lemon",
    "Lime",
    "Mango",
    "Melon",
    "Orange",
    "Papaya",
    "Peach",
    "Pear",
    "Pineapple",
    "Plum",
)


def _timestamp(value: object) -> float:
    """把持久化时间转成可排序数值；损坏值按最旧处理。"""
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class SplitGroup:
    """同一项目下的一组会话；成员结束后仍保留关系。"""

    group_id: str
    project_cwd: str
    session_keys: list[str]
    focus_key: str | None = None
    name: str = ""
    collapsed: bool = False
    updated_at: float = 0.0


@dataclass
class SplitLayoutStore:
    """内存中的分屏布局；读写磁盘时整体序列化。"""

    version: int = LAYOUT_VERSION
    last_project: str = ""
    last_focus_key: str = ""
    groups: dict[str, SplitGroup] = field(default_factory=dict)
    session_to_group: dict[str, str] = field(default_factory=dict)
    pinned_session_keys: dict[str, float] = field(default_factory=dict)
    pinned_group_ids: dict[str, float] = field(default_factory=dict)

    def get_group(self, session_key: str) -> SplitGroup | None:
        gid = self.session_to_group.get(session_key)
        if not gid:
            return None
        return self.groups.get(gid)

    def group_session_keys(self, session_key: str) -> list[str]:
        group = self.get_group(session_key)
        if group is None:
            return [session_key]
        return list(group.session_keys)

    def ordered_groups(self) -> list[SplitGroup]:
        """按最近创建或使用时间返回会话组，最新的组排在侧边栏最上方。"""
        return sorted(
            self.groups.values(),
            key=lambda group: (group.updated_at, group.group_id),
            reverse=True,
        )

    def set_collapsed(self, group_id: str, collapsed: bool) -> bool:
        """更新侧边栏折叠态；实际变化时返回 True。"""
        group = self.groups.get(group_id)
        if group is None or group.collapsed == collapsed:
            return False
        group.collapsed = collapsed
        return True

    def toggle_session_pin(self, session_key: str) -> bool:
        """切换独立会话置顶状态，返回切换后的状态。"""
        if session_key in self.pinned_session_keys:
            del self.pinned_session_keys[session_key]
            return False
        self.pinned_session_keys[session_key] = time.time()
        return True

    def toggle_group_pin(self, group_id: str) -> bool:
        """切换整个会话组置顶状态，返回切换后的状态。"""
        if group_id in self.pinned_group_ids:
            del self.pinned_group_ids[group_id]
            return False
        if group_id not in self.groups:
            return False
        self.pinned_group_ids[group_id] = time.time()
        return True

    def set_group(
        self,
        project_cwd: str,
        session_keys: list[str],
        *,
        focus_key: str | None = None,
    ) -> None:
        """写入或更新组合；session_keys 去重保序，最多 MAX_PANES 个。"""
        keys: list[str] = []
        seen: set[str] = set()
        for key in session_keys:
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
        if len(keys) < 2:
            return
        if len(keys) > MAX_PANES:
            keys = keys[:MAX_PANES]
        focus = focus_key if focus_key in keys else keys[0]
        # 改动现有分屏（增格、换成员）时沿用原组名；全新组合才生成新身份。
        existing_gid = next(
            (self.session_to_group[key] for key in keys if key in self.session_to_group),
            None,
        )
        existing = self.groups.get(existing_gid) if existing_gid else None
        self._drop_sessions_from_other_groups(keys, keep_gid=existing_gid)
        for key in keys:
            # 进入会话组后只能整体置顶，旧的单会话置顶状态不再生效。
            self.pinned_session_keys.pop(key, None)
        gid = existing_gid or str(uuid.uuid4())
        if existing is None:
            existing = SplitGroup(
                group_id=gid,
                project_cwd=project_cwd,
                session_keys=keys,
                focus_key=focus,
                name=self._new_group_name(),
            )
            self.groups[gid] = existing
        else:
            existing.project_cwd = project_cwd
            existing.session_keys = keys
            existing.focus_key = focus
            if not existing.name:
                existing.name = self._new_group_name()
        existing.updated_at = time.time()
        self.last_project = project_cwd
        self.last_focus_key = focus or ""
        self._reindex_group(gid)

    def migrate_session_key(self, old_key: str, new_key: str) -> None:
        """占位卡转正或重扫后会话键变化时，把分屏记忆从旧键迁到新键。"""
        if not old_key or not new_key or old_key == new_key:
            return
        pinned_at = self.pinned_session_keys.pop(old_key, None)
        if pinned_at is not None:
            self.pinned_session_keys[new_key] = pinned_at
        gid = self.session_to_group.get(old_key)
        if not gid:
            return
        group = self.groups.get(gid)
        if group is None:
            return
        seen: set[str] = set()
        migrated: list[str] = []
        for key in group.session_keys:
            mapped = new_key if key == old_key else key
            if mapped not in seen:
                migrated.append(mapped)
                seen.add(mapped)
        group.session_keys = migrated[:MAX_PANES]
        if group.focus_key == old_key:
            group.focus_key = new_key
        if self.last_focus_key == old_key:
            self.last_focus_key = new_key
        if len(group.session_keys) < 2:
            self._delete_group(gid)
            return
        self._drop_sessions_from_other_groups(group.session_keys)
        self._reindex_group(gid)

    def remove_session(self, session_key: str) -> None:
        """从组合中移除单个会话；不足两个成员时解散组。"""
        self.pinned_session_keys.pop(session_key, None)
        gid = self.session_to_group.pop(session_key, None)
        if not gid:
            return
        group = self.groups.get(gid)
        if group is None:
            return
        group.session_keys = [k for k in group.session_keys if k != session_key]
        if len(group.session_keys) < 2:
            self._delete_group(gid)
            return
        if group.focus_key == session_key:
            group.focus_key = group.session_keys[0]
        group.updated_at = time.time()
        self._reindex_group(gid)

    def prune_inactive(self, is_active: Callable[[str], bool]) -> None:
        """剔除不再活跃/托管的会话键。"""
        dead: list[str] = []
        for key in list(self.session_to_group):
            if not is_active(key):
                dead.append(key)
        for key in dead:
            self.remove_session(key)

    def _drop_sessions_from_other_groups(
        self, keys: list[str], *, keep_gid: str | None = None,
    ) -> None:
        """新组合写入前，把这些键从旧组摘掉（避免一键多组）。"""
        for key in keys:
            old_gid = self.session_to_group.get(key)
            if not old_gid or old_gid == keep_gid:
                continue
            group = self.groups.get(old_gid)
            if group is None:
                continue
            if set(group.session_keys) == set(keys):
                continue
            group.session_keys = [k for k in group.session_keys if k != key]
            if len(group.session_keys) < 2:
                self._delete_group(old_gid)
            else:
                if group.focus_key == key:
                    group.focus_key = group.session_keys[0]
                self._reindex_group(old_gid)

    def _delete_group(self, gid: str) -> None:
        """删除组及其全部反向索引，不删除任何会话。"""
        self.groups.pop(gid, None)
        self.pinned_group_ids.pop(gid, None)
        for key in list(self.session_to_group):
            if self.session_to_group.get(key) == gid:
                del self.session_to_group[key]

    def _new_group_name(self) -> str:
        """生成当前布局内不重名的水果组名。"""
        used = {group.name for group in self.groups.values() if group.name}
        available = [
            f"Group {fruit}"
            for fruit in _FRUIT_NAMES
            if f"Group {fruit}" not in used
        ]
        if available:
            return random.SystemRandom().choice(available)
        index = 2
        while f"Group Apple {index}" in used:
            index += 1
        return f"Group Apple {index}"

    def _reindex_group(self, gid: str) -> None:
        group = self.groups.get(gid)
        if group is None:
            return
        for key in list(self.session_to_group):
            if self.session_to_group.get(key) == gid and key not in group.session_keys:
                del self.session_to_group[key]
        for key in group.session_keys:
            self.session_to_group[key] = gid


def load_layout() -> SplitLayoutStore:
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return SplitLayoutStore()
    if not isinstance(raw, dict):
        return SplitLayoutStore()
    store = SplitLayoutStore(version=int(raw.get("version") or LAYOUT_VERSION))
    store.last_project = str(raw.get("last_project") or "")
    store.last_focus_key = str(raw.get("last_focus_key") or "")
    pinned_sessions = raw.get("pinned_session_keys") or {}
    if isinstance(pinned_sessions, dict):
        store.pinned_session_keys = {
            str(key): _timestamp(value)
            for key, value in pinned_sessions.items()
            if key
        }
    pinned_groups = raw.get("pinned_group_ids") or {}
    if isinstance(pinned_groups, dict):
        store.pinned_group_ids = {
            str(key): _timestamp(value)
            for key, value in pinned_groups.items()
            if key
        }
    groups_raw = raw.get("groups") or {}
    if isinstance(groups_raw, dict):
        for gid, g in groups_raw.items():
            if not isinstance(g, dict):
                continue
            keys = g.get("session_keys") or []
            if not isinstance(keys, list):
                continue
            session_keys = [str(k) for k in keys if k][:MAX_PANES]
            if len(session_keys) < 2:
                continue
            store.groups[str(gid)] = SplitGroup(
                group_id=str(gid),
                project_cwd=str(g.get("project_cwd") or ""),
                session_keys=session_keys,
                focus_key=str(g["focus_key"]) if g.get("focus_key") else None,
                name=str(g.get("name") or ""),
                collapsed=bool(g.get("collapsed", False)),
                updated_at=_timestamp(g.get("updated_at")),
            )
    # 旧版布局没有名字；迁移时按现有顺序分配一次，下一次写盘后保持稳定。
    for group in store.groups.values():
        if not group.name:
            group.name = store._new_group_name()
    # 磁盘上的 session_to_group 可能陈旧或与 groups 矛盾；一律以 groups 重建。
    store.session_to_group.clear()
    for gid in store.groups:
        store._reindex_group(gid)
    store.pinned_group_ids = {
        gid: pinned_at
        for gid, pinned_at in store.pinned_group_ids.items()
        if gid in store.groups
    }
    for key in list(store.pinned_session_keys):
        if key in store.session_to_group:
            del store.pinned_session_keys[key]
    return store


def save_layout(store: SplitLayoutStore) -> None:
    """原子写分屏布局文件。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    payload = {
        "version": store.version,
        "last_project": store.last_project,
        "last_focus_key": store.last_focus_key,
        "pinned_session_keys": store.pinned_session_keys,
        "pinned_group_ids": store.pinned_group_ids,
        "groups": {
            gid: {
                "project_cwd": g.project_cwd,
                "session_keys": g.session_keys,
                "focus_key": g.focus_key,
                "name": g.name,
                "collapsed": g.collapsed,
                "updated_at": g.updated_at,
            }
            for gid, g in store.groups.items()
        },
        "session_to_group": dict(store.session_to_group),
    }
    tmp_path = LAYOUT_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, LAYOUT_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def resolve_active_group(
    store: SplitLayoutStore,
    session_key: str,
    *,
    is_active: Callable[[str], bool],
    find_session: Callable[[str], dict | None],
) -> tuple[str, list[str]]:
    """解析选中会话应恢复的分屏组合。

    返回 (project_cwd, ordered_session_keys)；同伴已不活跃则降级为单格。
    """
    group = store.get_group(session_key)
    if group is None:
        session = find_session(session_key)
        project = ""
        if session:
            from pickup.display import _normalize_cwd

            project = _normalize_cwd(session.get("cwd"))
        return project, [session_key]
    alive = [k for k in group.session_keys if is_active(k)]
    if session_key not in alive:
        alive = [session_key] if is_active(session_key) else []
    if not alive:
        return group.project_cwd, [session_key]
    if session_key in alive and len(alive) == 1:
        return group.project_cwd, alive
    # 保持原顺序，只留活跃成员
    ordered = [k for k in group.session_keys if k in alive]
    if session_key in ordered:
        # 聚焦项不变，顺序保持
        pass
    elif session_key in alive:
        ordered = [session_key] + [k for k in ordered if k != session_key]
    return group.project_cwd, ordered or [session_key]
