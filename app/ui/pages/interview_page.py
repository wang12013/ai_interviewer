"""面试对话页。

M3 阶段为纯文字对话：面试官的话以气泡呈现，候选人在这里打字回答。
M4/M5 会把同一套气泡接上语音播报与实时转写，所以这里刻意把「消息呈现」
和「音频状态」分成两组接口，后面只需在气泡上挂音频控件。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...audio.speaker import Speaker
from ...audio.transcriber import VoiceTranscriber
from ...config import config
from ...models import PHASE_LABELS, format_duration, human_duration
from ...paths import audio_dir_for
from ...repositories import InterviewRepo, MessageRepo, QuestionRepo
from ...services.director import InterviewDirector, Turn
from ...services.prompts import STYLE_PROFILES
from ..widgets.common import label, make_button, run_async

logger = logging.getLogger("ui")

PHASE_SEQUENCE = ("opening", "resume", "jd", "closing")
BUBBLE_MAX_WIDTH = 620


class AnswerEdit(QPlainTextEdit):
    """Enter 发送，Shift+Enter 换行；输入法组合中按 Enter 不触发发送。"""

    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._composing = False

    def inputMethodEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if is_enter and not shift and not self._composing:
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class InterviewPage(QWidget):
    finished = Signal(str)
    # 语音回调来自合成/播放线程，必须经信号排队回主线程再碰控件
    speakStateChanged = Signal(str)
    speakFailed = Signal(str)
    # 转写回调来自采集/上传线程，同样要经信号回主线程
    voicePartial = Signal(str)
    voiceFinal = Signal(str)
    voiceError = Signal(str)
    voiceState = Signal(str)
    voiceLevel = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")

        self._interview_id: str | None = None
        self._record: dict = {}
        self._director: InterviewDirector | None = None
        self._planned_seconds = 20 * 60
        self._busy = False
        self._alive = False
        self._typing_row: QWidget | None = None
        self._typing_label: QLabel | None = None
        self._typing_base = ""
        self._dots = 0
        self._chips: dict[str, QLabel] = {}

        self._speaker: Speaker | None = None
        self._speaking = False
        self._speech_error_shown = False
        self._degrade_noticed = False

        # 语音回答：转写器懒加载，partial 用一个临时气泡呈现
        self._transcriber: VoiceTranscriber | None = None
        self._listening = False
        self._partial_row: QWidget | None = None
        self._partial_label: QLabel | None = None
        self._voice_error_shown = False

        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(420)
        self._typing_timer.timeout.connect(self._animate_typing)

        self.speakStateChanged.connect(self._on_speak_state)
        self.speakFailed.connect(self._on_speak_failed)
        self.voicePartial.connect(self._on_voice_partial)
        self.voiceFinal.connect(self._on_voice_final)
        self.voiceError.connect(self._on_voice_error)
        self.voiceState.connect(self._on_voice_state)
        self.voiceLevel.connect(self._on_voice_level)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_chat(), 1)
        outer.addWidget(self._build_composer(), 0)

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("InterviewBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 14, 24, 12)
        layout.setSpacing(18)

        text_box = QWidget()
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        self.title_label = label("", "PageTitle")
        self.meta_label = label("", "PageSubtitle")
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.meta_label)
        layout.addWidget(text_box, 1)

        chips = QWidget()
        chips_layout = QHBoxLayout(chips)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)
        for phase in PHASE_SEQUENCE:
            chip = label(PHASE_LABELS.get(phase, phase), "PhaseChip")
            chip.setProperty("active", False)
            self._chips[phase] = chip
            chips_layout.addWidget(chip)
        layout.addWidget(chips, 0, Qt.AlignmentFlag.AlignVCenter)

        timer_box = QWidget()
        timer_layout = QVBoxLayout(timer_box)
        timer_layout.setContentsMargins(0, 0, 0, 0)
        timer_layout.setSpacing(0)
        self.timer_label = label("00:00", "TimerLabel")
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.timer_hint = label("", "TimerHint")
        self.timer_hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        timer_layout.addWidget(self.timer_label)
        timer_layout.addWidget(self.timer_hint)
        layout.addWidget(timer_box, 0, Qt.AlignmentFlag.AlignVCenter)

        self.end_btn = make_button("结束本轮", kind="danger", on_click=self._end_clicked)
        self.end_btn.setFixedWidth(92)
        layout.addWidget(self.end_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return bar

    def _build_chat(self) -> QWidget:
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("ChatScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        canvas = QWidget()
        canvas.setObjectName("ChatCanvas")
        self.chat_layout = QVBoxLayout(canvas)
        self.chat_layout.setContentsMargins(26, 18, 26, 20)
        self.chat_layout.setSpacing(11)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chat_scroll.setWidget(canvas)
        return self.chat_scroll

    def _build_composer(self) -> QWidget:
        composer = QFrame()
        composer.setObjectName("Composer")
        layout = QVBoxLayout(composer)
        layout.setContentsMargins(26, 12, 26, 12)
        layout.setSpacing(7)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.edit = AnswerEdit()
        self.edit.setObjectName("AnswerEdit")
        self.edit.setFixedHeight(78)
        self.edit.setPlaceholderText("把你的回答写在这里，想清楚再说也没关系…")
        self.edit.submitted.connect(self._send)
        row.addWidget(self.edit, 1)

        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(7)
        self.send_btn = make_button("发送", kind="primary", on_click=self._send)
        self.send_btn.setFixedWidth(88)
        self.mic_btn = make_button("语音回答", kind="ghost", on_click=self._toggle_mic)
        self.mic_btn.setFixedWidth(88)
        self.mic_btn.setEnabled(False)
        self.mic_btn.setToolTip("点一下开始说话，说完再点一下；停顿一段时间会自动结束")
        side.addWidget(self.send_btn)
        side.addWidget(self.mic_btn)
        row.addLayout(side)

        layout.addLayout(row)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.speak_status = label("", "SpeakStatus")
        self.speak_status.setVisible(False)
        self.skip_btn = make_button("跳过播报", kind="link",
                                    on_click=self._skip_speech)
        self.skip_btn.setVisible(False)
        status_row.addWidget(self.speak_status, 0)
        status_row.addWidget(self.skip_btn, 0)
        status_row.addStretch(1)
        self.hint_label = label("Enter 发送 · Shift + Enter 换行", "Hint")
        status_row.addWidget(self.hint_label, 0)
        layout.addLayout(status_row)
        return composer

    # ------------------------------------------------------------------
    # 载入
    # ------------------------------------------------------------------
    def load(self, interview_id: str) -> None:
        self._teardown()
        # 上一次面试留下的播报器可能带着旧的音色/协议兜底决定，必须重建
        self.reload_voice()
        self._interview_id = interview_id
        self._record = InterviewRepo.get(interview_id) or {}
        self._alive = True
        self.end_btn.setVisible(True)
        self.end_btn.setEnabled(True)
        self.hint_label.setText("Enter 发送 · Shift + Enter 换行")
        self._apply_speak_state(False)
        self._clear_partial()
        self._listening = False
        self._voice_error_shown = False
        self._set_mic_button("语音回答", active=False, enabled=self._mic_capable())

        try:
            self._director = InterviewDirector(interview_id)
        except Exception as exc:  # noqa: BLE001
            self._director = None
            self._render_header(self._record)
            self._clear_chat()
            self._add_notice(f"无法载入这一场面试：{exc}")
            self._lock_down("面试无法开始")
            return

        self._render_header(self._record)
        self._clear_chat()

        messages = MessageRepo.list_by_interview(interview_id)
        for message in messages:
            self._add_message(message.get("role") or "assistant", message.get("content") or "")

        running = (self._record.get("status") or "running") == "running"
        self._refresh_progress()
        self._tick()
        self._timer.start()

        if not running:
            self._add_notice("这一场面试已经结束，下面是完整对话记录。")
            self._lock_down("面试已结束")
            return

        if messages:
            self._add_notice("已恢复上次的对话，可以接着回答。")
            self._set_busy(False)
            self.edit.setFocus()
            return

        self._add_notice("面试开始，听完问题直接作答即可。")
        self._set_busy(True)
        self._show_typing("面试官正在准备第一个问题")
        run_async(self._director.begin, on_success=self._on_turn, on_error=self._on_error)

    def _teardown(self) -> None:
        self._alive = False
        self._timer.stop()
        self._typing_timer.stop()
        self._typing_row = None
        self._typing_label = None
        self._director = None
        self._cancel_voice()
        if self._transcriber is not None:
            self._transcriber.shutdown()
            self._transcriber = None
        if self._speaker is not None:
            self._speaker.stop()
        self._speaking = False
        self._speech_error_shown = False
        self._degrade_noticed = False
        self._apply_speak_state(False)

    def reload_voice(self) -> None:
        """丢掉旧的播报器，下一句话按最新的设置重新建。

        面试页是单例，``_speaker`` 会跨场存活。若不在这里重建，既有的
        音色兜底决定（以及构造时读到的协议）会一直压着新配置，用户改了
        发音人却听不到变化。
        """
        speaker, self._speaker = self._speaker, None
        if speaker is None:
            return
        # 先摘回调，后台线程收尾时才不会往界面推已过期的状态
        speaker.on_state = None
        speaker.on_error = None
        try:
            speaker.stop()
            speaker.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("释放旧播报器失败：%s", exc)

    def _render_header(self, record: dict) -> None:
        company = str(record.get("company") or "")
        position = str(record.get("position") or "模拟面试")
        self.title_label.setText(f"{company} · {position}" if company else position)

        minutes = int(record.get("planned_minutes") or 20)
        self._planned_seconds = minutes * 60
        style = str(record.get("style") or "friendly")
        style_label = STYLE_PROFILES.get(style, {}).get("label", "温和")
        total = QuestionRepo.stats(self._interview_id or "").get("total", 0)
        self.meta_label.setText(
            f"计划 {minutes} 分钟 · 风格{style_label} · 主问题 {total} 道"
        )
        self.timer_hint.setText(f"共 {format_duration(self._planned_seconds)}")

    def _clear_chat(self) -> None:
        self._typing_timer.stop()
        self._typing_row = None
        self._typing_label = None
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget:
                # 先摘掉父子关系，否则 deleteLater 生效前旧气泡会和新气泡叠在一起
                widget.setParent(None)
                widget.deleteLater()

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------
    def _send(self) -> None:
        if not self._alive or self._busy or not self._director:
            return
        text = self.edit.toPlainText().strip()
        if not text:
            return
        self.edit.clear()
        self._submit_text(text)

    def _submit_text(self, text: str) -> None:
        """把一段回答交给面试官。键盘和语音最终都走这里。"""
        if not self._alive or self._busy or not self._director:
            return
        text = (text or "").strip()
        if not text:
            return
        self._add_message("user", text)
        self._set_busy(True)
        self._show_typing("面试官正在思考")
        run_async(self._director.respond, text, on_success=self._on_turn, on_error=self._on_error)

    def _on_turn(self, turn: Turn | None) -> None:
        if not self._alive or turn is None:
            return
        self._hide_typing()

        if turn.text:
            self._add_message("assistant", turn.text)
            self._speak(turn.text)
        if turn.forced:
            self._add_notice(turn.forced)

        self._refresh_progress(turn)

        if turn.action == "end":
            self._lock_down("本场面试已结束")
            self._add_notice("面试已结束，可以在「面试历史」里查看这一场的记录。")
            return

        self._set_busy(False)
        self.edit.setFocus()

    def _on_error(self, message: str) -> None:
        if not self._alive:
            return
        self._hide_typing()
        self._add_notice(f"这一轮没走通：{message}")
        self._set_busy(False)

    def _add_message(self, role: str, text: str) -> QWidget:
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(9)

        is_user = role == "user"
        avatar = QLabel("我" if is_user else "面")
        avatar.setObjectName("AvatarUser" if is_user else "AvatarAI")
        avatar.setFixedSize(28, 28)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        bubble = QFrame()
        bubble.setObjectName("BubbleUser" if is_user else "BubbleAI")
        bubble.setMaximumWidth(BUBBLE_MAX_WIDTH)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(14, 11, 14, 11)
        bubble_layout.setSpacing(2)
        content = label(text or "", "BubbleTextUser" if is_user else "BubbleText", wrap=True)
        content.setFixedWidth(self._text_width(content, text or ""))
        bubble_layout.addWidget(content)

        if is_user:
            outer.addStretch(1)
            outer.addWidget(bubble, 0)
            outer.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        else:
            outer.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            outer.addWidget(bubble, 0)
            outer.addStretch(1)

        self.chat_layout.addWidget(row)
        self._scroll_bottom()
        return row

    @staticmethod
    def _text_width(widget: QLabel, text: str) -> int:
        """按换行后的实际行宽定气泡宽度，否则 QLabel 的 sizeHint 会把气泡压得很窄。"""
        widget.ensurePolished()
        limit = BUBBLE_MAX_WIDTH - 34
        metrics = widget.fontMetrics()
        rect = metrics.boundingRect(
            QRect(0, 0, limit, 10000), Qt.TextFlag.TextWordWrap, text or " "
        )
        return max(48, min(limit, rect.width() + 4))

    def _add_notice(self, text: str) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(0)
        notice = label(text, "SystemNotice", wrap=True)
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(notice, 1)
        self.chat_layout.addWidget(row)
        self._scroll_bottom()

    # ------------------------------------------------------------------
    # 思考中的占位气泡
    # ------------------------------------------------------------------
    def _show_typing(self, base_text: str) -> None:
        self._hide_typing()
        self._typing_base = base_text
        self._dots = 0

        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(9)

        avatar = QLabel("面")
        avatar.setObjectName("AvatarAI")
        avatar.setFixedSize(28, 28)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        bubble = QFrame()
        bubble.setObjectName("BubbleTyping")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(14, 11, 14, 11)
        bubble_layout.setSpacing(0)
        text_label = label(base_text, "TypingText")
        bubble_layout.addWidget(text_label)

        outer.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        outer.addWidget(bubble, 0)
        outer.addStretch(1)

        self._typing_row = row
        self._typing_label = text_label
        self.chat_layout.addWidget(row)
        self._typing_timer.start()
        self._scroll_bottom()

    def _animate_typing(self) -> None:
        if not self._typing_label:
            return
        self._dots = (self._dots + 1) % 4
        self._typing_label.setText(self._typing_base + "…" * (self._dots + 1))

    def _hide_typing(self) -> None:
        self._typing_timer.stop()
        if self._typing_row is not None:
            self.chat_layout.removeWidget(self._typing_row)
            self._typing_row.setParent(None)
            self._typing_row.deleteLater()
        self._typing_row = None
        self._typing_label = None

    def _scroll_bottom(self) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        QTimer.singleShot(30, lambda: bar.setValue(bar.maximum()))

    # ------------------------------------------------------------------
    # 计时与进度
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if not self._director:
            return
        elapsed = self._director.elapsed_seconds()
        self.timer_label.setText(format_duration(elapsed))
        remain = self._planned_seconds - elapsed
        if remain < 0:
            self.timer_hint.setText("已超出计划，可随时结束")
            self._restyle(self.timer_label, "TimerOver")
        elif remain <= 180:
            self.timer_hint.setText(f"还剩 {format_duration(remain)}")
            self._restyle(self.timer_label, "TimerWarn")
        else:
            self.timer_hint.setText(f"共 {format_duration(self._planned_seconds)}")
            self._restyle(self.timer_label, "TimerLabel")

    def _refresh_progress(self, turn: Turn | None = None) -> None:
        if not self._interview_id:
            return
        progress = QuestionRepo.phase_progress(self._interview_id)
        current = turn.phase if turn else self._current_phase()
        for phase, chip in self._chips.items():
            done, total = progress.get(phase, (0, 0))
            chip.setText(f"{PHASE_LABELS.get(phase, phase)} {done}/{total}" if total
                         else PHASE_LABELS.get(phase, phase))
            chip.setProperty("active", phase == current)
            chip.style().unpolish(chip)
            chip.style().polish(chip)

        minutes = int(self._record.get("planned_minutes") or 20)
        if turn and turn.depth:
            self.meta_label.setText(f"计划 {minutes} 分钟 · 正在第 {turn.depth} 层追问")
        else:
            self.meta_label.setText(
                f"计划 {minutes} 分钟 · 当前{PHASE_LABELS.get(current, current)}阶段"
            )

    def _current_phase(self) -> str:
        return self._director.phase if self._director else "opening"

    @staticmethod
    def _restyle(widget: QWidget, name: str) -> None:
        if widget.objectName() == name:
            return
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ------------------------------------------------------------------
    # 状态开关
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        editable = self._alive and not busy
        self.edit.setEnabled(editable)
        self.send_btn.setEnabled(editable)
        self.send_btn.setText("思考中" if busy else "发送")
        # 录音时不受 busy 影响，否则用户刚说两句就被锁住了
        if not self._listening:
            self.mic_btn.setEnabled(editable and self._mic_capable())

    def _lock_down(self, hint: str) -> None:
        self._busy = False
        self._alive = False
        self._timer.stop()
        self._hide_typing()
        self._cancel_voice()
        if self._speaker is not None:
            self._speaker.stop()
        self.speak_status.setVisible(False)
        self.skip_btn.setVisible(False)
        self.edit.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.send_btn.setText("已结束")
        self.mic_btn.setEnabled(False)
        self.end_btn.setVisible(False)
        self.hint_label.setText(hint)

    # ------------------------------------------------------------------
    # 语音回答（麦克风实时转写）
    # ------------------------------------------------------------------
    @staticmethod
    def _mic_capable() -> bool:
        """有 AppID + APIKey 才能转写；缺任何一个都直接把按钮点灰。"""
        return bool(str(config.get("xfyun.app_id") or "").strip()
                    and str(config.get("xfyun.api_key") or "").strip())

    def _ensure_transcriber(self) -> VoiceTranscriber:
        if self._transcriber is None:
            transcriber = VoiceTranscriber()
            # 全部转成信号：这些回调来自后台线程，直接碰控件会崩
            transcriber.on_partial = self.voicePartial.emit
            transcriber.on_final = self.voiceFinal.emit
            transcriber.on_error = self.voiceError.emit
            transcriber.on_state = self.voiceState.emit
            transcriber.on_level = self.voiceLevel.emit
            self._transcriber = transcriber
        return self._transcriber

    def _toggle_mic(self) -> None:
        if not self._alive or self._busy:
            return
        if self._listening:
            self._stop_voice()
            return

        missing = [
            name for key, name in (
                ("xfyun.app_id", "AppID"), ("xfyun.api_key", "APIKey"),
            ) if not str(config.get(key) or "").strip()
        ]
        if missing:
            self._add_notice("语音回答需要先在设置里填讯飞" + "、".join(missing))
            return

        # 面试官还在读题就先掐掉播报，否则麦克风会把播报声录进去
        if self._speaking:
            self._skip_speech()
            self._add_notice("已打断播报，开始录音")

        transcriber = self._ensure_transcriber()
        transcriber.audio_dir = audio_dir_for(self._interview_id or "unknown")
        self._voice_error_shown = False
        transcriber.start()

    def _stop_voice(self) -> None:
        if self._transcriber is None:
            return
        self.mic_btn.setEnabled(False)
        self._transcriber.stop()

    def _cancel_voice(self) -> None:
        """放弃当前这段录音（结束时/切场时用）。"""
        if self._transcriber is not None and self._transcriber.active:
            self._transcriber.shutdown()
        self._listening = False
        self._clear_partial()

    def _on_voice_state(self, state: str) -> None:
        if not self._alive and state != "idle":
            return
        if state == "connecting":
            self._set_mic_button("连接中…", active=False, enabled=False)
            self.hint_label.setText("正在连接转写服务…")
        elif state == "listening":
            self._listening = True
            self._set_mic_button("● 结束", active=True, enabled=True)
            self._show_partial("")
            self._update_listen_hint(0.0)
        elif state == "thinking":
            self._set_mic_button("转写中…", active=False, enabled=False)
            self.hint_label.setText("正在整理你刚才的回答…")
        else:
            self._listening = False
            self._set_mic_button("语音回答", active=False,
                                 enabled=self._alive and not self._busy and self._mic_capable())
            if self._alive:
                self.hint_label.setText("Enter 发送 · Shift + Enter 换行")

    def _set_mic_button(self, text: str, *, active: bool, enabled: bool) -> None:
        self.mic_btn.setText(text)
        self.mic_btn.setObjectName("Danger" if active else "Ghost")
        self.mic_btn.setEnabled(enabled)
        # objectName 变了要重新算样式，否则按钮颜色不会跟着换
        self.mic_btn.style().unpolish(self.mic_btn)
        self.mic_btn.style().polish(self.mic_btn)

    def _update_listen_hint(self, level: float) -> None:
        seconds = config.get("interview.auto_end_silence_sec", 2.0)
        try:
            seconds = float(seconds or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        bar = "▁▃▅▇"[max(0, min(3, int(level * 3.999)))]
        tail = f"· 停顿 {seconds:.0f} 秒自动结束" if seconds > 0 else "· 说完点结束"
        self.hint_label.setText(f"正在听你说 {bar}  {tail}")

    def _on_voice_level(self, value: float) -> None:
        if self._listening and self._alive:
            self._update_listen_hint(value)

    def _on_voice_partial(self, text: str) -> None:
        if not self._alive or not self._listening:
            return
        self._show_partial(text)

    def _on_voice_final(self, text: str) -> None:
        if not self._alive:
            return
        self._clear_partial()
        text = (text or "").strip()
        if not text:
            return
        self._submit_text(text)

    def _on_voice_error(self, message: str) -> None:
        if not self._alive:
            return
        self._clear_partial()
        self._listening = False
        self._set_mic_button("语音回答", active=False,
                             enabled=not self._busy and self._mic_capable())
        if self._voice_error_shown:
            return
        self._voice_error_shown = True
        self._add_notice(f"{message}；也可以直接打字回答")

    # -- partial 气泡 ---------------------------------------------------
    def _show_partial(self, text: str) -> None:
        """正在转写的临时气泡：灰色斜体，不落库，定稿时被正式气泡替换。"""
        if self._partial_row is None:
            row = QWidget()
            outer = QHBoxLayout(row)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(9)

            avatar = QLabel("我")
            avatar.setObjectName("AvatarUser")
            avatar.setFixedSize(28, 28)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

            bubble = QFrame()
            bubble.setObjectName("BubbleVoice")
            bubble_layout = QVBoxLayout(bubble)
            bubble_layout.setContentsMargins(14, 11, 14, 11)
            bubble_layout.setSpacing(0)
            content = label("", "VoiceText", wrap=True)
            content.setFixedWidth(220)
            bubble_layout.addWidget(content)

            outer.addStretch(1)
            outer.addWidget(bubble, 0)
            outer.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)

            self._partial_row = row
            self._partial_label = content
            self.chat_layout.addWidget(row)

        self._partial_label.setText(text or "正在听…")
        self._partial_label.setFixedWidth(
            self._text_width(self._partial_label, text or "正在听…")
        )
        self._scroll_bottom()

    def _clear_partial(self) -> None:
        row, self._partial_row = self._partial_row, None
        self._partial_label = None
        if row is not None:
            self.chat_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()

    # ------------------------------------------------------------------
    # 语音播报
    # ------------------------------------------------------------------
    @staticmethod
    def _tts_enabled() -> bool:
        return bool(config.get("xfyun.tts.enabled", True))

    def _ensure_speaker(self) -> Speaker | None:
        """懒加载：只有真的要出声时才去碰音频设备。"""
        if self._speaker is not None:
            return self._speaker
        if not self._tts_enabled():
            return None
        try:
            speaker = Speaker()
        except Exception as exc:  # noqa: BLE001
            logger.warning("语音模块初始化失败：%s", exc)
            return None
        speaker.on_state = self.speakStateChanged.emit
        speaker.on_error = self.speakFailed.emit
        self._speaker = speaker
        return speaker

    def _speak(self, text: str) -> None:
        speaker = self._ensure_speaker()
        if speaker is None:
            return
        ready, reason = speaker.ready()
        if not ready:
            if not self._speech_error_shown:
                self._speech_error_shown = True
                self._add_notice(f"{reason}，面试官暂时只用文字提问")
            return
        speaker.say(text)
        # 降级是异步发生的，稍后再看一次结果
        QTimer.singleShot(2500, self._check_degrade)

    def _check_degrade(self) -> None:
        if self._degrade_noticed or self._speaker is None:
            return
        # 音色被自动换掉：协议还是超拟人，但说话的人变了，必须单独提示
        notice = self._speaker.voice_notice
        if notice:
            self._degrade_noticed = True
            self._add_notice(f"{notice}；可在设置页换一个已开通的音色")
            return
        if not self._speaker.degraded:
            return
        self._degrade_noticed = True
        reason = self._speaker.degraded_reason or "超拟人合成不可用"
        self._add_notice(f"{reason}，已改用标准发音人，听感会朴素一些")

    def _skip_speech(self) -> None:
        if self._speaker is not None:
            self._speaker.stop()

    def _on_speak_state(self, state: str) -> None:
        try:
            self._apply_speak_state(state == "speaking" and self._alive)
        except RuntimeError:
            # 窗口已经销毁，迟到的播报状态直接丢掉
            pass

    def _apply_speak_state(self, speaking: bool) -> None:
        if speaking and self._listening:
            # 面试官开口了说明这一轮回答已经结束，先停采集并把内容交上去，
            # 否则麦克风会把播报声一起录进来
            self._stop_voice()
        self._speaking = speaking
        self.speak_status.setVisible(speaking)
        self.skip_btn.setVisible(speaking)
        if speaking:
            self.speak_status.setText("面试官正在说话")
        if self._alive:
            self.hint_label.setText(
                "可以随时点「跳过播报」打断" if speaking
                else "Enter 发送 · Shift + Enter 换行"
            )

    def _on_speak_failed(self, message: str) -> None:
        logger.warning("语音播报失败：%s", message)
        if not self._alive or self._speech_error_shown:
            return
        self._speech_error_shown = True
        try:
            self._add_notice(f"语音播报出错：{message}。文字对话不受影响。")
        except RuntimeError:
            pass

    def shutdown(self) -> None:
        """窗口关闭时释放音频设备。"""
        if self._transcriber is not None:
            # 摘掉回调，避免收尾线程往已销毁的控件 emit
            self._transcriber.on_partial = None
            self._transcriber.on_final = None
            self._transcriber.on_error = None
            self._transcriber.on_state = None
            self._transcriber.on_level = None
            self._transcriber.shutdown()
            self._transcriber = None
        if self._speaker is not None:
            # 先摘掉回调，否则后台线程收尾时会往已销毁的控件上报状态
            self._speaker.on_state = None
            self._speaker.on_error = None
            self._speaker.close()
            self._speaker = None

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 切到别的页面就别继续说下去了
        if self._speaker is not None:
            self._speaker.stop()
        # 离开面试页时把正在录的音也停掉，别在后台偷偷占用麦克风
        self._cancel_voice()
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # 结束
    # ------------------------------------------------------------------
    def _end_clicked(self) -> None:
        if not self._director or not self._alive:
            return
        elapsed = self._director.elapsed_seconds()
        box = QMessageBox(self)
        box.setWindowTitle("结束本轮面试")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"已进行 {format_duration(elapsed)}，确定结束本轮面试？")
        box.setInformativeText("结束后不再继续提问，这一场的记录会保存到面试历史。")
        confirm = box.addButton("结束面试", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("继续面试", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        self._finish_manually()

    def _finish_manually(self) -> None:
        """真正落库并通知外部；确认框的逻辑单独放在 _end_clicked 里，方便测试。"""
        if not self._director or not self._alive:
            return
        result = self._director.finish("manual")
        self._lock_down(f"本场面试已结束 · 用时 {human_duration(result.get('duration'))}")
        self._add_notice(
            f"面试结束，用时 {human_duration(result.get('duration'))}，记录已保存。"
        )
        self.finished.emit(self._interview_id or "")
