"""端到端加密通道：X25519 + HKDF-SHA256 + ChaCha20-Poly1305。

**为什么是这三样**：手机侧用系统自带的 CryptoKit 就能一一对应实现
（`Curve25519.KeyAgreement` / `HKDF<SHA256>` / `ChaChaPoly`），不需要在 App 里
塞第三方密码学库。曾经考虑过 NaCl 的 `box`，但它用的 XSalsa20 在 CryptoKit 里
没有对应实现，会逼手机端自带一套实现，故排除。

握手（每条连接一次，双方各出一把临时密钥，兼顾前向保密与身份认证）：

    设备 → 主机   device_static_pub ‖ device_eph_pub
    主机 → 设备   host_eph_pub

    ikm  = DH(d_eph, h_eph) ‖ DH(d_static, h_static)
         ‖ DH(d_eph, h_static) ‖ DH(d_static, h_eph)
    salt = device_eph_pub ‖ host_eph_pub
    key_d2h = HKDF(ikm, salt, "corral/remote/v2 d2h")
    key_h2d = HKDF(ikm, salt, "corral/remote/v2 h2d")

把两把长期密钥的 DH 结果混进 ikm，等于隐式双向认证：没有配对时交换过的那把
长期私钥，就算中继完全恶意也推不出会话密钥，因此**不需要额外的签名**。临时
密钥保证即使长期私钥日后泄露，也解不开此前录下的流量。

每个方向各有一个从 0 开始的 64 位计数器，随机数 = 4 字节零 ‖ 8 字节大端计数器。
两个方向用不同的密钥，所以计数器可以各自独立、互不干扰。计数器溢出前通道早已
关闭；真跑到上限时直接判定通道失效，绝不回绕（回绕会重用随机数，等于泄露明文）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_INFO_D2H = b"corral/remote/v2 d2h"
_INFO_H2D = b"corral/remote/v2 h2d"
_KEY_LEN = 32
_NONCE_LEN = 12
_MAX_COUNTER = (1 << 64) - 1

_MISSING_DEPS_HINT = (
    "手机端接力需要额外的加密与网络组件。请执行：pip install 'corral[remote]'\n"
    "（用 pipx 安装的话：pipx inject corral cryptography websockets）"
)


class CryptoUnavailable(RuntimeError):
    """缺少 cryptography 依赖时抛出，带可直接照做的安装提示。"""

    def __init__(self) -> None:
        super().__init__(_MISSING_DEPS_HINT)


def _backend():
    """惰性导入 cryptography：没装 remote 附加依赖的用户不该为此付出导入成本。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise CryptoUnavailable() from exc
    return X25519PrivateKey, X25519PublicKey, ChaCha20Poly1305


def available() -> bool:
    try:
        _backend()
    except CryptoUnavailable:
        return False
    return True


# ---------------------------------------------------------------------------
# 密钥
# ---------------------------------------------------------------------------

def generate_private_key_bytes() -> bytes:
    X25519PrivateKey, _, _ = _backend()
    from cryptography.hazmat.primitives import serialization

    key = X25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_bytes(private_key_bytes: bytes) -> bytes:
    X25519PrivateKey, _, _ = _backend()
    from cryptography.hazmat.primitives import serialization

    key = X25519PrivateKey.from_private_bytes(private_key_bytes)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _dh(private_key_bytes: bytes, peer_public_bytes: bytes) -> bytes:
    X25519PrivateKey, X25519PublicKey, _ = _backend()
    private = X25519PrivateKey.from_private_bytes(private_key_bytes)
    peer = X25519PublicKey.from_public_bytes(peer_public_bytes)
    return private.exchange(peer)


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = _KEY_LEN) -> bytes:
    """HKDF-SHA256（自实现，避免为了几行摘要再拉一层依赖表面）。"""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------

class ChannelError(RuntimeError):
    """握手失败、随机数耗尽或密文校验不通过。一律直接断开，不做任何重试。"""


class SecureChannel:
    """一条已完成握手的加密通道。非线程安全，调用方负责串行化。"""

    def __init__(self, send_key: bytes, recv_key: bytes, peer_static_public: bytes) -> None:
        _, _, ChaCha20Poly1305 = _backend()
        self._send = ChaCha20Poly1305(send_key)
        self._recv = ChaCha20Poly1305(recv_key)
        self._send_counter = 0
        self._recv_counter = 0
        self.peer_static_public = peer_static_public

    @staticmethod
    def _nonce(counter: int) -> bytes:
        if counter > _MAX_COUNTER:
            raise ChannelError("加密通道随机数已耗尽")
        return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = self._nonce(self._send_counter)
        self._send_counter += 1
        return nonce[4:] + self._send.encrypt(nonce, plaintext, None)

    def decrypt(self, frame: bytes) -> bytes:
        if len(frame) < 8 + 16:
            raise ChannelError("密文帧过短")
        counter = int.from_bytes(frame[:8], "big")
        # 底层是有序可靠的 WebSocket，计数器必须严格递增；乱序或重放一律拒绝。
        if counter != self._recv_counter:
            raise ChannelError("加密帧顺序异常")
        self._recv_counter = counter + 1
        try:
            return self._recv.decrypt(self._nonce(counter), frame[8:], None)
        except Exception as exc:  # cryptography 的 InvalidTag 等
            raise ChannelError("密文校验失败") from exc


class Handshake:
    """握手状态：先造临时密钥，拿到对端材料后再算出通道。"""

    def __init__(self, static_private: bytes) -> None:
        self.static_private = static_private
        self.static_public = public_key_bytes(static_private)
        self.ephemeral_private = generate_private_key_bytes()
        self.ephemeral_public = public_key_bytes(self.ephemeral_private)

    def _derive(
        self,
        *,
        device_static: bytes,
        device_eph: bytes,
        host_static: bytes,
        host_eph: bytes,
        as_host: bool,
    ) -> SecureChannel:
        if as_host:
            ikm = (
                _dh(self.ephemeral_private, device_eph)
                + _dh(self.static_private, device_static)
                + _dh(self.static_private, device_eph)
                + _dh(self.ephemeral_private, device_static)
            )
        else:
            ikm = (
                _dh(self.ephemeral_private, host_eph)
                + _dh(self.static_private, host_static)
                + _dh(self.ephemeral_private, host_static)
                + _dh(self.static_private, host_eph)
            )
        salt = device_eph + host_eph
        d2h = _hkdf(ikm, salt, _INFO_D2H)
        h2d = _hkdf(ikm, salt, _INFO_H2D)
        if as_host:
            return SecureChannel(send_key=h2d, recv_key=d2h, peer_static_public=device_static)
        return SecureChannel(send_key=d2h, recv_key=h2d, peer_static_public=host_static)

    def accept(self, device_static: bytes, device_eph: bytes) -> SecureChannel:
        """开发机侧：收到设备的长期公钥与临时公钥后算出通道。"""
        _validate_public(device_static, "设备长期公钥")
        _validate_public(device_eph, "设备临时公钥")
        return self._derive(
            device_static=device_static,
            device_eph=device_eph,
            host_static=self.static_public,
            host_eph=self.ephemeral_public,
            as_host=True,
        )

    def complete(self, host_static: bytes, host_eph: bytes) -> SecureChannel:
        """设备侧：收到开发机的临时公钥后算出通道（长期公钥来自配对二维码）。"""
        _validate_public(host_static, "开发机长期公钥")
        _validate_public(host_eph, "开发机临时公钥")
        return self._derive(
            device_static=self.static_public,
            device_eph=self.ephemeral_public,
            host_static=host_static,
            host_eph=host_eph,
            as_host=False,
        )


def _validate_public(value: bytes, label: str) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ChannelError(f"{label}长度不合法")


# ---------------------------------------------------------------------------
# 推送封装
# ---------------------------------------------------------------------------
#
# 推送不能走上面那条通道：投递的时候手机上根本没有连接在跑。这里改用「双方长期
# 密钥直接协商 + 每条推送一把新盐」的一次性封装，手机侧由通知服务扩展在本地解开。
# 中继只负责把这团密文转给苹果，看不到标题正文——这是既能让推送有可读内容、
# 又不让中继看到内容的唯一办法（Signal 一类应用同样做法）。

_INFO_PUSH = b"corral/remote/v2 push"


def seal_for_device(static_private: bytes, device_public: bytes, plaintext: bytes) -> bytes:
    _, _, ChaCha20Poly1305 = _backend()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _hkdf(_dh(static_private, device_public), salt, _INFO_PUSH)
    return salt + nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)


def open_from_host(static_private: bytes, host_public: bytes, sealed: bytes) -> bytes:
    """设备侧的对应解封；这里保留一份是为了让协议有可执行的双向参考实现。"""
    _, _, ChaCha20Poly1305 = _backend()
    if len(sealed) < 16 + 12 + 16:
        raise ChannelError("推送密文过短")
    salt, nonce, ciphertext = sealed[:16], sealed[16:28], sealed[28:]
    key = _hkdf(_dh(static_private, host_public), salt, _INFO_PUSH)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ChannelError("推送密文校验失败") from exc


# ---------------------------------------------------------------------------
# 配对码
# ---------------------------------------------------------------------------

def new_pairing_code() -> str:
    """一次性配对码。用 base32 去掉易混字符，方便手动念给另一台设备。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def codes_equal(a: str, b: str) -> bool:
    """比对配对码。恒定时间，且对手输容错：忽略大小写、空格与分隔符。

    空串一律判不等——否则「还没生成配对码」的状态会被当成「配对码是空的」，
    任何人发一个空码就配对成功了。
    """
    left, right = _normalize_code(a), _normalize_code(b)
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode(), right.encode())


def _normalize_code(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def random_id(length: int = 16) -> str:
    return os.urandom(length).hex()


# ---------------------------------------------------------------------------
# 开发机注册身份（Ed25519）与路由标识
# ---------------------------------------------------------------------------

_ROUTE_CONTEXT = b"corral/relay/v2 route"
_AUTH_CONTEXT = b"corral/relay/v2 host-auth"


def routing_id_from_x25519(public_key: bytes) -> str:
    """由开发机 X25519 公钥派生公开路由标识。手机扫码后自己也能算出来。"""
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ChannelError("开发机长期公钥长度不合法")
    import base64

    digest = hashlib.sha256(_ROUTE_CONTEXT + public_key).digest()[:16]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def generate_host_key_bytes() -> bytes:
    if not available():
        raise CryptoUnavailable()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def host_public_key_bytes(private_key_bytes: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign_host_assertion(private_key_bytes: bytes, routing_id: str, unix_ts: int, nonce: bytes) -> str:
    """生成 ``X-Corral-Auth: v2.<rid>.<ts>.<nonce>.<sig>``。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(nonce) != 16:
        raise ChannelError("断言 nonce 长度不合法")
    message = _AUTH_CONTEXT + b"\n" + routing_id.encode("ascii") + b"\n" + str(unix_ts).encode("ascii") + b"\n" + nonce
    sig = Ed25519PrivateKey.from_private_bytes(private_key_bytes).sign(message)
    return f"v2.{routing_id}.{unix_ts}.{_b64url(nonce)}.{_b64url(sig)}"
