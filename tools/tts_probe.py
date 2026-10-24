"""音色探测：一次性搞清楚哪个发音人能真正合成出声音。

讯飞的超拟人是按发音人逐个授权的，控制台里显示「已开通」也可能因为
应用没绑定该服务而调不通。这个脚本对每个候选音色发一句 12 字的短文本，
几秒钟就能把问题定位到具体某个 vcn 或某个返回码。

用法：

    python tools/tts_probe.py                # 检测内置的全部音色
    python tools/tts_probe.py --voice x6_lingfeiyi_pro   # 只测一个
    python tools/tts_probe.py --play         # 检测完顺便播放第一个可用的
    python tools/tts_probe.py --text "你好"  # 自定义探测文本

前提：设置页里已经填好讯飞 AppID / APIKey / APISecret。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，直接 print 中文和 · 会炸
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.audio.tts_client import PROBE_TEXT, probe_voices  # noqa: E402
from app.config import config  # noqa: E402
from app.paths import CONFIG_FILE, DATA_DIR  # noqa: E402

# 常见返回码的人话解释，排查时不用再翻文档
CODE_HINTS = {
    11200: "该发音人未开通 / 应用未绑定超拟人服务",
    11201: "当日调用量已用完",
    10010: "应用未授权该能力",
    10139: "参数不合法（音色名、编码或采样率）",
    10163: "参数校验失败",
    10009: "输入内容非法",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="讯飞超拟人音色探测")
    parser.add_argument("--voice", action="append", default=None,
                        help="只检测指定 vcn，可重复")
    parser.add_argument("--text", default=PROBE_TEXT, help="探测用文本")
    parser.add_argument("--play", action="store_true", help="播放第一个可用音色")
    parser.add_argument("--timeout", type=float, default=10.0, help="单个音色超时秒数")
    args = parser.parse_args()

    print(f"数据目录：{DATA_DIR}")
    print(f"配置文件：{CONFIG_FILE}{'' if CONFIG_FILE.exists() else '（不存在，请先在设置页保存）'}")
    missing = [name for path, name in (
        ("xfyun.app_id", "AppID"),
        ("xfyun.api_key", "APIKey"),
        ("xfyun.api_secret", "APISecret"),
    ) if not str(config.get(path) or "").strip()]
    if missing:
        print("缺少讯飞凭证：" + "、".join(missing))
        return 2

    print(f"当前配置音色：{config.get('xfyun.tts.vcn')}")
    print(f"探测文本：{args.text}（{len(args.text)} 字）\n")

    report = probe_voices(args.voice, text=args.text, timeout=args.timeout)

    width = max((len(item.name) for item in report), default=4) * 2
    for item in report:
        mark = "OK  " if item.ok else "FAIL"
        line = f"[{mark}] {item.name:<{width}} {item.vcn:<26}"
        if item.ok:
            print(line + f" {item.cost_ms:>4}ms  {item.cost_ms and '音频正常'}")
        else:
            hint = CODE_HINTS.get(item.code or 0, "")
            print(line + f" code={item.code} {item.message}"
                  + (f"  ← {hint}" if hint else ""))

    ok_count = sum(1 for item in report if item.ok)
    print(f"\n可用 {ok_count}/{len(report)}")

    if ok_count == 0:
        print("\n全部失败时的排查顺序：")
        print("  1. 讯飞控制台 → 语音合成 → 超拟人语音合成 → 确认服务已开通")
        print("  2. 同一个 AppID 下「发音人管理」里逐个勾选要用的音色")
        print("  3. 确认 AppID / APIKey / APISecret 三者属于同一个应用")
        return 1

    if not report[0].ok:
        best = next(item for item in report if item.ok)
        print(f"建议在设置页把发音人改成：{best.name}（{best.vcn}）")

    if args.play:
        usable = next(item for item in report if item.ok)
        print(f"\n播放 {usable.name}…")
        from app.audio.speaker import Speaker  # noqa: PLC0415

        config.set("xfyun.tts.vcn", usable.vcn)
        speaker = Speaker()
        speaker.reset_voice()
        speaker.say(args.text, blocking=True, timeout=30)
        speaker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
