"""``python -m app.service [install|start|stop|remove|debug]`` - see windows_service.py."""

from __future__ import annotations

import sys

from app.service.windows_service import main

if __name__ == "__main__":
    main(sys.argv)
