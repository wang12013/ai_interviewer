"""面试对话页集成测试。

在本机拉起 mock 大模型，模拟「开始面试 → 回答 → 追问 → 再回答」的完整
点击流程，验证气泡渲染、消息落库、阶段进度、计时器，并截图。

运行：python tests/interview_gui_test.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QTimer  # noqa: E402

from app.config import config  # noqa: E402
from app.main import build_app, load_stylesheet  # noqa: E402
from app.models import Question  # noqa: E402
from app.repositories import (  # noqa: E402
    InterviewRepo,
    MessageRepo,
    QuestionRepo,
    ScoreRepo,
)
from app.ui.main_window import (  # noqa: E402
    PAGE_HISTORY,
    PAGE_INTERVIEW,
    MainWindow,
)
from app.ui.pages.interview_page import InterviewPage  # noqa: E402
from director_test import build_questions  # noqa: E402

FOLLOWUPS = [
    "你说的核心链路具体指哪几块？我想听你拆分的方式。",
    "这个灰度方案如果中途回滚，数据怎么对齐？",
]
DECISIONS = [
    {"action": "followup", "question": FOLLOWUPS[0],
     "score": {"relevance": 82, "depth": 70, "clarity": 78, "evidence": 60},
     "brief_note": "讲到了链路但没拆模块", "missing_points": ["模块边界"]},
    {"action": "followup", "question": FOLLOWUPS[1],
     "score": {"relevance": 85, "depth": 76, "clarity": 80, "evidence": 65},
     "brief_note": "回滚方案不够具体", "missing_points": ["补偿事务"]},
    {"action": "next", "question": "",
     "score": {"relevance": 88, "depth": 82, "clarity": 84, "evidence": 78},
     "brief_note": "回答完整，有量化", "missing_points": []},
]

ANSWERS = [
    "订单中台重构我主要负责分片和灰度两块。分片键选的是用户 ID，"
    "这样同一个用户的订单都落在同一个库，列表查询不用跨库。",
    "灰度期间是双写，新库写完再写老库，读流量按用户 ID 灰度切。"
    "如果中途回滚，会把新库这段时间的增量反向补回老库。",
    "压测是在预发环境做的，用线上 1.2 倍的峰值流量跑了两个小时，"
    "P99 从 800ms 降到 180ms，慢查询数量下降了 90%。",
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = payload.get("messages") or []
        system = messages[0].get("content", "") if messages else ""
        user_turns = [m for m in messages if m.get("role") == "user"]

        if "正在与候选人实时对话" in system:
            index = max(0, len(user_turns) - 1)
            decision = DECISIONS[min(index, len(DECISIONS) - 1)]
            content = json.dumps(decision, ensure_ascii=False)
        else:
            content = "就绪"

        body = json.dumps(
            {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def seed_interview() -> str:
    interview_id = InterviewRepo.create(
        title="高级后端工程师", company="星海科技", position="高级后端工程师",
        jd_text="1. 5 年以上 Java 经验\n2. 有高并发系统设计经验",
        resume_text="张伟，5 年 Java 经验，主导订单中台重构。",
        jd_profile={"must_have": ["5 年以上 Java 经验", "有高并发系统设计经验"]},
        resume_profile={"projects": [{"name": "订单中台重构"}]},
        planned_minutes=20, style="friendly", llm_model="mock-model",
    )
    QuestionRepo.bulk_insert(interview_id, build_questions())
    stamp = (datetime.now() - timedelta(minutes=4, seconds=12)).isoformat(timespec="seconds")
    InterviewRepo.update(interview_id, started_at=stamp)
    return interview_id


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config.set("llm.base_url", f"http://127.0.0.1:{port}/v1")
    config.set("llm.api_key", "mock-key")
    config.set("llm.model", "mock-model")
    config.set("interview.max_followups", 2)
    # 本机若配了真实讯飞密钥，不关掉就会真的去合成并出声，白烧额度
    config.set("xfyun.tts.enabled", False)

    app = build_app()
    load_stylesheet(app)

    interview_id = seed_interview()
    window = MainWindow()
    window.resize(1360, 860)
    window.show()
    window.interview_page.load(interview_id)
    window.go_to(PAGE_INTERVIEW)

    page: InterviewPage = window.interview_page
    state: dict[str, object] = {"step": 0}

    def wait_until(condition, then, *, timeout: float = 25.0, label: str = "") -> None:
        started = time.monotonic()

        def check() -> None:
            if condition():
                then()
                return
            if time.monotonic() - started > timeout:
                print(f"超时等待：{label}")
                app.quit()
                return
            QTimer.singleShot(120, check)

        QTimer.singleShot(120, check)

    def idle() -> bool:
        return not page._busy

    def step_ask() -> None:
        page.edit.setPlainText(ANSWERS[0])
        page._send()
        wait_until(lambda: idle() and MessageRepo.count(interview_id) >= 3, step_answer_two,
                   label="第一轮追问")

    def step_answer_two() -> None:
        page.edit.setPlainText(ANSWERS[1])
        page._send()
        wait_until(lambda: idle() and MessageRepo.count(interview_id) >= 5, step_answer_three,
                   label="第二轮追问")

    def step_answer_three() -> None:
        page.edit.setPlainText(ANSWERS[2])
        page._send()
        wait_until(lambda: idle() and MessageRepo.count(interview_id) >= 7, step_snapshot,
                   label="第三轮回答")

    def step_snapshot() -> None:
        out = Path(__file__).parent.parent / "tools" / "shots"
        out.mkdir(exist_ok=True)
        app.processEvents()
        target = out / "06-interview.png"
        window.grab().save(str(target))
        state["shot"] = str(target)

        state["messages"] = MessageRepo.count(interview_id)
        state["scores"] = len(ScoreRepo.list_by_interview(interview_id))
        state["bubbles"] = page.chat_layout.count()
        state["timer"] = page.timer_label.text()
        state["chips"] = {phase: chip.text() for phase, chip in page._chips.items()}
        state["active"] = [
            phase for phase, chip in page._chips.items()
            if chip.property("active") is True
        ]
        state["followups"] = len([
            q for q in QuestionRepo.list_by_interview(interview_id) if (q.get("depth") or 0) > 0
        ])

        # 结束这一场：确认框单独处理，这里走确认之后的落库与通知
        signals: list[str] = []
        page.finished.connect(lambda iid: signals.append(iid))
        page._finish_manually()
        app.processEvents()
        state["ended_signal"] = signals
        state["page_after_finish"] = window.stack.currentIndex()
        state["ended_status"] = (InterviewRepo.get(interview_id) or {}).get("status")
        after = out / "08-after-finish.png"
        window.grab().save(str(after))
        state["after_shot"] = str(after)

        # 再从历史里打开一次，验证只读形态
        page.load(interview_id)
        window.go_to(PAGE_INTERVIEW)
        app.processEvents()
        ended = out / "07-interview-ended.png"
        window.grab().save(str(ended))
        state["ended_shot"] = str(ended)
        state["ended_input"] = page.edit.isEnabled()
        state["ended_phase"] = (InterviewRepo.get(interview_id) or {}).get("phase")
        state["ended_meta"] = page.meta_label.text()
        app.quit()

    wait_until(lambda: idle() and MessageRepo.count(interview_id) >= 1, step_ask,
               label="开场提问")

    QTimer.singleShot(60000, app.quit)
    app.exec()

    record = InterviewRepo.get(interview_id)
    status = state.get("ended_status")
    valid = (
        state.get("messages", 0) >= 7
        and state.get("followups", 0) >= 2
        and state.get("scores", 0) >= 2
        and state.get("bubbles", 0) >= 7
        and bool(state.get("active"))
        and state.get("ended_signal") == [interview_id]
        and state.get("page_after_finish") == PAGE_HISTORY
        and state.get("ended_input") is False
        and status == "aborted"
    )

    print(f"对话截图：{state.get('shot')}")
    print(f"结束跳转截图：{state.get('after_shot')}")
    print(f"只读态截图：{state.get('ended_shot')}")
    print(f"消息条数：{state.get('messages')}")
    print(f"气泡数量：{state.get('bubbles')}")
    print(f"追问条数：{state.get('followups')}")
    print(f"评分条数：{state.get('scores')}")
    print(f"计时显示：{state.get('timer')}")
    print(f"阶段进度：{state.get('chips')}")
    print(f"高亮阶段：{state.get('active')}")
    print(f"结束后状态：{status}")
    print(f"结束后阶段：{state.get('ended_phase')} / {state.get('ended_meta')}")
    print(f"结束后信号：{state.get('ended_signal')}")
    print(f"结束后跳转页面：{state.get('page_after_finish')}")
    print(f"结束后输入框禁用：{state.get('ended_input') is False}")
    print("整体校验：", "通过" if valid else "未通过")

    InterviewRepo.delete(interview_id)
    server.shutdown()
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
