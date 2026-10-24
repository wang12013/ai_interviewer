"""语音回答的界面链路测试。

用假的转写器替身走完「点按钮 → 连接 → 收音 → 实时上屏 → 定稿提交」，
验证按钮状态机、临时气泡的生死、以及定稿后消息是否真的进了对话。

不占麦克风、不连讯飞，跑几秒钟就能结束。

运行：python tests/voice_gui_test.py
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QTimer  # noqa: E402

from app.config import config  # noqa: E402
from app.main import build_app, load_stylesheet  # noqa: E402
from app.repositories import MessageRepo  # noqa: E402
from app.ui.main_window import PAGE_INTERVIEW, MainWindow  # noqa: E402
from interview_gui_test import Handler, seed_interview  # noqa: E402

SPOKEN = "我先做个自我介绍，五年 Java 经验，做过后端架构。"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


class FakeTranscriber:
    """模拟一次完整的语音回答，节奏按真人说话放慢一点，方便观察中间态。"""

    def __init__(self) -> None:
        self.active = False
        self.audio_dir = None
        self.states: list[str] = []
        self.on_partial = None
        self.on_final = None
        self.on_error = None
        self.on_state = None
        self.on_level = None

    def start(self) -> None:
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        time.sleep(0.15)
        self.on_state("connecting") if self.on_state else None
        time.sleep(0.2)
        self.on_state("listening") if self.on_state else None
        self.on_level(0.9) if self.on_level else None
        time.sleep(0.25)
        self.on_partial("我先做个") if self.on_partial else None
        time.sleep(0.25)
        self.on_partial(SPOKEN) if self.on_partial else None
        time.sleep(0.3)
        self.on_state("thinking") if self.on_state else None
        time.sleep(0.2)
        self.active = False
        self.on_state("idle") if self.on_state else None
        self.on_final(SPOKEN) if self.on_final else None

    def stop(self, *, submit: bool = True) -> None:
        self.active = False

    def shutdown(self) -> None:
        self.active = False

    def ready(self):
        return True, ""


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config.set("llm.base_url", f"http://127.0.0.1:{port}/v1")
    config.set("llm.api_key", "mock-key")
    config.set("llm.model", "mock-model")
    config.set("interview.max_followups", 2)
    # 语音按钮只看这两项，给假值就能点亮
    config.set("xfyun.app_id", "mock-app")
    config.set("xfyun.api_key", "mock-key")
    # 关掉播报，否则会拿假 key 去连真实讯飞，测试既慢又会报 401
    backup_tts = config.get("xfyun.tts.enabled")
    config.set("xfyun.tts.enabled", False)

    app = build_app()
    load_stylesheet(app)

    interview_id = seed_interview()
    window = MainWindow()
    window.resize(1360, 860)
    window.show()
    window.interview_page.load(interview_id)
    window.go_to(PAGE_INTERVIEW)
    page = window.interview_page

    state: dict = {"partial_seen": False, "listening_seen": False, "shot": ""}

    def wait_until(condition, then, *, timeout: float = 20.0, label: str = "") -> None:
        started = time.monotonic()

        def poll() -> None:
            if condition():
                then()
                return
            if time.monotonic() - started > timeout:
                print(f"超时等待：{label}")
                app.quit()
                return
            QTimer.singleShot(80, poll)

        QTimer.singleShot(80, poll)

    def step_check_enabled() -> None:
        check("开场后语音按钮可用", page.mic_btn.isEnabled(),
              f"enabled={page.mic_btn.isEnabled()}")
        # 直接换掉转写器，但要像 _ensure_transcriber 那样把回调接到信号上，
        # 否则后台线程的 emit 到不了主线程
        fake = FakeTranscriber()
        fake.on_partial = page.voicePartial.emit
        fake.on_final = page.voiceFinal.emit
        fake.on_error = page.voiceError.emit
        fake.on_state = page.voiceState.emit
        fake.on_level = page.voiceLevel.emit
        page._transcriber = fake
        page.mic_btn.click()

        wait_until(lambda: page.mic_btn.text() == "● 结束", step_watch_partial,
                   label="进入收音状态")

    def step_watch_partial() -> None:
        state["listening_seen"] = True
        check("收音中按钮变成「● 结束」", page.mic_btn.objectName() == "Danger",
              page.mic_btn.objectName())
        wait_until(lambda: page._partial_label is not None, step_partial_text,
                   label="临时气泡出现")

    def step_partial_text() -> None:
        state["partial_seen"] = True
        out = Path(__file__).parent.parent / "tools" / "shots"
        out.mkdir(exist_ok=True)
        window.grab().save(str(out / "11-voice-listening.png"))
        state["shot"] = str(out / "11-voice-listening.png")
        wait_until(lambda: not page._listening and not page._busy,
                   step_after_submit, timeout=25, label="定稿提交")

    def step_after_submit() -> None:
        time.sleep(0.1)
        app.processEvents()
        users = [m for m in MessageRepo.list_by_interview(interview_id)
                 if m.get("role") == "user"]
        check("定稿内容进了正式对话",
              any(SPOKEN[:8] in (m.get("content") or "") for m in users),
              f"用户消息 {len(users)} 条")
        check("临时气泡已清理", page._partial_row is None,
              str(page._partial_row))
        check("按钮恢复成「语音回答」", page.mic_btn.text() == "语音回答",
              page.mic_btn.text())
        check("按钮回到普通样式", page.mic_btn.objectName() == "Ghost",
              page.mic_btn.objectName())
        check("提交后按钮重新可用", page.mic_btn.isEnabled(),
              f"busy={page._busy}")
        app.quit()

    wait_until(lambda: not page._busy and MessageRepo.count(interview_id) >= 1,
               step_check_enabled, label="开场提问")

    QTimer.singleShot(60000, app.quit)
    app.exec()

    config.set("xfyun.tts.enabled", True if backup_tts is None else backup_tts)

    check("确实经过了收音状态", state["listening_seen"])
    check("确实出现过临时气泡", state["partial_seen"])

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n通过 {passed}/{len(results)}")
    if state["shot"]:
        print("截图：", state["shot"])
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print("失败项：", "；".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
