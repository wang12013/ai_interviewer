"""GUI 集成测试：模拟导入材料后点击「开始面试」，验证分步异步回调与题目渲染。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QTimer  # noqa: E402

from app.config import config  # noqa: E402
from app.main import build_app, load_stylesheet  # noqa: E402
from app.repositories import InterviewRepo, QuestionRepo  # noqa: E402
from app.ui.pages.setup_page import SetupPage  # noqa: E402
from mock_llm_test import JD_PROFILE, RESUME_PROFILE, _questions  # noqa: E402

JD_TEXT = """公司名称：星海科技有限公司
招聘职位：高级后端工程师

岗位职责：
1. 负责订单域核心链路的设计与开发
2. 参与系统架构演进

任职要求：
1. 5 年以上 Java 开发经验
2. 熟悉 Spring Cloud 微服务架构
3. 有高并发系统设计经验
4. 熟悉 MySQL、Redis，了解分库分表
"""

RESUME_TEXT = """张伟，后端工程师，5 年 Java 开发经验。
2023.03-2024.06 订单中台重构（核心开发）
使用 Spring Cloud 与 ShardingSphere 完成订单库拆分，支撑日均 300 万单，
下单响应时间从 800ms 降到 180ms。
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = payload.get("messages") or []
        system = messages[0].get("content", "") if messages else ""

        if "招聘分析专家" in system:
            content = json.dumps(JD_PROFILE, ensure_ascii=False)
        elif "擅长从简历里找出值得深挖的点" in system:
            content = json.dumps(RESUME_PROFILE, ensure_ascii=False)
        else:
            content = json.dumps({"questions": _questions()}, ensure_ascii=False)

        body = json.dumps(
            {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config.set("llm.base_url", f"http://127.0.0.1:{port}/v1")
    config.set("llm.api_key", "mock-key")
    config.set("llm.model", "mock-model")
    config.set("xfyun.app_id", "mock-app")
    config.set("xfyun.api_secret", "mock-secret")
    config.set("xfyun.api_key", "mock-key")

    app = build_app()
    load_stylesheet(app)

    page = SetupPage()
    page.resize(1280, 1080)
    page.show()
    page._jd_text = JD_TEXT
    page._resume_text = RESUME_TEXT
    page.company_edit.setText("星海科技")
    page.position_edit.setText("高级后端工程师")

    state: dict[str, object] = {}

    def on_created(interview_id: str) -> None:
        state["id"] = interview_id
        QTimer.singleShot(400, finish)

    def finish() -> None:
        out = Path(__file__).parent.parent / "tools" / "shots"
        out.mkdir(exist_ok=True)
        page.scroll.verticalScrollBar().setValue(
            page.scroll.verticalScrollBar().maximum()
        )
        app.processEvents()
        target = out / "05-question-preview.png"
        page.grab().save(str(target))
        state["shot"] = str(target)
        state["visible"] = page.preview_card.isVisible()
        state["questions"] = len(
            QuestionRepo.list_by_interview(str(state["id"]))
        )
        app.quit()

    page.interviewCreated.connect(on_created)

    polls = {"n": 0}

    def poll() -> None:
        polls["n"] += 1
        if page._interview_id:
            page._on_enter_clicked()
            return
        if polls["n"] > 200:
            app.quit()
            return
        QTimer.singleShot(150, poll)

    QTimer.singleShot(200, page._on_start_clicked)
    QTimer.singleShot(600, poll)
    QTimer.singleShot(45000, app.quit)

    app.exec()
    server.shutdown()

    if "id" not in state:
        print("失败：没有生成面试（超时或链路中断）")
        print("状态栏：", page.status_label.text())
        return 1

    print(f"面试 ID：{state['id']}")
    print(f"入库题目：{state['questions']} 道")
    print(f"截图：{state['shot']}")
    print(f"状态栏：{page.status_label.text()}")

    valid = state["questions"] >= 18 and state.get("visible") is True
    print("预览面板可见：", state.get("visible"))
    InterviewRepo.delete(str(state["id"]))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
