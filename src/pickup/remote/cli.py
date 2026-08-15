"""`pickup remote` 子命令：把开发机接到手机上。

退出码沿用 pickup 既有的一套：0 成功、1 一般失败、2 用法错误。带 `--json` 的
子命令输出与 `agent_api` 同形状的 envelope，方便脚本和管家 Agent 调用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from contextlib import suppress

from pickup.i18n import join_names, t
from pickup.remote import config as remote_config
from pickup.remote import crypto, pairing

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

_PAIRING_TTL = 10 * 60


def _envelope(ok: bool, data=None, message: str = "") -> str:
    return json.dumps(
        {"ok": ok, "data": data, "error": None if ok else {"message": message}},
        ensure_ascii=False,
        indent=2,
    )


def _fail(message: str, as_json: bool = False) -> int:
    if as_json:
        print(_envelope(False, message=message))
    else:
        print(message, file=sys.stderr)
    return EXIT_ERROR


def _access_label(access: str) -> str:
    if access == "readonly":
        return t("remote.access.readonly")
    return t("remote.access.full")


def _check_dependencies() -> str:
    """依赖缺一不可，缺了就把安装命令原样给出来，不要让用户自己猜。"""
    missing = []
    if not crypto.available():
        missing.append("cryptography")
    try:
        import websockets  # noqa: F401
    except ImportError:
        missing.append("websockets")
    if not missing:
        return ""
    return t(
        "remote.deps.missing",
        names=join_names(missing),
        packages=" ".join(missing),
    )


def _stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    with suppress(OSError):
        os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def _cmd_start(args) -> int:
    problem = _check_dependencies()
    if problem:
        return _fail(problem, args.json)

    state = remote_config.load_state()
    if args.relay_url:
        try:
            state.relay_url = remote_config.validate_relay_url(
                args.relay_url, allow_insecure=args.insecure_relay
            )
        except ValueError as exc:
            return _fail(str(exc), args.json)
    elif state.relay_enabled and not args.no_relay:
        try:
            state.relay_url = remote_config.validate_relay_url(
                state.relay_url, allow_insecure=args.insecure_relay
            )
        except ValueError as exc:
            return _fail(str(exc), args.json)
    if args.no_relay:
        state.relay_enabled = False
    if args.no_local:
        state.local_enabled = False
    if args.port:
        state.local_port = int(args.port)
    remote_config.save_state(state)

    running = remote_config.read_pid()
    if running:
        if not args.force:
            return _fail(
                t("remote.start.already_running", pid=running),
                args.json,
            )
        _stop_pid(running)
        remote_config.clear_pid()

    from pickup.remote.daemon import RemoteDaemon

    daemon = RemoteDaemon(state)
    public_key = crypto.public_key_bytes(daemon.static_private)

    if not state.devices and not args.quiet:
        code = daemon.service.begin_pairing(_PAIRING_TTL)
        _print_pairing(state, code, public_key, state.local_port)

    if not args.quiet:
        print(t("remote.start.ready", name=state.host_name))
        if state.relay_enabled:
            print(t("remote.start.relay", url=state.relay_url))
        if state.local_enabled:
            print(t("remote.start.local_on"))
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _print_pairing(state, code: str, public_key: bytes, local_port: int, *, mode: str = "full") -> None:
    url = pairing.build_payload(state, code, public_key, local_port)
    qr = pairing.render_qr(url)
    mode_hint = t("remote.pair.readonly_hint") if mode == "readonly" else ""
    print(t("remote.pair.scan", mode_hint=mode_hint))
    if qr:
        print(qr)
        print(t("remote.pair.code_manual", code=code))
    else:
        print(pairing.render_fallback(url, code))
    print(t("remote.pair.valid_ten_minutes"))


# ---------------------------------------------------------------------------
# pair
# ---------------------------------------------------------------------------

def _cmd_pair(args) -> int:
    problem = _check_dependencies()
    if problem:
        return _fail(problem, args.json)
    state = remote_config.load_state()
    public_key = crypto.public_key_bytes(remote_config.load_or_create_identity())
    code = crypto.new_pairing_code()
    mode = "readonly" if args.readonly else "full"
    remote_config.write_pairing(code, _PAIRING_TTL, mode=mode)
    if args.json:
        payload = json.loads(pairing.as_json(state, code, public_key, state.local_port))
        payload["access"] = mode
        print(_envelope(True, payload))
        return EXIT_OK
    if not remote_config.read_pid():
        print(t("remote.pair.service_not_running"))
    _print_pairing(state, code, public_key, state.local_port, mode=mode)
    return EXIT_OK


# ---------------------------------------------------------------------------
# status / devices / unpair / stop / rotate-token
# ---------------------------------------------------------------------------

def _cmd_status(args) -> int:
    state = remote_config.load_state()
    pid = remote_config.read_pid()
    window = remote_config.read_pairing()
    data = {
        "running": bool(pid),
        "pid": pid,
        "host_id": state.routing_id or state.host_id,
        "host_name": state.host_name,
        "relay_url": state.relay_url if state.relay_enabled else "",
        "relay_enabled": state.relay_enabled,
        "local_enabled": state.local_enabled,
        "local_port": state.local_port,
        "devices": len(state.devices),
        "pairing_open": bool(window),
        "pairing_mode": remote_config.read_pairing_mode() if window else "",
        "dependencies_ok": not _check_dependencies(),
        "device_list": [
            {
                "id": d.id,
                "name": d.name,
                "access": d.access,
                "last_seen_at": d.last_seen_at,
            }
            for d in state.devices
        ],
    }
    snapshot = remote_config.read_status_snapshot() if pid else None
    if snapshot:
        data["online"] = snapshot.get("online") or []
        data["recent"] = snapshot.get("recent") or []
        data["relay_online"] = bool(snapshot.get("relay_online"))
        data["relay_connected_at"] = snapshot.get("relay_connected_at")
        data["relay_error"] = snapshot.get("relay_error") or ""
    elif state.relay_enabled:
        data["relay_online"] = False
        data["relay_connected_at"] = None
        data["relay_error"] = ""
    if args.json:
        print(_envelope(True, data))
        return EXIT_OK
    print(t("remote.status.host", name=state.host_name))
    state_label = t("remote.status.running") if pid else t("remote.status.not_running")
    pid_suffix = t("remote.status.pid_suffix", pid=pid) if pid else ""
    print(t("remote.status.line", state=state_label, pid_suffix=pid_suffix))
    if not state.relay_enabled:
        print(t("remote.status.relay_off"))
    else:
        relay_label = state.relay_url
        if snapshot is not None:
            if snapshot.get("relay_online"):
                connected_at = snapshot.get("relay_connected_at")
                since = ""
                if isinstance(connected_at, (int, float)) and connected_at:
                    since = t(
                        "remote.status.relay_since",
                        time=time.strftime("%H:%M:%S", time.localtime(connected_at)),
                    )
                print(t("remote.status.relay_online", label=relay_label, since=since))
            else:
                err = snapshot.get("relay_error") or ""
                suffix = t("remote.status.relay_error_suffix", error=err) if err else ""
                print(t("remote.status.relay_offline", label=relay_label, suffix=suffix))
        else:
            print(t("remote.status.relay_unknown", label=relay_label))
    print(
        t("remote.status.local_on")
        if state.local_enabled
        else t("remote.status.local_off")
    )
    print(t("remote.status.paired_count", count=len(state.devices)))
    for device in state.devices:
        print(
            t(
                "remote.status.device_item",
                name=device.name or device.id,
                access=_access_label(device.access),
            )
        )
    if snapshot:
        online = snapshot.get("online") or []
        print(t("remote.status.online_count", count=len(online)))
        for entry in online:
            name = entry.get("name") or entry.get("id") or "?"
            addr = entry.get("address") or ""
            suffix = t("remote.status.online_addr", addr=addr) if addr else ""
            print(
                t(
                    "remote.status.online_item",
                    name=name,
                    access=_access_label(entry.get("access") or ""),
                    suffix=suffix,
                )
            )
        recent = snapshot.get("recent") or []
        if recent:
            print(t("remote.status.recent_header"))
            for entry in recent[-8:]:
                ts = entry.get("ts") or 0
                stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
                print(f"  {stamp}  {entry.get('device') or '?'}  {entry.get('method') or '?'}")
    if window:
        remaining = int(window[1] - time.time())
        mode = remote_config.read_pairing_mode()
        print(
            t(
                "remote.status.pairing_window",
                mode=_access_label(mode),
                seconds=max(0, remaining),
            )
        )
    if pid:
        print(t("remote.status.kick_hint"))
    problem = _check_dependencies()
    if problem:
        print("\n" + problem)
    return EXIT_OK


def _cmd_devices(args) -> int:
    state = remote_config.load_state()
    devices = [
        {
            "id": d.id,
            "name": d.name,
            "platform": d.platform,
            "access": d.access,
            "paired_at": d.paired_at,
            "last_seen_at": d.last_seen_at,
            "push": bool(d.push_token),
        }
        for d in state.devices
    ]
    if args.json:
        print(_envelope(True, {"devices": devices}))
        return EXIT_OK
    if not devices:
        print(t("remote.devices.empty"))
        return EXIT_OK
    for device in devices:
        last = (
            time.strftime("%m-%d %H:%M", time.localtime(device["last_seen_at"]))
            if device["last_seen_at"]
            else t("remote.devices.never")
        )
        push = t("remote.devices.push_on") if device["push"] else t("remote.devices.push_off")
        print(
            t(
                "remote.devices.line",
                id=device["id"],
                name=device["name"],
                access=_access_label(device["access"]),
                last=last,
                push=push,
            )
        )
    return EXIT_OK


def _cmd_unpair(args) -> int:
    state = remote_config.load_state()
    device = remote_config.find_device_by_id(state, args.device_id)
    if device is None or not remote_config.remove_device(state, args.device_id):
        return _fail(t("remote.unpair.not_found", device_id=args.device_id), args.json)
    message = t("remote.unpair.done")
    print(_envelope(True, {"device_id": args.device_id}) if args.json else message)
    return EXIT_OK


def _cmd_rotate_token(args) -> int:
    state = remote_config.load_state()
    remote_config.rotate_host_token(state)
    message = t("remote.rotate.done")
    if args.json:
        print(_envelope(True, {"rotated": True, "host_id": state.routing_id or state.host_id}))
    else:
        print(message)
    return EXIT_OK


def _cmd_stop(args) -> int:
    pid = remote_config.read_pid()
    if not pid:
        return _fail(t("remote.stop.not_running"), args.json)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return _fail(t("remote.stop.failed", error=exc), args.json)
    print(_envelope(True, {"pid": pid}) if args.json else t("remote.stop.done"))
    return EXIT_OK


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pickup remote",
        description=t("remote.cli.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("remote.cli.epilog"),
    )
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help=t("remote.help.start"))
    start.add_argument("--relay-url", help=t("remote.help.relay_url"))
    start.add_argument(
        "--insecure-relay",
        action="store_true",
        help=t("remote.help.insecure_relay"),
    )
    start.add_argument("--no-relay", action="store_true", help=t("remote.help.no_relay"))
    start.add_argument("--no-local", action="store_true", help=t("remote.help.no_local"))
    start.add_argument("--port", type=int, help=t("remote.help.port"))
    start.add_argument(
        "--force",
        action="store_true",
        help=t("remote.help.force"),
    )
    start.add_argument("--quiet", action="store_true", help=t("remote.help.quiet"))
    start.set_defaults(func=_cmd_start)

    pair = sub.add_parser("pair", help=t("remote.help.pair"))
    pair.add_argument(
        "--readonly",
        action="store_true",
        help=t("remote.help.readonly"),
    )
    pair.set_defaults(func=_cmd_pair)

    status = sub.add_parser("status", help=t("remote.help.status"))
    status.set_defaults(func=_cmd_status)

    devices = sub.add_parser("devices", help=t("remote.help.devices"))
    devices.set_defaults(func=_cmd_devices)

    unpair = sub.add_parser("unpair", help=t("remote.help.unpair"))
    unpair.add_argument("device_id")
    unpair.set_defaults(func=_cmd_unpair)

    rotate = sub.add_parser("rotate-token", help=t("remote.help.rotate_token"))
    rotate.set_defaults(func=_cmd_rotate_token)

    stop = sub.add_parser("stop", help=t("remote.help.stop"))
    stop.set_defaults(func=_cmd_stop)

    for action in (start, pair, status, devices, unpair, rotate, stop):
        action.add_argument("--json", action="store_true", help=t("remote.help.json"))
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)
