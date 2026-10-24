"""出题引擎。

基于 JD 与简历的交叉分析生成题库。模型输出不可用时有内置题库兜底，
保证用户永远能开始面试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Question
from . import prompts
from .llm_client import LLMClient

VALID_PHASES = ("opening", "resume", "jd", "closing")
PHASE_ORDER = {phase: index for index, phase in enumerate(VALID_PHASES)}

# 兜底题库：模型不可用时使用，按阶段轮转取用
FALLBACK_POOL: list[tuple[str, str, int, str, str]] = [
    ("opening", "opening", 1, "先简单介绍一下你自己，重点讲两段和这个岗位最相关的经历。", "表达结构与岗位聚焦"),
    ("resume", "project", 2, "挑一个你投入最多的项目，说说你负责的那部分，最难的技术点在哪。", "项目深度与个人贡献"),
    ("resume", "project", 2, "这个项目里有没有做过技术选型？当时对比了哪些方案，最后为什么这么选。", "技术决策能力"),
    ("resume", "project", 3, "如果这个项目要重做一遍，你会改掉哪个设计？为什么当初没这么设计。", "反思与架构判断"),
    ("resume", "project", 2, "你说到性能提升了，具体是怎么测的？压测环境和线上有多接近。", "数据严谨性"),
    ("resume", "technical", 2, "你项目里用到了缓存，缓存和数据库不一致的情况怎么处理。", "一致性方案"),
    ("resume", "behavior", 2, "讲一次你和同事在技术方案上有分歧的经历，最后怎么解决的。", "协作与沟通"),
    ("jd", "technical", 2, "如果要设计一个支撑千万级日活的接口，你会从哪几个方面考虑。", "系统设计能力"),
    ("jd", "technical", 2, "线上突然出现大量慢查询，你的排查顺序是什么。", "问题定位能力"),
    ("jd", "technical", 3, "分布式事务在这个岗位的场景里必然会遇到，你会选哪种方案，代价是什么。", "分布式方案权衡"),
    ("jd", "role", 2, "你觉得自己在这个岗位上最欠缺的一块是什么，打算怎么补。", "自我认知"),
    ("jd", "role", 2, "这个岗位要频繁和产品、前端对齐，你之前的跨团队协作是什么样的。", "跨团队协作"),
    ("jd", "technical", 2, "代码评审时你主要看哪些点？举一个你提过的有效意见。", "工程素养"),
    ("jd", "behavior", 2, "如果排期紧张，功能做不完，你会怎么处理。", "优先级判断"),
    ("closing", "closing", 1, "关于这个岗位或者团队，你有什么想了解的吗。", "主动性"),
    ("closing", "closing", 1, "未来三年你希望自己在技术上是什样的状态。", "职业规划"),
]


@dataclass
class PlanResult:
    questions: list[Question] = field(default_factory=list)
    used_fallback: bool = False
    warning: str = ""


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.replace("；", ";").split(";") if part.strip()]
    return []


def _to_question(raw: dict, *, phase_hint: str | None = None) -> Question | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or raw.get("question") or "").strip()
    if len(text) < 6:
        return None

    phase = str(raw.get("phase") or "").strip().lower()
    if phase not in PHASE_ORDER:
        category = str(raw.get("category") or "").strip().lower()
        mapping = {
            "opening": "opening", "closing": "closing",
            "role": "jd", "technical": "jd", "behavior": "resume", "project": "resume",
        }
        phase = mapping.get(category, phase_hint or "resume")

    try:
        difficulty = int(raw.get("difficulty") or 2)
    except (TypeError, ValueError):
        difficulty = 2
    difficulty = max(1, min(3, difficulty))

    return Question(
        text=text,
        phase=phase,
        category=str(raw.get("category") or phase).strip().lower() or phase,
        difficulty=difficulty,
        intent=str(raw.get("intent") or "").strip(),
        reference_answer=str(raw.get("reference_answer") or "").strip(),
        anchors=_as_list(raw.get("anchors")),
    )


def split_counts(total: int) -> tuple[int, int]:
    total = max(4, total)
    resume_count = max(3, round((total - 2) * 0.45))
    jd_count = max(2, total - 2 - resume_count)
    return resume_count, jd_count


class QuestionPlanner:
    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or LLMClient()

    def plan(
        self,
        jd_profile: dict,
        resume_profile: dict,
        *,
        total: int = 18,
        spare: int = 12,
        style: str = "friendly",
    ) -> PlanResult:
        total = max(4, int(total))
        resume_count, jd_count = split_counts(total)
        messages = prompts.planner_messages(
            jd_profile, resume_profile,
            total=total, resume_count=resume_count, jd_count=jd_count,
            spare_count=spare, style=style,
        )

        questions: list[Question] = []
        warning = ""
        for attempt in range(2):
            try:
                data = self.client.chat_json(
                    messages,
                    temperature=0.6 if attempt == 0 else 0.4,
                    max_tokens=4096,
                    timeout=120,
                )
                raws = data.get("questions") or data.get("items") or []
                if not isinstance(raws, list):
                    raise ValueError("questions 不是数组")
                parsed = [q for q in (_to_question(item) for item in raws) if q]
                if len(parsed) >= min(8, total):
                    questions = parsed
                    break
                warning = f"模型只生成了 {len(parsed)} 道题，已补充题库"
            except Exception as exc:
                warning = str(exc)[:120]

        used_fallback = False
        if not questions:
            used_fallback = True
            questions = [
                Question(
                    text=text, phase=phase, category=category,
                    difficulty=difficulty, intent=intent,
                )
                for phase, category, difficulty, text, intent in FALLBACK_POOL[:total]
            ]

        questions = self._arrange(questions, total, spare)
        if used_fallback:
            warning = "本次使用内置通用题库，建议检查大模型配置后重新生成"
        return PlanResult(questions=questions, used_fallback=used_fallback, warning=warning)

    # ------------------------------------------------------------------
    @staticmethod
    def _arrange(questions: list[Question], total: int, spare: int) -> list[Question]:
        """按 opening → resume → jd → closing 排列，切出主问题与备用题。"""
        ordered = sorted(questions, key=lambda q: PHASE_ORDER.get(q.phase, 1))

        # 开场只留一题，多余的降级为 resume 阶段的通用题
        openings = [q for q in ordered if q.phase == "opening"]
        others = [q for q in ordered if q.phase != "opening"]
        head = openings[:1]

        main_others = others[: max(0, total - 1)]
        rest = others[max(0, total - 1):]
        # 备用题不该是收尾题
        rest = [q for q in rest if q.phase not in ("opening", "closing")][:spare]

        # 补足数量：从兜底池里按缺失阶段取
        missing = total - (len(head) + len(main_others))
        if missing > 0:
            existing = {q.text for q in head + main_others}
            for phase, category, difficulty, text, intent in FALLBACK_POOL:
                if missing <= 0:
                    break
                if text in existing:
                    continue
                if phase in ("opening", "closing") and any(q.phase == phase for q in head + main_others):
                    continue
                head.append(Question(text=text, phase=phase, category=category,
                                     difficulty=difficulty, intent=intent))
                existing.add(text)
                missing -= 1

        # 确保有收尾题
        if not any(q.phase == "closing" for q in head + main_others):
            main_others.append(Question(
                text="关于这个岗位或者团队，你有什么想了解的吗？",
                phase="closing", category="closing", difficulty=1, intent="主动性",
            ))

        closing = [q for q in main_others if q.phase == "closing"]
        middle = [q for q in main_others if q.phase != "closing"]
        final = head + middle + rest + closing
        for index, question in enumerate(final, start=1):
            question.seq = index
        return final
