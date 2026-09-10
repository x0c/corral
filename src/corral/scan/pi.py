"""Alias of `sesskit.parsers.pi` — same module object so patches and globals work."""

from __future__ import annotations

import sys

from sesskit.parsers import pi as _impl

sys.modules[__name__] = _impl
