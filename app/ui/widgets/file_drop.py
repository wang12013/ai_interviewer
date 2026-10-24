"""拖拽 / 点击上传区。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...services.doc_parser import FILE_DIALOG_FILTER, SUPPORTED_SUFFIXES
from .common import make_button


class FileDropArea(QFrame):
    fileSelected = Signal(str)
    fileCleared = Signal()

    def __init__(self, title: str, hint: str, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(112)
        self.setMaximumHeight(126)
        self._path: str | None = None
        self._build(title, hint)
        self._sync_state()

    # -- 构建 ------------------------------------------------------------
    def _build(self, title: str, hint: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(6)

        self.empty_box = QWidget()
        empty_layout = QVBoxLayout(self.empty_box)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setSpacing(5)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title_label = QLabel(title)
        title_label.setObjectName("DropTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_label = QLabel(hint)
        hint_label.setObjectName("DropHint")
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(title_label)
        empty_layout.addWidget(hint_label)

        self.loaded_box = QWidget()
        loaded_layout = QVBoxLayout(self.loaded_box)
        loaded_layout.setContentsMargins(0, 0, 0, 0)
        loaded_layout.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.file_label = QLabel()
        self.file_label.setObjectName("DropFile")
        head.addWidget(self.file_label, 1)
        self.clear_button = make_button("移除", kind="link",
                                        on_click=self._on_clear)
        head.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignRight)
        loaded_layout.addLayout(head)

        self.preview_label = QLabel()
        self.preview_label.setObjectName("DropHint")
        self.preview_label.setWordWrap(True)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        loaded_layout.addWidget(self.preview_label, 1)

        root.addWidget(self.empty_box, 1)
        root.addWidget(self.loaded_box, 1)

    # -- 状态 ------------------------------------------------------------
    def _sync_state(self) -> None:
        loaded = bool(self._path)
        self.empty_box.setVisible(not loaded)
        self.loaded_box.setVisible(loaded)
        self.setProperty("loaded", "true" if loaded else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def current_path(self) -> str | None:
        return self._path

    def set_preview(self, path: str, preview: str) -> None:
        self._path = path
        self.file_label.setText(Path(path).name)
        self.preview_label.setText(preview)
        self._sync_state()

    def set_loading(self, message: str = "正在读取…") -> None:
        self._path = "loading"
        self.file_label.setText("读取中")
        self.preview_label.setText(message)
        self._sync_state()

    def clear(self) -> None:
        self._path = None
        self.file_label.clear()
        self.preview_label.clear()
        self._sync_state()

    def _on_clear(self) -> None:
        self.clear()
        self.fileCleared.emit()

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._browse()
        super().mousePressEvent(event)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", "", FILE_DIALOG_FILTER)
        if path:
            if Path(path).suffix.lower() not in SUPPORTED_SUFFIXES:
                self.preview_label.setText("不支持该格式")
                return
            self.fileSelected.emit(path)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._extract_drop_path(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("hovered", "true")
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.setProperty("hovered", "false")
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("hovered", "false")
        self.style().unpolish(self)
        self.style().polish(self)
        path = self._extract_drop_path(event.mimeData())
        if path:
            event.acceptProposedAction()
            self.fileSelected.emit(path)

    @staticmethod
    def _extract_drop_path(mime: QMimeData) -> str | None:
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if local and Path(local).suffix.lower() in SUPPORTED_SUFFIXES:
                return local
        return None
