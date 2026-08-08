import sys
import unittest
from unittest import mock

from pickup import bootstrap, shim


class BootstrapShimTests(unittest.TestCase):
    def test_interactive_startup_repairs_shim_before_opening_cli(self):
        with (
            mock.patch.object(sys, "argv", ["pickup"]),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch.object(sys.stdout, "isatty", return_value=True),
            mock.patch.object(shim, "auto_install") as auto_install,
            mock.patch("pickup.cli.main") as cli_main,
        ):
            bootstrap.main()

        auto_install.assert_called_once_with()
        cli_main.assert_called_once_with()

    def test_non_interactive_startup_never_writes_shell_configuration(self):
        with (
            mock.patch.object(sys, "argv", ["pickup"]),
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            mock.patch.object(sys.stdout, "isatty", return_value=False),
            mock.patch.object(shim, "auto_install") as auto_install,
            mock.patch("pickup.cli.main") as cli_main,
        ):
            bootstrap.main()

        auto_install.assert_not_called()
        cli_main.assert_called_once_with()
