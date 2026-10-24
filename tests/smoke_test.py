"""不依赖 GUI 的核心链路自测：配置、数据库、仓储、出题排序、文本解析。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.models import Question  # noqa: E402
from app.services.doc_parser import clean_text, guess_meta  # noqa: E402
from app.services.question_planner import QuestionPlanner, split_counts  # noqa: E402

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
    else:
        failed.append(f"{name} {detail}")


# --- 配置 ---------------------------------------------------------------
cfg = Config()
check("配置默认值", cfg.get("interview.planned_minutes") == 20)

# 这台机器上可能已经保存过真实密钥，先清空才能验证「缺失项识别」
SECRET_FIELDS = ("llm.api_key", "xfyun.app_id", "xfyun.api_secret", "xfyun.api_key")
saved_secrets = {path: cfg.get(path) for path in SECRET_FIELDS}
for path in SECRET_FIELDS:
    cfg.set(path, "")
check("配置缺失项识别", "大模型 API Key" in cfg.missing_items(),
      str(cfg.missing_items()))

cfg.set("llm.api_key", "sk-test-1234567890")
cfg.set("xfyun.app_id", "app123")
cfg.set("xfyun.api_secret", "secret")
cfg.set("xfyun.api_key", "key")
check("配置写入后完整", cfg.is_ready(), str(cfg.missing_items()))

with tempfile.TemporaryDirectory() as tmp:
    tmp_file = Path(tmp) / "config.json"
    from app import config as config_module

    origin = config_module.CONFIG_FILE
    origin_secret = config_module.SECRET_FILE
    config_module.CONFIG_FILE = tmp_file
    config_module.SECRET_FILE = Path(tmp) / "secrets.bin"
    try:
        cfg.save()
        raw = tmp_file.read_text(encoding="utf-8")
        check("配置文件不含明文密钥", "sk-test-1234567890" not in raw)
        reloaded = Config()
        check("密钥可正确解密回读", reloaded.get("llm.api_key") == "sk-test-1234567890")
    finally:
        config_module.CONFIG_FILE = origin
        # 不恢复的话，后续任何 save() 都会写进已被删除的临时目录
        config_module.SECRET_FILE = origin_secret

# 把机器上原有的密钥放回去，后面的用例还要用
for path, value in saved_secrets.items():
    cfg.set(path, value or "")

# --- 数据库与仓储 -------------------------------------------------------
from app.repositories import InterviewRepo, MessageRepo, QuestionRepo, ReportRepo  # noqa: E402

interview_id = InterviewRepo.create(
    title="高级后端工程师",
    company="测试公司",
    position="高级后端工程师",
    jd_text="需要 5 年 Java 经验",
    resume_text="5 年 Java 开发",
    planned_minutes=20,
)
check("创建面试记录", bool(InterviewRepo.get(interview_id)))

question = Question(text="讲讲你做过的订单系统", phase="resume", category="project")
QuestionRepo.bulk_insert(interview_id, [question])
rows = QuestionRepo.list_by_interview(interview_id)
check("题目写入与读取", len(rows) == 1 and rows[0]["text"] == "讲讲你做过的订单系统")

MessageRepo.add(interview_id, role="interviewer", content="你好，我们开始吧")
MessageRepo.add(interview_id, role="candidate", content="好的")
check("消息序号递增", MessageRepo.count(interview_id) == 2)

ReportRepo.upsert(interview_id, {"overall": 82.5, "grade": "B", "dimensions": {"专业深度": 85}})
report = ReportRepo.get_by_interview(interview_id)
check("报告写入与 JSON 解析", report is not None and report["dimensions"]["专业深度"] == 85)

stats = QuestionRepo.stats(interview_id)
check("题目统计", stats["total"] == 1, str(stats))

# --- 出题排序 -----------------------------------------------------------
resume_count, jd_count = split_counts(18)
check("题量分配", resume_count + jd_count + 2 == 18, f"{resume_count}/{jd_count}")

fake = (
    [Question(text=f"开场题 {i}", phase="opening") for i in range(2)]
    + [Question(text=f"简历题 {i}", phase="resume") for i in range(10)]
    + [Question(text=f"岗位题 {i}", phase="jd") for i in range(10)]
    + [Question(text=f"收尾题 {i}", phase="closing") for i in range(2)]
)
arranged = QuestionPlanner._arrange(fake, 18, 12)
check("主问题数量为 18", len([q for q in arranged]) >= 18, str(len(arranged)))
check("第一题是开场", arranged[0].phase == "opening")
check("最后一题是收尾", arranged[-1].phase == "closing", arranged[-1].phase)
check("序号连续", [q.seq for q in arranged] == list(range(1, len(arranged) + 1)))
check("开场只保留一题", sum(1 for q in arranged if q.phase == "opening") == 1)

# --- 文本处理 -----------------------------------------------------------
jd_sample = """公司名称：某某科技有限公司
招聘职位：高级后端工程师

任职要求：
1. 5 年以上 Java 开发经验
2. 熟悉 Spring Cloud 微服务架构
3. 有高并发系统设计经验
"""
company, position = guess_meta(jd_sample)
check("识别公司", "某某科技" in company, company)
check("识别岗位", "后端工程师" in position, position)

messy = "第一行\r\n\r\n\r\n第 2 页\n12\n内容"
check("文本清洗去空行", "\n\n\n" not in clean_text(messy))

# --- 输出 ---------------------------------------------------------------
print(f"通过 {len(passed)} 项")
for name in passed:
    print(f"  OK   {name}")
if failed:
    print(f"失败 {len(failed)} 项")
    for item in failed:
        print(f"  FAIL {item}")
    raise SystemExit(1)
print("全部通过")
