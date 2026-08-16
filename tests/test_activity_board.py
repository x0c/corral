"""活动会话看板：成员资格、当前页冻结与翻页。"""

from __future__ import annotations

import unittest

from pickup.activity_board import (
    ActivityBoard,
    BoardCandidate,
    collect_candidates,
)
from pickup.attention import AttentionState
from pickup.split_layout import MAX_PANES


def _cand(key: str, kind: str, updated_at: float = 0.0) -> BoardCandidate:
    return BoardCandidate(key=key, kind=kind, updated_at=updated_at)  # type: ignore[arg-type]


class CollectCandidatesTests(unittest.TestCase):
    def test_only_hosted_waiting_working_unread(self) -> None:
        class _Store:
            def all_sessions(self):
                return [
                    {"source": "claude", "id": "wait", "keepalive_name": "k1"},
                    {"source": "claude", "id": "work", "keepalive_name": "k2"},
                    {"source": "claude", "id": "unread", "keepalive_name": "k3"},
                    {"source": "claude", "id": "idle", "keepalive_name": "k4"},
                    {"source": "claude", "id": "external", "live": True},
                    {"source": "shell", "id": "term", "keepalive_name": "k5"},
                ]

            def attention_for(self, key: str) -> AttentionState:
                kind = {
                    "claude:wait": "waiting",
                    "claude:work": "working",
                    "claude:unread": "unread",
                    "claude:idle": "none",
                    "claude:external": "waiting",
                    "shell:term": "working",
                }.get(key, "none")
                return AttentionState(kind=kind)  # type: ignore[arg-type]

        keys = [item.key for item in collect_candidates(_Store())]
        self.assertEqual(keys, ["claude:wait", "claude:work", "claude:unread"])


class ActivityBoardSyncTests(unittest.TestCase):
    def test_first_sync_takes_priority_prefix(self) -> None:
        board = ActivityBoard()
        snap = board.sync([
            _cand("w", "waiting", 3),
            _cand("g", "working", 2),
            _cand("r", "unread", 1),
        ])
        self.assertEqual(snap.keys, ("w", "g", "r"))
        self.assertEqual(snap.page, 0)
        self.assertEqual(snap.total, 3)
        self.assertEqual(snap.waiting_off_page, 0)

    def test_new_urgent_does_not_bump_full_page(self) -> None:
        board = ActivityBoard()
        first = [_cand(f"s{i}", "working", 10 - i) for i in range(MAX_PANES)]
        board.sync(first)
        later = [_cand("urgent", "waiting", 99), *first]
        snap = board.sync(later)
        self.assertNotIn("urgent", snap.keys)
        self.assertEqual(snap.waiting_off_page, 1)
        self.assertEqual(snap.total, MAX_PANES + 1)
        self.assertEqual(snap.page_count, 2)

    def test_empty_slot_fills_from_overflow(self) -> None:
        board = ActivityBoard()
        first = [_cand(f"s{i}", "working", 10 - i) for i in range(MAX_PANES)]
        board.sync(first)
        remaining = first[1:]
        overflow = [_cand("urgent", "waiting", 99), *remaining]
        snap = board.sync(overflow)
        self.assertIn("urgent", snap.keys)
        self.assertNotIn("s0", snap.keys)
        self.assertEqual(len(snap.keys), MAX_PANES)

    def test_typing_pane_stays_when_no_longer_eligible(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "working"), _cand("b", "working")])
        board.set_typing_key("a")
        snap = board.sync([_cand("b", "working"), _cand("c", "waiting")])
        self.assertIn("a", snap.keys)
        self.assertIn("c", snap.keys)

    def test_turn_page_slices_queue(self) -> None:
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertEqual(list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)])

    def test_later_page_does_not_pull_earlier_members_when_queue_shifts(self) -> None:
        board = ActivityBoard()
        first = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(first)
        board.turn_page(1)
        shifted = [_cand("urgent", "waiting", 99), *first]
        snap = board.sync(shifted)
        self.assertEqual(snap.page, 1)
        self.assertEqual(list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)])
        self.assertNotIn("urgent", snap.keys)
        self.assertNotIn("s0", snap.keys)

    def test_dismiss_skips_until_reset(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "waiting"), _cand("b", "working")])
        board.dismiss("a")
        snap = board.sync([_cand("a", "waiting"), _cand("b", "working")])
        self.assertEqual(snap.keys, ("b",))
        board.reset()
        snap = board.sync([_cand("a", "waiting"), _cand("b", "working")])
        self.assertEqual(snap.keys, ("a", "b"))

    def test_dismiss_while_typing_does_not_return(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "waiting"), _cand("b", "working")])
        board.set_typing_key("a")
        board.dismiss("a")
        board.set_typing_key("a")
        snap = board.sync([_cand("a", "waiting"), _cand("b", "working")])
        self.assertEqual(snap.keys, ("b",))
        self.assertNotIn("a", snap.keys)

    def test_empty_snapshot(self) -> None:
        board = ActivityBoard()
        snap = board.sync([])
        self.assertEqual(snap.keys, ())
        self.assertEqual(snap.page_count, 1)
        self.assertEqual(snap.total, 0)


class ActivityBoardComboTests(unittest.TestCase):
    """翻页 / dismiss / 打字钉住 / 队列收缩的组合场景。"""

    def test_turn_page_fill_only_from_current_page_start(self) -> None:
        """翻页后本页有空位时，只从当前页起点之后的队列补位。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertEqual(
            list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)]
        )
        # 本页只有 2 个成员、还有空位，但前页成员不被拉回补位。
        for i in range(MAX_PANES):
            self.assertNotIn(f"s{i}", snap.keys)

    def test_turn_page_dismiss_member_stays_gone_and_front_page_not_backfilled(
        self,
    ) -> None:
        """翻页后 dismiss 当前页成员：下一次 sync 不再出现，前页也不回填。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        last = f"s{MAX_PANES + 1}"
        board.dismiss(last)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertNotIn(last, snap.keys)
        # 当前页只剩 1 个成员、空位更多，前页成员依然不被拉回。
        self.assertEqual(list(snap.keys), [f"s{MAX_PANES}"])
        for i in range(MAX_PANES):
            self.assertNotIn(f"s{i}", snap.keys)

    def test_typing_pin_replaces_last_when_page_full(self) -> None:
        """打字格不够格且当前页已满：顶掉末位补位成员。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 10 - i) for i in range(MAX_PANES)]
        board.sync(items)
        board.set_typing_key("z")
        snap = board.sync(items)
        self.assertEqual(snap.page, 0)
        self.assertEqual(len(snap.keys), MAX_PANES)
        self.assertEqual(snap.keys[-1], "z")
        self.assertNotIn(f"s{MAX_PANES - 1}", snap.keys)
        self.assertEqual(
            list(snap.keys[:-1]), [f"s{i}" for i in range(MAX_PANES - 1)]
        )

    def test_typing_pin_appends_when_page_not_full(self) -> None:
        """打字格不够格且当前页未满：直接追加到本页。"""
        board = ActivityBoard()
        board.sync([_cand("b", "working"), _cand("c", "working")])
        board.set_typing_key("a")
        snap = board.sync([_cand("b", "working"), _cand("c", "working")])
        self.assertEqual(snap.keys, ("b", "c", "a"))
        self.assertEqual(snap.total, 2)

    def test_queue_shrink_pulls_page_back_in_range(self) -> None:
        """翻到末页后队列整体收缩：page 回退，keys 不越界也不含不够格成员。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        self.assertEqual(board.sync(items).page, 1)
        shrunk = [_cand("s0", "working", 20)]
        snap = board.sync(shrunk)
        self.assertEqual(snap.page, 0)
        self.assertEqual(snap.page_count, 1)
        self.assertEqual(snap.keys, ("s0",))
        for i in range(1, MAX_PANES + 2):
            self.assertNotIn(f"s{i}", snap.keys)
        self.assertLess(snap.page, snap.page_count)


if __name__ == "__main__":
    unittest.main()
