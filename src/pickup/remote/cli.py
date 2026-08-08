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


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def _cmd_start(args) -> int:
    problem = _check_dependencies()
    if problem:
        return _fail(problem, args.json)

    state = remote_config.load_state()
    if args.relay_url:
        state.relay_url = args.relay_url
    if args.no_relay:
        state.relay_enabled = False
    if args.no_local:
        state.local_enabled = False
    if args.port:
        state.local_port = int(args.port)
    remote_config.save_state(state)

    running = remote_config.read_pid()
    if running and not args.force:
        return _fail(f"常驻服务已经在跑了（进程 {running}）。要重开先执行 pickup remote stop", args.json)

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


def _print_pairing(state, code: str, public_key: bytes, local_port: int) -> None:
    url = pairing.build_payload(state, code, public_key, local_port)
    qr = pairing.render_qr(url)
    print("\n用 pickup 手机版扫下面这个码完成配对：\n")
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
    remote_config.write_pairing(code, _PAIRING_TTL)
    if args.json:
        print(_envelope(True, json.loads(pairing.as_json(state, code, public_key, state.local_port))))
        return EXIT_OK
    if not remote_config.read_pid():
        print("提示：常驻服务还没启动，扫码后要等 pickup remote start 跑起来才能连上。\n")
    _print_pairing(state, code, public_key, state.local_port)
    return EXIT_OK


# ---------------------------------------------------------------------------
# status / devices / unpair / stop
# ---------------------------------------------------------------------------

def _cmd_status(args) -> int:
    state = remote_config.load_state()
    pid = remote_config.read_pid()
    window = remote_config.read_pairing()
    data = {
        "running": bool(pid),
        "pid": pid,
        "host_id": state.host_id,
        "host_name": state.host_name,
        "relay_url": state.relay_url if state.relay_enabled else "",
        "relay_enabled": state.relay_enabled,
        "local_enabled": state.local_enabled,
        "local_port": state.local_port,
        "devices": len(state.devices),
        "pairing_open": bool(window),
        "dependencies_ok": not _check_dependencies(),
    }
    if args.json:
        print(_envelope(True, data))
        return EXIT_OK
    print(f"开发机：{state.host_name}")
    print(f"状态：{'运行中' if pid else '未启动'}" + (f"（进程 {pid}）" if pid else ""))
    print(f"中继：{state.relay_url if state.relay_enabled else '已关闭'}")
    print(f"局域网直连：{'已开启' if state.local_enabled else '已关闭'}")
    print(f"已配对手机：{len(state.devices)} 台")
    if window:
        remaining = int(window[1] - time.time())
        print(f"配对窗口：开放中，还剩 {max(0, remaining)} 秒")
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
        print(f"  {device['id']}  {device['name']:<16} 最近连接 {last}  {push}")
    return EXIT_OK


def _cmd_unpair(args) -> int:
    state = remote_config.load_state()
    if not remote_config.remove_device(state, args.device_id):
        return _fail(f"没有找到编号为 {args.device_id} 的设备", args.json)
    message = "已解除配对。那台手机需要重新扫码才能再连上。"
    print(_envelope(True, {"device_id": args.device_id}) if args.json else message)
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
            "  pickup remote status       # 看看跑起来没有\n"
            "  pickup remote stop         # 停掉\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="启动常驻服务")
    start.add_argument("--relay-url", help="自建中继地址（默认用公共中继）")
    start.add_argument("--no-relay", action="store_true", help="不连中继，只允许局域网直连")
    start.add_argument("--no-local", action="store_true", help="关掉局域网直连")
    start.add_argument("--port", type=int, help="局域网直连监听端口")
    start.add_argument("--force", action="store_true", help="已有实例在跑时仍然启动")
    start.add_argument("--quiet", action="store_true", help="不打印二维码和提示")
    start.set_defaults(func=_cmd_start)

    pair = sub.add_parser("pair", help="生成配对二维码")
    pair.set_defaults(func=_cmd_pair)

    status = sub.add_parser("status", help="查看运行状态")
    status.set_defaults(func=_cmd_status)

    devices = sub.add_parser("devices", help="列出已配对的手机")
    devices.set_defaults(func=_cmd_devices)

    unpair = sub.add_parser("unpair", help="解除某台手机的配对")
    unpair.add_argument("device_id")
    unpair.set_defaults(func=_cmd_unpair)

    stop = sub.add_parser("stop", help="停止常驻服务")
    stop.set_defaults(func=_cmd_stop)

    for action in (start, pair, status, devices, unpair, stop):
        action.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)
