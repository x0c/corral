"""活动会话看板：自动铺当前需要盯的托管会话，不写入持久分屏组合。

成员资格：pickup 自己托管、且关注态是等待回答 / 执行中 / 未读新结果。
别的窗口里跑的会话没有实时画面，不进格子。超过一页时当前页成员冻结，
新急件排到后面；格子空出来才从队列按优先级补位。正在打字的那一格即使
已经不够格，也留到用户离开这格再撤。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pickup.attention import AttentionKind, AttentionState
from pickup.split_layout import MAX_PANES

BOARD_KINDS: frozenset[AttentionKind] = frozenset({"waiting", "working", "unread"})
_KIND_RANK: dict[AttentionKind, int] = {"waiting": 0, "working": 1, "unread": 2}


@dataclass(frozen=True)
class BoardCandidate:
    """一条够格进看板的托管会话。"""

    key: str
    kind: AttentionKind
    updated_at: float = 0.0


@dataclass(frozen=True)
class BoardSnapshot:
    """当前页要展示的成员，以及翻页/角标所需的计数。"""

    keys: tuple[str, ...]
    page: int
    page_count: int
    total: int
    waiting_off_page: int
    waiting_total: int


def collect_candidates(store) -> list[BoardCandidate]:
    """从会话库收集够格的托管会话，按「等回话 > 干活 > 未读」再按新鲜度排。"""
    import pickup
    from pickup.models import is_shell_session

    candidates: list[BoardCandidate] = []
    for session in store.all_sessions():
        if is_shell_session(session):
            continue
        if not session.get("keepalive_name"):
            continue
        key = pickup.session_key(session)
        state: AttentionState = store.attention_for(key)
        if state.kind not in BOARD_KINDS:
            continue
        candidates.append(
            BoardCandidate(key=key, kind=state.kind, updated_at=state.updated_at)
        )
    candidates.sort(
        key=lambda item: (_KIND_RANK.get(item.kind, 9), -item.updated_at, item.key)
    )
    return candidates


class ActivityBoard:
    """一次进入看板期间的稳定分页状态。离开看板时 ``reset()``。"""

    def __init__(self) -> None:
        self._page = 0
        self._locked: list[str] = []
        self._skipped: set[str] = set()
        self._typing_key: str | None = None
        self._eligible: list[str] = []

    def reset(self) -> None:
        """离开看板：丢掉本轮冻结页、跳过名单和打字钉住。"""
        self._page = 0
        self._locked = []
        self._skipped.clear()
        self._typing_key = None
        self._eligible = []

    def set_typing_key(self, key: str | None) -> None:
        """正在打字的那一格：不够格也不撤，直到焦点离开。

        本轮已关掉的格子不能再钉住，否则关格后焦点还在那一格时会弹回来。
        """
        if key and key in self._skipped:
            self._typing_key = None
            return
        self._typing_key = key or None

    def dismiss(self, key: str) -> None:
        """本轮访问里把这一格拿掉（关格）；关注态变化或重新进入后再出现。"""
        if not key:
            return
        self._skipped.add(key)
        self._locked = [item for item in self._locked if item != key]
        if self._typing_key == key:
            self._typing_key = None

    def turn_page(self, delta: int) -> None:
        """显式翻页：按当前队列重新切片。打字中不要调。"""
        eligible = [key for key in self._eligible if key not in self._skipped]
        if not eligible:
            self._page = 0
            self._locked = []
            return
        page_count = max(1, math.ceil(len(eligible) / MAX_PANES))
        self._page = max(0, min(self._page + delta, page_count - 1))
        start = self._page * MAX_PANES
        self._locked = eligible[start:start + MAX_PANES]

    def sync(self, candidates: list[BoardCandidate]) -> BoardSnapshot:
        """按「当前页不插队、空位才补」更新锁定成员，返回这一帧快照。

        补位不得把更前页的人拉进本页：队头新插进来的急件算前页，
        翻到后页后空位只从本页已有成员之后的队列取。
        """
        eligible = [
            item.key
            for item in candidates
            if item.key not in self._skipped
        ]
        self._eligible = list(eligible)
        typing = self._typing_key

        if not self._locked:
            self._locked = eligible[:MAX_PANES]
            self._page = 0
        else:
            kept: list[str] = []
            for key in self._locked:
                if key in self._skipped:
                    continue
                if key == typing or key in eligible:
                    kept.append(key)
            # 后页空位不得用「当前队列下标」去切：新急件插到队头后，前页成员
            # 会整体后移，看起来像被补进本页。page>0 时，排在本页已有成员前面
            # 的一律视为更前页/插队，只从本页成员之后的队列补。
            min_locked_pos = None
            if self._page > 0:
                pos = {key: index for index, key in enumerate(eligible)}
                locked_positions = [pos[key] for key in kept if key in pos]
                if locked_positions:
                    min_locked_pos = min(locked_positions)
            for key in eligible:
                if len(kept) >= MAX_PANES:
                    break
                if key in kept:
                    continue
                if min_locked_pos is not None:
                    key_pos = pos.get(key)
                    if key_pos is not None and key_pos < min_locked_pos:
                        continue
                elif self._page > 0:
                    # 本页成员已全部不够格：不要用会错位的 start 下标，留给下面整页重切。
                    continue
                kept.append(key)
            if typing and typing not in kept and typing not in self._skipped:
                if len(kept) < MAX_PANES:
                    kept.append(typing)
                else:
                    kept[-1] = typing
            if not kept and eligible:
                page_count = max(1, math.ceil(len(eligible) / MAX_PANES))
                self._page = min(self._page, page_count - 1)
                kept = eligible[self._page * MAX_PANES:][:MAX_PANES]
            self._locked = kept

        visible = tuple(self._locked)
        total = len(eligible)
        page_count = max(1, math.ceil(total / MAX_PANES)) if total else 1
        if self._page >= page_count:
            self._page = page_count - 1
        waiting_keys = {item.key for item in candidates if item.kind == "waiting"}
        waiting_total = sum(1 for key in eligible if key in waiting_keys)
        waiting_off_page = sum(
            1 for key in eligible if key in waiting_keys and key not in visible
        )
        return BoardSnapshot(
            keys=visible,
            page=self._page,
            page_count=page_count,
            total=total,
            waiting_off_page=waiting_off_page,
            waiting_total=waiting_total,
        )
