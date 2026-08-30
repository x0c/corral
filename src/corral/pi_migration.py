"""旧 Pi 隔离历史迁回默认项目目录。

旧版 Corral 把每条托管 Pi 写入 ``corral-<ident>/`` / ``pickup-<ident>/``，
Pi 原生 ``/resume`` 不递归扫描这些目录，因此看不到历史。迁移只处理可机械证明
的主会话：JSONL header id 必须精确等于目录 ident。subagent 的随机 id 不相等，
不会被复制。

首版采用最保守的 copy-through：目标以原子 no-replace 方式创建、逐字节 hash
校验，源目录原样保留作回滚备份。目标已存在时只接受同 hash；不同内容绝不覆盖。
正在被 Pi 进程使用的隔离目录整目录延后。该策略已经让 Pi 原生 ``/resume`` 看见
历史，同时没有删除/改写用户 JSONL。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from corral import titles
from corral.scan.common import live_processes, open_file_paths, process_environ

PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_LEGACY_DIR_RE = re.compile(r"^(?:corral|pickup)-(.+)$")
_JOURNAL_NAME = "pi-migration-v1.json"


def _header(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
        data = json.loads(first)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") != "session":
        return None
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _encode_cwd(cwd: str) -> str:
    resolved = os.path.realpath(os.path.expanduser(str(cwd or "")))
    stripped = resolved.lstrip("/\\")
    safe = re.sub(r"[/\\:]", "-", stripped)
    return f"--{safe}--"


def _active_legacy_dirs() -> set[str]:
    """当前 Pi 进程正在使用的 session-dir；失败时宁可少迁、不影响 Corral。"""
    processes = list(live_processes("pi"))
    if not processes:
        return set()
    pids = [pid for pid, _cwd in processes]
    active: set[str] = set()
    for pid in pids:
        env = process_environ(pid)
        raw = str(env.get(PI_SESSION_DIR_ENV) or "").strip()
        if raw:
            active.add(os.path.realpath(os.path.expanduser(raw)))
    for paths in open_file_paths(pids).values():
        for raw in paths:
            if str(raw).endswith(".jsonl"):
                active.add(os.path.realpath(os.path.dirname(os.path.expanduser(str(raw)))))
    return active


def _copy_no_replace(source: Path, destination: Path) -> tuple[str, str]:
    """复制并原子 no-replace 落地；返回 ``(status, sha)``。"""
    source_sha = _sha256(source)
    if destination.exists():
        return ("already" if _sha256(destination) == source_sha else "conflict", source_sha)

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.corral-migrate-{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as src, tmp.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        # hard-link 是跨 macOS/Linux/Windows 的 no-replace 原语：目标已存在时
        # 必定失败，不会像 os.replace 那样覆盖用户文件。
        try:
            os.link(tmp, destination)
        except FileExistsError:
            status = "already" if _sha256(destination) == source_sha else "conflict"
            return status, source_sha
        if _sha256(destination) != source_sha:
            try:
                destination.unlink()
            except OSError:
                pass
            raise OSError("迁移目标 hash 校验失败")
        stat = source.stat()
        try:
            os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except OSError:
            pass
        return "copied", source_sha
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_journal(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def migrate_legacy_sessions(
    root: str | os.PathLike[str] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    *,
    active_dirs: set[str] | None = None,
) -> dict:
    """把确定可识别的旧主会话复制到 Pi 默认目录，返回结构化报告。

    本函数幂等、无覆盖、无删除。任何单目录错误只记录，不中断其余目录。
    ``active_dirs`` 仅供测试/调用方已有现场快照时注入；默认自动读取进程环境与
    打开文件。
    """
    agent_root = Path(root).expanduser() if root is not None else Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent"
    ).expanduser()
    sessions_root = agent_root / "sessions"
    report: dict = {
        "version": 1,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "copied": [],
        "already": [],
        "deferred": [],
        "conflicts": [],
        "skipped": [],
        "errors": [],
    }
    if not sessions_root.is_dir():
        return report

    active_source = active_dirs if active_dirs is not None else _active_legacy_dirs()
    active = {os.path.realpath(item) for item in active_source}
    legacy_dirs: list[Path] = []
    for current, dirs, _files in os.walk(sessions_root):
        current_path = Path(current)
        for name in list(dirs):
            if _LEGACY_DIR_RE.match(name):
                legacy_dirs.append(current_path / name)
                # 旧房间里的子目录不是另一层项目，不必继续递归。
                dirs.remove(name)

    for directory in sorted(legacy_dirs):
        real_directory = os.path.realpath(directory)
        match = _LEGACY_DIR_RE.match(directory.name)
        ident = match.group(1) if match else ""
        if real_directory in active:
            report["deferred"].append({"sourceDir": str(directory), "reason": "active"})
            continue
        candidates: list[tuple[Path, dict]] = []
        try:
            files = sorted(directory.glob("*.jsonl"))
        except OSError as exc:
            report["errors"].append({"sourceDir": str(directory), "error": str(exc)})
            continue
        for source in files:
            header = _header(source)
            if header is not None and str(header.get("id") or "") == ident:
                candidates.append((source, header))
        if len(candidates) != 1:
            report["skipped"].append({
                "sourceDir": str(directory),
                "reason": "no-exact-main" if not candidates else "multiple-exact-main",
                "candidateCount": len(candidates),
            })
            continue

        source, header = candidates[0]
        cwd = str(header.get("cwd") or "").strip()
        if not cwd:
            report["skipped"].append({"source": str(source), "reason": "missing-cwd"})
            continue
        destination_dir = sessions_root / _encode_cwd(cwd)
        destination = destination_dir / source.name

        # 同一默认目录里已有同 session id 的不同文件名也算冲突；Pi picker
        # 不应同时出现两个同 id、不同内容的候选。
        same_id: list[Path] = []
        if destination_dir.is_dir():
            for sibling in destination_dir.glob("*.jsonl"):
                sibling_header = _header(sibling)
                if sibling_header is not None and str(sibling_header.get("id") or "") == ident:
                    same_id.append(sibling)
        if same_id and destination not in same_id:
            try:
                source_sha = _sha256(source)
                same_hash = next((item for item in same_id if _sha256(item) == source_sha), None)
            except OSError as exc:
                report["errors"].append({"source": str(source), "error": str(exc)})
                continue
            if same_hash is not None:
                report["already"].append({
                    "source": str(source),
                    "destination": str(same_hash),
                    "sha256": source_sha,
                })
            else:
                report["conflicts"].append({
                    "source": str(source),
                    "destinations": [str(item) for item in same_id],
                    "reason": "same-id-different-content",
                })
            continue

        try:
            status, digest = _copy_no_replace(source, destination)
        except OSError as exc:
            report["errors"].append({
                "source": str(source),
                "destination": str(destination),
                "error": str(exc),
            })
            continue
        item = {"source": str(source), "destination": str(destination), "sha256": digest}
        if status == "copied":
            report["copied"].append(item)
        elif status == "already":
            report["already"].append(item)
        else:
            item["reason"] = "destination-different-content"
            report["conflicts"].append(item)

    journal_root = (
        Path(cache_dir).expanduser() if cache_dir is not None else Path(titles.CACHE_DIR)
    )
    try:
        _write_journal(journal_root / _JOURNAL_NAME, report)
    except OSError:
        # journal 失败不能把已安全落地、已 hash 校验的目标反算成失败；下次
        # 调用仍会按目标 hash 幂等识别。
        pass
    return report
