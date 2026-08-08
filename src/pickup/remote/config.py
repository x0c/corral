"""开发机侧的持久化状态：本机身份密钥、已配对设备、中继地址。

放在缓存目录下的独立子目录（`~/.cache/pickup/remote/`），沿用 pickup 其余模块
统一的 `PICKUP_CACHE_DIR > XDG_CACHE_HOME > ~/.cache` 约定，方便测试隔离。

**私钥文件权限固定 0600、目录 0700**，与关注状态库同一套要求。私钥只在本机
落盘，任何情况下都不写日志、不进遥测、不随诊断输出。
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pickup.split_layout import layout_cache_dir

_DEFAULT_RELAY_URL = "wss://pickup-relay.caozc.top"
_STATE_FILENAME = "remote.json"
_KEY_FILENAME = "identity.key"

_lock = threading.Lock()


def remote_dir() -> Path:
    return layout_cache_dir() / "remote"


def state_path() -> Path:
    return remote_dir() / _STATE_FILENAME


def identity_key_path() -> Path:
    return remote_dir() / _KEY_FILENAME


def _ensure_dir() -> Path:
    directory = remote_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


@dataclass
class PairedDevice:
    """一台已经配对过的手机。

    `public_key` 是设备的 X25519 公钥（十六进制）。`push_token` 只有在设备允许
    推送后才有值，用于让中继投递通知；服务端不解析它，原样转交中继。
    """

    id: str
    name: str
    public_key: str
    paired_at: float
    last_seen_at: float = 0.0
    push_token: str = ""
    push_env: str = ""  # "sandbox" / "production"，由客户端上报，中继据此选投递环境
    platform: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> PairedDevice:
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            public_key=str(raw.get("public_key") or ""),
            paired_at=float(raw.get("paired_at") or 0.0),
            last_seen_at=float(raw.get("last_seen_at") or 0.0),
            push_token=str(raw.get("push_token") or ""),
            push_env=str(raw.get("push_env") or ""),
            platform=str(raw.get("platform") or ""),
        )


@dataclass
class RemoteState:
    """`remote.json` 的完整内容。私钥单独存文件，不在这里。"""

    host_id: str = ""
    host_name: str = ""
    # 给中继看的注册密钥。中继只用它防冒名顶替与滥用，看不到任何会话内容；
    # 首次连接时由开发机自己生成并由中继记住（信任首次使用）。
    host_token: str = ""
    relay_url: str = _DEFAULT_RELAY_URL
    relay_enabled: bool = True
    local_enabled: bool = True
    local_port: int = 0  # 0 表示由系统分配，启动后写回实际端口
    devices: list[PairedDevice] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["devices"] = [asdict(d) for d in self.devices]
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> RemoteState:
        return cls(
            host_id=str(raw.get("host_id") or ""),
            host_name=str(raw.get("host_name") or ""),
            host_token=str(raw.get("host_token") or ""),
            relay_url=str(raw.get("relay_url") or _DEFAULT_RELAY_URL),
            relay_enabled=bool(raw.get("relay_enabled", True)),
            local_enabled=bool(raw.get("local_enabled", True)),
            local_port=int(raw.get("local_port") or 0),
            devices=[PairedDevice.from_dict(d) for d in raw.get("devices") or [] if isinstance(d, dict)],
        )


def default_host_name() -> str:
    """给这台开发机起一个人类看得懂的名字，手机端列表里显示的就是它。"""
    for getter in (lambda: socket.gethostname(), lambda: os.uname().nodename):  # type: ignore[attr-defined]
        try:
            name = str(getter() or "").strip()
        except Exception:
            continue
        if name:
            # macOS 的 .local 后缀对用户没有意义，去掉更干净
            return name[: -len(".local")] if name.endswith(".local") else name
    return "dev"


def load_state() -> RemoteState:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    state = RemoteState.from_dict(raw if isinstance(raw, dict) else {})
    changed = False
    if not state.host_id:
        state.host_id = secrets.token_hex(16)
        changed = True
    if not state.host_name:
        state.host_name = default_host_name()
        changed = True
    if not state.host_token:
        state.host_token = secrets.token_urlsafe(32)
        changed = True
    if changed:
        save_state(state)
    return state


def save_state(state: RemoteState) -> None:
    _ensure_dir()
    path = state_path()
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
    with _lock:
        tmp.write_text(payload, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)


def load_or_create_identity() -> bytes:
    """返回本机 X25519 私钥原始字节；不存在则生成并落盘。"""
    path = identity_key_path()
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    from pickup.remote.crypto import generate_private_key_bytes

    key = generate_private_key_bytes()
    _ensure_dir()
    tmp = path.with_suffix(".tmp")
    with _lock:
        tmp.write_bytes(key)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    return key


def add_device(state: RemoteState, device: PairedDevice) -> RemoteState:
    """登记一台新配对设备；同一公钥重复配对时就地更新，不产生重复条目。"""
    devices = [d for d in state.devices if d.public_key != device.public_key]
    devices.append(device)
    state.devices = devices
    save_state(state)
    return state


def remove_device(state: RemoteState, device_id: str) -> bool:
    before = len(state.devices)
    state.devices = [d for d in state.devices if d.id != device_id]
    if len(state.devices) == before:
        return False
    save_state(state)
    return True


def touch_device(state: RemoteState, public_key: str, **updates: object) -> PairedDevice | None:
    """更新设备的最后连接时间与推送信息；找不到返回 None。"""
    for device in state.devices:
        if device.public_key != public_key:
            continue
        device.last_seen_at = time.time()
        for key, value in updates.items():
            if value not in (None, "") and hasattr(device, key):
                setattr(device, key, value)
        save_state(state)
        return device
    return None


def find_device(state: RemoteState, public_key: str) -> PairedDevice | None:
    for device in state.devices:
        if device.public_key == public_key:
            return device
    return None


# ---------------------------------------------------------------------------
# 配对窗口与进程标记
# ---------------------------------------------------------------------------
#
# 配对码写在文件里而不是留在常驻进程的内存里，是为了让「开一次配对窗口」这件事
# 不需要在两个进程之间新建一条控制通道：`pickup remote pair` 写文件并打二维码，
# 常驻服务每次判断准入时顺手读一下。少一条 IPC 通道就少一类会坏的东西。

_PAIRING_FILENAME = "pairing.json"
_PID_FILENAME = "remote.pid"


def pairing_path() -> Path:
    return remote_dir() / _PAIRING_FILENAME


def write_pairing(code: str, ttl: float) -> None:
    _ensure_dir()
    path = pairing_path()
    payload = json.dumps({"code": code, "expires_at": time.time() + ttl})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def read_pairing() -> tuple[str, float] | None:
    """返回 (配对码, 过期时间)；没有窗口或已过期时返回 None（并顺手清掉过期文件）。"""
    try:
        raw = json.loads(pairing_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    code = str(raw.get("code") or "")
    expires_at = float(raw.get("expires_at") or 0.0)
    if not code or expires_at <= time.time():
        clear_pairing()
        return None
    return code, expires_at


def clear_pairing() -> None:
    try:
        pairing_path().unlink()
    except OSError:
        pass


def pid_path() -> Path:
    return remote_dir() / _PID_FILENAME


def write_pid() -> None:
    _ensure_dir()
    pid_path().write_text(str(os.getpid()), encoding="utf-8")


def read_pid() -> int | None:
    """返回常驻服务的进程号；进程已经不在了就当作没有在跑。"""
    try:
        pid = int(pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def clear_pid() -> None:
    try:
        pid_path().unlink()
    except OSError:
        pass
