"""主窗口：左侧导航 + 页面栈 + 底部状态条。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import config
from ..repositories import InterviewRepo
from .pages.history_page import HistoryPage
from .pages.interview_page import InterviewPage
from .pages.settings_page import SettingsPage
from .pages.setup_page import SetupPage
from .widgets.common import label, spacer
from .widgets.toast import Toast

APP_VERSION = "1.0.0"

PAGE_SETUP = 0
PAGE_INTERVIEW = 1
PAGE_HISTORY = 2
PAGE_SETTINGS = 3


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 模拟面试器")
        self.resize(1360, 860)
        self.setMinimumSize(1120, 720)
        self._checked_unfinished = False
        self._build()
        self._wire()
        self.refresh_status()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.setup_page = SetupPage()
        self.interview_page = InterviewPage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()

        self.stack.addWidget(self.setup_page)      # 0
        self.stack.addWidget(self.interview_page)  # 1
        self.stack.addWidget(self.history_page)    # 2
        self.stack.addWidget(self.settings_page)   # 3

        right_layout.addWidget(self.stack, 1)
        right_layout.addWidget(self._build_statusbar(), 0)
        layout.addWidget(right, 1)

        self.setCentralWidget(central)
        self.toast = Toast(self)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("SideBar")
        sidebar.setFixedWidth(184)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(0)

        brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(18, 20, 18, 18)
        brand_layout.setSpacing(2)
        brand_layout.addWidget(label("AI 模拟面试", "Brand"))
        brand_layout.addWidget(label("AI Interview Coach", "BrandSub"))
        layout.addWidget(brand)

        self.nav_layout = QVBoxLayout()
        self.nav_layout.setContentsMargins(10, 0, 10, 0)
        self.nav_layout.setSpacing(3)
        layout.addLayout(self.nav_layout)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self._nav_buttons: dict[int, QPushButton] = {}
        for index, text in ((PAGE_SETUP, "新建面试"),
                            (PAGE_HISTORY, "面试历史"),
                            (PAGE_SETTINGS, "设置")):
            self._add_nav(text, index)

        layout.addStretch(1)
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 0, 18, 0)
        footer_layout.setSpacing(3)
        footer_layout.addWidget(label("本地运行 · 数据不出电脑", "BrandSub", wrap=True))
        footer_layout.addWidget(label(f"v{APP_VERSION}", "BrandSub"))
        layout.addWidget(footer)
        return sidebar

    def _add_nav(self, text: str, index: int) -> None:
        button = QPushButton(text)
        button.setObjectName("NavButton")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.go_to(index))
        self.nav_group.addButton(button, index)
        self._nav_buttons[index] = button
        self.nav_layout.addWidget(button)

    def _build_statusbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        self.status_left = label("", "StatusText")
        layout.addWidget(self.status_left, 1)
        self.status_right = label("", "StatusText")
        layout.addWidget(self.status_right, 0)
        return bar

    # ------------------------------------------------------------------
    def _wire(self) -> None:
        self.setup_page.interviewCreated.connect(self._on_interview_created)
        self.setup_page.gotoSettings.connect(lambda: self.go_to(PAGE_SETTINGS))
        self.settings_page.saved.connect(self._on_settings_saved)
        self.history_page.interviewActivated.connect(self._on_history_activated)
        self.history_page.newInterviewRequested.connect(lambda: self.go_to(PAGE_SETUP))
        self.interview_page.finished.connect(self._on_interview_finished)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 退出前释放音频设备，否则进程可能残留占用声卡
        try:
            self.interview_page.shutdown()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    def go_to(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == PAGE_SETUP:
            self.setup_page._refresh_config_summary()
            button = self._nav_buttons.get(PAGE_SETUP)
        elif index == PAGE_HISTORY:
            self.history_page.refresh()
            button = self._nav_buttons.get(PAGE_HISTORY)
        elif index == PAGE_SETTINGS:
            button = self._nav_buttons.get(PAGE_SETTINGS)
        else:
            button = None
        if button:
            button.setChecked(True)
        else:
            for nav in self._nav_buttons.values():
                nav.setChecked(False)

    def _on_interview_created(self, interview_id: str) -> None:
        self.interview_page.load(interview_id)
        self.go_to(PAGE_INTERVIEW)
        self.toast.show_message("面试开始，直接作答即可", 3000)

    def _on_interview_finished(self, interview_id: str) -> None:
        self.refresh_status()
        self.go_to(PAGE_HISTORY)
        if interview_id:
            self.toast.show_message("面试已结束，记录已保存", 3600)

    def _on_settings_saved(self) -> None:
        self.setup_page._refresh_config_summary()
        # 音色/协议/开关都可能是刚改的，让播报器按新配置重建
        self.interview_page.reload_voice()
        self.refresh_status()
        self.toast.show_message("设置已保存")

    def _on_history_activated(self, interview_id: str) -> None:
        record = InterviewRepo.get(interview_id)
        if not record:
            return
        # 未完场的接着聊，已结束的当只读对话记录看
        self.interview_page.load(interview_id)
        self.go_to(PAGE_INTERVIEW)
        if record.get("status") != "running":
            self.toast.show_message("这一场已经结束，当前为只读记录", 3000)

    # ------------------------------------------------------------------
    def refresh_status(self) -> None:
        missing = config.missing_items()
        if missing:
            self.status_left.setText("配置未完成：" + "、".join(missing))
        else:
            model = config.get("llm.model", "")
            mode = "超拟人" if config.get("xfyun.tts.mode") == "super" else "标准合成"
            self.status_left.setText(f"大模型 {model} · 讯飞语音已配置（{mode}）")
        total = InterviewRepo.count_all()
        self.status_right.setText(f"历史面试 {total} 场")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._checked_unfinished:
            self._checked_unfinished = True
            running = InterviewRepo.list_running()
            if running:
                self.toast.show_message(
                    f"有 {len(running)} 场面试未正常结束，可在面试历史中查看", 3600
                )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast._reposition()
