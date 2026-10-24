# AI 模拟面试器

一个 Windows 桌面端的 AI 模拟面试官：上传岗位 JD 与简历，自动生成面试问题，进行一场**可语音对话**的模拟面试，并保留完整对话记录。

不是网页套壳，是本地运行的 PySide6 桌面程序，数据全部存在本机。

## 功能

- **岗位化出题**：解析 JD / 简历（PDF、DOCX、TXT），结构化出候选人画像，再按项目经历、专业技能、场景设计、岗位匹配等维度生成题库（18 道主问题 + 12 道备用 + 兜底题库）。
- **会追问**：根据回答内容决定是否深挖，追问链最多 3 层，答不上来会换角度或被主动带过，不会卡死在一道题上。
- **语音面试**：面试官用讯飞**超拟人**音色播报（可换发音人、调语速音量），候选人的语音回答经**实时语音转写**边说边上屏，说完自动转成正式消息。
- **时长可控**：默认 20 分钟，题库冗余 + 时间提醒注入 + 代码层守卫（剩余不足 1 分钟强制收尾、剩余充足时拦截模型提前结束），也可以随时点「结束面试」。
- **对话历史**：每场面试完整留存，可回看全部问答、阶段、时长。
- **可配置**：大模型走 OpenAI 兼容协议（Base URL / 模型名 / API Key 全部可填），讯飞三件套（AppID / APIKey / APISecret）同样在界面里配置。

## 截图之外的现状

已实现：解析 → 出题 → 面试对话 → 语音播报 → 语音转写 → 历史回看。

尚未实现：**面试复盘报告**（历史页选一场后生成评分与不足建议）。数据库与界面已预留入口，规划在下一版补齐。

## 环境要求

- Windows 10 / 11
- Python 3.10 及以上（开发验证于 3.13）

## 安装

```bash
git clone https://github.com/wang12013/ai_interviewer.git
cd ai_interviewer
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 运行

双击 `启动.bat`，或：

```bash
python run.py
```

`启动.bat` 会依次尝试项目内 `.venv`、本机常见的 Python 环境、PATH 上的 Python。

首次启动需要先在「设置」里填配置，否则出题与语音都会不可用。

## 配置项

设置页把密钥用 Windows **DPAPI** 加密后存到 `data/secrets.bin`，明文只在内存里出现；日志里统一脱敏成 `sk-****abcd`。

| 分组 | 字段 | 说明 |
|---|---|---|
| 大模型 | Base URL / 模型名 / API Key | OpenAI 兼容接口，DeepSeek、通义、Kimi、本地 Ollama 均可 |
| 讯飞语音合成 | AppID / APIKey / APISecret | 需开通「超拟人语音合成」 |
| 发音人 | 聆小璇 / 聆飞逸 / 聆小玥 / 聆玉言 / 聆小糖 / 高冷男神 | 控制台里**已开通**哪个才能用哪个，可一键检测 |
| 面试 | 计划时长 / 追问层数 / 静音自动断句秒数 | 默认 20 分钟、3 层、2 秒 |

讯飞需要**分别开通**三项服务，缺一项对应功能就不可用：

1. 超拟人语音合成 —— 面试官发声（缺了会自动降级到标准合成，能听但不拟人）
2. 实时语音转写 RTASR —— 语音回答（缺了只能用键盘作答）
3. 大模型的 API —— 出题与追问（必需）

音色逐个授权，控制台显示「已开通」不等于应用已绑定。设置页的「检测全部音色」会逐个试一句短文本，直接告诉你哪些能用。

## 自检工具

出问题时先跑这两个，比看日志快：

```bash
# 音色：逐个发音人试合成，附返回码解释
python tools/tts_probe.py

# 麦克风与转写：列设备 → 录 3 秒看音量 → 试转写握手
python tools/asr_probe.py
```

## 打包

```bash
python build.py         # 产出 dist/AIInterviewer/ 绿色版目录
python build.py --zip   # 额外压成可直接分发的 zip
```

## 目录结构

```
app/
├── main.py                 # 入口：建库、装配窗口
├── paths.py / config.py    # 路径与配置（含 DPAPI 加密）
├── db.py / models.py / repositories.py
├── services/
│   ├── doc_parser.py       # PDF/DOCX/TXT 提取
│   ├── profiler.py         # JD / 简历结构化
│   ├── question_planner.py # 出题
│   ├── director.py         # 面试导演：状态机 + 追问决策 + 节奏守卫
│   ├── llm_client.py       # OpenAI 兼容客户端
│   └── prompts.py          # 全部提示词
├── audio/
│   ├── tts_client.py       # 讯飞合成（超拟人/标准双协议，三级降级）
│   ├── splitter.py         # 长文本按标点切句
│   ├── player.py           # PCM 播放队列，可打断
│   ├── speaker.py          # 合成→切句→并行合成→按序播放
│   ├── recorder.py         # 麦克风采集（16k 直连 / 自动重采样）
│   ├── asr_client.py       # RTASR 客户端
│   └── transcriber.py      # 采集→节流上传→定稿
└── ui/                     # 主窗口、四个页面、控件、QSS
tests/                      # 10 套测试，含本地 mock 的讯飞与大模型
tools/                      # 自检脚本、截图脚本
docs/01-详细设计.md          # 详细设计文档（协议、DDL、状态机、验收清单）
```

## 测试

全部测试不依赖外网，用本地 mock 服务模拟讯飞与大模型：

```bash
python tests/smoke_test.py       # 配置加密、建表、CRUD
python tests/mock_llm_test.py    # 解析 → 出题 → 入库
python tests/director_test.py    # 导演机：追问、深度、早收拦截、强制收尾
python tests/tts_test.py         # 签名、切句、双协议、三级降级
python tests/asr_test.py         # 签名、节流、局部/定稿、静音判定
python tests/gui_flow_test.py    # 界面流程（需图形环境）
python tests/interview_gui_test.py
python tests/tts_gui_test.py
python tests/voice_gui_test.py
```

## 数据与隐私

所有数据只在本机：`data/app.db`（对话）、`data/secrets.bin`（加密密钥）、`data/logs/`、`data/files/`（上传的简历）、`data/audio/`（录音）。删掉 `data/` 目录即彻底重置。

仓库里不含任何密钥与运行数据。
