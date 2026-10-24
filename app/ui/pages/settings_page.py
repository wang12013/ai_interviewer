"""设置页：大模型、讯飞语音、面试偏好。"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...audio.speaker import Speaker
from ...audio.tts_client import probe_voices
from ...config import PROVIDER_PRESETS, VOICE_PRESETS, config, voice_short
from ...services.llm_client import LLMClient, friendly_error
from ..widgets.common import (
    card,
    divider,
    field,
    label,
    make_button,
    page_header,
    run_async,
    scroll_column,
    spacer,
    two_columns,
)

# 试听用的固定句子：覆盖陈述、数字和问句，能听出语速和语调是否合适
VOICE_TEST_TEXT = (
    "你好，我是今天的面试官。这轮大概二十分钟，"
    "我们先从你最近负责的项目聊起，你随时可以打断我。"
)

PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "dashscope": "通义千问 DashScope",
    "moonshot": "月之暗面 Kimi",
    "zhipu": "智谱 GLM",
    "spark": "讯飞星火",
    "openai": "OpenAI",
    "custom": "自定义（OpenAI 兼容）",
}

STYLE_LABELS = {"friendly": "温和", "neutral": "标准", "stress": "压力"}
STYLE_VALUES = {v: k for k, v in STYLE_LABELS.items()}


class SettingsPage(QWidget):
    saved = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._build()
        self.load_values()

    # -- 构建 ------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(32, 24, 32, 28)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        layout.addWidget(page_header(
            "设置",
            "配置大模型与讯飞语音服务，密钥仅保存在本机并加密存储。",
        ))

        layout.addWidget(self._build_llm_card())
        layout.addWidget(self._build_xfyun_card())
        layout.addWidget(self._build_interview_card())
        layout.addWidget(spacer(height=8))

        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(self._build_footer_bar(), 0)

    def _build_llm_card(self) -> QWidget:
        frame, layout = card(
            title="大模型",
            desc="OpenAI 兼容接口，支持 DeepSeek、通义、Kimi、智谱、星火、OpenAI 等。",
        )

        self.provider_combo = QComboBox()
        for key, name in PROVIDER_LABELS.items():
            self.provider_combo.addItem(name, key)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com/v1")
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("deepseek-chat")

        self.llm_key_edit = QLineEdit()
        self.llm_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_key_edit.setPlaceholderText("sk-...")
        self.show_llm_key = QCheckBox("显示")
        self.show_llm_key.toggled.connect(
            lambda checked: self.llm_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(8)
        key_layout.addWidget(self.llm_key_edit, 1)
        key_layout.addWidget(self.show_llm_key, 0)

        self.llm_temperature = QDoubleSpinBox()
        self.llm_temperature.setRange(0.0, 1.5)
        self.llm_temperature.setSingleStep(0.1)
        self.llm_temperature.setDecimals(1)
        self.llm_temperature.setFixedWidth(110)

        layout.addWidget(field("服务商", self.provider_combo))
        layout.addWidget(two_columns(
            field("Base URL", self.base_url_edit),
            field("模型名", self.model_edit),
        ))
        layout.addWidget(field("API Key", key_row,
                               "保存在本机 data/secrets.bin，使用 Windows DPAPI 加密"))
        layout.addWidget(field("温度（越低越稳定，出题建议 0.6 左右）", self.llm_temperature))

        layout.addWidget(divider())
        self.llm_test_btn = make_button("测试连接", kind="ghost", on_click=self._test_llm)
        self.llm_status = label("", "Hint", wrap=True)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.llm_test_btn, 0)
        row.addWidget(self.llm_status, 1)
        layout.addLayout(row)
        return frame

    def _build_xfyun_card(self) -> QWidget:
        frame, layout = card(
            title="讯飞语音",
            desc="同一个 AppID 下需要开通「实时语音转写」与「语音合成（超拟人）」。",
        )

        self.app_id_edit = QLineEdit()
        self.app_id_edit.setPlaceholderText("控制台 AppID")
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.xf_key_edit = QLineEdit()
        self.xf_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_xf_keys = QCheckBox("显示")
        self.show_xf_keys.toggled.connect(self._toggle_xf_keys)

        layout.addWidget(two_columns(
            field("AppID", self.app_id_edit),
            field("APISecret", self.secret_edit),
        ))
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(8)
        key_layout.addWidget(self.xf_key_edit, 1)
        key_layout.addWidget(self.show_xf_keys, 0)
        layout.addWidget(field("APIKey", key_row, "转写接口使用 APIKey 做签名，合成接口使用 APISecret"))

        layout.addWidget(divider())

        self.tts_mode_combo = QComboBox()
        self.tts_mode_combo.addItem("超拟人合成（拟人度最高）", "super")
        self.tts_mode_combo.addItem("标准合成（兜底）", "standard")

        self.voice_combo = QComboBox()
        for preset in VOICE_PRESETS:
            # 下拉里直接带出性别和用途，选音色时不用再去翻控制台
            self.voice_combo.addItem(preset.caption, preset.vcn)
        self.voice_combo.setMinimumWidth(260)

        self.speed_slider = self._slider(50)
        self.volume_slider = self._slider(60)
        self.pitch_slider = self._slider(50)
        self.speed_value = label("50", "Hint")
        self.volume_value = label("60", "Hint")
        self.pitch_value = label("50", "Hint")
        self.speed_slider.valueChanged.connect(lambda v: self.speed_value.setText(str(v)))
        self.volume_slider.valueChanged.connect(lambda v: self.volume_value.setText(str(v)))
        self.pitch_slider.valueChanged.connect(lambda v: self.pitch_value.setText(str(v)))

        layout.addWidget(two_columns(
            field("合成模式", self.tts_mode_combo),
            field("发音人", self.voice_combo),
        ))
        layout.addWidget(self._slider_row("语速", self.speed_slider, self.speed_value))
        layout.addWidget(self._slider_row("音量", self.volume_slider, self.volume_value))
        layout.addWidget(self._slider_row("音调", self.pitch_slider, self.pitch_value))

        layout.addWidget(divider())
        self.tts_enabled_check = QCheckBox("面试时让面试官开口提问（关闭后只用文字）")
        self.tts_enabled_check.setChecked(True)
        layout.addWidget(self.tts_enabled_check)

        self.voice_test_btn = make_button("试听发音人", kind="ghost", on_click=self._test_voice)
        self.voice_check_btn = make_button("检测全部音色", kind="ghost",
                                           on_click=self._check_voices)
        self.xf_status = label(
            "填好密钥后点「试听发音人」；若报未授权，点「检测全部音色」看哪个能用",
            "Hint", wrap=True,
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.voice_test_btn, 0)
        row.addWidget(self.voice_check_btn, 0)
        row.addWidget(self.xf_status, 1)
        layout.addLayout(row)
        return frame

    def _build_interview_card(self) -> QWidget:
        frame, layout = card(
            title="面试偏好",
            desc="这些参数决定面试时长与追问强度，可在每次新建面试时临时调整。",
        )

        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(5, 120)
        self.minutes_spin.setSuffix(" 分钟")
        self.minutes_spin.setFixedWidth(130)

        self.style_combo = QComboBox()
        for key, name in STYLE_LABELS.items():
            self.style_combo.addItem(name, key)

        self.followup_spin = QSpinBox()
        self.followup_spin.setRange(1, 5)
        self.followup_spin.setSuffix(" 层")
        self.followup_spin.setFixedWidth(110)

        self.vad_check = QCheckBox("静音自动结束本轮回答")
        self.vad_check.setChecked(True)
        self.vad_secs = QDoubleSpinBox()
        self.vad_secs.setRange(0.5, 6.0)
        self.vad_secs.setSingleStep(0.5)
        self.vad_secs.setDecimals(1)
        self.vad_secs.setSuffix(" 秒")
        self.vad_secs.setFixedWidth(110)

        self.save_audio_check = QCheckBox("保存每轮回答的录音（用于复盘回放）")
        self.save_audio_check.setChecked(True)

        layout.addWidget(two_columns(
            field("计划时长", self.minutes_spin, "低于此时长手动结束会提示"),
            field("面试风格", self.style_combo),
        ))
        layout.addWidget(two_columns(
            field("单题最大追问层数", self.followup_spin),
            field("静音判定阈值", self.vad_secs),
        ))
        layout.addWidget(self.vad_check)
        layout.addWidget(self.save_audio_check)
        return frame

    def _build_footer_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(58)
        row = QHBoxLayout(bar)
        row.setContentsMargins(32, 10, 32, 10)
        row.setSpacing(10)

        self.save_hint = label("", "Hint")
        row.addWidget(self.save_hint, 1)
        row.addWidget(make_button("恢复默认", kind="ghost", on_click=self._reset_defaults), 0)
        self.save_btn = make_button("保存设置", kind="primary", on_click=self._save)
        row.addWidget(self.save_btn, 0)
        return bar

    @staticmethod
    def _slider(value: int) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(value)
        return slider

    @staticmethod
    def _slider_row(name: str, slider: QSlider, value_label: QLabel) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        caption = label(name, "FieldLabel")
        caption.setFixedWidth(40)
        value_label.setFixedWidth(28)
        layout.addWidget(caption, 0)
        layout.addWidget(slider, 1)
        layout.addWidget(value_label, 0)
        return container

    # -- 数据 ------------------------------------------------------------
    def load_values(self) -> None:
        provider = config.get("llm.provider", "deepseek")
        index = self.provider_combo.findData(provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)

        self.base_url_edit.setText(str(config.get("llm.base_url", "")))
        self.model_edit.setText(str(config.get("llm.model", "")))
        self.llm_key_edit.setText(str(config.get("llm.api_key", "")))
        self.llm_temperature.setValue(float(config.get("llm.temperature", 0.7)))

        self.app_id_edit.setText(str(config.get("xfyun.app_id", "")))
        self.secret_edit.setText(str(config.get("xfyun.api_secret", "")))
        self.xf_key_edit.setText(str(config.get("xfyun.api_key", "")))

        mode = str(config.get("xfyun.tts.mode", "super"))
        mode_index = self.tts_mode_combo.findData(mode)
        self.tts_mode_combo.setCurrentIndex(max(0, mode_index))
        self.tts_enabled_check.setChecked(bool(config.get("xfyun.tts.enabled", True)))

        vcn = str(config.get("xfyun.tts.vcn", ""))
        voice_index = self.voice_combo.findData(vcn)
        # 配置里的音色不在列表（手填过或版本升级）时落到第一个，保存即纠正
        self.voice_combo.setCurrentIndex(voice_index if voice_index >= 0 else 0)

        self.speed_slider.setValue(int(config.get("xfyun.tts.speed", 50)))
        self.volume_slider.setValue(int(config.get("xfyun.tts.volume", 60)))
        self.pitch_slider.setValue(int(config.get("xfyun.tts.pitch", 50)))

        self.minutes_spin.setValue(int(config.get("interview.planned_minutes", 20)))
        style_index = self.style_combo.findData(config.get("interview.style", "friendly"))
        self.style_combo.setCurrentIndex(max(0, style_index))
        self.followup_spin.setValue(int(config.get("interview.max_followups", 3)))
        self.vad_secs.setValue(float(config.get("interview.auto_end_silence_sec", 2.0)))
        self.save_audio_check.setChecked(bool(config.get("interview.save_audio", True)))

        self._update_save_hint()

    def _collect(self) -> None:
        config.set("llm.provider", self.provider_combo.currentData())
        config.set("llm.base_url", self.base_url_edit.text().strip())
        config.set("llm.model", self.model_edit.text().strip())
        config.set("llm.api_key", self.llm_key_edit.text().strip())
        config.set("llm.temperature", round(self.llm_temperature.value(), 2))

        config.set("xfyun.app_id", self.app_id_edit.text().strip())
        config.set("xfyun.api_secret", self.secret_edit.text().strip())
        config.set("xfyun.api_key", self.xf_key_edit.text().strip())
        config.set("xfyun.tts.mode", self.tts_mode_combo.currentData())
        config.set("xfyun.tts.enabled", self.tts_enabled_check.isChecked())
        config.set("xfyun.tts.vcn", self.voice_combo.currentData())
        config.set("xfyun.tts.speed", self.speed_slider.value())
        config.set("xfyun.tts.volume", self.volume_slider.value())
        config.set("xfyun.tts.pitch", self.pitch_slider.value())

        config.set("interview.planned_minutes", self.minutes_spin.value())
        config.set("interview.style", self.style_combo.currentData())
        config.set("interview.max_followups", self.followup_spin.value())
        config.set("interview.auto_end_silence_sec", round(self.vad_secs.value(), 1))
        config.set("interview.save_audio", self.save_audio_check.isChecked())

    def _save(self) -> None:
        self._collect()
        config.save()
        self._update_save_hint()
        self.saved.emit()

    def _reset_defaults(self) -> None:
        from copy import deepcopy

        from ...config import DEFAULT_CONFIG

        config._data = deepcopy(DEFAULT_CONFIG)
        self.load_values()
        self.save_hint.setText("已恢复默认值，记得点保存")

    def _update_save_hint(self) -> None:
        missing = config.missing_items()
        if missing:
            self.save_hint.setText("待补充：" + "、".join(missing))
        else:
            self.save_hint.setText("配置完整")

    # -- 交互 ------------------------------------------------------------
    def _on_provider_changed(self) -> None:
        provider = self.provider_combo.currentData()
        preset = PROVIDER_PRESETS.get(provider)
        if not preset:
            return
        if provider != "custom":
            self.base_url_edit.setText(preset["base_url"])
            self.model_edit.setText(preset["model"])

    def _toggle_xf_keys(self, checked: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        self.secret_edit.setEchoMode(mode)
        self.xf_key_edit.setEchoMode(mode)

    def _test_llm(self) -> None:
        self._collect()
        if not self.base_url_edit.text().strip() or not self.llm_key_edit.text().strip():
            self._set_status(self.llm_status, "请先填写 Base URL 与 API Key", ok=False)
            return
        self.llm_test_btn.setEnabled(False)
        self._set_status(self.llm_status, "正在请求…", ok=None)

        def job():
            return LLMClient().test_connection()

        run_async(
            job,
            on_success=self._on_llm_tested,
            on_error=lambda msg: self._set_status(self.llm_status, msg, ok=False),
            on_finished=lambda: self.llm_test_btn.setEnabled(True),
        )

    def _on_llm_tested(self, result) -> None:
        ok, message, elapsed = result
        self._set_status(self.llm_status, f"{message} · {elapsed:.1f}s", ok=ok)

    def _test_voice(self) -> None:
        self._collect()  # 用界面上当前的选择试听，不必先保存
        missing = [
            name for path, name in (
                ("xfyun.app_id", "AppID"),
                ("xfyun.api_key", "APIKey"),
                ("xfyun.api_secret", "APISecret"),
            ) if not str(config.get(path) or "").strip()
        ]
        if missing:
            self._set_status(self.xf_status, "请先填写讯飞" + "、".join(missing), ok=False)
            return

        self.voice_test_btn.setEnabled(False)
        self._set_status(self.xf_status, "正在合成并播放…", ok=None)

        def job():
            speaker = Speaker()
            errors: list[str] = []
            speaker.on_error = errors.append
            started = time.monotonic()
            speaker.say(VOICE_TEST_TEXT, blocking=True, timeout=60)
            elapsed = time.monotonic() - started
            report = {
                "errors": errors,
                "elapsed": elapsed,
                "degraded": speaker.degraded,
                "detail": speaker.degraded_reason,
                "voice_notice": speaker.voice_notice,
                "voice": speaker.voice,
            }
            speaker.close()
            return report

        run_async(
            job,
            on_success=self._on_voice_tested,
            on_error=lambda msg: self._set_status(self.xf_status, msg, ok=False),
            on_finished=lambda: self.voice_test_btn.setEnabled(True),
        )

    def _on_voice_tested(self, report: dict) -> None:
        errors = report.get("errors") or []
        elapsed = float(report.get("elapsed") or 0)
        if errors:
            self._set_status(self.xf_status, errors[0], ok=False)
            return
        # 音色被自动替换过：能出声但换了人，必须让用户知道
        notice = str(report.get("voice_notice") or "")
        if notice:
            self._set_status(self.xf_status, notice + "，试听用的是兜底音色", ok=False)
            return
        if report.get("degraded"):
            reason = report.get("detail") or "超拟人合成不可用"
            self._set_status(
                self.xf_status,
                f"{reason}；已用标准发音人试听（{elapsed:.1f}s），拟人感会弱一些",
                ok=False,
            )
            return
        mode = "超拟人" if str(config.get("xfyun.tts.mode")) == "super" else "标准"
        voice = voice_short(str(report.get("voice") or ""))
        self._set_status(self.xf_status,
                         f"{mode}合成正常 · {voice} · 用时 {elapsed:.1f}s", ok=True)

    # ------------------------------------------------------------------
    # 音色检测
    # ------------------------------------------------------------------
    def _check_voices(self) -> None:
        """逐个试每个音色，直接告诉用户哪几个是真的开通了。"""
        self._collect()
        missing = [
            name for path, name in (
                ("xfyun.app_id", "AppID"),
                ("xfyun.api_key", "APIKey"),
                ("xfyun.api_secret", "APISecret"),
            ) if not str(config.get(path) or "").strip()
        ]
        if missing:
            self._set_status(self.xf_status, "请先填写讯飞" + "、".join(missing), ok=False)
            return

        self.voice_test_btn.setEnabled(False)
        self.voice_check_btn.setEnabled(False)
        self._set_status(self.xf_status, "正在逐个检测音色，每个约 1 秒…", ok=None)

        run_async(
            probe_voices,
            on_success=self._on_voices_checked,
            on_error=lambda msg: self._set_status(self.xf_status, msg, ok=False),
            on_finished=lambda: (
                self.voice_test_btn.setEnabled(True),
                self.voice_check_btn.setEnabled(True),
            ),
        )

    def _on_voices_checked(self, report: list) -> None:
        available = [item for item in report if item.ok]
        failed = [item for item in report if not item.ok]
        if not failed:
            fastest = min((item.cost_ms for item in available), default=0)
            self._set_status(
                self.xf_status,
                f"{len(available)} 个音色全部可用 · 最快 {fastest}ms",
                ok=True,
            )
            return

        if not available:
            codes = "；".join(f"{item.name} {item.code or ''}".strip()
                             for item in failed[:3])
            self._set_status(
                self.xf_status,
                f"全部音色都失败：{codes}。请确认控制台已开通「超拟人语音合成」",
                ok=False,
            )
            return

        names = "、".join(item.name for item in available)
        blocked = "；".join(
            f"{item.name}（{item.code or '不可用'}）" for item in failed[:3]
        )
        more = f" 等 {len(failed)} 个" if len(failed) > 3 else ""
        self._set_status(
            self.xf_status,
            f"可用：{names}｜未开通：{blocked}{more}",
            ok=False,
        )

    @staticmethod
    def _set_status(target: QLabel, text: str, *, ok: bool | None) -> None:
        target.setText(text)
        target.setObjectName(
            "SuccessHint" if ok is True else "ErrorHint" if ok is False else "Hint"
        )
        target.style().unpolish(target)
        target.style().polish(target)
