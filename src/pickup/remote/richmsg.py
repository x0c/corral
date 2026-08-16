"""富消息解析：在纯文本对话之外，保留助手到底做了哪些事。

现有的 `load_conversation` 只提取真人消息和助手的文本回复，**工具调用全部丢弃**
（`docs/SKILL.md` 把这条写成了产品边界：实测一条会话丢掉 1683 次工具调用，含
361 次改文件、837 次命令执行）。在电脑上这没问题——用户自己盯着终端；但在手机上
只看到「我改好了」而看不到改了什么，等于让人凭空相信助手。

所以这一层单独解析原始历史，额外产出工具调用摘要。它**不改动**任何现有扫描器的
输出，只是并行的第二种读法；`agent_api` 的字段契约不受影响。

解析口径按各助手真实历史格式对齐（本机实采，非推测）：

- Codex：``response_item`` 下的 ``function_call`` / ``custom_tool_call``，
  结果在同 ``call_id`` 的 ``*_output`` 条目里。``custom_tool_call`` 的 ``input``
  是一段 JS 源码，命令藏在 ``tools.exec_command({...})`` 调用里。
- Claude：assistant 消息 ``content`` 数组里的 ``tool_use``，结果是下一条 user
  消息里同 ``tool_use_id`` 的 ``tool_result``。
- Cursor：assistant 消息 ``content`` 里的 ``tool-call``，结果在 ``role="tool"``
  的 ``tool-result`` 里，按 ``toolCallId`` 关联。
- Kimi / OpenCode：暂时回落到纯文本（保持可用，不产出工具卡片）。

拿不准的格式一律降级成「有一次工具调用」，绝不猜测语义——宁可少显示，也不能
在手机上编造助手做过的事。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field

from pickup.scan.common import classify_tool

_MAX_DETAIL = 4000
_MAX_OUTPUT = 2000
_MAX_TEXT = 40000

# 只驱动手机端「问题选项按钮」；词表与分类函数已下沉到 scan.common.classify_tool。
QUESTION_KINDS = {"question"}

classify = classify_tool


@dataclass
class ToolCall:
    """一次工具调用及其结果，供手机端渲染成可折叠卡片。"""

    call_id: str
    name: str
    kind: str
    summary: str
    detail: str = ""
    status: str = "running"  # running / ok / error
    output: str = ""
    options: list[str] = field(default_factory=list)  # 仅提问型工具：可选答案

    def to_dict(self) -> dict:
        data = {
            "id": self.call_id,
            "name": self.name,
            "kind": self.kind,
            "summary": self.summary,
            "status": self.status,
        }
        if self.detail:
            data["detail"] = self.detail
        if self.output:
            data["output"] = self.output
        if self.options:
            data["options"] = self.options
        return data


@dataclass
class RichMessage:
    """聊天流里的一条。``tools`` 非空时表示这一条里助手做了哪些事。"""

    seq: int
    role: str  # user / assistant
    text: str = ""
    timestamp: float | None = None
    tools: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict:
        data: dict = {"seq": self.seq, "role": self.role}
        if self.text:
            data["text"] = self.text[:_MAX_TEXT]
        if self.timestamp is not None:
            data["ts"] = self.timestamp
        if self.tools:
            data["tools"] = [t.to_dict() for t in self.tools]
        return data


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------

def _clip(text: object, limit: int) -> str:
    value = "" if text is None else str(text)
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _basename(path: object) -> str:
    text = str(path or "").strip()
    return os.path.basename(text.rstrip("/")) or text


def _first_line(text: object, limit: int = 120) -> str:
    value = str(text or "").strip().splitlines()
    return _clip(value[0] if value else "", limit)


# 命令开头常见的环境准备语句：把它们当摘要毫无信息量（实测 Cursor 侧几乎每条
# 命令都以 `export PATH=…` 开头，摘要清一色相同，用户完全看不出跑了什么）。
_NOISE_COMMAND_PREFIX = ("export ", "set -", "cd ", "#", "source ", "unset ", "PATH=")


def _command_summary(text: object, limit: int = 160) -> str:
    """从一段可能多行的命令里挑出最能说明「这是在干什么」的一行。"""
    lines = [line.strip() for line in str(text or "").splitlines()]
    meaningful = [
        line
        for line in lines
        if line and not line.startswith(_NOISE_COMMAND_PREFIX) and line not in ("&&", "||")
    ]
    return _clip((meaningful or [line for line in lines if line] or [""])[0], limit)


def summarize(name: str, kind: str, args: dict | str) -> tuple[str, str]:
    """把工具参数压成 (一行摘要, 展开详情)。参数结构千奇百怪，取不到就退回工具名。"""
    detail = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=2)
    detail = _clip(detail, _MAX_DETAIL)
    if not isinstance(args, dict):
        return (f"{name}: {_first_line(args)}" if args else name), detail
    for key in ("file_path", "path", "filePath", "notebook_path"):
        if args.get(key):
            return f"{name} {_basename(args[key])}", detail
    for key in ("cmd", "command", "script"):
        if args.get(key):
            return _command_summary(args[key]), detail
    for key in ("pattern", "query", "q", "search_term"):
        if args.get(key):
            return f"{name} {_first_line(args[key], 100)}", detail
    for key in ("prompt", "instruction", "description", "question"):
        if args.get(key):
            return f"{name} {_first_line(args[key], 100)}", detail
    return name, detail


def _extract_options(args: dict) -> list[str]:
    """提问型工具的候选答案。手机端会把它们渲染成一排可点的按钮。"""
    if not isinstance(args, dict):
        return []
    for key in ("options", "choices", "questions"):
        raw = args.get(key)
        if not isinstance(raw, list):
            continue
        labels: list[str] = []
        for item in raw:
            if isinstance(item, str):
                labels.append(_clip(item, 80))
            elif isinstance(item, dict):
                # AskUserQuestion 一类会嵌一层 {question, options:[{label}]}
                nested = item.get("options")
                if isinstance(nested, list):
                    labels.extend(
                        _clip(o.get("label") or o.get("title") or o.get("text"), 80)
                        for o in nested
                        if isinstance(o, dict)
                    )
                    continue
                label = item.get("label") or item.get("title") or item.get("text") or item.get("name")
                if label:
                    labels.append(_clip(label, 80))
        labels = [x for x in labels if x]
        if labels:
            return labels[:8]
    return []


def _result_text(value: object) -> str:
    """各家的工具结果结构不同：字符串、片段数组、带 output 的对象都见过。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clip(value, _MAX_OUTPUT)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return _clip("\n".join(p for p in parts if p), _MAX_OUTPUT)
    if isinstance(value, dict):
        for key in ("text", "output", "content", "result", "stdout"):
            if value.get(key):
                return _result_text(value[key])
        return _clip(json.dumps(value, ensure_ascii=False), _MAX_OUTPUT)
    return _clip(str(value), _MAX_OUTPUT)


_FAILURE_RE = re.compile(
    r"^(?:exit code:?\s*[1-9]|error:|traceback \(most recent call last\)|command failed|fatal:)",
    re.I | re.M,
)


def _looks_failed(text: str) -> bool:
    """只在助手没有显式给出成功/失败信号时，退而求其次从结果文本里判断。

    判据必须严格到「几乎不会误报」：手机上把一次正常执行标成红色失败，比不标
    颜色更糟——用户会因此以为出了事而白跑一趟。所以只认行首出现的明确失败标记，
    不认正文里偶然出现的 error 字样（实测有命令把 error 当普通输出打印）。
    """
    return bool(_FAILURE_RE.search(text[:600]))


# ---------------------------------------------------------------------------
# 增量读取
# ---------------------------------------------------------------------------

class RichReader:
    """按会话保存读取进度，`poll()` 只返回上次之后新增的消息。

    对 JSONL 记住字节偏移；对 Cursor 的 SQLite 记住 rowid。文件被截断或换掉
    （新会话复用同一路径）时自动整轮重读，不会卡在错误的偏移上。
    """

    def __init__(self, session: dict) -> None:
        self.session = dict(session)
        self.runtime_id = str(session.get("source") or "")
        self.path = str(session.get("path") or "")
        self._offset = 0
        self._rowid = 0
        self._seq = 0
        self._size = 0
        self._pending: dict[str, ToolCall] = {}
        # call_id → 宿主助手消息：结果回填后要按原 seq 再推一次，手机端才能合并状态。
        self._host_by_call: dict[str, RichMessage] = {}

    def reset(self) -> None:
        self._offset = 0
        self._rowid = 0
        self._seq = 0
        self._size = 0
        self._pending = {}
        self._host_by_call = {}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _register_tool(self, host: RichMessage, tool: ToolCall) -> None:
        if not tool.call_id:
            return
        self._pending[tool.call_id] = tool
        self._host_by_call[tool.call_id] = host

    def _finish_tool(
        self,
        call_id: str,
        *,
        output: object,
        failed: bool | None = None,
    ) -> RichMessage | None:
        """回填工具结果；返回需要重新推送的宿主消息（无则 None）。"""
        tool = self._pending.pop(call_id, None)
        if tool is None:
            return None
        tool.output = _result_text(output)
        if failed is None:
            tool.status = "error" if _looks_failed(tool.output) else "ok"
        else:
            tool.status = "error" if failed or _looks_failed(tool.output) else "ok"
        return self._host_by_call.get(call_id)

    def poll(self) -> list[RichMessage]:
        parser = _PARSERS.get(self.runtime_id)
        if parser is None:
            return []
        try:
            return parser(self)
        except (OSError, sqlite3.Error, ValueError):
            # 历史文件正被写入或格式异常：这一轮当作没有新消息，下一轮再试。
            return []

    def read_all(self) -> list[RichMessage]:
        self.reset()
        return self.poll()


def _iter_new_jsonl(reader: RichReader):
    """从上次的字节偏移继续读；只吐出完整的行，半行留到下轮。"""
    try:
        size = os.path.getsize(reader.path)
    except OSError:
        return
    if size < reader._size:  # 文件被截断/替换
        reader.reset()
    reader._size = size
    if size <= reader._offset:
        return
    with open(reader.path, encoding="utf-8", errors="replace") as handle:
        handle.seek(reader._offset)
        buffered = handle.read()
        consumed = 0
        for line in buffered.splitlines(keepends=True):
            if not line.endswith("\n"):
                break  # 最后一行还没写完，等下一轮
            consumed += len(line.encode("utf-8"))
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except ValueError:
                continue
        reader._offset += consumed


# --- Codex ----------------------------------------------------------------

_EXEC_CMD_RE = re.compile(r'exec_command\(\s*\{.*?"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)


def _codex_custom_input(raw: str) -> tuple[str, str]:
    """`custom_tool_call` 的 input 是一段 JS，真正的命令埋在 exec_command 参数里。"""
    match = _EXEC_CMD_RE.search(raw or "")
    if not match:
        return _first_line(raw, 160), _clip(raw, _MAX_DETAIL)
    try:
        command = json.loads(f'"{match.group(1)}"')
    except ValueError:
        command = match.group(1)
    return _first_line(command, 160), _clip(command, _MAX_DETAIL)


def _parse_codex(reader: RichReader) -> list[RichMessage]:
    messages: list[RichMessage] = []

    def attach(tool: ToolCall) -> None:
        if messages and messages[-1].role == "assistant":
            messages[-1].tools.append(tool)
        else:
            messages.append(RichMessage(reader._next_seq(), "assistant", "", None, [tool]))

    for entry in _iter_new_jsonl(reader):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        timestamp = _codex_time(entry)

        if kind == "message" and payload.get("role") == "user":
            text = _codex_content_text(payload.get("content"))
            if text:
                messages.append(RichMessage(reader._next_seq(), "user", text, timestamp))
        elif kind == "agent_message":
            text = _clip(payload.get("message") or payload.get("text"), _MAX_TEXT)
            if text:
                messages.append(RichMessage(reader._next_seq(), "assistant", text, timestamp))
        elif kind == "function_call":
            name = str(payload.get("name") or "tool")
            try:
                args = json.loads(payload.get("arguments") or "{}")
            except ValueError:
                args = payload.get("arguments") or {}
            summary, detail = summarize(name, classify(name), args)
            tool = ToolCall(
                call_id=str(payload.get("call_id") or payload.get("id") or ""),
                name=name,
                kind=classify(name),
                summary=summary,
                detail=detail,
                options=_extract_options(args) if classify(name) in QUESTION_KINDS else [],
            )
            attach(tool)
            if messages:
                reader._register_tool(messages[-1], tool)
        elif kind == "custom_tool_call":
            name = str(payload.get("name") or "tool")
            summary, detail = _codex_custom_input(str(payload.get("input") or ""))
            tool = ToolCall(
                call_id=str(payload.get("call_id") or payload.get("id") or ""),
                name=name,
                kind=classify(name) if classify(name) != "other" else "shell",
                summary=summary or name,
                detail=detail,
            )
            attach(tool)
            if messages:
                reader._register_tool(messages[-1], tool)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            host = reader._finish_tool(
                str(payload.get("call_id") or ""),
                output=payload.get("output"),
            )
            if host is not None and (not messages or messages[-1] is not host):
                messages.append(host)
    return messages


def _codex_time(entry: dict) -> float | None:
    from pickup.scan.codex import entry_time  # 复用既有的时间解析，避免两套口径

    try:
        return entry_time(entry)
    except Exception:
        return None


def _codex_content_text(content: object) -> str:
    if isinstance(content, str):
        return _clip(content, _MAX_TEXT)
    if isinstance(content, list):
        parts = [str(p.get("text") or "") for p in content if isinstance(p, dict)]
        return _clip("\n".join(p for p in parts if p), _MAX_TEXT)
    return ""


# --- Claude ---------------------------------------------------------------

def _parse_claude(reader: RichReader) -> list[RichMessage]:
    from pickup.scan.claude import entry_time, extract_text

    messages: list[RichMessage] = []
    for entry in _iter_new_jsonl(reader):
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        entry_type = entry.get("type")
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        try:
            timestamp = entry_time(entry)
        except Exception:
            timestamp = None

        if entry_type == "user":
            # tool_result 挂在 user 轮次下，但不是真人说的话，只用来回填工具状态。
            if isinstance(content, list):
                handled = False
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        handled = True
                        host = reader._finish_tool(
                            str(part.get("tool_use_id") or ""),
                            output=part.get("content"),
                            failed=bool(part.get("is_error")),
                        )
                        if host is not None and (not messages or messages[-1] is not host):
                            messages.append(host)
                if handled:
                    continue
            origin = entry.get("origin")
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                continue
            text = extract_text(content or "")
            if text:
                messages.append(RichMessage(reader._next_seq(), "user", _clip(text, _MAX_TEXT), timestamp))
            continue

        if entry_type != "assistant" or not isinstance(content, list):
            continue
        texts = [
            (part.get("text") or "").strip()
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and (part.get("text") or "").strip()
        ]
        tools: list[ToolCall] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_use":
                continue
            name = str(part.get("name") or "tool")
            kind = classify(name)
            args = _tool_args(part.get("input"))
            summary, detail = summarize(name, kind, args)
            tool = ToolCall(
                call_id=str(part.get("id") or ""),
                name=name,
                kind=kind,
                summary=summary,
                detail=detail,
                options=_extract_options(args) if kind in QUESTION_KINDS else [],
            )
            tools.append(tool)
        if texts or tools:
            message = RichMessage(
                reader._next_seq(), "assistant", _clip("\n\n".join(texts), _MAX_TEXT), timestamp, tools
            )
            messages.append(message)
            for tool in tools:
                reader._register_tool(message, tool)
    return messages


# --- Cursor ---------------------------------------------------------------

def _cursor_db_path(path: str) -> str:
    return path if path.endswith("store.db") else os.path.join(path, "store.db")


def _parse_cursor(reader: RichReader) -> list[RichMessage]:
    db_path = _cursor_db_path(reader.path)
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    messages: list[RichMessage] = []
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT rowid, * FROM blobs WHERE rowid > ? ORDER BY rowid", (reader._rowid,)
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    for row in rows:
        reader._rowid = max(reader._rowid, int(row[0]))
        blob = next((v for v in row[1:] if isinstance(v, (bytes, bytearray))), None)
        if blob is None:
            continue
        try:
            entry = json.loads(bytes(blob).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # DAG 二进制 blob，跳过
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role == "user":
            text = content if isinstance(content, str) else _cursor_text(content)
            if text:
                messages.append(RichMessage(reader._next_seq(), "user", _clip(text, _MAX_TEXT)))
        elif role == "assistant":
            texts, tools = _cursor_assistant(content, reader)
            if texts or tools:
                message = RichMessage(reader._next_seq(), "assistant", texts, None, tools)
                messages.append(message)
                for tool in tools:
                    reader._register_tool(message, tool)
        elif role == "tool" and isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool-result":
                    continue
                host = reader._finish_tool(
                    str(part.get("toolCallId") or ""),
                    output=part.get("result"),
                )
                if host is not None and (not messages or messages[-1] is not host):
                    messages.append(host)
    conn.close()
    return messages


def _cursor_text(content: object) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return ""


def _cursor_assistant(content: object, reader: RichReader) -> tuple[str, list[ToolCall]]:
    if not isinstance(content, list):
        return _clip(content if isinstance(content, str) else "", _MAX_TEXT), []
    texts: list[str] = []
    tools: list[ToolCall] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            texts.append(part["text"].strip())
        elif part.get("type") == "tool-call":
            name = str(part.get("toolName") or "tool")
            kind = classify(name)
            args = part.get("args") if isinstance(part.get("args"), dict) else {}
            summary, detail = summarize(name, kind, args)
            tool = ToolCall(
                call_id=str(part.get("toolCallId") or ""),
                name=name,
                kind=kind,
                summary=summary,
                detail=detail,
                options=_extract_options(args) if kind in QUESTION_KINDS else [],
            )
            tools.append(tool)
    return _clip("\n\n".join(texts), _MAX_TEXT), tools


# --- 其余运行时：回落到纯文本 ----------------------------------------------

def _parse_plain(reader: RichReader) -> list[RichMessage]:
    """Kimi / OpenCode 暂不解析工具调用，整轮重读纯文本对话。

    这里刻意不做增量：这两家的历史结构与前三家差异较大，做一半的增量比整轮
    重读更容易出错。会话条数有限，重读成本可接受。
    """
    from pickup.runtime import default_registry

    if reader._seq:  # 已经读过一轮，交给上层的整轮刷新
        return []
    registry = default_registry()
    try:
        runtime = registry.get(reader.runtime_id)
        plain = runtime.load_conversation(reader.session)
    except Exception:
        return []
    return [
        RichMessage(reader._next_seq(), item.role, _clip(item.text, _MAX_TEXT), item.timestamp)
        for item in plain
    ]


_PARSERS = {
    "codex": _parse_codex,
    "claude": _parse_claude,
    "cursor": _parse_cursor,
    "kimi": _parse_plain,
    "opencode": _parse_plain,
}


def supports_tool_calls(runtime_id: str) -> bool:
    return runtime_id in ("codex", "claude", "cursor")


def _tool_args(raw: object) -> dict:
    """各助手工具参数有时是 dict、有时是 JSON 字符串，统一成 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def pending_prompts(session: dict) -> list[dict]:
    """从会话历史里找出仍在等待用户回答的提问型工具调用。

    手机端可直接渲染 ``options`` 为可点按钮；没有选项时仍返回摘要供自由输入。
    """
    reader = RichReader(session)
    items = reader.read_all()
    prompts: list[dict] = []
    for message in items:
        if message.role != "assistant":
            continue
        for tool in message.tools:
            if tool.kind not in QUESTION_KINDS or tool.status != "running":
                continue
            entry: dict = {
                "id": tool.call_id,
                "name": tool.name,
                "summary": tool.summary,
                # 始终带上 options（可为 []），避免手机端把缺字段当成整包解码失败。
                "options": list(tool.options),
            }
            if tool.detail:
                entry["detail"] = tool.detail
            prompts.append(entry)
    return prompts
