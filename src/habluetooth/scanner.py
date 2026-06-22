"""
Backwards-compatible alias of the local bleak scanner module.

The local bleak scanner moved to :mod:`habluetooth.scanner_bleak`; this module
stays as an alias so existing ``habluetooth.scanner`` imports and patch targets
keep working unchanged.
"""

from __future__ import annotations

import sys

from . import scanner_bleak

# Make ``habluetooth.scanner`` resolve to the same module object as
# ``habluetooth.scanner_bleak`` so attribute access and ``unittest.mock.patch``
# targets that reference ``habluetooth.scanner.<name>`` operate on the real
# implementation rather than a detached copy.
sys.modules[__name__] = scanner_bleak
