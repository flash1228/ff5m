## Feather on-printer regression runner package.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import sys

# The modules Klipper had loaded when this package was first imported, before its own imports: which Feather modules
# a start or a session loaded (the memory report lists them; the harness loads more).
MODULES_BEFORE = frozenset(sys.modules)

from .feature import UITestFeature  # noqa: E402


__all__ = ("UITestFeature",)
