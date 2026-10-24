"""界面通用构件与后台任务封装。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# 小构件
# ---------------------------------------------------------------------------


def label(text: str, object_name: str | None = None, *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    if wrap:
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return widget


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


def spacer(width: int = 0, height: int = 0) -> QWidget:
    widget = QWidget()
    if width:
        widget.setFixedWidth(width)
    if height:
        widget.setFixedHeight(height)
    return widget


def card(
    *,
    title: str | None = None,
    desc: str | None = None,
    margins: tuple[int, int, int, int] = (18, 16, 18, 16),
    spacing: int = 12,
) -> tuple[QFrame, QVBoxLayout]:
    """返回 (卡片外框, 内容布局)。没有标题时直接往布局里加控件。"""
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)

    if title:
        layout.addWidget(label(title, "CardTitle"))
    if desc:
        layout.addWidget(label(desc, "CardDesc", wrap=True))
    return frame, layout


def field(label_text: str, widget: QWidget, hint: str | None = None) -> QWidget:
    container = QWidget()
    container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    layout.addWidget(label(label_text, "FieldLabel"))
    layout.addWidget(widget)
    if hint:
        layout.addWidget(label(hint, "Hint", wrap=True))
    return container


def two_columns(left: QWidget, right: QWidget, spacing: int = 14) -> QWidget:
    container = QWidget()
    # 垂直方向不参与拉伸，否则卡片里的多余空间会把它撑高，内容被挤到中间
    container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    layout.addWidget(left, 1)
    layout.addWidget(right, 1)
    return container


def page_header(title: str, subtitle: str | None = None, *, extra: QWidget | None = None) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(16)

    text_box = QWidget()
    text_layout = QVBoxLayout(text_box)
    text_layout.setContentsMargins(0, 0, 0, 0)
    text_layout.setSpacing(3)
    text_layout.addWidget(label(title, "PageTitle"))
    if subtitle:
        text_layout.addWidget(label(subtitle, "PageSubtitle", wrap=True))
    layout.addWidget(text_box, 1)

    if extra is not None:
        layout.addWidget(extra, 0, Qt.AlignmentFlag.AlignVCenter)
    return container


def scroll_column(parent: QWidget) -> tuple[QWidget, QVBoxLayout]:
    """常用：给页面内容套一层滚动区，返回 (容器, 布局)。调用方自行放进 QScrollArea。"""
    inner = QWidget()
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(28, 22, 28, 24)
    layout.setSpacing(16)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return inner, layout


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------


class _TaskSignals(QObject):
    success = Signal(object)
    failure = Signal(str)
    finished = Signal()


class Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _TaskSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.success.emit(result)
        except Exception as exc:  # noqa: BLE001 - 后台异常统一转成提示
            self.signals.failure.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.signals.finished.emit()


_pool = QThreadPool.globalInstance()
# 持有运行中的任务，避免 Python 侧被回收导致信号丢失
_active_tasks: set["Task"] = set()


def run_async(
    fn: Callable[..., Any],
    *args: Any,
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_finished: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Task:
    task = Task(fn, *args, **kwargs)
    _active_tasks.add(task)
    task.signals.finished.connect(lambda: _active_tasks.discard(task))
    if on_success:
        task.signals.success.connect(on_success)
    if on_error:
        task.signals.failure.connect(on_error)
    if on_finished:
        task.signals.finished.connect(on_finished)
    _pool.start(task)
    return task


def make_button(text: str, *, kind: str = "default", on_click=None) -> QPushButton:
    button = QPushButton(text)
    if kind != "default":
        button.setObjectName({"primary": "Primary", "ghost": "Ghost",
                              "danger": "Danger", "link": "LinkButton"}.get(kind, kind))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click:
        button.clicked.connect(on_click)
    return button
