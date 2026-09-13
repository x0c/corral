"""Transport-level channel lifecycle: unconfirmed channels must not leak slots.

Background (2026-09-13): the phone races LAN and relay handshakes in parallel.
Losers used to stay open after HELLO without ever sending a decryptable frame,
so each reconnect left zombie channels behind until the LAN server and the relay
client both hit their 8-channel caps and refused every new connection — the
phone then sat on "Connecting" with `Currently online: 0` while TCP was fine.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest

from corral.remote import crypto, protocol
from corral.remote.config import RemoteState
from corral.remote.service import RemoteService
from corral.remote.transport import local as local_mod
from corral.remote.transport import relay as relay_mod

try:  # pragma: no cover - optional dependency
    import websockets  # noqa: F401

    _HAS_WS = True
except Exception:  # pragma: no cover
    _HAS_WS = False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CacheDirMixin:
    def _use_temp_cache(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        self._old_state = os.environ.get("CORRAL_STATE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        os.environ["CORRAL_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, old in (("CORRAL_CACHE_DIR", self._old_cache), ("CORRAL_STATE_DIR", self._old_state)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@unittest.skipUnless(_HAS_WS, "websockets not installed")
class LocalServerUnconfirmedChannelTests(_CacheDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._use_temp_cache()
        self.service = RemoteService()
        self.service.begin_pairing()  # accept the unknown probe key during handshake
        self._old_ttl = local_mod.UNCONFIRMED_TTL
        local_mod.UNCONFIRMED_TTL = 0.4
        self.addCleanup(setattr, local_mod, "UNCONFIRMED_TTL", self._old_ttl)

    def test_handshake_without_key_confirmation_is_closed_and_slot_released(self):
        async def scenario() -> tuple[bool, int]:
            import websockets

            host_private = crypto.generate_private_key_bytes()
            state = RemoteState(host_id="test", host_name="t", local_port=_free_port())
            server = local_mod.LocalServer(self.service, state, host_private)
            stop = asyncio.Event()
            run_task = asyncio.create_task(server.run(stop))
            for _ in range(50):
                if server.port or server.listen_failed:
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(server.listen_failed)
            try:
                handshake = crypto.Handshake(crypto.generate_private_key_bytes())
                async with websockets.connect(
                    f"ws://127.0.0.1:{server.port}",
                    subprotocols=[protocol.SUBPROTOCOL],
                    ping_interval=None,
                ) as ws:
                    kind, channel_id, _ = protocol.decode_frame(await ws.recv())
                    self.assertEqual(kind, protocol.FRAME_DEVICE_OPEN)
                    await ws.send(
                        protocol.encode_frame(
                            protocol.FRAME_HELLO,
                            channel_id,
                            handshake.static_public + handshake.ephemeral_public,
                        )
                    )
                    kind, _cid, _payload = protocol.decode_frame(await ws.recv())
                    self.assertEqual(kind, protocol.FRAME_HELLO)
                    self.assertEqual(server._channels, 1)
                    # Never send a DATA frame: this is a handshake loser.
                    closed_by_host = False
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=3.0)
                    except websockets.exceptions.ConnectionClosed:
                        closed_by_host = True
                    except asyncio.TimeoutError:
                        closed_by_host = False
                for _ in range(50):
                    if server._channels == 0:
                        break
                    await asyncio.sleep(0.02)
                return closed_by_host, server._channels
            finally:
                stop.set()
                await run_task

        closed_by_host, remaining = asyncio.run(scenario())
        self.assertTrue(closed_by_host, "host must close a channel that never confirms its key")
        self.assertEqual(remaining, 0, "the slot must be released for the next connection")


class RelayClientUnconfirmedChannelTests(_CacheDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._use_temp_cache()
        self.service = RemoteService()
        self.state = RemoteState(host_id="test", host_name="t")
        self.client = relay_mod.RelayClient(self.service, self.state, crypto.generate_private_key_bytes())
        self.written: list[tuple[int, bytes]] = []
        self.client._write = lambda frame_type, cid, payload: self.written.append((frame_type, cid))  # type: ignore[method-assign]

    def test_expired_unconfirmed_channel_is_dropped_and_relay_told(self):
        cid = b"\x01" * 16
        channel = self.client._open_channel(cid)
        self.assertIsNotNone(channel)
        self.assertIn(cid, self.client._channels)
        self.client._expire_unconfirmed(cid)
        self.assertNotIn(cid, self.client._channels)
        self.assertIn((protocol.FRAME_DEVICE_CLOSE, cid), self.written)

    def test_confirmed_channel_survives_expiry_check(self):
        cid = b"\x02" * 16
        channel = self.client._open_channel(cid)
        assert channel is not None
        channel._secure = object()  # pretend handshake + confirmation completed
        channel._confirmed = True
        self.client._expire_unconfirmed(cid)
        self.assertIn(cid, self.client._channels)
        self.assertEqual(self.written, [])

    def test_host_side_close_frees_slot_for_new_channels(self):
        for index in range(relay_mod._MAX_CHANNELS):
            self.assertIsNotNone(self.client._open_channel(bytes([index]) * 16))
        self.assertIsNone(self.client._open_channel(b"\xff" * 16), "cap must still hold")
        # A kick / supersede closes the HostChannel, which must release its slot.
        self.client._channels[b"\x00" * 16].close()
        self.assertNotIn(b"\x00" * 16, self.client._channels)
        self.assertIsNotNone(self.client._open_channel(b"\xff" * 16))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
