"""面试历史页。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...models import STATUS_LABELS, InterviewSummary, format_duration
from ...repositories import InterviewRepo
from ..widgets.common import label, make_button, page_header


class _HistoryRow(QFrame):
    clicked = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, summary: InterviewSummary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary = summary
        self.setObjectName("HistoryRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 14, 12)
        layout.setSpacing(12)

        text_box = QWidget()
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        title = label(summary.title, "RowTitle")
        text_layout.addWidget(title)

        meta_parts = [part for part in (summary.company, summary.position) if part]
        meta_parts.append(format_duration(summary.duration_sec))
        meta_parts.append(summary.started_display)
        meta_parts.append(STATUS_LABELS.get(summary.status, summary.status))
        meta = label("  ·  ".join(meta_parts), "RowMeta")
        text_layout.addWidget(meta)
        layout.addWidget(text_box, 1)

        if summary.overall is not None:
            score = label(f"{summary.overall:.0f}", "ScoreBadge")
            score.setStyleSheet("color: #2C2C2A;")
            layout.addWidget(score, 0, Qt.AlignmentFlag.AlignVCenter)
            caption = label("分", "RowMeta")
            layout.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
        elif summary.status == "running":
            tag = label("进行中", "PhaseTag")
            layout.addWidget(tag, 0, Qt.AlignmentFlag.AlignVCenter)

        self.delete_btn = make_button("删除", kind="link",
                                      on_click=lambda: self.deleteRequested.emit(self.summary.id))
        layout.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.summary.id)
        super().mousePressEvent(event)


class HistoryPage(QWidget):
    interviewActivated = Signal(str)
    newInterviewRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(32, 24, 32, 28)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        layout.addWidget(page_header(
            "面试历史",
            "每一场面试都会保留完整对话，可以随时回看或生成复盘报告。",
        ))

        self.list_box = QWidget()
        self.list_layout = QVBoxLayout(self.list_box)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.list_box)

        self.empty_box = QWidget()
        empty_layout = QVBoxLayout(self.empty_box)
        empty_layout.setContentsMargins(0, 60, 0, 0)
        empty_layout.setSpacing(14)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        empty_layout.addWidget(label("还没有面试记录", "CardTitle"))
        empty_layout.addWidget(label(
            "导入一份 JD 和简历，开始你的第一场模拟面试。", "CardDesc"
        ))
        empty_layout.addWidget(
            make_button("去新建面试", kind="primary",
                        on_click=lambda: self.newInterviewRequested.emit()),
            0, Qt.AlignmentFlag.AlignHCenter,
        )
        layout.addWidget(self.empty_box)

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def refresh(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        summaries = InterviewRepo.list_summaries()
        self.empty_box.setVisible(not summaries)
        for summary in summaries:
            row = _HistoryRow(summary)
            row.clicked.connect(self.interviewActivated.emit)
            row.deleteRequested.connect(self._confirm_delete)
            self.list_layout.addWidget(row)

    def _confirm_delete(self, interview_id: str) -> None:
        answer = QMessageBox.question(
            self, "删除面试记录",
            "删除后对话记录、录音与报告都会一并移除，且无法恢复。确定删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            InterviewRepo.delete(interview_id)
            self.refresh()
