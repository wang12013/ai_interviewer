"""麦克风与实时转写自检。

分三步，前三步不需要密钥，能快速把问题定位到「设备 / 权限 / 授权」中的某一层：

    1. 列出录音设备
    2. 录 3 秒，报告帧数、音量、是否有声音（判断设备和权限）
    3. 连一次 RTASR 并说一句话（判断开通与授权）

用法：

    python tools/asr_probe.py              # 三步全跑
    python tools/asr_probe.py --no-record  # 只测连接，不占用麦克风
    python tools/asr_probe.py --device 11  # 指定录音设备
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from app.audio.asr_client import ASRError, RTASRClient, RTASR_ENDPOINT  # noqa: E402
from app.audio.recorder import (  # noqa: E402
    FRAME_BYTES,
    TARGET_RATE,
    AudioInputError,
    MicrophoneRecorder,
    list_input_devices,
)
from app.config import config  # noqa: E402


def step_devices() -> list[tuple[int, str]]:
    print("① 录音设备")
    devices = list_input_devices()
    if not devices:
        print("   没有找到任何录音设备。请插上麦克风，或检查系统隐私设置里的「麦克风」权限。")
        return []
    for index, name in devices:
        print(f"   [{index}] {name}")
    return devices


def step_record(device: int | None, seconds: float = 3.0) -> bool:
    print(f"\n② 录 {seconds:.0f} 秒（请正常说一句话）")
    recorder = MicrophoneRecorder(device=device)
    stats = {"frames": 0, "peak": 0.0, "voiced": False}

    def on_frame(frame: bytes) -> None:
        stats["frames"] += 1
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float64)
        rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
        stats["peak"] = max(stats["peak"], rms)
        if rms >= 350:
            stats["voiced"] = True

    try:
        recorder.start(on_frame)
    except AudioInputError as exc:
        print(f"   打开麦克风失败：{exc}")
        return False

    print(f"   设备采样率：{recorder.source_rate} Hz"
          + ("（将重采样到 16000）" if recorder.source_rate != TARGET_RATE else "（直接 16000）"))
    time.sleep(seconds)
    recorder.stop()

    expect = int(seconds * 1000 / 40)
    print(f"   收到 {stats['frames']} 帧（{seconds:.0f} 秒理论值约 {expect} 帧）")
    print(f"   峰值音量 RMS：{stats['peak']:.0f}（说话通常 >1000，安静环境 <300）")

    if stats["frames"] < expect * 0.6:
        print("   ✗ 帧数明显偏少：麦克风可能被其他程序独占，或驱动有问题。")
        return False
    if not stats["voiced"]:
        print("   ✓ 设备通路正常，但没听到说话声：检查是否静音、麦克风增益过低，")
        print("     或选错了设备（试试 --device 换一个）。")
        return False
    print("   ✓ 采集正常，且检测到说话声。")
    return True


def step_asr(device: int | None, seconds: float = 4.0) -> bool:
    print(f"\n③ 实时转写（说 {seconds:.0f} 秒）")
    app_id = str(config.get("xfyun.app_id") or "").strip()
    api_key = str(config.get("xfyun.api_key") or "").strip()
    if not app_id or not api_key:
        print("   缺少讯飞 AppID / APIKey，跳过。请先在设置页填写并保存。")
        return False

    endpoint = str(config.get("xfyun.asr.endpoint") or RTASR_ENDPOINT)
    texts: list[str] = []
    errors: list[str] = []
    client = RTASRClient(on_text=lambda text, final: texts.append(text),
                         on_error=lambda err: errors.append(err.describe()))

    started = time.monotonic()
    try:
        client.connect(app_id, api_key, endpoint=endpoint,
                       lang=str(config.get("xfyun.asr.lang") or "cn"))
    except ASRError as exc:
        print(f"   ✗ 握手失败：{exc.describe()}")
        if exc.is_permission:
            print("     → 控制台里确认已开通「实时语音转写」，且 Key 属于同一个应用。")
        return False
    print(f"   ✓ 握手成功（{int((time.monotonic() - started) * 1000)}ms）")

    recorder = MicrophoneRecorder(device=device)
    try:
        recorder.start(lambda frame: client.send_frame(frame))
    except AudioInputError as exc:
        client.abort()
        print(f"   ✗ 麦克风打开失败：{exc}")
        return False

    time.sleep(seconds)
    recorder.stop()
    final_text = client.finish(wait=5.0)

    if errors:
        print(f"   服务端报错：{errors[0]}")
    print(f"   上传 {FRAME_BYTES * client.sent_frames // 1024} KB，"
          f"{client.sent_frames} 帧")
    if final_text:
        print(f"   ✓ 识别结果：{final_text}")
        return True
    print("   ✗ 没有识别到文本。若②能听到声音，多半是发音人音量太低或离麦克风太远。")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="麦克风与实时转写自检")
    parser.add_argument("--device", type=int, default=None, help="录音设备序号")
    parser.add_argument("--seconds", type=float, default=3.0, help="录音时长")
    parser.add_argument("--no-record", action="store_true", help="跳过录音，只测连接")
    args = parser.parse_args()

    print(f"讯飞配置：AppID {config.get('xfyun.app_id') or '（未填）'}")
    devices = step_devices()

    if args.no_record:
        client = RTASRClient()
        endpoint = str(config.get("xfyun.asr.endpoint") or RTASR_ENDPOINT)
        try:
            client.connect(str(config.get("xfyun.app_id") or ""),
                           str(config.get("xfyun.api_key") or ""), endpoint=endpoint)
        except ASRError as exc:
            print(f"\n连接失败：{exc.describe()}")
            return 1
        print("\n连接正常，转写服务可用。")
        client.abort()
        return 0

    if not devices:
        return 1

    record_ok = step_record(args.device, args.seconds)
    asr_ok = step_asr(args.device, args.seconds)

    print("\n结论")
    if record_ok and asr_ok:
        print("  麦克风与转写链路都能用，可以在面试页点「语音回答」了。")
        return 0
    if not record_ok:
        print("  先解决麦克风（第②步），再试转写。")
    else:
        print("  麦克风没问题，问题在转写服务：确认控制台开通「实时语音转写」。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
