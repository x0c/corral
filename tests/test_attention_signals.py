from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pickup.attention_signals import inspect_session


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(source: str, path: Path, *, live: bool = True, **extra) -> dict:
    return {"source": source, "path": str(path), "id": "session-1", "live": live, **extra}


class ClaudeAttentionSignalTests(unittest.TestCase):
    def test_question_is_waiting_and_matching_result_resumes_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            prompt = {
                "type": "user",
                "uuid": "user-1",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": "开始"},
            }
            question = {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "ask-1", "input": {}}],
                },
            }
            _write_jsonl(path, [prompt, question])
            evidence = inspect_session(_session("claude", path))
            self.assertEqual(evidence.phase, "waiting")
            self.assertIsNotNone(evidence.question_token)

            answer = {
                "type": "user",
                "uuid": "user-2",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "ask-1", "content": "已选择"}],
                },
            }
            # 起始 prompt 可能已落在有界尾部之外，配对回答本身也必须恢复 working。
            _write_jsonl(path, [question, answer])
            evidence = inspect_session(_session("claude", path))
            self.assertEqual(evidence.phase, "working")
            self.assertIsNone(evidence.question_token)

    def test_turn_duration_is_idle_and_agent_token_does_not_hash_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            base = [
                {
                    "type": "assistant",
                    "uuid": "assistant-fixed",
                    "message": {"content": [{"type": "text", "text": "第一份正文"}]},
                }
            ]
            _write_jsonl(path, base)
            first = inspect_session(_session("claude", path)).activity_token
            base[0]["message"]["content"][0]["text"] = "完全不同的正文"
            _write_jsonl(path, base)
            second = inspect_session(_session("claude", path)).activity_token
            self.assertEqual(first, second)

            base.append({"type": "system", "subtype": "turn_duration", "uuid": "stop-1"})
            _write_jsonl(path, base)
            self.assertEqual(inspect_session(_session("claude", path)).phase, "idle")

    def test_non_live_session_never_reports_working_or_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "开始"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "ask-1"}]
                        },
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("claude", path, live=False)).phase, "idle")


class CodexAttentionSignalTests(unittest.TestCase):
    def test_task_lifecycle_and_structured_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            entries = [
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-1",
                    },
                },
            ]
            _write_jsonl(path, entries)
            waiting = inspect_session(_session("codex", path))
            self.assertEqual(waiting.phase, "waiting")
            self.assertIsNotNone(waiting.question_token)

            answer = {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call-1"},
            }
            entries.append(answer)
            _write_jsonl(path, [entries[1], answer])
            self.assertEqual(inspect_session(_session("codex", path)).phase, "working")

            entries = [
                *entries,
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
            ]
            _write_jsonl(path, entries)
            completed = inspect_session(_session("codex", path))
            self.assertEqual(completed.phase, "idle")
            self.assertIsNotNone(completed.activity_token)

    def test_non_live_task_started_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}}])
            self.assertEqual(inspect_session(_session("codex", path, live=False)).phase, "idle")

    def test_tail_agent_activity_keeps_live_turn_working_without_visible_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-07-01T10:00:05Z",
                        "type": "response_item",
                        "payload": {"type": "agent_message", "id": "message-1", "message": "进度"},
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("codex", path)).phase, "working")
            self.assertEqual(inspect_session(_session("codex", path, live=False)).phase, "idle")


class KimiAttentionSignalTests(unittest.TestCase):
    def test_prompt_question_answer_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            entries = [
                {"type": "turn.prompt", "time": 1000, "turnId": "turn-1"},
                {
                    "type": "context.append_loop_event",
                    "time": 1100,
                    "event": {
                        "type": "tool.call",
                        "name": "AskUserQuestion",
                        "toolCallId": "ask-1",
                        "uuid": "event-1",
                    },
                },
            ]
            _write_jsonl(path, entries)
            self.assertEqual(inspect_session(_session("kimi", path)).phase, "waiting")

            answer = {
                "type": "context.append_loop_event",
                "time": 1200,
                "event": {"type": "tool.result", "toolCallId": "ask-1", "uuid": "event-2"},
            }
            entries.append(answer)
            _write_jsonl(path, [entries[1], answer])
            self.assertEqual(inspect_session(_session("kimi", path)).phase, "working")

            entries.append({"type": "turn.cancel", "time": 1300, "turnId": "turn-1"})
            _write_jsonl(path, entries)
            cancelled = inspect_session(_session("kimi", path))
            self.assertEqual(cancelled.phase, "idle")
            self.assertIsNotNone(cancelled.activity_token)

    def test_stable_latest_step_end_is_conservatively_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            _write_jsonl(
                path,
                [
                    {"type": "turn.prompt", "time": 1000},
                    {
                        "type": "context.append_loop_event",
                        "time": 1200,
                        "event": {"type": "step.end", "uuid": "step-1", "turnId": "turn-1"},
                    },
                ],
            )
            evidence = inspect_session(_session("kimi", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNotNone(evidence.activity_token)


class OpenCodeAttentionSignalTests(unittest.TestCase):
    def _database(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, data TEXT)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        return connection

    def test_incomplete_assistant_and_pending_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("m1", "session-1", 1, 1, json.dumps({"role": "assistant", "time": {"created": 1}})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "p1",
                    "m1",
                    "session-1",
                    2,
                    2,
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "question",
                            "callID": "ask-1",
                            "state": {"status": "running"},
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()
            evidence = inspect_session(_session("opencode", path))
            self.assertEqual(evidence.phase, "waiting")
            self.assertIsNotNone(evidence.question_token)

    def test_completed_question_and_completed_assistant_are_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (
                    "m1",
                    "session-1",
                    1,
                    2,
                    json.dumps({"role": "assistant", "finish": "stop", "time": {"created": 1, "completed": 2}}),
                ),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "p1",
                    "m1",
                    "session-1",
                    2,
                    2,
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "question",
                            "callID": "ask-1",
                            "state": {"status": "completed"},
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()
            evidence = inspect_session(_session("opencode", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNone(evidence.question_token)
            self.assertIsNotNone(evidence.activity_token)


class CursorAttentionSignalTests(unittest.TestCase):
    def _database(self, path: Path, objects: list[dict]) -> None:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        for index, obj in enumerate(objects):
            connection.execute(
                "INSERT INTO blobs VALUES (?, ?)",
                (f"blob-{index:04d}", json.dumps(obj, ensure_ascii=False).encode()),
            )
        connection.commit()
        connection.close()

    def test_default_non_live_cursor_does_not_open_database(self) -> None:
        with mock.patch("pickup.attention_signals.sqlite3.connect") as connect:
            evidence = inspect_session(
                {"source": "cursor", "path": "/无需存在/store.db", "live": False}
            )
        connect.assert_not_called()
        self.assertEqual(evidence.phase, "unknown")

    def test_probe_reads_activity_but_never_infers_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "新的结果"}],
                    }
                ],
            )
            evidence = inspect_session(_session("cursor", path, live=False, signal_probe=True))
            self.assertEqual(evidence.phase, "unknown")
            self.assertIsNotNone(evidence.activity_token)

    def test_structured_terminal_marker_generates_activity_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [{"role": "assistant", "content": [], "status": "aborted"}],
            )
            evidence = inspect_session(_session("cursor", path, live=False, signal_probe=True))
            self.assertEqual(evidence.phase, "unknown")
            self.assertIsNotNone(evidence.activity_token)

    def test_unpaired_ask_question_is_waiting_only_while_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            question = {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "ask-1", "args": {}}
                ],
            }
            self._database(path, [question])
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "waiting")
            not_live = inspect_session(_session("cursor", path, live=False, signal_probe=True))
            self.assertEqual(not_live.phase, "unknown")
            self.assertIsNone(not_live.question_token)

    def test_answer_clears_question_and_tail_query_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answered_path = Path(directory) / "answered.db"
            self._database(
                answered_path,
                [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "ask-1"}
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [
                            {"type": "tool-result", "toolName": "AskQuestion", "toolCallId": "ask-1"}
                        ],
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", answered_path)).phase, "unknown")

            bounded_path = Path(directory) / "bounded.db"
            old_question = {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "too-old"}
                ],
            }
            fillers = [
                {"role": "assistant", "content": [{"type": "text", "text": f"结果 {index}"}]}
                for index in range(220)
            ]
            self._database(bounded_path, [old_question, *fillers])
            evidence = inspect_session(_session("cursor", bounded_path))
            self.assertEqual(evidence.phase, "unknown")
            self.assertIsNone(evidence.question_token)


class AttentionSignalFallbackTests(unittest.TestCase):
    def test_unknown_missing_and_corrupt_inputs_degrade_safely(self) -> None:
        self.assertEqual(inspect_session({"source": "future", "live": True}).phase, "unknown")
        self.assertEqual(inspect_session({"source": "claude", "path": "/missing", "live": True}).phase, "unknown")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text("not-json\n{broken", encoding="utf-8")
            self.assertEqual(inspect_session(_session("codex", path)).phase, "unknown")

    def test_unchanged_history_has_stable_observed_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "timestamp": "2026-07-01T10:00:05Z",
                        "message": {"content": [{"type": "text", "text": "结果"}]},
                    }
                ],
            )
            first = inspect_session(_session("claude", path))
            second = inspect_session(_session("claude", path))
            self.assertEqual(first, second)
            self.assertGreater(first.observed_at, 0)


if __name__ == "__main__":
    unittest.main()
