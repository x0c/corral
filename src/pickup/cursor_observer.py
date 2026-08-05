"""Cursor 会话状态观察器的安装、卸载与事件接收。

观察器只管理 Cursor 用户级 ``hooks.json`` 中由 pickup 创建的条目。配置损坏、
版本未知或权限不足时必须停止写入；hook 事件接收则始终故障开放，不能影响 Cursor。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

OBSERVER_API_VERSION = 1
CURSOR_HOOK_VERSION = 1
TARGET = "cursor"
HOOK_EVENTS = ("beforeSubmitPrompt", "afterAgentResponse", "stop", "sessionEnd")
_HOOK_SUFFIX = " -m pickup _cursor-hook"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_PERMISSION = 4


class ObserverError(Exception):
    """可稳定映射到结构化错误和退出码的观察器异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = EXIT_ERROR,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ObserverError(
            "usage_error",
            message,
            exit_code=EXIT_USAGE,
            hint="运行 pickup observer --help 查看用法",
        )


def _home_path(home: str | os.PathLike[str] | None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _config_path(home: str | os.PathLike[str] | None) -> Path:
    return _home_path(home) / ".cursor" / "hooks.json"


def _cache_dir(home: str | os.PathLike[str] | None) -> Path:
    override = os.environ.get("PICKUP_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg_root = os.environ.get("XDG_CACHE_HOME")
    if xdg_root:
        return Path(xdg_root).expanduser() / "pickup"
    return _home_path(home) / ".cache" / "pickup"


def _hook_command() -> str:
    argv = [sys.executable, "-m", "pickup", "_cursor-hook"]
    if os.name == "nt":
        # Cursor 在 Windows 上按原生命令行规则拆分参数，POSIX 单引号会成为路径正文。
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _managed_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    return isinstance(command, str) and command.rstrip().endswith(_HOOK_SUFFIX)


def _read_config(path: Path, *, missing_ok: bool = True) -> tuple[dict[str, Any], bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return {"version": CURSOR_HOOK_VERSION, "hooks": {}}, None
        raise ObserverError(
            "config_not_found",
            "未找到 Cursor 用户级观察配置",
            exit_code=EXIT_NOT_FOUND,
        ) from None
    except PermissionError as exc:
        raise ObserverError(
            "permission_denied",
            "没有权限读取 Cursor 用户级观察配置",
            exit_code=EXIT_PERMISSION,
        ) from exc
    except OSError as exc:
        raise ObserverError("read_failed", f"读取 Cursor 观察配置失败：{exc}") from exc

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObserverError(
            "invalid_config",
            "Cursor 用户级观察配置不是有效的 UTF-8 JSON，已停止修改",
            hint="请先修复该配置文件，再重新执行安装",
        ) from exc
    if not isinstance(value, dict):
        raise ObserverError("invalid_config", "Cursor 用户级观察配置顶层必须是对象，已停止修改")
    if value.get("version") != CURSOR_HOOK_VERSION:
        raise ObserverError(
            "unsupported_config_version",
            f"暂不支持 Cursor 观察配置版本 {value.get('version')!r}，已停止修改",
        )
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ObserverError("invalid_config", "Cursor 观察配置中的 hooks 必须是对象，已停止修改")
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ObserverError(
                "invalid_config",
                f"Cursor 观察配置中的 {event} 必须是列表，已停止修改",
            )
    return value, raw


def _state(config: dict[str, Any], path: Path, *, exists: bool) -> dict[str, Any]:
    hooks = config.get("hooks", {})
    current_command = _hook_command()
    managed_events: list[str] = []
    outdated_events: list[str] = []
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        managed = [entry for entry in entries if _managed_entry(entry)]
        if managed:
            managed_events.append(event)
        if len(managed) != 1 or managed[0].get("command") != current_command:
            outdated_events.append(event)
    installed = len(managed_events) == len(HOOK_EVENTS)
    healthy = installed and not outdated_events
    return {
        "target": TARGET,
        "status": "installed" if healthy else ("outdated" if installed else "not_installed"),
        "installed": installed,
        "healthy": healthy,
        "config_exists": exists,
        "config_path": str(path),
        "managed_events": managed_events,
        "outdated_events": outdated_events,
    }


def status(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """只读检查 Cursor 观察器安装状态。"""
    path = _config_path(home)
    config, raw = _read_config(path)
    return _state(config, path, exists=raw is not None)


def _desired_config(config: dict[str, Any]) -> dict[str, Any]:
    # JSON 往返复制既避免就地污染调用方，也确保只处理可序列化的配置内容。
    desired = json.loads(json.dumps(config, ensure_ascii=False))
    hooks = desired.setdefault("hooks", {})
    managed = {"command": _hook_command()}
    for event in HOOK_EVENTS:
        existing = hooks.get(event, [])
        kept = [entry for entry in existing if not _managed_entry(entry)]
        hooks[event] = [*kept, dict(managed)]
    return desired


def _without_managed_entries(config: dict[str, Any]) -> dict[str, Any]:
    desired = json.loads(json.dumps(config, ensure_ascii=False))
    hooks = desired.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        existing = hooks.get(event, [])
        hooks[event] = [entry for entry in existing if not _managed_entry(entry)]
    return desired


def _serialized(config: dict[str, Any]) -> bytes:
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _backup(raw: bytes, home: str | os.PathLike[str] | None) -> Path:
    directory = _cache_dir(home) / "cursor-hooks-backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    path = directory / f"hooks.{time.time_ns()}.{digest}.json"
    path.write_bytes(raw)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with tmp.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _apply(
    *,
    home: str | os.PathLike[str] | None,
    dry_run: bool,
    remove: bool,
) -> dict[str, Any]:
    path = _config_path(home)
    config, raw = _read_config(path)
    desired = _without_managed_entries(config) if remove else _desired_config(config)
    content = _serialized(desired)
    unchanged = raw is not None and raw == content
    if raw is None and remove:
        unchanged = True

    if remove:
        action = "unchanged" if unchanged else ("would_uninstall" if dry_run else "uninstalled")
    else:
        existed = raw is not None
        action = "unchanged" if unchanged else (
            "would_update" if dry_run and existed else
            "would_install" if dry_run else
            "updated" if existed else
            "installed"
        )

    backup_path: Path | None = None
    if not unchanged and not dry_run:
        try:
            if raw is not None:
                backup_path = _backup(raw, home)
            _atomic_write(path, content)
        except PermissionError as exc:
            raise ObserverError(
                "permission_denied",
                "没有权限修改 Cursor 用户级观察配置，原文件未被覆盖",
                exit_code=EXIT_PERMISSION,
            ) from exc
        except OSError as exc:
            raise ObserverError("write_failed", f"写入 Cursor 观察配置失败：{exc}") from exc

    result = _state(desired, path, exists=raw is not None or (not remove and not dry_run))
    result.update({
        "status": action,
        "changed": not unchanged,
        "dry_run": dry_run,
        "backup_path": str(backup_path) if backup_path else None,
    })
    return result


def install(
    home: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """幂等安装或升级 pickup 管理的 Cursor 观察条目。"""
    return _apply(home=home, dry_run=dry_run, remove=False)


def uninstall(
    home: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """只移除 pickup 管理的 Cursor 观察条目。"""
    return _apply(home=home, dry_run=dry_run, remove=True)


def _evidence(event_name: str, payload: dict[str, Any]):
    from pickup.attention import AttentionEvidence

    phase = "working" if event_name in {"beforeSubmitPrompt", "afterAgentResponse"} else "idle"
    generation = payload.get("generation_id")
    timestamp = payload.get("timestamp")
    token_base = str(generation or timestamp or time.time_ns())
    return AttentionEvidence(
        phase=phase,
        activity_token=f"{token_base}:{event_name}",
        observed_at=time.time(),
        source="observer",
    )


def ingest(
    event_name: str | None,
    payload: dict[str, Any] | object,
    attention_store=None,
) -> dict[str, Any]:
    """接收一次 Cursor hook 事件；任何异常都返回 ignored，不向调用方抛出。"""
    try:
        if not isinstance(payload, dict):
            return {"status": "ignored", "reason": "invalid_payload"}
        event = str(event_name or payload.get("hook_event_name") or "")
        if event not in HOOK_EVENTS:
            return {"status": "ignored", "reason": "unsupported_event"}
        raw_session_id = payload.get("conversation_id") or payload.get("session_id")
        session_id = str(raw_session_id or "").strip()
        if not session_id:
            return {"status": "ignored", "reason": "missing_session_id"}
        if attention_store is None:
            from pickup.attention import AttentionStore

            attention_store = AttentionStore()
        evidence = _evidence(event, payload)
        attention_store.record_event(TARGET, session_id, evidence)
        return {
            "status": "recorded",
            "runtime": TARGET,
            "session_id": session_id,
            "event": event,
            "phase": evidence.phase,
        }
    except Exception:
        # Cursor 会把 hook 进程的失败视为扩展故障；状态圆点绝不能阻断 Agent。
        return {"status": "ignored", "reason": "observer_failure"}


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="pickup observer", add_help=True)
    parser.add_argument("action", choices=("status", "install", "uninstall"))
    parser.add_argument("target")
    parser.add_argument("--json", action="store_true", dest="json_output", help="输出结构化 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只预演，不写入或备份任何文件")
    return parser


def _ok(data: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": {"version": OBSERVER_API_VERSION, "dry_run": dry_run},
    }


def _error(exc: ObserverError, *, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
        "meta": {"version": OBSERVER_API_VERSION, "dry_run": dry_run},
    }


def _print(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if payload["ok"]:
        data = payload["data"]
        labels = {
            "installed": "已安装",
            "updated": "已更新",
            "unchanged": "无需变更",
            "uninstalled": "已卸载",
            "would_install": "将安装",
            "would_update": "将更新",
            "would_uninstall": "将卸载",
            "not_installed": "未安装",
            "outdated": "需要更新",
        }
        print(f"Cursor 会话状态观察器：{labels.get(data.get('status'), data.get('status'))}")
    else:
        print(f"Cursor 会话状态观察器：{payload['error']['message']}")


def cli_main(argv: Sequence[str] | None = None) -> int:
    """处理 ``pickup observer ...``；非 TTY 自动输出 JSON envelope。"""
    args_list = list(argv if argv is not None else sys.argv[1:])
    json_requested = "--json" in args_list
    dry_run = "--dry-run" in args_list
    json_output = json_requested or not sys.stdout.isatty()
    try:
        args = _parser().parse_args(args_list)
        if args.target != TARGET:
            raise ObserverError(
                "target_not_found",
                f"不存在观察目标：{args.target}",
                exit_code=EXIT_NOT_FOUND,
            )
        if args.action == "status" and args.dry_run:
            raise ObserverError(
                "usage_error",
                "status 是只读操作，不能使用 --dry-run",
                exit_code=EXIT_USAGE,
            )
        handler = {"status": status, "install": install, "uninstall": uninstall}[args.action]
        data = handler(dry_run=args.dry_run) if args.action != "status" else handler()
        _print(_ok(data, dry_run=args.dry_run), json_output=json_output)
        return EXIT_OK
    except ObserverError as exc:
        _print(_error(exc, dry_run=dry_run), json_output=json_output)
        return exc.exit_code
    except PermissionError:
        wrapped = ObserverError(
            "permission_denied",
            "没有权限访问 Cursor 用户级观察配置",
            exit_code=EXIT_PERMISSION,
        )
        _print(_error(wrapped, dry_run=dry_run), json_output=json_output)
        return wrapped.exit_code
    except Exception as exc:
        wrapped = ObserverError("observer_failed", f"Cursor 会话状态观察器执行失败：{exc}")
        _print(_error(wrapped, dry_run=dry_run), json_output=json_output)
        return wrapped.exit_code


__all__ = ["cli_main", "ingest", "install", "status", "uninstall"]
