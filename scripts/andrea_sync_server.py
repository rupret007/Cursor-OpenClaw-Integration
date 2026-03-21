#!/usr/bin/env python3
"""Start Andrea lockstep HTTP server (local-first)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.andrea_sync.server import main  # noqa: E402

if __name__ == "__main__":
    main()
