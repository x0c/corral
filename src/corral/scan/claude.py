"""Alias of `sesskit.parsers.claude` — same module object so patches and globals work."""

from __future__ import annotations

import sys

from sesskit.parsers import claude as _impl

sys.modules[__name__] = _impl
