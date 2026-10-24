"""面试导演：状态机、追问决策与时长守卫。

这一层不依赖 Qt，只做纯业务逻辑：给定候选人的一句回答，决定面试官下
一句说什么，并把问答、评分写入数据库。界面层放在后台线程里调用，
避免大模型的水流把主线程卡住。

设计要点：
- 追问深度以数据库里的 depth 为准，重启后能接着上次的深度继续
- 时间守卫优先于模型决策：最后一分钟一定收尾，模型说 closing 但时间
  还早就一律拦掉
- 题库问完但时间没到，从兜底池里补一道没问过的，保证 20 分钟撑得住
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from ..config import config
from ..models import PHASE_LABELS
from ..repositories import InterviewRepo, MessageRepo, QuestionRepo, ScoreRepo
from . import prompts
from .llm_client import LLMClient, friendly_error
from .question_planner import FALLBACK_POOL

ACTIONS = ("followup", "next", "hint", "closing")

# 最后一分钟以内强制收尾的阈值
CLOSING_WINDOW_SEC = 60
# 时间还早时，模型擅自收尾需要拦截的阈值
CLOSING_MIN_REMAIN_SEC = 180


def _parse_stamp(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now()


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, number))


@dataclass
class Turn:
    """一轮面试官动作。"""

    action: str                       # ask / followup / hint / closing / end
    text: str = ""
    question: dict | None = None
    phase: str = "resume"
    depth: int = 0
    ai_message_id: str | None = None
    answer_message_id: str | None = None
    score: dict | None = None
    note: str = ""
    forced: str = ""                  # 被守卫规则改写时的说明，界面会用浅色提示
    elapsed: int = 0

    @property
    def is_end(self) -> bool:
        return self.action == "end"


@dataclass
class DirectorState:
    phase: str = "opening"
    depth: int = 0
    max_depth: int = 3
    elapsed: int = 0
    planned: int = 1200
    done: int = 0
    total: int = 0
    remaining_questions: int = 0

    @property
    def remain(self) -> int:
        return self.planned - self.elapsed


class InterviewDirector:
    def __init__(self, interview_id: str, client: LLMClient | None = None) -> None:
        record = InterviewRepo.get(interview_id)
        if not record:
            raise ValueError("面试记录不存在")
        self.interview_id = interview_id
        self.record = record
        self.client = client or LLMClient()
        self.style = str(record.get("style") or "friendly")
        self.company = str(record.get("company") or "")
        self.position = str(record.get("position") or "")
        self.planned_minutes = max(1, int(record.get("planned_minutes") or 20))
        self.max_depth = max(1, int(config.get("interview.max_followups", 3)))
        self._started = _parse_stamp(record.get("started_at"))
        self._lock = threading.RLock()
        self._finished = record.get("status") != "running"

        self.question: dict | None = None
        self.root: dict | None = None
        self.phase = str(record.get("phase") or "opening")
        self._restore()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def planned_seconds(self) -> int:
        return self.planned_minutes * 60

    def elapsed_seconds(self) -> int:
        return max(0, int((datetime.now() - self._started).total_seconds()))

    def is_finished(self) -> bool:
        return self._finished

    def state(self) -> DirectorState:
        progress = QuestionRepo.phase_progress(self.interview_id)
        done, total = progress.get(self.phase, (0, 0))
        return DirectorState(
            phase=self.phase,
            depth=int((self.question or {}).get("depth") or 0),
            max_depth=self.max_depth,
            elapsed=self.elapsed_seconds(),
            planned=self.planned_seconds,
            done=done,
            total=total,
            remaining_questions=QuestionRepo.count_remaining(self.interview_id),
        )

    def _restore(self) -> None:
        """上次退出时可能停在某一题上，重启后接着这一题继续。"""
        if self._finished:
            return
        rows = QuestionRepo.list_by_interview(self.interview_id)
        last = None
        for row in rows:
            if row.get("status") == "asked":
                last = row
        if last is None:
            return
        self.question = last
        self.phase = str(last.get("phase") or self.phase)

        root = last
        depth_guard = 0
        while root.get("parent_id") and depth_guard < 10:
            parent = QuestionRepo.get(root["parent_id"])
            if not parent:
                break
            root = parent
            depth_guard += 1
        self.root = root

    # ------------------------------------------------------------------
    # 开场
    # ------------------------------------------------------------------
    def begin(self) -> Turn:
        with self._lock:
            if self._finished:
                return Turn(action="end", text="", forced="本场面试已经结束")
            if self.question:
                # 已经停在某题上，界面直接复用库里那条消息，不再重复发问
                return Turn(
                    action="ask", text="", question=self.question,
                    phase=self.phase, depth=int(self.question.get("depth") or 0),
                    elapsed=self.elapsed_seconds(),
                )
            row = self._next_question()
            if row is None:
                return self._farewell("没有可用的题目", self.elapsed_seconds())
            return self._ask_turn(row, elapsed=self.elapsed_seconds())

    # ------------------------------------------------------------------
    # 回答
    # ------------------------------------------------------------------
    def respond(self, answer: str) -> Turn:
        with self._lock:
            if self._finished:
                return Turn(action="end", text="", forced="本场面试已经结束")

            answer = (answer or "").strip() or "（这道题我暂时没有想好）"
            elapsed = self.elapsed_seconds()
            remain = self.planned_seconds - elapsed
            answer_id = MessageRepo.add(
                self.interview_id,
                role="user",
                content=answer,
                question_id=(self.question or {}).get("id"),
            )

            # 守卫一：最后一分钟，无论模型怎么说都立刻收尾
            if self.phase != "closing" and 0 <= remain <= CLOSING_WINDOW_SEC:
                return self._closing_turn(
                    forced="时间接近尾声，进入收尾环节", elapsed=elapsed
                )

            decision, error = self._decide(answer)
            self._save_score(answer_id, decision)

            # 模型思考期间用户点了结束，就别再追加提问了
            if self._finished:
                return Turn(action="end", text="", answer_message_id=answer_id, elapsed=elapsed)

            action = str(decision.get("action") or "").strip().lower()
            text = str(decision.get("question") or "").strip()
            forced = ""

            if action not in ACTIONS:
                action = "next"
                if error:
                    forced = f"面试官没跟上（{error}），先进入下一题"

            # 守卫二：时间还早，不允许提前收尾
            if action == "closing" and remain > CLOSING_MIN_REMAIN_SEC:
                action = "next"

            # 守卫三：追问深度到顶
            if action == "followup" and not self._allow_followup():
                action, forced = "next", "追问已到上限，进入下一题"

            # 守卫四：模型说追问却没给内容
            if action in ("followup", "hint") and not text:
                action = "next"

            if action == "closing":
                return self._closing_turn(elapsed=elapsed, forced=forced)
            if action == "hint":
                return self._hint_turn(text, decision, answer_id, elapsed)
            if action == "followup":
                return self._followup_turn(text, decision, answer_id, elapsed)
            return self._advance(elapsed=elapsed, forced=forced)

    # ------------------------------------------------------------------
    # 结束
    # ------------------------------------------------------------------
    def finish(self, reason: str = "manual") -> dict:
        # 不拿 respond 的那把锁：否则用户在大模型思考时点结束会被卡住
        elapsed = self.elapsed_seconds()
        self._finished = True
        # 题目正常问完算完成；手动结束则看有没有聊满计划时长
        if reason == "natural" or elapsed >= self.planned_seconds:
            status = "finished"
        else:
            status = "aborted"
        InterviewRepo.set_status(self.interview_id, status, elapsed)
        return {"status": status, "duration": elapsed, "reason": reason}

    # ------------------------------------------------------------------
    # 内部：具体动作
    # ------------------------------------------------------------------
    def _ask_turn(self, row: dict, *, forced: str = "", elapsed: int | None = None) -> Turn:
        phase = str(row.get("phase") or "resume")
        if phase != self.phase:
            self.phase = phase
            InterviewRepo.update(self.interview_id, phase=phase)
        QuestionRepo.set_status(row["id"], "asked", mark_asked=not row.get("asked_at"))

        text = str(row.get("text") or "")
        message_id = MessageRepo.add(
            self.interview_id, role="assistant", content=text, question_id=row["id"]
        )
        self.question = QuestionRepo.get(row["id"]) or row
        self.root = self.question
        return Turn(
            action="ask",
            text=text,
            question=self.question,
            phase=self.phase,
            depth=int(self.question.get("depth") or 0),
            ai_message_id=message_id,
            forced=forced,
            elapsed=self.elapsed_seconds() if elapsed is None else elapsed,
        )

    def _followup_turn(
        self, text: str, decision: dict, answer_id: str, elapsed: int
    ) -> Turn:
        parent = self.question or self.root
        if not parent:
            return self._advance(elapsed=elapsed)
        note = str(decision.get("brief_note") or "").strip()
        question_id = QuestionRepo.add_followup(
            self.interview_id, parent, text, intent=note or "追问"
        )
        row = QuestionRepo.get(question_id) or {}
        self.question = row
        message_id = MessageRepo.add(
            self.interview_id, role="assistant", content=text, question_id=question_id
        )
        return Turn(
            action="followup",
            text=text,
            question=row,
            phase=str(row.get("phase") or self.phase),
            depth=int(row.get("depth") or 1),
            ai_message_id=message_id,
            answer_message_id=answer_id,
            note=note,
            elapsed=elapsed,
        )

    def _hint_turn(
        self, text: str, decision: dict, answer_id: str, elapsed: int
    ) -> Turn:
        question_id = (self.question or {}).get("id")
        message_id = MessageRepo.add(
            self.interview_id, role="assistant", content=text, question_id=question_id
        )
        return Turn(
            action="hint",
            text=text,
            question=self.question,
            phase=self.phase,
            depth=int((self.question or {}).get("depth") or 0),
            ai_message_id=message_id,
            answer_message_id=answer_id,
            note=str(decision.get("brief_note") or ""),
            elapsed=elapsed,
        )

    def _advance(self, *, elapsed: int, forced: str = "") -> Turn:
        self._mark_answered()
        row = self._next_question()
        if row is None:
            return self._farewell(forced or "题库已经问完", elapsed)
        return self._ask_turn(row, forced=forced, elapsed=elapsed)

    def _closing_turn(self, *, elapsed: int, forced: str = "") -> Turn:
        self._mark_answered()
        row = QuestionRepo.next_pending(self.interview_id, "closing")
        if row is None:
            return self._farewell(forced or "进入收尾", elapsed)
        return self._ask_turn(row, forced=forced, elapsed=elapsed)

    def _farewell(self, forced: str, elapsed: int) -> Turn:
        message_id = MessageRepo.add(
            self.interview_id, role="assistant", content=prompts.FAREWELL_TEXT
        )
        self._mark_answered()
        self.finish("natural")
        return Turn(
            action="end",
            text=prompts.FAREWELL_TEXT,
            phase=self.phase,
            ai_message_id=message_id,
            forced=forced,
            elapsed=elapsed,
        )

    # ------------------------------------------------------------------
    # 内部：选下一题
    # ------------------------------------------------------------------
    def _mark_answered(self) -> None:
        target = self.root or self.question
        if target:
            QuestionRepo.mark_answered_branch(self.interview_id, target["id"])
        self.question = None
        self.root = None

    def _allow_followup(self) -> bool:
        depth = int((self.question or {}).get("depth") or 0)
        return depth < self.max_depth

    def _next_question(self) -> dict | None:
        row = QuestionRepo.next_pending(self.interview_id)
        if row:
            return row
        return self._extend_pool()

    def _extend_pool(self) -> dict | None:
        """题库问完但时间没到，从兜底池里补一道没问过的题。"""
        rows = QuestionRepo.list_by_interview(self.interview_id)
        asked = {(row.get("text") or "").strip() for row in rows}
        for phase, category, difficulty, text, intent in FALLBACK_POOL:
            if phase == "closing" or text in asked:
                continue
            question_id = QuestionRepo.add_root(
                self.interview_id,
                text=text, phase=phase, category=category,
                difficulty=difficulty, intent=intent,
            )
            return QuestionRepo.get(question_id)
        return None

    # ------------------------------------------------------------------
    # 内部：模型决策
    # ------------------------------------------------------------------
    def _decide(self, answer: str) -> tuple[dict, str]:
        state = self.state()
        system = prompts.director_system_prompt(
            company=self.company,
            position=self.position,
            style=self.style,
            elapsed_min=state.elapsed / 60,
            planned_min=self.planned_minutes,
            phase=self.phase,
            phase_label=PHASE_LABELS.get(self.phase, self.phase),
            phase_done=state.done,
            phase_total=state.total,
            depth=state.depth,
            max_depth=self.max_depth,
            current_question=str((self.question or {}).get("text") or ""),
            intent=str((self.question or {}).get("intent") or ""),
        )
        messages = [{"role": "system", "content": system}]
        messages.extend(self._history())
        messages.append({
            "role": "user",
            "content": f"候选人的回答：{answer}\n\n只输出决策 JSON。",
        })
        try:
            decision = self.client.chat_json(
                messages, temperature=0.6, max_tokens=600, timeout=45
            )
            if not isinstance(decision, dict):
                raise ValueError("决策不是对象")
            return decision, ""
        except Exception as exc:  # noqa: BLE001 - 降级到规则
            return self._heuristic(answer), friendly_error(exc)[:60]

    def _history(self) -> list[dict]:
        rows = MessageRepo.list_by_interview(self.interview_id)
        # 最后一条是刚落库的当前回答，会在消息末尾单独拼一次，这里要剔掉
        if rows and rows[-1].get("role") == "user":
            rows = rows[:-1]
        history: list[dict] = []
        for row in rows[-8:]:
            content = (row.get("content") or "").strip()
            if not content:
                continue
            role = "assistant" if row.get("role") == "assistant" else "user"
            history.append({"role": role, "content": content[:400]})
        return history

    def _heuristic(self, answer: str) -> dict:
        """大模型不可用时的规则兜底，保证面试能继续推进。"""
        text = answer.strip()
        followup = "能展开说说吗？我想听具体一点的细节。"
        evidence = "这个结论有具体的数据或者案例支撑吗？"
        if self._allow_followup():
            if len(text) < 40:
                return {"action": "followup", "question": followup, "brief_note": "回答过短"}
            if not any(ch.isdigit() for ch in text) and len(text) < 120:
                return {"action": "followup", "question": evidence, "brief_note": "缺少量化依据"}
        return {"action": "next", "brief_note": "规则推进"}

    def _save_score(self, message_id: str, decision: dict) -> dict | None:
        raw = decision.get("score") or {}
        if not isinstance(raw, dict):
            return None
        score = {
            "relevance": _num(raw.get("relevance")),
            "depth": _num(raw.get("depth")),
            "clarity": _num(raw.get("clarity")),
            "evidence": _num(raw.get("evidence")),
        }
        if all(value is None for value in score.values()):
            return None
        score["comment"] = str(decision.get("brief_note") or "")
        score["missing_points"] = decision.get("missing_points") or []
        try:
            ScoreRepo.upsert(self.interview_id, message_id, score)
        except Exception:  # noqa: BLE001 - 评分失败不该打断面试
            return None
        return score
