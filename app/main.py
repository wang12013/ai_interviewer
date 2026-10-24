"""应用入口：日志、异常钩子、样式与主窗口。"""

from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from . import paths
from .ui.main_window import MainWindow

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> None:
    paths.ensure_dirs()
    handler = RotatingFileHandler(
        paths.LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(stream)


def install_excepthook() -> None:
    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.getLogger("uncaught").error(text)
        try:
            QMessageBox.critical(
                None, "程序出错了",
                f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入日志：\n{paths.LOG_FILE}",
            )
        except Exception:
            pass

    sys.excepthook = hook


def load_stylesheet(app: QApplication) -> None:
    qss_path = paths.resource("app", "ui", "qss", "app.qss")
    if not qss_path.exists():
        logging.getLogger("ui").warning("未找到样式文件：%s", qss_path)
        return
    text = qss_path.read_text(encoding="utf-8")
    # QSS 里的相对 url 按进程工作目录解析，打包后会失效。
    # 注意不能用 file:// URL：Qt 的 QSS 解析器不认百分号编码，中文路径必须原样传入。
    for asset in qss_path.parent.iterdir():
        if asset.suffix.lower() in {".svg", ".png", ".ico"}:
            text = text.replace(f"url({asset.name})", f'url("{asset.as_posix()}")')
    app.setStyleSheet(text)


def build_app() -> QApplication:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("AI 模拟面试器")
    app.setApplicationDisplayName("AI 模拟面试器")
    app.setOrganizationName("AIInterviewer")
    app.setStyle("Fusion")
    return app


def main() -> int:
    setup_logging()
    install_excepthook()
    app = build_app()
    load_stylesheet(app)
    window = MainWindow()
    window.show()
    logging.getLogger("app").info("启动完成，数据目录 %s", paths.DATA_DIR)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
