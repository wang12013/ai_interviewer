"""SQLite 访问层。

单连接 + 可重入锁。UI 线程与后台线程共用，WAL 模式下读写不互相阻塞。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from .paths import DB_FILE, ensure_dirs

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS interviews (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT,
    position        TEXT,
    jd_text         TEXT,
    jd_file         TEXT,
    resume_text     TEXT,
    resume_file     TEXT,
    jd_profile      TEXT,
    resume_profile  TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    phase           TEXT DEFAULT 'opening',
    planned_minutes INTEGER NOT NULL DEFAULT 20,
    style           TEXT DEFAULT 'friendly',
    turn_count      INTEGER DEFAULT 0,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_sec    INTEGER DEFAULT 0,
    llm_model       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interviews_started ON interviews(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_interviews_status ON interviews(status);

CREATE TABLE IF NOT EXISTS questions (
    id               TEXT PRIMARY KEY,
    interview_id     TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    parent_id        TEXT,
    depth            INTEGER DEFAULT 0,
    phase            TEXT,
    category         TEXT,
    difficulty       INTEGER DEFAULT 2,
    text             TEXT NOT NULL,
    intent           TEXT,
    reference_answer TEXT,
    anchors          TEXT,
    status           TEXT DEFAULT 'pending',
    asked_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_interview ON questions(interview_id, seq);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    interview_id    TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    question_id     TEXT,
    audio_path      TEXT,
    duration_ms     INTEGER,
    started_at      TEXT,
    ended_at        TEXT,
    asr_confidence  REAL,
    edited          INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_interview ON messages(interview_id, seq);

CREATE TABLE IF NOT EXISTS message_scores (
    message_id    TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    interview_id  TEXT NOT NULL,
    relevance     REAL,
    depth         REAL,
    clarity       REAL,
    evidence      REAL,
    comment       TEXT,
    missing       TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id            TEXT PRIMARY KEY,
    interview_id  TEXT NOT NULL UNIQUE REFERENCES interviews(id) ON DELETE CASCADE,
    overall       REAL,
    grade         TEXT,
    dimensions    TEXT,
    summary       TEXT,
    strengths     TEXT,
    weaknesses    TEXT,
    suggestions   TEXT,
    qa_review     TEXT,
    jd_gap        TEXT,
    markdown      TEXT,
    model         TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""


class Database:
    _instance: "Database | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "Database":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self) -> None:
        ensure_dirs()
        self._rlock = threading.RLock()
        self._conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._rlock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._rlock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._rlock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._rlock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._rlock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._rlock:
            self._conn.close()


db = Database()
