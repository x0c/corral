"""pickup.remote.richmsg：各助手工具调用摘要与提问选项解析。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pickup.remote import richmsg


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(source: str, path: Path, **extra) -> dict:
    return {"source": source, "path": str(path), "id": "session-1", **extra}


def _cursor_db(path: Path, objects: list[dict]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    for index, obj in enumerate(objects):
        connection.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (f"blob-{index:04d}", json.dumps(obj, ensure_ascii=False).encode()),
        )
    connection.commit()
    connection.close()


class RichmsgUtilityTests(unittest.TestCase):
    def test_classify_maps_known_tools(self) -> None:
        self.assertEqual(richmsg.classify("Write"), "write")
        self.assertEqual(richmsg.classify("Bash"), "shell")
        self.assertEqual(richmsg.classify("AskUserQuestion"), "question")
        self.assertEqual(richmsg.classify("unknown_tool"), "other")

    def test_summarize_file_and_command(self) -> None:
        summary, _ = richmsg.summarize("Write", "write", {"file_path": "/tmp/foo/bar.py"})
        self.assertEqual(summary, "Write bar.py")
        summary, _ = richmsg.summarize(
            "bash",
            "shell",
            {"command": "export PATH=/usr/bin\npython3 -m pytest tests/"},
        )
        self.assertEqual(summary, "python3 -m pytest tests/")

    def test_extract_options_flat_and_nested(self) -> None:
        flat = richmsg._extract_options({"options": ["继续", "取消"]})
        self.assertEqual(flat, ["继续", "取消"])
        nested = richmsg._extract_options(
            {
                "questions": [
                    {
                        "question": "选方案",
                        "options": [{"label": "方案 A"}, {"label": "方案 B"}],
                    }
                ]
            }
        )
        self.assertEqual(nested, ["方案 A", "方案 B"])

    def test_looks_failed_only_on_explicit_markers(self) -> None:
        self.assertFalse(richmsg._looks_failed("all good\nprinted error in body"))
        self.assertTrue(richmsg._looks_failed("exit code: 1\nsomething broke"))


class CodexRichmsgTests(unittest.TestCase):
    def test_function_call_edit_shell_and_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            entries = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": "帮我改一下",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "apply_patch",
                        "call_id": "edit-1",
                        "arguments": json.dumps({"path": "/proj/main.py"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "edit-1",
                        "output": "patched",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "shell",
                        "call_id": "shell-1",
                        "input": 'tools.exec_command({"cmd":"npm test"})',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "shell-1",
                        "output": "exit code: 1\nfailed",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "ask-1",
                        "arguments": json.dumps({"question": "继续吗？", "options": ["是", "否"]}),
                    },
                },
            ]
            _write_jsonl(path, entries)
            messages = richmsg.RichReader(_session("codex", path)).read_all()

            user = [m for m in messages if m.role == "user"]
            self.assertEqual(len(user), 1)
            self.assertEqual(user[0].text, "帮我改一下")

            tools = [t for m in messages if m.role == "assistant" for t in m.tools]
            self.assertEqual(len(tools), 3)

            edit = next(t for t in tools if t.call_id == "edit-1")
            self.assertEqual(edit.kind, "edit")
            self.assertIn("main.py", edit.summary)
            self.assertEqual(edit.status, "ok")

            shell = next(t for t in tools if t.call_id == "shell-1")
            self.assertEqual(shell.kind, "shell")
            self.assertIn("npm test", shell.summary)
            self.assertEqual(shell.status, "error")

            ask = next(t for t in tools if t.call_id == "ask-1")
            self.assertEqual(ask.kind, "question")
            self.assertEqual(ask.options, ["是", "否"])
            self.assertEqual(ask.status, "running")

    def test_agent_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "payload": {"type": "agent_message", "message": "已完成"},
                    }
                ],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual(messages[0].role, "assistant")
            self.assertEqual(messages[0].text, "已完成")


class ClaudeRichmsgTests(unittest.TestCase):
    def test_tool_use_with_string_input_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            entries = [
                {
                    "type": "user",
                    "uuid": "u1",
                    "origin": {"kind": "human"},
                    "message": {"role": "user", "content": "开始"},
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "我来改文件"},
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "w1",
                                "input": json.dumps({"file_path": "/tmp/demo.txt"}),
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u2",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "w1", "content": "ok"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "b1",
                                "input": {"command": "false"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u3",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "b1",
                                "content": "error: command failed",
                                "is_error": True,
                            }
                        ],
                    },
                },
            ]
            _write_jsonl(path, entries)
            messages = richmsg.RichReader(_session("claude", path)).read_all()
            tools = [t for m in messages if m.role == "assistant" for t in m.tools]

            write = next(t for t in tools if t.call_id == "w1")
            self.assertEqual(write.kind, "write")
            self.assertEqual(write.status, "ok")

            bash = next(t for t in tools if t.call_id == "b1")
            self.assertEqual(bash.kind, "shell")
            self.assertEqual(bash.status, "error")

    def test_ask_user_question_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "AskUserQuestion",
                                    "id": "ask-1",
                                    "input": {
                                        "questions": [
                                            {
                                                "question": "选哪个？",
                                                "options": [
                                                    {"label": "方案一"},
                                                    {"label": "方案二"},
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                    }
                ],
            )
            messages = richmsg.RichReader(_session("claude", path)).read_all()
            tool = messages[0].tools[0]
            self.assertEqual(tool.kind, "question")
            self.assertEqual(tool.options, ["方案一", "方案二"])
            self.assertEqual(tool.status, "running")

            prompts = richmsg.pending_prompts(_session("claude", path))
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["options"], ["方案一", "方案二"])


class CursorRichmsgTests(unittest.TestCase):
    def test_tool_call_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(
                path,
                [
                    {"role": "user", "content": "查一下"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "正在搜索"},
                            {
                                "type": "tool-call",
                                "toolName": "grep",
                                "toolCallId": "g1",
                                "args": {"pattern": "TODO"},
                            },
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "g1",
                                "result": "src/main.py:42",
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool-call",
                                "toolName": "AskQuestion",
                                "toolCallId": "q1",
                                "args": {"question": "继续？", "choices": ["好", "停"]},
                            }
                        ],
                    },
                ],
            )
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            tools = [t for m in messages if m.role == "assistant" for t in m.tools]

            grep = next(t for t in tools if t.call_id == "g1")
            self.assertEqual(grep.kind, "search")
            self.assertEqual(grep.status, "ok")

            ask = next(t for t in tools if t.call_id == "q1")
            self.assertEqual(ask.kind, "question")
            self.assertEqual(ask.options, ["好", "停"])
            self.assertEqual(ask.status, "running")


class RichmsgSerializationTests(unittest.TestCase):
    def test_to_dict_includes_options_in_tools(self) -> None:
        tool = richmsg.ToolCall(
            call_id="1",
            name="AskUserQuestion",
            kind="question",
            summary="选方案",
            options=["A", "B"],
        )
        message = richmsg.RichMessage(seq=1, role="assistant", tools=[tool])
        data = message.to_dict()
        self.assertIn("tools", data)
        self.assertEqual(data["tools"][0]["options"], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
