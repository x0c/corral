"""活跃会话看板：自动铺当前需要盯的托管会话，不写入持久分屏组合。

成员资格：pickup 自己托管、且关注态是等待回答 / 执行中 / 未读新结果，或
「刚刚」（与侧栏时间行同一条 3 分钟界）内还有真实对话活动。别的窗口里跑的
会话没有实时画面，不进格子。超过一页时当前页成员冻结，
新急件排到后面；格子空出来才从队列按优先级补位。正在看的那一格即使
已经不够格，也留到用户离开这格再撤。当前页里刚不够格的格子先暂留一会儿，
避免会话刚结束就抽走、整页跟着跳。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from pickup.attention import AttentionKind, AttentionState
from pickup.display import JUST_NOW_SECONDS
from pickup.split_layout import MAX_PANES

BOARD_KINDS: frozenset[AttentionKind] = frozenset({"waiting", "working", "unread"})
BOARD_LINGER_SECONDS = 30.0
_KIND_RANK: dict[AttentionKind, int] = {
    "waiting": 0,
    "working": 1,
    "unread": 2,
    # 「刚刚还在用」档：没有待办信号，只是最近仍在活动，排在三档待办之后。
    "none": 3,
}
# 「刚刚还在活跃」与侧栏时间行「刚刚」共用同一条界（display.JUST_NOW_SECONDS）
# 和同一时间源（会话最近真实活动时间）：侧栏显示「刚刚」的托管会话，看板也认活跃。
RECENT_ACTIVE_SECONDS = JUST_NOW_SECONDS


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


def collect_candidates(store, now: float | None = None) -> list[BoardCandidate]:
    """从会话库收集够格的托管会话，按「等回话 > 干活 > 未读 > 刚刚活跃」排。

    「刚刚活跃」档没有待办信号：会话最近真实活动（mtime，与侧栏「刚刚」文案
    同源）还在 ``RECENT_ACTIVE_SECONDS`` 窗口内即算，覆盖用户正在正常使用、
    但本轮既没在等回话也没未读的托管会话。未来时间 / 时钟漂移出的负差值按
    刚刚活跃处理（与 display 的相对时间同规则）。
    """
    import pickup
    from pickup.models import is_shell_session

    if now is None:
        now = time.time()
    candidates: list[BoardCandidate] = []
    for session in store.all_sessions():
        if is_shell_session(session):
            continue
        if not session.get("keepalive_name"):
            continue
        key = pickup.session_key(session)
        state: AttentionState = store.attention_for(key)
        mtime = float(session.get("mtime") or 0.0)
        if state.kind not in BOARD_KINDS:
            if not mtime or now - mtime > RECENT_ACTIVE_SECONDS:
                continue
        candidates.append(
            BoardCandidate(
                key=key,
                kind=state.kind if state.kind in BOARD_KINDS else "none",
                updated_at=max(state.updated_at, mtime),
            )
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
        self._linger_until: dict[str, float] = {}

    def reset(self) -> None:
        """离开看板：丢掉本轮冻结页、跳过名单、打字钉住和暂留。"""
        self._page = 0
        self._locked = []
        self._skipped.clear()
        self._typing_key = None
        self._eligible = []
        self._linger_until.clear()

    def set_typing_key(self, key: str | None) -> None:
        """正在看的那一格：不够格也不撤，直到焦点离开。

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
        self._linger_until.pop(key, None)
        if self._typing_key == key:
            self._typing_key = None

    def next_linger_deadline(self) -> float | None:
        """当前页最早一条暂留到期时间；没有暂留则返回 None。"""
        if not self._linger_until:
            return None
        return min(self._linger_until.values())

    def _refresh_linger(
        self,
        eligible_set: set[str],
        typing: str | None,
        now: float,
    ) -> set[str]:
        """当前页刚不够格的成员暂留到到期；重新够格、关掉、翻走或到期则清掉。

        到期的条目必须拿掉，不能因为还在当前页就再续一轮——否则格子永远不撤。
        """
        previous = self._linger_until
        still: dict[str, float] = {}
        for key, deadline in previous.items():
            if key in self._skipped or key in eligible_set or key == typing:
                continue
            if key not in self._locked:
                continue
            if deadline > now:
                still[key] = deadline
        for key in self._locked:
            if key in self._skipped or key in eligible_set or key == typing:
                continue
            if key in still or key in previous:
                continue
            still[key] = now + BOARD_LINGER_SECONDS
        self._linger_until = still
        return set(still)

    def turn_page(self, delta: int) -> None:
        """显式翻页：按当前队列重新切片。打字中不要调。"""
        eligible = [key for key in self._eligible if key not in self._skipped]
        if not eligible:
            self._page = 0
            self._locked = []
            self._linger_until.clear()
            return
        page_count = max(1, math.ceil(len(eligible) / MAX_PANES))
        self._page = max(0, min(self._page + delta, page_count - 1))
        start = self._page * MAX_PANES
        self._locked = eligible[start:start + MAX_PANES]
        locked = set(self._locked)
        self._linger_until = {
            key: deadline
            for key, deadline in self._linger_until.items()
            if key in locked
        }

    def sync(
        self,
        candidates: list[BoardCandidate],
        now: float | None = None,
    ) -> BoardSnapshot:
        """按「当前页不插队、空位才补」更新锁定成员，返回这一帧快照。

        补位不得把更前页的人拉进本页：队头新插进来的急件算前页，
        翻到后页后空位只从本页已有成员之后的队列取。当前页刚不够格的
        成员先暂留，到期或显式关格后再让位。
        """
        if now is None:
            now = time.monotonic()
        eligible = [
            item.key
            for item in candidates
            if item.key not in self._skipped
        ]
        self._eligible = list(eligible)
        eligible_set = set(eligible)
        typing = self._typing_key
        lingering = self._refresh_linger(eligible_set, typing, now)

        if not self._locked:
            self._locked = eligible[:MAX_PANES]
            self._page = 0
        else:
            kept: list[str] = []
            for key in self._locked:
                if key in self._skipped:
                    continue
                if key == typing or key in eligible_set or key in lingering:
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
        extra = sum(1 for key in visible if key in self._linger_until)
        total = len(eligible) + extra
        page_count = max(1, math.ceil(total / MAX_PANES)) if total else 1
        if self._page >= page_count:
            if lingering:
                # 后页成员还在暂留：页码跟着当前页走，不要因为够格队列缩短就把人打回第 1 页。
                page_count = self._page + 1
            else:
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
