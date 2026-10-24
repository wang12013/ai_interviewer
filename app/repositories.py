"""表级仓储，屏蔽 SQL 细节。"""

from __future__ import annotations

from typing import Any, Iterable

from .db import db
from .models import InterviewSummary, Message, Question, dumps, loads, new_id, now_iso


class InterviewRepo:
    @staticmethod
    def create(
        *,
        title: str,
        company: str = "",
        position: str = "",
        jd_text: str = "",
        resume_text: str = "",
        jd_file: str = "",
        resume_file: str = "",
        jd_profile: dict | None = None,
        resume_profile: dict | None = None,
        planned_minutes: int = 20,
        style: str = "friendly",
        llm_model: str = "",
    ) -> str:
        interview_id = new_id()
        stamp = now_iso()
        db.execute(
            """INSERT INTO interviews
               (id, title, company, position, jd_text, jd_file, resume_text, resume_file,
                jd_profile, resume_profile, status, phase, planned_minutes, style,
                turn_count, started_at, duration_sec, llm_model, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'running','opening',?,?,0,?,0,?,?)""",
            (
                interview_id, title, company, position, jd_text, jd_file,
                resume_text, resume_file,
                dumps(jd_profile or {}), dumps(resume_profile or {}),
                planned_minutes, style, stamp, llm_model, stamp,
            ),
        )
        return interview_id

    @staticmethod
    def get(interview_id: str) -> dict | None:
        return db.query_one("SELECT * FROM interviews WHERE id = ?", (interview_id,))

    @staticmethod
    def update(interview_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        db.execute(
            f"UPDATE interviews SET {columns} WHERE id = ?",
            (*fields.values(), interview_id),
        )

    @staticmethod
    def set_status(interview_id: str, status: str, duration_sec: int) -> None:
        db.execute(
            "UPDATE interviews SET status = ?, duration_sec = ?, ended_at = ? WHERE id = ?",
            (status, duration_sec, now_iso(), interview_id),
        )

    @staticmethod
    def list_summaries(limit: int = 100) -> list[InterviewSummary]:
        rows = db.query(
            """SELECT i.*, r.id AS report_id, r.overall AS overall
               FROM interviews i LEFT JOIN reports r ON r.interview_id = i.id
               ORDER BY i.started_at DESC LIMIT ?""",
            (limit,),
        )
        return [InterviewSummary.from_row(row) for row in rows]

    @staticmethod
    def list_running() -> list[dict]:
        return db.query("SELECT * FROM interviews WHERE status = 'running' ORDER BY started_at DESC")

    @staticmethod
    def delete(interview_id: str) -> None:
        db.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))

    @staticmethod
    def count_all() -> int:
        return db.scalar("SELECT COUNT(*) FROM interviews", default=0) or 0


class QuestionRepo:
    @staticmethod
    def bulk_insert(interview_id: str, questions: Iterable[Question]) -> None:
        rows = []
        for index, question in enumerate(questions, start=1):
            question.seq = index
            rows.append(question.to_row(interview_id))
        if not rows:
            return
        db.execute_many(
            """INSERT INTO questions
               (id, interview_id, seq, parent_id, depth, phase, category, difficulty,
                text, intent, reference_answer, anchors, status, asked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    @staticmethod
    def get(question_id: str) -> dict | None:
        return db.query_one("SELECT * FROM questions WHERE id = ?", (question_id,))

    @staticmethod
    def list_by_interview(interview_id: str) -> list[dict]:
        return db.query(
            "SELECT * FROM questions WHERE interview_id = ? ORDER BY seq", (interview_id,)
        )

    @staticmethod
    def main_questions(interview_id: str) -> list[dict]:
        return db.query(
            "SELECT * FROM questions WHERE interview_id = ? AND depth = 0 ORDER BY seq",
            (interview_id,),
        )

    @staticmethod
    def next_pending(interview_id: str, phase: str | None = None) -> dict | None:
        if phase:
            row = db.query_one(
                """SELECT * FROM questions
                   WHERE interview_id = ? AND status = 'pending' AND phase = ?
                   ORDER BY seq LIMIT 1""",
                (interview_id, phase),
            )
            if row:
                return row
        return db.query_one(
            """SELECT * FROM questions
               WHERE interview_id = ? AND status = 'pending'
               ORDER BY seq LIMIT 1""",
            (interview_id,),
        )

    @staticmethod
    def set_status(question_id: str, status: str, *, mark_asked: bool = False) -> None:
        if mark_asked:
            db.execute(
                "UPDATE questions SET status = ?, asked_at = ? WHERE id = ?",
                (status, now_iso(), question_id),
            )
        else:
            db.execute("UPDATE questions SET status = ? WHERE id = ?", (status, question_id))

    @staticmethod
    def mark_answered_branch(interview_id: str, root_id: str) -> None:
        """把一道主问题连同它派生出的所有追问一起标记为已回答。"""
        rows = db.query(
            "SELECT id, parent_id FROM questions WHERE interview_id = ?", (interview_id,)
        )
        children: dict[str | None, list[str]] = {}
        for row in rows:
            children.setdefault(row.get("parent_id"), []).append(row["id"])

        stack = [root_id]
        target: list[str] = []
        while stack:
            current = stack.pop()
            target.append(current)
            stack.extend(children.get(current, []))
        if not target:
            return
        placeholders = ",".join("?" for _ in target)
        db.execute(
            f"UPDATE questions SET status = 'answered' WHERE id IN ({placeholders})", target
        )

    @staticmethod
    def add_root(
        interview_id: str,
        *,
        text: str,
        phase: str = "resume",
        category: str = "technical",
        difficulty: int = 2,
        intent: str = "",
    ) -> str:
        """追加一道主问题（题库耗尽时补题用），排在现有题目之后。"""
        question_id = new_id()
        seq = (db.scalar(
            "SELECT MAX(seq) FROM questions WHERE interview_id = ?", (interview_id,), default=0
        ) or 0) + 1
        db.execute(
            """INSERT INTO questions
               (id, interview_id, seq, parent_id, depth, phase, category, difficulty,
                text, intent, reference_answer, anchors, status, asked_at)
               VALUES (?,?,?,NULL,0,?,?,?,?,?,'','[]','pending',NULL)""",
            (question_id, interview_id, seq, phase, category, difficulty, text, intent),
        )
        return question_id

    @staticmethod
    def add_followup(interview_id: str, parent: dict, text: str, intent: str = "") -> str:
        question_id = new_id()
        seq = (db.scalar(
            "SELECT MAX(seq) FROM questions WHERE interview_id = ?", (interview_id,), default=0
        ) or 0) + 1
        db.execute(
            """INSERT INTO questions
               (id, interview_id, seq, parent_id, depth, phase, category, difficulty,
                text, intent, reference_answer, anchors, status, asked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'','[]','asked',?)""",
            (
                question_id, interview_id, seq, parent.get("id"),
                (parent.get("depth") or 0) + 1, parent.get("phase"),
                parent.get("category"), parent.get("difficulty") or 2,
                text, intent, now_iso(),
            ),
        )
        return question_id

    @staticmethod
    def stats(interview_id: str) -> dict:
        row = db.query_one(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status IN ('asked','answered') THEN 1 ELSE 0 END) AS done
               FROM questions WHERE interview_id = ? AND depth = 0""",
            (interview_id,),
        ) or {}
        return {"total": row.get("total") or 0, "done": row.get("done") or 0}

    @staticmethod
    def count_remaining(interview_id: str) -> int:
        return db.scalar(
            "SELECT COUNT(*) FROM questions WHERE interview_id = ? AND status = 'pending'",
            (interview_id,), default=0,
        ) or 0

    @staticmethod
    def phase_progress(interview_id: str) -> dict[str, tuple[int, int]]:
        """各阶段的 (已问, 总数)，只统计主问题。"""
        rows = db.query(
            """SELECT phase,
                      COUNT(*) AS total,
                      SUM(CASE WHEN status IN ('asked','answered') THEN 1 ELSE 0 END) AS done
               FROM questions WHERE interview_id = ? AND depth = 0
               GROUP BY phase""",
            (interview_id,),
        )
        return {
            (row.get("phase") or "resume"): (int(row.get("done") or 0), int(row.get("total") or 0))
            for row in rows
        }


class MessageRepo:
    @staticmethod
    def add(
        interview_id: str,
        *,
        role: str,
        content: str,
        question_id: str | None = None,
        audio_path: str | None = None,
        duration_ms: int | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        edited: int = 0,
    ) -> str:
        message = Message(
            role=role, content=content, question_id=question_id, audio_path=audio_path,
            duration_ms=duration_ms, started_at=started_at, ended_at=ended_at, edited=edited,
        )
        seq = (db.scalar(
            "SELECT MAX(seq) FROM messages WHERE interview_id = ?", (interview_id,), default=0
        ) or 0) + 1
        db.execute(
            """INSERT INTO messages
               (id, interview_id, seq, role, content, question_id, audio_path, duration_ms,
                started_at, ended_at, asr_confidence, edited, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                message.id, interview_id, seq, role, content, question_id, audio_path,
                duration_ms, started_at, ended_at, None, edited, now_iso(),
            ),
        )
        db.execute(
            "UPDATE interviews SET turn_count = ? WHERE id = ?", (seq, interview_id)
        )
        return message.id

    @staticmethod
    def list_by_interview(interview_id: str) -> list[dict]:
        return db.query(
            "SELECT * FROM messages WHERE interview_id = ? ORDER BY seq", (interview_id,)
        )

    @staticmethod
    def update_content(message_id: str, content: str) -> None:
        db.execute(
            "UPDATE messages SET content = ?, edited = 1 WHERE id = ?", (content, message_id)
        )

    @staticmethod
    def count(interview_id: str) -> int:
        return db.scalar(
            "SELECT COUNT(*) FROM messages WHERE interview_id = ?", (interview_id,), default=0
        ) or 0


class ScoreRepo:
    @staticmethod
    def upsert(interview_id: str, message_id: str, score: dict) -> None:
        db.execute(
            """INSERT INTO message_scores
               (message_id, interview_id, relevance, depth, clarity, evidence, comment, missing)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET
                 relevance=excluded.relevance, depth=excluded.depth,
                 clarity=excluded.clarity, evidence=excluded.evidence,
                 comment=excluded.comment, missing=excluded.missing""",
            (
                message_id, interview_id,
                score.get("relevance"), score.get("depth"),
                score.get("clarity"), score.get("evidence"),
                score.get("comment") or "",
                dumps(score.get("missing_points") or []),
            ),
        )

    @staticmethod
    def list_by_interview(interview_id: str) -> list[dict]:
        return db.query(
            "SELECT * FROM message_scores WHERE interview_id = ?", (interview_id,)
        )


class ReportRepo:
    @staticmethod
    def upsert(interview_id: str, report: dict) -> str:
        existing = ReportRepo.get_by_interview(interview_id)
        payload = (
            report.get("overall"),
            report.get("grade"),
            dumps(report.get("dimensions") or {}),
            report.get("summary") or "",
            dumps(report.get("strengths") or []),
            dumps(report.get("weaknesses") or []),
            dumps(report.get("suggestions") or []),
            dumps(report.get("qa_review") or []),
            dumps(report.get("jd_gap") or []),
            report.get("markdown") or "",
            report.get("model") or "",
        )
        if existing:
            db.execute(
                """UPDATE reports SET overall=?, grade=?, dimensions=?, summary=?,
                   strengths=?, weaknesses=?, suggestions=?, qa_review=?, jd_gap=?,
                   markdown=?, model=?, created_at=? WHERE interview_id=?""",
                (*payload, now_iso(), interview_id),
            )
            return existing["id"]
        report_id = new_id()
        db.execute(
            """INSERT INTO reports
               (id, interview_id, overall, grade, dimensions, summary, strengths, weaknesses,
                suggestions, qa_review, jd_gap, markdown, model, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (report_id, interview_id, *payload, now_iso()),
        )
        return report_id

    @staticmethod
    def get_by_interview(interview_id: str) -> dict | None:
        row = db.query_one("SELECT * FROM reports WHERE interview_id = ?", (interview_id,))
        if not row:
            return None
        for key in ("dimensions", "strengths", "weaknesses", "suggestions", "qa_review", "jd_gap"):
            row[key] = loads(row.get(key), [] if key != "dimensions" else {})
        return row
