"""公共中继的账号登录：GitHub 设备码流程。

``corral login`` 向中继要一份设备码，打印验证地址，轮询直到浏览器里点了授权。
凭据以 0600 落在状态目录 ``account.json``。自建单租户中继不需要这一步。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from corral.i18n import t
from corral.remote import config as remote_config
from corral.remote import crypto

_DEFAULT_HTTP = "https://corral-relay.caozc.top"


def http_base(relay_url: str) -> str:
    cleaned = str(relay_url or "").strip().rstrip("/")
    if cleaned.startswith("wss://"):
        return "https://" + cleaned[len("wss://") :]
    if cleaned.startswith("ws://"):
        return "http://" + cleaned[len("ws://") :]
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        return cleaned
    return _DEFAULT_HTTP


def _request(url: str, payload: dict | None = None, token: str = "") -> tuple[int, dict]:
    data = None
    headers = {"accept": "application/json", "user-agent": "corral-cli"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
            return resp.status, body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {"error": exc.reason}
        return exc.code, body if isinstance(body, dict) else {}
    except (OSError, ValueError) as exc:
        return 0, {"error": str(exc)}


def login(relay_url: str) -> tuple[bool, str]:
    base = http_base(relay_url)
    status, body = _request(f"{base}/v2/auth/device", {})
    if status != 200 or not body.get("device_code"):
        return False, t("remote.login.device_failed", error=body.get("error") or status)
    device_code = str(body["device_code"])
    user_code = str(body.get("user_code") or "")
    uri = str(body.get("verification_uri") or f"{base}/device")
    interval = max(3, int(body.get("interval") or 5))
    expires = time.time() + int(body.get("expires_in") or 900)
    print(t("remote.login.visit", uri=uri, code=user_code))
    while time.time() < expires:
        time.sleep(interval)
        status, token_body = _request(
            f"{base}/v2/auth/token",
            {"device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        )
        if token_body.get("access_token"):
            account = token_body.get("account") or {}
            remote_config.save_account(
                {
                    "token": token_body["access_token"],
                    "account_id": account.get("id") or "",
                    "login": account.get("login") or "",
                    "relay_url": relay_url,
                }
            )
            return True, t("remote.login.ok", login=account.get("login") or "")
        err = str(token_body.get("error") or "")
        if err == "authorization_pending":
            continue
        if err == "expired_token":
            return False, t("remote.login.expired")
        return False, t("remote.login.denied", error=err or status)
    return False, t("remote.login.expired")


def logout() -> str:
    remote_config.clear_account()
    return t("remote.logout.ok")


def whoami() -> dict:
    return remote_config.load_account()


def register_host(state: remote_config.RemoteState) -> tuple[bool, str]:
    """把本机 Ed25519 公钥登记到多租户中继。单租户或未登录时直接跳过。"""
    account = remote_config.load_account()
    token = str(account.get("token") or "")
    if not token:
        if remote_config.is_public_relay(state.relay_url):
            return False, t("remote.login.required")
        return True, ""
    if not crypto.available():
        return False, t("remote.deps.missing", names="cryptography", packages="cryptography")
    pub = crypto.host_public_key_bytes(remote_config.load_or_create_host_key())
    status, body = _request(
        f"{http_base(state.relay_url)}/v2/hosts",
        {
            "routing_id": state.host_id,
            "name": state.host_name,
            "ed25519_pub": pub.hex(),
            "bundle_id": "com.x0c.corral",
        },
        token=token,
    )
    if status in (200, 201) and body.get("ok"):
        if isinstance(body.get("quota"), dict):
            account["quota"] = body["quota"]
            remote_config.save_account(account)
        return True, ""
    if status == 401:
        return False, t("remote.login.required")
    return False, t("remote.login.register_failed", error=body.get("error") or status)
