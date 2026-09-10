"""Alias of `sesskit.parsers.opencode` — same module object so patches and globals work."""

from __future__ import annotations

import sys

from sesskit.parsers import opencode as _impl

sys.modules[__name__] = _impl
