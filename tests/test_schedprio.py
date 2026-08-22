"""schedprio：交互优先级提升的平台适配与容错。"""
from __future__ import annotations

import sys
import unittest
from unittest import mock


class SchedPrioTests(unittest.TestCase):
    def test_boost_interactive_never_raises(self) -> None:
        from corral import schedprio

        schedprio.boost_interactive()

    def test_boost_ui_worker_never_raises(self) -> None:
        from corral import schedprio

        schedprio.boost_ui_worker()

    def test_darwin_path_calls_qos_api(self) -> None:
        from corral import schedprio

        fake_lib = mock.Mock()
        fake_lib.pthread_set_qos_class_self_np.return_value = 0
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch("ctypes.CDLL", return_value=fake_lib),
            mock.patch.object(schedprio, "_best_effort_nice"),
            mock.patch.object(schedprio, "_darwin_restore_foreground_policy") as restore,
        ):
            schedprio.boost_interactive()
        restore.assert_called_once_with()
        fake_lib.pthread_set_qos_class_self_np.assert_called_once_with(
            schedprio._QOS_USER_INTERACTIVE, 0
        )

    def test_darwin_worker_uses_user_initiated(self) -> None:
        from corral import schedprio

        fake_lib = mock.Mock()
        fake_lib.pthread_set_qos_class_self_np.return_value = 0
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch("ctypes.CDLL", return_value=fake_lib),
        ):
            schedprio.boost_ui_worker()
        fake_lib.pthread_set_qos_class_self_np.assert_called_once_with(
            schedprio._QOS_USER_INITIATED, 0
        )

    def test_qos_api_failure_is_swallowed(self) -> None:
        from corral import schedprio

        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch("ctypes.CDLL", side_effect=OSError("nope")),
            mock.patch.object(schedprio, "_best_effort_nice"),
            mock.patch.object(schedprio, "_darwin_restore_foreground_policy"),
        ):
            schedprio.boost_interactive()  # 不得抛


if __name__ == "__main__":
    unittest.main()
