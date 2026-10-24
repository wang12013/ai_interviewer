"""开发期启动脚本：python run.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _check_python() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10 及以上版本")


if __name__ == "__main__":
    _check_python()
    from app.main import main

    raise SystemExit(main())
