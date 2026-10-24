"""面试导演状态机测试。

用本地 mock 覆盖大模型，验证：开场取题、追问深度守卫、提前收尾拦截、
最后一分钟强制收尾、题库耗尽补题、消息与评分入库、重启后续接。

运行：python tests/director_test.py
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import config  # noqa: E402
from app.db import db  # noqa: E402
from app.models import Question, format_duration  # noqa: E402
from app.repositories import InterviewRepo, MessageRepo, QuestionRepo, ScoreRepo  # noqa: E402
from app.services.director import InterviewDirector  # noqa: E402

# mock 返回的决策，测试过程中按需改写
MODE: dict = {
    "action": "next",
    "question": "",
    "score": {"relevance": 80, "depth": 70, "clarity": 75, "evidence": 60},
    "brief_note": "回答完整",
    "missing_points": ["没讲迁移成本"],
}

def build_questions() -> list[Question]:
    """每次建一组新的 Question（id 是随机生成的，不能跨面试复用）。"""
    specs = [
        ("先做个自我介绍，重点讲和这个岗位最相关的两段经历。",
         "opening", "opening", 1, "表达结构"),
        ("订单中台重构里，分片键为什么选用户 ID 而不是订单 ID？",
         "resume", "project", 3, "技术决策"),
        ("灰度期间新旧库双写，一致性怎么保证的？",
         "resume", "project", 2, "一致性方案"),
        ("日均 300 万单这个数字是怎么统计出来的？",
         "resume", "project", 2, "数据严谨性"),
        ("岗位要求高并发设计能力，你哪个项目能证明这一点？",
         "jd", "technical", 2, "岗位匹配"),
        ("关于团队和这个岗位，你有什么想了解的吗？",
         "closing", "closing", 1, "主动性"),
    ]
    return [
        Question(text=text, phase=phase, category=category,
                 difficulty=difficulty, intent=intent)
        for text, phase, category, difficulty, intent in specs
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = payload.get("messages") or []
        system = messages[0].get("content", "") if messages else ""

        if "正在与候选人实时对话" in system:
            content = json.dumps(MODE, ensure_ascii=False)
        else:
            content = "就绪"

        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}]
        }, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_interview(minutes: int = 20, start_offset_min: float = 0.0) -> tuple[str, list[dict]]:
    interview_id = InterviewRepo.create(
        title="高级后端工程师", company="星海科技", position="高级后端工程师",
        jd_text="1. 5 年以上 Java 经验", resume_text="张伟，5 年 Java 经验",
        jd_profile={"must_have": ["5 年以上 Java 经验"]},
        resume_profile={"projects": [{"name": "订单中台重构"}]},
        planned_minutes=minutes, style="friendly", llm_model="mock-model",
    )
    QuestionRepo.bulk_insert(interview_id, build_questions())
    if start_offset_min:
        stamp = (datetime.now() - timedelta(minutes=start_offset_min)).isoformat(timespec="seconds")
        InterviewRepo.update(interview_id, started_at=stamp)
    return interview_id, QuestionRepo.list_by_interview(interview_id)


def run_tests() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    config.set("interview.max_followups", 2)

    # ---- 1. 开场 ----
    interview_id, questions = make_interview()
    director = InterviewDirector(interview_id)
    turn = director.begin()
    results.append(("开场返回第一题", turn.action == "ask" and turn.phase == "opening", turn.action))
    results.append(("开场题写入消息表", MessageRepo.count(interview_id) == 1,
                    str(MessageRepo.count(interview_id))))
    results.append(("首题标记为已问", QuestionRepo.get(questions[0]["id"])["status"] == "asked", ""))

    # ---- 2. 追问 ----
    MODE.update({"action": "followup", "question": "你说的这两段经历里，哪一段更能代表你？"})
    turn = director.respond("我在上一家公司做订单中台重构。")
    results.append(("短回答触发追问", turn.action == "followup" and turn.depth == 1, turn.action))
    results.append(("追问落到问题表",
                    turn.question is not None and turn.question["parent_id"] == questions[0]["id"], ""))

    # ---- 3. 深度守卫：max_followups=2，第二次追问后再次要求追问应被拦 ----
    turn = director.respond("第一段经历是大促流量治理，第二段是订单库拆分。")
    results.append(("第二次追问仍允许", turn.action == "followup" and turn.depth == 2, turn.action))
    turn = director.respond("大促那次我负责限流降级，把峰值扛住了。")
    results.append(("超过追问上限改问下一题", turn.action == "ask" and turn.forced != "", turn.forced or turn.action))
    results.append(("下一题进入简历阶段", turn.phase == "resume", turn.phase))

    # ---- 4. 提前收尾拦截 ----
    MODE.update({"action": "closing", "question": "今天就到这里吧。"})
    turn = director.respond("分片键选用户 ID 是为了让同一用户的订单落在同一库，避免跨库查询。")
    results.append(("时间充足时拦掉收尾", turn.action == "ask", turn.action))

    # ---- 5. 评分入库 ----
    scores = ScoreRepo.list_by_interview(interview_id)
    results.append(("回答评分写入", len(scores) >= 3 and scores[-1]["depth"] == 70,
                    str(len(scores))))

    # ---- 6. 走完全程 ----
    MODE.update({"action": "next", "question": ""})
    guard = 0
    while not turn.is_end and guard < 30:
        turn = director.respond("这个话题我可以从方案、踩过的坑和最终效果三块来讲，整体延迟从 800ms 降到了 120ms。")
        guard += 1
    results.append(("题目问完后自然结束", turn.is_end, turn.forced))
    results.append(("结束语入库", "复盘报告" in turn.text, turn.text[:20]))
    record = InterviewRepo.get(interview_id)
    results.append(("状态标记为已完成", record["status"] == "finished", record["status"]))
    results.append(("结束原因不靠手动", turn.forced != "manual", turn.forced))

    # ---- 7. 题库耗尽补题 ----
    rows = QuestionRepo.list_by_interview(interview_id)
    results.append(("题库耗尽时自动补题", len(rows) > len(questions), f"{len(rows)} 题"))

    # ---- 8. 最后一分钟强制收尾 ----
    late_id, _ = make_interview(start_offset_min=19.3)
    late = InterviewDirector(late_id)
    late.begin()
    MODE.update({"action": "followup", "question": "再展开说说？"})
    turn = late.respond("我会先把核心链路梳理清楚，再做压测。")
    results.append(("最后一分钟强制收尾", turn.action == "ask" and "收尾" in turn.forced, turn.forced))
    results.append(("收尾落在 closing 阶段", turn.phase == "closing", turn.phase))

    # ---- 9. 重启后续接 ----
    resume_id, _ = make_interview()
    first = InterviewDirector(resume_id)
    first.begin()
    MODE.update({"action": "followup", "question": "那一段具体怎么拆的？"})
    first.respond("我负责拆分。")
    again = InterviewDirector(resume_id)
    results.append(("重启后停在原题深度", again.question is not None and
                    (again.question.get("depth") or 0) == 1,
                    str((again.question or {}).get("depth"))))
    again_turn = again.begin()
    results.append(("续接时不重复发问", again_turn.text == "" and again_turn.action == "ask", again_turn.action))

    # ---- 10. 手动结束 ----
    manual_id, _ = make_interview()
    manual = InterviewDirector(manual_id)
    manual.begin()
    info = manual.finish("manual")
    results.append(("手动结束记为提前结束", info["status"] == "aborted", info["status"]))
    results.append(("时长格式化正常", format_duration(info["duration"]).count(":") == 1, ""))

    # ---- 清理 ----
    for target in (interview_id, late_id, resume_id, manual_id):
        InterviewRepo.delete(target)
    leftover = db.scalar("SELECT COUNT(*) FROM messages WHERE interview_id IN (?,?,?,?)",
                         (interview_id, late_id, resume_id, manual_id), default=0)
    results.append(("级联清理干净", leftover == 0, str(leftover)))
    return results


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    origin = {
        "base_url": config.get("llm.base_url"),
        "api_key": config.get("llm.api_key"),
        "model": config.get("llm.model"),
        "followups": config.get("interview.max_followups"),
    }
    config.set("llm.base_url", f"http://127.0.0.1:{port}/v1")
    config.set("llm.api_key", "mock-key")
    config.set("llm.model", "mock-model")

    try:
        results = run_tests()
    finally:
        config.set("llm.base_url", origin["base_url"])
        config.set("llm.api_key", origin["api_key"])
        config.set("llm.model", origin["model"])
        config.set("interview.max_followups", origin["followups"])
        server.shutdown()

    failed = [item for item in results if not item[1]]
    for name, ok, detail in results:
        flag = "OK  " if ok else "FAIL"
        print(f"  {flag} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if failed:
        print(f"失败 {len(failed)} 项")
        return 1
    print(f"全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
