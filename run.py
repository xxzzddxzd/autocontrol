#!/usr/bin/env python3
"""Launcher usable from cron without installing the package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autocontrol.cli import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
