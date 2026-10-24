"""新建面试页：导入 JD 与简历，生成题库，确认后进入面试。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...config import config, voice_short
from ...models import PHASE_LABELS, Question, dumps
from ...repositories import InterviewRepo, QuestionRepo
from ...services.doc_parser import extract_text, guess_meta, summarize_preview
from ...services.profiler import Profiler
from ...services.question_planner import PlanResult, QuestionPlanner
from ..widgets.common import (
    card,
    divider,
    field,
    label,
    make_button,
    page_header,
    run_async,
    two_columns,
)
from ..widgets.file_drop import FileDropArea

STYLE_LABELS = {"friendly": "温和", "neutral": "标准", "stress": "压力"}
DIFFICULTY_DOTS = {1: "基础", 2: "进阶", 3: "挑战"}


class SetupPage(QWidget):
    interviewCreated = Signal(str)
    gotoSettings = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._jd_text = ""
        self._resume_text = ""
        self._jd_profile: dict = {}
        self._resume_profile: dict = {}
        self._profiler: Profiler | None = None
        self._planner: QuestionPlanner | None = None
        self._plan: PlanResult | None = None
        self._interview_id: str | None = None
        self._busy = False
        self._build()
        self._refresh_config_summary()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
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
            "新建面试",
            "导入目标岗位的 JD 和你自己的简历，AI 会据此准备一整套针对性问题。",
        ))

        layout.addWidget(self._build_material_card())
        layout.addWidget(self._build_summary_card())
        layout.addLayout(self._build_action_row())
        layout.addWidget(self._build_preview_card())
        layout.addWidget(self._build_hint_footer())

        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll)

    def _build_material_card(self) -> QWidget:
        frame, layout = card(title="导入材料", desc="支持 PDF、Word、TXT 与 Markdown，可以直接把文件拖进来。")

        self.jd_drop = FileDropArea(
            "拖入岗位 JD",
            "支持 PDF / DOCX / TXT / MD，或点击选择文件",
        )
        self.jd_drop.fileSelected.connect(self._on_jd_selected)
        self.jd_drop.fileCleared.connect(self._on_jd_cleared)

        self.resume_drop = FileDropArea(
            "拖入你的简历",
            "支持 PDF / DOCX / TXT / MD，或点击选择文件",
        )
        self.resume_drop.fileSelected.connect(self._on_resume_selected)
        self.resume_drop.fileCleared.connect(self._on_resume_cleared)

        layout.addWidget(two_columns(self.jd_drop, self.resume_drop))

        self.company_edit = QLineEdit()
        self.company_edit.setPlaceholderText("选填，例如：字节跳动")
        self.position_edit = QLineEdit()
        self.position_edit.setPlaceholderText("选填，例如：高级后端工程师")
        layout.addWidget(two_columns(
            field("公司", self.company_edit, "导入 JD 后会自动识别，可修改"),
            field("岗位", self.position_edit, "用于生成面试官人设与话术"),
        ))
        return frame

    def _build_summary_card(self) -> QWidget:
        frame, layout = card(title="本轮配置", desc="使用全局设置中的偏好，随时可以调整。")
        self.summary_label = label("", "CardDesc", wrap=True)
        layout.addWidget(self.summary_label)
        self.settings_link = make_button("调整面试设置", kind="link",
                                         on_click=lambda: self.gotoSettings.emit())
        layout.addWidget(self.settings_link, 0, Qt.AlignmentFlag.AlignLeft)
        return frame

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(12)

        self.status_label = label("", "Hint", wrap=True)
        row.addWidget(self.status_label, 1)

        self.start_btn = make_button("开始面试", kind="primary", on_click=self._on_start_clicked)
        row.addWidget(self.start_btn, 0)
        return row

    def _build_preview_card(self) -> QWidget:
        frame, layout = card(
            title="题目预览",
            desc="生成完成后再进入面试。可以返回重新导入材料或调整配置。",
        )
        self.preview_card = frame
        frame.setVisible(False)

        self.preview_meta = label("", "CardDesc", wrap=True)
        layout.addWidget(self.preview_meta)
        layout.addWidget(divider())

        self.question_box = QWidget()
        self.question_layout = QVBoxLayout(self.question_box)
        self.question_layout.setContentsMargins(0, 0, 0, 0)
        self.question_layout.setSpacing(8)
        self.question_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.question_box)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        actions.addStretch(1)
        actions.addWidget(make_button("重新生成", kind="ghost",
                                      on_click=self._on_start_clicked), 0)
        self.enter_btn = make_button("进入面试", kind="primary",
                                     on_click=self._on_enter_clicked)
        actions.addWidget(self.enter_btn, 0)
        layout.addLayout(actions)
        return frame

    def _build_hint_footer(self) -> QWidget:
        self.hint_footer = label(
            "提示：题目由大模型实时生成，导入更完整的 JD 与简历能显著提升针对性。",
            "Hint", wrap=True,
        )
        return self.hint_footer

    # ------------------------------------------------------------------
    # 配置摘要
    # ------------------------------------------------------------------
    def _refresh_config_summary(self) -> None:
        minutes = int(config.get("interview.planned_minutes", 20))
        count = int(config.get("interview.question_count", 18))
        style = STYLE_LABELS.get(str(config.get("interview.style", "friendly")), "温和")
        followups = int(config.get("interview.max_followups", 3))
        voice = voice_short(str(config.get("xfyun.tts.vcn", "")))
        self.summary_label.setText(
            f"计划 {minutes} 分钟 · 主问题 {count} 道 · 风格{style} · 单题最多追问 {followups} 层 · 发音人 {voice}"
        )
        missing = config.missing_items()
        if missing:
            self.status_label.setText("还差这些配置才能开始：" + "、".join(missing))
            self.start_btn.setEnabled(False)
        else:
            if not self._busy:
                self.status_label.setText("")
            self.start_btn.setEnabled(True)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._refresh_config_summary()
        super().showEvent(event)

    # ------------------------------------------------------------------
    # 文件导入
    # ------------------------------------------------------------------
    def _on_jd_selected(self, path: str) -> None:
        self.jd_drop.set_loading("正在读取 JD…")
        run_async(
            extract_text, path,
            on_success=lambda text: self._on_text_ready("jd", path, text),
            on_error=lambda msg: self._on_extract_failed("jd", msg),
        )

    def _on_resume_selected(self, path: str) -> None:
        self.resume_drop.set_loading("正在读取简历…")
        run_async(
            extract_text, path,
            on_success=lambda text: self._on_text_ready("resume", path, text),
            on_error=lambda msg: self._on_extract_failed("resume", msg),
        )

    def _on_text_ready(self, kind: str, path: str, text: str) -> None:
        preview = summarize_preview(text)
        if kind == "jd":
            self._jd_text = text
            self.jd_drop.set_preview(path, preview)
            company, position = guess_meta(text)
            if company and not self.company_edit.text().strip():
                self.company_edit.setText(company)
            if position and not self.position_edit.text().strip():
                self.position_edit.setText(position)
        else:
            self._resume_text = text
            self.resume_drop.set_preview(path, preview)
        self._invalidate_result()
        self.status_label.setText(f"已读取 {Path(path).name}（{len(text)} 字）")

    def _on_extract_failed(self, kind: str, message: str) -> None:
        drop = self.jd_drop if kind == "jd" else self.resume_drop
        drop.clear()
        self.status_label.setText(f"读取失败：{message}")

    def _on_jd_cleared(self) -> None:
        self._jd_text = ""
        self._invalidate_result()

    def _on_resume_cleared(self) -> None:
        self._resume_text = ""
        self._invalidate_result()

    def _invalidate_result(self) -> None:
        self._plan = None
        self._interview_id = None
        self.preview_card.setVisible(False)
        self.start_btn.setText("开始面试")

    # ------------------------------------------------------------------
    # 生成流程
    # ------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        if self._busy:
            return
        if not config.is_ready():
            self.status_label.setText("请先在设置里补全大模型与讯飞配置")
            self.gotoSettings.emit()
            return
        if not self._jd_text.strip():
            self.status_label.setText("请先导入岗位 JD")
            return
        if not self._resume_text.strip():
            self.status_label.setText("请先导入简历")
            return

        self._set_busy(True)
        self.preview_card.setVisible(False)
        self._set_status("正在分析岗位要求…")

        self._profiler = Profiler()
        run_async(
            self._profiler.parse_jd, self._jd_text,
            on_success=self._on_jd_parsed,
            on_error=self._on_failed,
        )

    def _on_jd_parsed(self, jd_profile: dict) -> None:
        self._jd_profile = jd_profile
        if jd_profile.get("company") and not self.company_edit.text().strip():
            self.company_edit.setText(str(jd_profile["company"]))
        if jd_profile.get("position") and not self.position_edit.text().strip():
            self.position_edit.setText(str(jd_profile["position"]))
        self._set_status("正在拆解简历里的项目经历…")
        run_async(
            self._profiler.parse_resume, self._resume_text,
            on_success=self._on_resume_parsed,
            on_error=self._on_failed,
        )

    def _on_resume_parsed(self, resume_profile: dict) -> None:
        self._resume_profile = resume_profile
        self._set_status("正在准备面试问题，通常需要 10 秒左右…")
        self._planner = QuestionPlanner()
        total = int(config.get("interview.question_count", 18))
        spare = int(config.get("interview.spare_count", 12))
        style = str(config.get("interview.style", "friendly"))
        run_async(
            self._planner.plan, self._jd_profile, self._resume_profile,
            total=total, spare=spare, style=style,
            on_success=self._on_planned,
            on_error=self._on_failed,
        )

    def _on_planned(self, result: PlanResult) -> None:
        try:
            self._persist(result)
        except Exception as exc:  # noqa: BLE001
            self._on_failed(f"保存失败：{exc}")
            return
        self._set_busy(False)
        self._plan = result
        self._render_questions(result)
        self.start_btn.setText("重新生成")

        if result.used_fallback:
            self._set_status(result.warning or "已使用内置题库")
        elif result.warning:
            self._set_status(result.warning)
        else:
            self._set_status("题目已生成，确认无误后进入面试")

    def _persist(self, result: PlanResult) -> None:
        total = len(result.questions)
        main_count = sum(1 for q in result.questions if q.phase != "closing")
        title = self.position_edit.text().strip() or self._jdfallback_title()
        interview_id = InterviewRepo.create(
            title=title,
            company=self.company_edit.text().strip(),
            position=self.position_edit.text().strip(),
            jd_text=self._jd_text,
            resume_text=self._resume_text,
            jd_file=self.jd_drop.current_path() or "",
            resume_file=self.resume_drop.current_path() or "",
            jd_profile=self._jd_profile,
            resume_profile=self._resume_profile,
            planned_minutes=int(config.get("interview.planned_minutes", 20)),
            style=str(config.get("interview.style", "friendly")),
            llm_model=str(config.get("llm.model", "")),
        )
        QuestionRepo.bulk_insert(interview_id, result.questions)
        InterviewRepo.update(interview_id, title=title)
        self._interview_id = interview_id
        self._generated_count = (main_count, total)

    def _jdfallback_title(self) -> str:
        return "模拟面试"

    def _render_questions(self, result: PlanResult) -> None:
        while self.question_layout.count():
            item = self.question_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        main_questions = [q for q in result.questions if q.depth == 0]
        self.preview_meta.setText(
            f"共 {len(result.questions)} 道题，其中主问题 {len(main_questions)} 道，"
            f"其余为时间富余时的备用题。"
        )
        for index, question in enumerate(result.questions, start=1):
            self.question_layout.addWidget(self._question_row(index, question))
        self.preview_card.setVisible(True)

    def _question_row(self, index: int, question: Question) -> QWidget:
        row = QFrame()
        row.setObjectName("QuestionRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        badge = QLabel(str(index))
        badge.setObjectName("QuestionIndex")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedWidth(26)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text_box = QWidget()
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)

        text_label = QLabel(question.text)
        text_label.setObjectName("QuestionText")
        text_label.setWordWrap(True)
        text_layout.addWidget(text_label)

        meta_parts = [f"{PHASE_LABELS.get(question.phase, question.phase)}",
                      DIFFICULTY_DOTS.get(question.difficulty, "")]
        if question.intent:
            meta_parts.append(f"考察：{question.intent}")
        meta = QLabel("  ·  ".join(part for part in meta_parts if part))
        meta.setObjectName("QuestionIntent")
        meta.setWordWrap(True)
        text_layout.addWidget(meta)
        layout.addWidget(text_box, 1)
        return row

    def _on_enter_clicked(self) -> None:
        if not self._interview_id:
            return
        InterviewRepo.update(
            self._interview_id,
            company=self.company_edit.text().strip(),
            position=self.position_edit.text().strip(),
            title=self.position_edit.text().strip() or "模拟面试",
        )
        self.interviewCreated.emit(self._interview_id)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.start_btn.setEnabled(not busy)
        self.start_btn.setText("生成中…" if busy else "开始面试")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._set_status(f"生成失败：{message}")
