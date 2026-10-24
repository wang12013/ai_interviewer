"""用本地 mock 服务验证大模型客户端与出题链路，不依赖真实 API Key。

运行：python tests/mock_llm_test.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import config  # noqa: E402
from app.services.doc_parser import extract_text  # noqa: E402
from app.services.llm_client import LLMClient  # noqa: E402
from app.services.profiler import Profiler  # noqa: E402
from app.services.question_planner import QuestionPlanner  # noqa: E402

JD_PROFILE = {
    "company": "星海科技",
    "position": "高级后端工程师",
    "level": "P6",
    "must_have": ["5 年以上 Java 经验", "熟悉 Spring Cloud", "有高并发系统设计经验"],
    "nice_to_have": ["熟悉 Kubernetes"],
    "tech_stack": ["Java", "Spring Cloud", "MySQL", "Redis"],
    "responsibilities": ["负责订单域核心链路"],
    "soft_skills": ["跨部门沟通"],
    "business_domain": "电商",
    "keywords": ["Java", "高并发"],
}

RESUME_PROFILE = {
    "name": "张伟",
    "years": 5,
    "current_role": "后端工程师",
    "education": "本科",
    "skills": ["Java", "Spring Cloud", "MySQL", "Redis"],
    "projects": [
        {
            "name": "订单中台重构",
            "role": "核心开发",
            "period": "2023.03-2024.06",
            "stack": ["Spring Cloud", "ShardingSphere"],
            "highlights": ["支撑日均 300 万单"],
            "probe_points": ["分片键选择的取舍", "改造期间的灰度方案"],
        }
    ],
    "achievements": ["主导订单库拆分"],
    "gaps": ["缺少带人经验"],
}


def _questions() -> list[dict]:
    items = [
        {
            "phase": "opening", "category": "opening", "difficulty": 1,
            "text": "先简单介绍一下你自己，重点讲和这个岗位最相关的两段经历。",
            "intent": "表达结构", "reference_answer": "结构清晰；有量化", "anchors": ["经历"],
        }
    ]
    for index in range(10):
        items.append({
            "phase": "resume", "category": "project", "difficulty": 2,
            "text": f"你在订单中台重构里，第 {index + 1} 个技术决策是怎么做的？具体讲讲取舍。",
            "intent": "技术决策", "reference_answer": "讲清取舍；给出数据", "anchors": ["分片"],
        })
    for index in range(9):
        items.append({
            "phase": "jd", "category": "technical", "difficulty": 2,
            "text": f"岗位要求里的第 {index + 1} 条能力，你在哪个项目里体现过？",
            "intent": "岗位匹配", "reference_answer": "举具体例子", "anchors": ["项目"],
        })
    items.append({
        "phase": "closing", "category": "closing", "difficulty": 1,
        "text": "关于这个岗位或者团队，你有什么想了解的吗？",
        "intent": "主动性", "reference_answer": "问有价值的问题", "anchors": [],
    })
    return items


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音
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
        elif "在真实面试准备问题清单" in system or "准备问题清单" in system:
            content = json.dumps({"questions": _questions()}, ensure_ascii=False)
        elif "面试评估专家" in system:
            content = json.dumps({"overall": 78.0, "grade": "B", "dimensions": {}}, ensure_ascii=False)
        else:
            content = "就绪"

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in [content[i:i + 40] for i in range(0, len(content), 40)]:
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return

        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}]
        }, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    origin = {
        "base_url": config.get("llm.base_url"),
        "api_key": config.get("llm.api_key"),
        "model": config.get("llm.model"),
        "provider": config.get("llm.provider"),
    }
    config.set("llm.base_url", f"http://127.0.0.1:{port}/v1")
    config.set("llm.api_key", "mock-key")
    config.set("llm.model", "mock-model")

    results: list[tuple[str, bool, str]] = []
    try:
        jd_text = (
            "公司名称：星海科技有限公司\n招聘职位：高级后端工程师\n\n"
            "任职要求：\n1. 5 年以上 Java 开发经验\n2. 熟悉 Spring Cloud 微服务\n"
            "3. 有高并发系统设计经验\n"
        )
        resume_text = (
            "张伟，后端工程师，5 年 Java 经验。\n项目名称：订单中台重构\n"
            "使用 Spring Cloud 与 ShardingSphere 完成订单库拆分，支撑日均 300 万单。\n"
        )

        profiler = Profiler(LLMClient())
        jd_profile = profiler.parse_jd(jd_text)
        results.append(("JD 解析命中结构化字段",
                        jd_profile.get("must_have") == JD_PROFILE["must_have"],
                        str(jd_profile.get("must_have"))[:60]))

        resume_profile = profiler.parse_resume(resume_text)
        results.append(("简历解析提取到项目",
                        bool(resume_profile.get("projects"))
                        and resume_profile["projects"][0]["name"] == "订单中台重构",
                        str(resume_profile.get("projects"))[:60]))

        planner = QuestionPlanner(LLMClient())
        plan = planner.plan(jd_profile, resume_profile, total=18, spare=12)
        results.append(("未走兜底题库", not plan.used_fallback, plan.warning))
        results.append(("题目数量充足", len(plan.questions) >= 18, str(len(plan.questions))))
        results.append(("首题为开场", plan.questions[0].phase == "opening", plan.questions[0].phase))
        results.append(("末题为收尾", plan.questions[-1].phase == "closing", plan.questions[-1].phase))
        results.append(("题目有考察点", all(q.intent for q in plan.questions[:10]), ""))

        # 流式接口
        streamed = "".join(LLMClient().chat_stream(
            [{"role": "system", "content": "准备问题清单"}, {"role": "user", "content": "test"}]
        ))
        results.append(("流式接口可拼接", "questions" in streamed, str(len(streamed))))

        # 写库
        from app.repositories import InterviewRepo, QuestionRepo

        interview_id = InterviewRepo.create(
            title=jd_profile["position"], company=jd_profile["company"],
            position=jd_profile["position"], jd_text=jd_text, resume_text=resume_text,
            jd_profile=jd_profile, resume_profile=resume_profile,
            planned_minutes=20, style="friendly", llm_model="mock-model",
        )
        QuestionRepo.bulk_insert(interview_id, plan.questions)
        stored = QuestionRepo.list_by_interview(interview_id)
        results.append(("题目成功入库", len(stored) == len(plan.questions), str(len(stored))))
        InterviewRepo.delete(interview_id)
    finally:
        for key, value in origin.items():
            config.set(f"llm.{key}", value)
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
