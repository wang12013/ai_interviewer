"""把 JD 与简历解析成结构化画像。

LLM 调用失败时退化为本地正则提取，保证流程不断。
"""

from __future__ import annotations

import re

from ..services import prompts
from ..services.llm_client import LLMClient

_SKILL_KEYWORDS = [
    "Java", "Python", "Go", "C++", "C#", "JavaScript", "TypeScript", "Rust", "PHP", "Ruby",
    "Spring", "Spring Boot", "Spring Cloud", "Django", "Flask", "FastAPI", "React", "Vue",
    "Angular", "Node.js", "MySQL", "PostgreSQL", "Oracle", "Redis", "MongoDB", "Elasticsearch",
    "Kafka", "RocketMQ", "RabbitMQ", "Docker", "Kubernetes", "K8s", "Linux", "Nginx",
    "AWS", "阿里云", "腾讯云", "Hadoop", "Spark", "Flink", "Hive", "ClickHouse",
    "TensorFlow", "PyTorch", "机器学习", "深度学习", "微服务", "分布式", "高并发", "DevOps",
    "MySQL优化", "JVM", "Netty", "ZooKeeper", "Nacos", "MyBatis", "Dubbo",
]

_MUST_HINTS = ("任职要求", "岗位要求", "职位要求", "任职资格", "我们希望你", "你需要具备", "Requirements")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            output.append(item.strip())
    return output


def _fallback_jd(text: str) -> dict:
    lines = [line.strip(" ·-—•·\t") for line in text.split("\n")]
    lines = [line for line in lines if len(line) > 6]
    skills = [kw for kw in _SKILL_KEYWORDS if kw.lower() in text.lower()]
    requirements: list[str] = []
    capturing = False
    for line in lines:
        if any(hint in line for hint in _MUST_HINTS):
            capturing = True
            continue
        if capturing:
            if len(requirements) >= 12:
                break
            requirements.append(line)
    if not requirements:
        requirements = lines[:10]
    return {
        "company": "",
        "position": "",
        "level": "",
        "must_have": _dedupe(requirements)[:12],
        "nice_to_have": [],
        "tech_stack": _dedupe(skills),
        "responsibilities": [],
        "soft_skills": [],
        "business_domain": "",
        "keywords": _dedupe(skills),
        "_fallback": True,
    }


def _fallback_resume(text: str) -> dict:
    skills = [kw for kw in _SKILL_KEYWORDS if kw.lower() in text.lower()]
    years_match = re.search(r"(\d{1,2})\s*年(?:以上)?(?:工作)?经验", text)
    projects = re.findall(r"(?:项目名称|项目)[：:]\s*([^\n]{2,30})", text)
    return {
        "name": "",
        "years": int(years_match.group(1)) if years_match else None,
        "current_role": "",
        "education": "",
        "skills": _dedupe(skills),
        "projects": [
            {"name": name, "role": "", "period": "", "stack": [], "highlights": [], "probe_points": []}
            for name in projects[:5]
        ],
        "achievements": [],
        "gaps": [],
        "_fallback": True,
    }


class Profiler:
    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or LLMClient()

    def parse_jd(self, text: str) -> dict:
        try:
            data = self.client.chat_json(
                prompts.jd_profile_messages(text), temperature=0.2, max_tokens=1200
            )
            if not isinstance(data, dict) or not data.get("must_have"):
                raise ValueError("JD 解析结果缺少 must_have")
            data.setdefault("tech_stack", [])
            data.setdefault("keywords", data.get("tech_stack", []))
            return data
        except Exception:
            return _fallback_jd(text)

    def parse_resume(self, text: str) -> dict:
        try:
            data = self.client.chat_json(
                prompts.resume_profile_messages(text), temperature=0.3, max_tokens=1600
            )
            if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
                raise ValueError("简历解析结果缺少 projects")
            data.setdefault("skills", [])
            return data
        except Exception:
            return _fallback_resume(text)
