"""Codex hosted panes must bind to an exact thread, never a nearby rollout."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import codex_identity, codex_proxy, keepalive, liveness
from corral.models import LaunchPlan
from corral.store import SessionStore


class CodexClaimTests(unittest.TestCase):
    def test_exact_thread_claim_invalidates_scan_and_rejects_wrong_file(self) -> None:
        thread_id = "01a0d806-55d8-76a3-bb24-3e3b74232588"
        other_id = "01a0d805-4a23-7843-b326-97750355dee5"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            claims = home / ".cache" / "corral" / "codex-claims"
            sessions = home / ".codex" / "sessions" / "2026" / "09" / "25"
            sessions.mkdir(parents=True)
            rollout = sessions / f"rollout-2026-09-25T10-00-00-{thread_id}.jsonl"
            rollout.write_text(json.dumps({
                "type": "session_meta", "payload": {"id": thread_id},
            }) + "\n", encoding="utf-8")
            claim_path = claims / "pane.json"
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(codex_identity, "CLAIM_DIR", claims),
                mock.patch.dict("os.environ", {
                    codex_identity.CLAIM_PATH_ENV: str(claim_path),
                }),
            ):
                rollout.unlink()
                self.assertTrue(codex_identity.write_claim(thread_id, str(rollout), 123))
                self.assertNotEqual(codex_identity.claim_signature(), ())
                claim_path.unlink()
                rollout.write_text(json.dumps({
                    "type": "session_meta", "payload": {"id": thread_id},
                }) + "\n", encoding="utf-8")
                self.assertFalse(codex_identity.write_claim(other_id, str(rollout), 123))
                self.assertEqual(codex_identity.claim_signature(), ())
                self.assertTrue(codex_identity.write_claim(thread_id, str(rollout), 123))
                self.assertNotEqual(codex_identity.claim_signature(), ())
                self.assertEqual(json.loads(claim_path.read_text())["thread_id"], thread_id)
                self.assertEqual(claim_path.stat().st_mode & 0o777, 0o600)

    def test_only_response_to_root_thread_request_writes_claim(self) -> None:
        pending: dict[str, str] = {}
        thread = {"id": "01a0d806-55d8-76a3-bb24-3e3b74232588", "path": "/tmp/rollout"}
        with mock.patch.object(codex_proxy, "write_claim") as writer:
            codex_proxy._observe_response({"method": "thread/started", "params": {"thread": thread}}, pending)
            codex_proxy._observe_response({"id": 2, "result": {"thread": thread}}, pending)
            writer.assert_not_called()
            pending["2"] = "thread/start"
            codex_proxy._observe_response({"id": 2, "result": {"thread": thread}}, pending)
            writer.assert_called_once()
            self.assertEqual(pending, {})

    def test_codex_launch_uses_corral_owned_bridge(self) -> None:
        with mock.patch.object(keepalive, "_ensure_config_file", return_value="/tmp/tmux.conf"):
            plan = keepalive.wrap_plan(
                LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox"), "/tmp"),
                "codex", "pane1234",
            )
        self.assertIn("corral.codex_proxy", plan.argv)
        self.assertIn("CORRAL_CODEX_CLAIM_PATH=", " ".join(plan.argv))
        self.assertEqual(plan.argv[-2:], ("codex", "--dangerously-bypass-approvals-and-sandbox"))

    def test_remote_resume_keeps_default_auto_approval_on_app_server(self) -> None:
        tui_args, server_args = codex_proxy._remote_launch_args([
            "codex", "resume", "--dangerously-bypass-approvals-and-sandbox", "thread-id",
        ])
        self.assertEqual(tui_args, ["resume", "thread-id"])
        self.assertIn("approval_policy=never", server_args)
        self.assertIn("sandbox_mode=danger-full-access", server_args)

    def test_short_pane_name_cannot_claim_a_codex_history(self) -> None:
        session = {"source": "codex", "id": "01a0d813-c110-7ff0-893d-27f06fffa28d"}
        with mock.patch.object(
            liveness, "_list_tmux_sessions", return_value=[["corral-codex-01a0d813", "7444"]],
        ):
            liveness.annotate([session])
        self.assertNotIn("keepalive_name", session)

        store = object.__new__(SessionStore)
        self.assertIsNone(store._claim_unique_hosted_newcomer(
            "codex:01a0d813", {"source": "codex", "cwd": "/tmp", "id": "01a0d813"},
        ))


if __name__ == "__main__":
    unittest.main()
