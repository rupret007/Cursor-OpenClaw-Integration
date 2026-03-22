#!/usr/bin/env python3
"""Start Andrea lockstep HTTP server (local-first)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import env_loader  # noqa: E402
from services.andrea_sync.server import main  # noqa: E402


def _load_env() -> None:
    env_loader.merge_dotenv_paths([_ROOT / ".env", Path.cwd() / ".env"], override=False)
    override_paths = [Path.home() / "andrea-lockstep.env"]
    extra = (os.environ.get("ANDREA_ENV_FILE") or "").strip()
    if extra:
        override_paths.append(Path(extra).expanduser())
    env_loader.merge_dotenv_paths(override_paths, override=True)


_load_env()

if __name__ == "__main__":
    main()
