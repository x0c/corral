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
    return (
        "手机端接力还缺少这些组件：" + "、".join(missing) + "\n"
        "安装办法（二选一）：\n"
        "  pip install 'pickup[remote]'\n"
        "  pipx inject pickup " + " ".join(missing)
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
                f"常驻服务已经在跑了（进程 {running}）。要重开先执行 pickup remote stop，"
                "或加 --force 先停旧进程再启动",
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
        print(f"开发机「{state.host_name}」已就绪，按 Ctrl+C 退出。")
        if state.relay_enabled:
            print(f"  中继：{state.relay_url}")
        if state.local_enabled:
            print("  局域网直连：已开启")
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _print_pairing(state, code: str, public_key: bytes, local_port: int, *, mode: str = "full") -> None:
    url = pairing.build_payload(state, code, public_key, local_port)
    qr = pairing.render_qr(url)
    mode_hint = "（只读：只能看会话与画面，不能输入或改会话）" if mode == "readonly" else ""
    print(f"\n用 pickup 手机版扫下面这个码完成配对{mode_hint}：\n")
    if qr:
        print(qr)
        print(f"配对码（扫不了码时手动输入）：{code}")
    else:
        print(pairing.render_fallback(url, code))
    print("十分钟内有效。\n")


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
        print("提示：常驻服务还没启动，扫码后要等 pickup remote start 跑起来才能连上。\n")
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
    print(f"开发机：{state.host_name}")
    print(f"状态：{'运行中' if pid else '未启动'}" + (f"（进程 {pid}）" if pid else ""))
    if not state.relay_enabled:
        print("中继：已关闭")
    else:
        relay_label = state.relay_url
        if snapshot is not None:
            if snapshot.get("relay_online"):
                connected_at = snapshot.get("relay_connected_at")
                since = ""
                if isinstance(connected_at, (int, float)) and connected_at:
                    since = time.strftime("，自 %H:%M:%S", time.localtime(connected_at))
                print(f"中继：在线（{relay_label}{since}）")
            else:
                err = snapshot.get("relay_error") or ""
                suffix = f"：{err}" if err else ""
                print(f"中继：离线（{relay_label}）{suffix}")
        else:
            print(f"中继：{relay_label}（运行状态未知）")
    print(f"局域网直连：{'已开启' if state.local_enabled else '已关闭'}")
    print(f"已配对手机：{len(state.devices)} 台")
    for device in state.devices:
        access = "只读" if device.access == "readonly" else "完整"
        print(f"  · {device.name or device.id}（{access}）")
    if snapshot:
        online = snapshot.get("online") or []
        print(f"当前在线：{len(online)} 台")
        for entry in online:
            access = "只读" if entry.get("access") == "readonly" else "完整"
            name = entry.get("name") or entry.get("id") or "?"
            addr = entry.get("address") or ""
            suffix = f" @ {addr}" if addr else ""
            print(f"  · {name}（{access}）{suffix}")
        recent = snapshot.get("recent") or []
        if recent:
            print("最近远程操作：")
            for entry in recent[-8:]:
                ts = entry.get("ts") or 0
                stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
                print(f"  {stamp}  {entry.get('device') or '?'}  {entry.get('method') or '?'}")
    if window:
        remaining = int(window[1] - time.time())
        mode = remote_config.read_pairing_mode()
        mode_label = "只读" if mode == "readonly" else "完整"
        print(f"配对窗口：开放中（{mode_label}），还剩 {max(0, remaining)} 秒")
    if pid:
        print("提示：已解除配对的手机最多约两秒内会被踢下线。")
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
        print("还没有配对过任何手机。执行 pickup remote pair 生成二维码。")
        return EXIT_OK
    for device in devices:
        last = (
            time.strftime("%m-%d %H:%M", time.localtime(device["last_seen_at"]))
            if device["last_seen_at"]
            else "从未"
        )
        push = "已开推送" if device["push"] else "未开推送"
        access = "只读" if device["access"] == "readonly" else "完整"
        print(f"  {device['id']}  {device['name']:<16} {access}  最近连接 {last}  {push}")
    return EXIT_OK


def _cmd_unpair(args) -> int:
    state = remote_config.load_state()
    device = remote_config.find_device_by_id(state, args.device_id)
    if device is None or not remote_config.remove_device(state, args.device_id):
        return _fail(f"没有找到编号为 {args.device_id} 的设备", args.json)
    message = (
        "已解除配对。若常驻服务在跑，那台手机最多约两秒内会被踢下线；"
        "之后需要重新扫码才能再连上。"
    )
    print(_envelope(True, {"device_id": args.device_id}) if args.json else message)
    return EXIT_OK


def _cmd_rotate_token(args) -> int:
    state = remote_config.load_state()
    remote_config.rotate_host_token(state)
    message = (
        "已轮换中继注册凭据。请重启常驻服务（pickup remote stop && pickup remote start）"
        "使新凭据生效；已配对手机不必重新扫码。"
    )
    if args.json:
        print(_envelope(True, {"rotated": True, "host_id": state.routing_id or state.host_id}))
    else:
        print(message)
    return EXIT_OK


def _cmd_stop(args) -> int:
    pid = remote_config.read_pid()
    if not pid:
        return _fail("常驻服务没有在跑", args.json)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return _fail(f"停不下来：{exc}", args.json)
    print(_envelope(True, {"pid": pid}) if args.json else "已通知常驻服务退出。")
    return EXIT_OK


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pickup remote",
        description=(
            "把这台开发机接到手机上：手机能看会话、看实时画面、发消息、新建和接力。\n"
            "开发机主动往外连中继，不需要开端口、不需要公网 IP、不需要 VPN。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程：\n"
            "  pickup remote start        # 首次启动会直接打一个配对二维码\n"
            "  pickup remote pair         # 再配一部手机\n"
            "  pickup remote pair --readonly  # 只读配对（不能输入/删改）\n"
            "  pickup remote status       # 看看跑起来没有\n"
            "  pickup remote rotate-token # 轮换中继注册凭据\n"
            "  pickup remote stop         # 停掉\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="启动常驻服务")
    start.add_argument("--relay-url", help="自建中继地址（默认用公共中继，必须 wss://）")
    start.add_argument(
        "--insecure-relay",
        action="store_true",
        help="允许明文 ws:// 中继（会把注册凭据明文发出，仅调试用）",
    )
    start.add_argument("--no-relay", action="store_true", help="不连中继，只允许局域网直连")
    start.add_argument("--no-local", action="store_true", help="关掉局域网直连")
    start.add_argument("--port", type=int, help="局域网直连监听端口")
    start.add_argument(
        "--force",
        action="store_true",
        help="已有实例在跑时先停掉旧进程再启动（不会双开）",
    )
    start.add_argument("--quiet", action="store_true", help="不打印二维码和提示")
    start.set_defaults(func=_cmd_start)

    pair = sub.add_parser("pair", help="生成配对二维码")
    pair.add_argument(
        "--readonly",
        action="store_true",
        help="只读配对：手机只能看会话与画面，不能输入、新建、删除",
    )
    pair.set_defaults(func=_cmd_pair)

    status = sub.add_parser("status", help="查看运行状态")
    status.set_defaults(func=_cmd_status)

    devices = sub.add_parser("devices", help="列出已配对的手机")
    devices.set_defaults(func=_cmd_devices)

    unpair = sub.add_parser("unpair", help="解除某台手机的配对")
    unpair.add_argument("device_id")
    unpair.set_defaults(func=_cmd_unpair)

    rotate = sub.add_parser("rotate-token", help="轮换中继注册凭据")
    rotate.set_defaults(func=_cmd_rotate_token)

    stop = sub.add_parser("stop", help="停止常驻服务")
    stop.set_defaults(func=_cmd_stop)

    for action in (start, pair, status, devices, unpair, rotate, stop):
        action.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)
