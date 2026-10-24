"""开发期截图工具：python tools/screenshot.py

按页面依次渲染并把窗口保存成 PNG，放在 tools/shots/ 下，方便在没有肉眼看屏幕时核对布局。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402

from app.main import build_app, load_stylesheet  # noqa: E402
from app.ui.main_window import (  # noqa: E402
    PAGE_HISTORY,
    PAGE_SETUP,
    PAGE_SETTINGS,
    MainWindow,
)

PAGES = [
    ("01-setup", PAGE_SETUP),
    ("02-history", PAGE_HISTORY),
    ("03-settings", PAGE_SETTINGS),
    ("04-settings-bottom", PAGE_SETTINGS),
]


def main() -> int:
    app = build_app()
    load_stylesheet(app)
    window = MainWindow()
    window.resize(1360, 860)
    window.show()

    out_dir = Path(__file__).parent / "shots"
    out_dir.mkdir(exist_ok=True)

    state = {"index": 0}

    def capture() -> None:
        index = state["index"]
        if index >= len(PAGES):
            app.quit()
            return
        name, page = PAGES[index]
        window.go_to(page)
        app.processEvents()
        QTimer.singleShot(220, lambda: _save(name, page))
        state["index"] = index + 1

    def _save(name: str, page: int) -> None:
        window.go_to(page)
        app.processEvents()
        if name.endswith("bottom"):
            current = window.stack.currentWidget()
            scroll = getattr(current, "scroll", None)
            if scroll is not None:
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
                app.processEvents()
        target = out_dir / f"{name}.png"
        window.grab().save(str(target))
        print(f"saved {target}")
        QTimer.singleShot(120, capture)

    QTimer.singleShot(700, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
