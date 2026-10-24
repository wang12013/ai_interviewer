"""播报状态的界面测试。

验证面试官说话时界面上的状态胶囊与「跳过播报」按钮，以及打断后能否干净收尾。
音频用假播放器接住，不依赖声卡；合成走本地 mock 服务，不依赖真实密钥。

运行：python tests/tts_gui_test.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QTimer  # noqa: E402

import tts_test as T  # noqa: E402  复用 mock 服务与假播放器
from app.audio.speaker import Speaker  # noqa: E402
from app.main import build_app, load_stylesheet  # noqa: E402
from app.repositories import InterviewRepo, MessageRepo, QuestionRepo  # noqa: E402
from app.ui.main_window import PAGE_INTERVIEW, MainWindow  # noqa: E402
from director_test import build_questions  # noqa: E402

SPEECH = (
    "我们先从你最近负责的项目聊起。你在里面具体负责哪一块？"
    "如果重来一次，你会怎么调整分工？"
)

state: dict = {}
ctx: dict = {}


def wait_until(cond, then, label: str = "", tries: int = 150) -> None:
    def tick(attempt: int = 0) -> None:
        if cond():
            then()
            return
        if attempt >= tries:
            print(f"等待超时：{label}")
            state["timeout"] = label
            ctx["app"].exit(2)
            return
        QTimer.singleShot(100, lambda: tick(attempt + 1))

    tick()


def setup_interview() -> str:
    interview_id = InterviewRepo.create(
        title="高级后端工程师",
        company="星海科技",
        position="高级后端工程师",
        jd_text="1. 5 年以上 Java 经验\n2. 熟悉高并发设计",
        resume_text="张伟，5 年 Java 经验，负责订单中台重构。",
        jd_profile={"must_have": ["5 年以上 Java 经验"]},
        resume_profile={"projects": [{"name": "订单中台重构"}]},
        planned_minutes=20,
        style="friendly",
        llm_model="mock-model",
    )
    QuestionRepo.bulk_insert(interview_id, build_questions())
    # 先放一条面试官消息，load 时才不会立刻触发 director.begin（那要调大模型）
    MessageRepo.add(
        interview_id, role="assistant",
        content="我们开始吧。先做个自我介绍，重点讲和这个岗位最相关的两段经历。",
    )
    return interview_id


def step_start_speech() -> None:
    page = ctx["page"]
    page._add_message("assistant", SPEECH)
    page._speak(SPEECH)
    # 状态胶囊先亮（合成还没完成），音频稍后才到，两个条件都满足才算准备好
    wait_until(
        lambda: page.speak_status.isVisible()
        and (ctx.get("player") is not None and ctx["player"].total_bytes > 0),
        step_capture_speaking,
        label="播报状态出现且有音频",
    )


def step_capture_speaking() -> None:
    app = ctx["app"]
    page = ctx["page"]
    shots: Path = ctx["shots"]
    app.processEvents()

    target = shots / "09-speaking.png"
    page.window().grab().save(str(target))
    state["speaking_shot"] = str(target)
    state["status_text"] = page.speak_status.text()
    state["skip_visible"] = page.skip_btn.isVisible()
    state["hint_during"] = page.hint_label.text()
    state["fed_bytes"] = ctx["player"].total_bytes if ctx.get("player") else 0

    page._skip_speech()
    wait_until(lambda: not page.speak_status.isVisible(), step_capture_stopped,
               label="播报状态消失")


def step_capture_stopped() -> None:
    app = ctx["app"]
    page = ctx["page"]
    shots: Path = ctx["shots"]
    app.processEvents()
    time.sleep(0.3)

    target = shots / "10-speaking-stopped.png"
    page.window().grab().save(str(target))
    state["stopped_shot"] = str(target)
    state["skip_hidden"] = not page.skip_btn.isVisible()
    state["hint_after"] = page.hint_label.text()
    state["fed_after_stop"] = ctx["player"].total_bytes if ctx.get("player") else 0
    state["clears"] = ctx["player"].clears if ctx.get("player") else 0
    app.exit(0)


def print_report() -> int:
    fed = state.get("fed_bytes", 0)
    checks = [
        ("播报状态胶囊出现", bool(state.get("status_text")), state.get("status_text", "")),
        ("跳过按钮可见", state.get("skip_visible") is True, ""),
        ("提示语切换为可打断", "跳过播报" in state.get("hint_during", ""),
         state.get("hint_during", "")),
        ("音频已送进播放器", fed > 0, f"{fed} 字节"),
        ("打断后胶囊隐藏", state.get("skip_hidden") is True, ""),
        ("打断后提示语复原", "Enter 发送" in state.get("hint_after", ""),
         state.get("hint_after", "")),
        ("打断后不再追加音频", state.get("fed_after_stop") == fed,
         f"{state.get('fed_after_stop')} 字节"),
        ("打断了播放缓冲", state.get("clears", 0) >= 1, f"clear {state.get('clears')} 次"),
    ]
    print()
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    print(f"\n播报截图：{state.get('speaking_shot')}")
    print(f"打断截图：{state.get('stopped_shot')}")
    if state.get("errors"):
        print("播报错误：")
        for item in state["errors"][:5]:
            print("   ", item)
    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n通过 {len(checks) - len(failed)}/{len(checks)}")
    return 0 if not failed else 1


def main() -> int:
    from app.main import setup_logging

    setup_logging()
    backup = T.setup_config()
    T.reset_state("ok")
    server = T.MockServer()
    server.start()
    app = build_app()
    load_stylesheet(app)

    window = MainWindow()
    window.resize(1240, 880)
    window.show()
    window.go_to(PAGE_INTERVIEW)

    interview_id = setup_interview()
    page = window.interview_page
    page.load(interview_id)
    app.processEvents()

    # 注入可控播放器：测试环境没有声卡，也便于统计真正喂进去多少音频
    def factory(on_state):
        player = T.FakePlayer(on_state)
        ctx["player"] = player
        return player

    speaker = Speaker(player_factory=factory)
    speaker.on_state = page.speakStateChanged.emit
    speaker.on_error = page.speakFailed.emit
    page._speaker = speaker

    shots = ROOT / "tools" / "shots"
    shots.mkdir(exist_ok=True)
    ctx.update({"app": app, "page": page, "shots": shots, "speaker": speaker})

    QTimer.singleShot(200, step_start_speech)
    QTimer.singleShot(40000, lambda: app.exit(3))
    app.exec()

    page.shutdown()
    server.stop()
    T.restore_config(backup)
    InterviewRepo.delete(interview_id)
    return print_report()


if __name__ == "__main__":
    raise SystemExit(main())
