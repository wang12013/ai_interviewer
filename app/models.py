"""数据模型与通用工具。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_duration(seconds: int | float | None) -> str:
    total = max(0, int(seconds or 0))
    return f"{total // 60:02d}:{total % 60:02d}"


def human_duration(seconds: int | float | None) -> str:
    total = max(0, int(seconds or 0))
    minutes = total // 60
    if minutes >= 60:
        return f"{minutes // 60} 小时 {minutes % 60} 分"
    return f"{minutes} 分 {total % 60} 秒"


def loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


PHASE_LABELS = {
    "opening": "开场",
    "resume": "简历深挖",
    "jd": "岗位匹配",
    "closing": "收尾",
    "extra": "加分追问",
}

CATEGORY_LABELS = {
    "opening": "开场",
    "technical": "技术",
    "project": "项目",
    "behavior": "行为",
    "role": "岗位",
    "closing": "收尾",
}

STATUS_LABELS = {
    "running": "进行中",
    "finished": "已完成",
    "aborted": "提前结束",
}

GRADE_COLORS = {
    "S": "#1D9E75",
    "A": "#1D9E75",
    "B": "#378ADD",
    "C": "#BA7517",
    "D": "#E24B4A",
}


@dataclass
class Question:
    text: str
    phase: str = "resume"
    category: str = "technical"
    difficulty: int = 2
    intent: str = ""
    reference_answer: str = ""
    anchors: list[str] = field(default_factory=list)
    id: str = field(default_factory=new_id)
    seq: int = 0
    parent_id: str | None = None
    depth: int = 0
    status: str = "pending"

    def to_row(self, interview_id: str) -> tuple:
        return (
            self.id,
            interview_id,
            self.seq,
            self.parent_id,
            self.depth,
            self.phase,
            self.category,
            self.difficulty,
            self.text,
            self.intent,
            self.reference_answer,
            dumps(self.anchors),
            self.status,
            None,
        )


@dataclass
class Message:
    role: str
    content: str
    id: str = field(default_factory=new_id)
    seq: int = 0
    question_id: str | None = None
    audio_path: str | None = None
    duration_ms: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    asr_confidence: float | None = None
    edited: int = 0


@dataclass
class InterviewSummary:
    id: str
    title: str
    company: str
    position: str
    status: str
    phase: str
    planned_minutes: int
    duration_sec: int
    turn_count: int
    started_at: str
    ended_at: str | None
    has_report: bool = False
    overall: float | None = None

    @classmethod
    def from_row(cls, row: dict) -> "InterviewSummary":
        return cls(
            id=row["id"],
            title=row.get("title") or "未命名面试",
            company=row.get("company") or "",
            position=row.get("position") or "",
            status=row.get("status") or "finished",
            phase=row.get("phase") or "opening",
            planned_minutes=row.get("planned_minutes") or 20,
            duration_sec=row.get("duration_sec") or 0,
            turn_count=row.get("turn_count") or 0,
            started_at=row.get("started_at") or "",
            ended_at=row.get("ended_at"),
            has_report=bool(row.get("report_id")),
            overall=row.get("overall"),
        )

    @property
    def subtitle(self) -> str:
        bits = [b for b in (self.company, self.position) if b]
        return " · ".join(bits) if bits else self.title

    @property
    def started_display(self) -> str:
        try:
            return datetime.fromisoformat(self.started_at).strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return self.started_at
