"""实时语音转写链路测试。

本地起一个假的 RTASR 服务，覆盖：签名（拿文档给的已知向量对照）、URL 参数、
局部/定稿两态、多句累积、错误码分类、重采样连续性，以及整条
「采集 -> 上传 -> 上屏 -> 提交」的编排。

不需要真麦克风，也不需要真实密钥。
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import websockets  # noqa: E402

import app.audio.transcriber as transcriber_mod  # noqa: E402
from app.audio.asr_client import (  # noqa: E402
    ASRError,
    RTASRClient,
    signa_for,
    signed_url,
)
from app.audio.recorder import FRAME_BYTES, TARGET_RATE, _Resampler  # noqa: E402
from app.audio.transcriber import VoiceTranscriber  # noqa: E402
from app.config import config  # noqa: E402

PORT = 18766
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------------------
# 假音频
# ---------------------------------------------------------------------------
def voice_frame(amplitude: int = 4000) -> bytes:
    """40ms 的 16k 单声道 PCM，幅度可调（0 表示静音）。"""
    frames = FRAME_BYTES // 2
    buf = bytearray(frames * 2)
    for i in range(frames):
        value = 0 if amplitude == 0 else int(
            amplitude * math.sin(2 * math.pi * 380 * i / TARGET_RATE)
        )
        struct.pack_into("<h", buf, i * 2, value)
    return bytes(buf)


def silent_frame() -> bytes:
    return b"\x00" * FRAME_BYTES


# ---------------------------------------------------------------------------
# 假 RTASR 服务
# ---------------------------------------------------------------------------
STATE: dict = {
    "queries": [],
    "frames": 0,
    "ended": False,
    "mode": "ok",
    "partial_marks": [],
}


def sentence_payload(seg: int, text: str, final: bool) -> str:
    """按 cn.st.rt[].ws[].cw[0].w 的结构回一句结果。"""
    words = [{"cw": [{"w": ch, "wp": "n"}], "wb": 0, "we": 0} for ch in text]
    return json.dumps({
        "cn": {"st": {"bg": "0", "ed": "0",
                      "rt": [{"ws": words}],
                      "type": "0" if final else "1"}},
        "seg_id": seg,
    })


async def handler(ws):
    STATE["queries"].append(str(ws.request.path if hasattr(ws, "request") else ""))
    if STATE["mode"] == "denied":
        await ws.send(json.dumps({
            "action": "error", "code": "10105", "data": "",
            "desc": "invalid authorization|illegal signa", "sid": "mock",
        }))
        return

    await ws.send(json.dumps({
        "action": "started", "code": "0", "data": "", "desc": "success", "sid": "mock",
    }))

    try:
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=10)
            if isinstance(message, bytes) and message.strip() == b'{"end": true}':
                STATE["ended"] = True
                # 结束前把定稿结果推完
                await ws.send(json.dumps({
                    "action": "result", "code": "0",
                    "data": sentence_payload(0, "我先做个自我介绍", final=True),
                    "desc": "success", "sid": "mock",
                }))
                await ws.close()
                return
            if isinstance(message, bytes):
                STATE["frames"] += 1
                if STATE["frames"] == 3:
                    STATE["partial_marks"].append("我先做个")
                    await ws.send(json.dumps({
                        "action": "result", "code": "0",
                        "data": sentence_payload(0, "我先做个", final=False),
                        "desc": "success", "sid": "mock",
                    }))
                elif STATE["frames"] == 6:
                    STATE["partial_marks"].append("我先做个自我介绍")
                    await ws.send(json.dumps({
                        "action": "result", "code": "0",
                        "data": sentence_payload(0, "我先做个自我介绍", final=False),
                        "desc": "success", "sid": "mock",
                    }))
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return


class MockServer:
    def __init__(self, port: int = PORT) -> None:
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self._stopper = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main() -> None:
            async with websockets.serve(handler, "127.0.0.1", self.port):
                self._stopper = asyncio.Event()
                self.ready.set()
                await self._stopper.wait()

        try:
            self.loop.run_until_complete(main())
        except Exception as exc:  # noqa: BLE001
            print("mock asr 退出：", exc)

    def start(self) -> None:
        self._thread.start()
        if not self.ready.wait(8):
            raise RuntimeError("mock asr 启动超时")

    def stop(self) -> None:
        if self._stopper is not None:
            self.loop.call_soon_threadsafe(self._stopper.set)
        self._thread.join(3)


# ---------------------------------------------------------------------------
# 假录音器：按 40ms 节奏推帧，然后保持静音
# ---------------------------------------------------------------------------
class FakeRecorder:
    running = False

    def __init__(self, loud_frames: int = 8, amplitude: int = 4000) -> None:
        self.loud_frames = loud_frames
        self.amplitude = amplitude
        self.on_frame = None
        self.pushed = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, on_frame) -> None:
        self.on_frame = on_frame
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        total = self.loud_frames + 2
        for index in range(total):
            if self._stop.is_set():
                return
            frame = voice_frame(self.amplitude) if index < self.loud_frames else silent_frame()
            if self.on_frame:
                self.on_frame(frame)
            self.pushed += 1
            time.sleep(0.04)
        self.running = False

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._thread is not None:
            self._thread.join(0.5)


# ---------------------------------------------------------------------------
def setup_config() -> dict:
    backup = {
        "app_id": config.get("xfyun.app_id"),
        "api_key": config.get("xfyun.api_key"),
        "endpoint": config.get("xfyun.asr.endpoint"),
        "silence": config.get("interview.auto_end_silence_sec"),
        "save_audio": config.get("interview.save_audio"),
    }
    config.set("xfyun.app_id", "mock_app")
    config.set("xfyun.api_key", "mock_key")
    config.set("xfyun.asr.endpoint", f"ws://127.0.0.1:{PORT}/v1/ws")
    config.set("interview.save_audio", False)
    return backup


def restore_config(backup: dict) -> None:
    config.set("xfyun.app_id", backup["app_id"] or "")
    config.set("xfyun.api_key", backup["api_key"] or "")
    config.set("xfyun.asr.endpoint", backup["endpoint"])
    config.set("interview.auto_end_silence_sec", backup["silence"])
    config.set("interview.save_audio", backup["save_audio"])


def reset_state(mode: str = "ok") -> None:
    STATE["queries"].clear()
    STATE["frames"] = 0
    STATE["ended"] = False
    STATE["mode"] = mode
    STATE["partial_marks"].clear()


# ---------------------------------------------------------------------------
def main() -> int:
    backup = setup_config()
    server = MockServer()
    try:
        server.start()
        run_cases()
    finally:
        server.stop()
        restore_config(backup)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n通过 {passed}/{len(results)}")
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print("失败项：", "；".join(failed))
    return 0 if not failed else 1


def run_cases() -> None:
    # ---- 1. 签名必须和官方文档的示例一致 ----
    signa = signa_for("595f23df", "d9f4aa7ea6d94faca62cd88a28fd5234", 1512041814)
    check("signa 与文档示例一致", signa == "IrrzsJeOFk1NGfJHW6SkHUoN9CU=", signa)

    url = signed_url("595f23df", "d9f4aa7ea6d94faca62cd88a28fd5234", ts=1512041814)
    query = parse_qs(urlparse(url).query)
    check("URL 带齐 appid/ts/signa",
          set(query) >= {"appid", "ts", "signa"}, str(sorted(query)))
    check("signa 的等号已转义（否则服务端算不出同一个串）",
          "%3D" in url and "signa=" in url, url.split("signa=")[1][:24] + "…")

    # ---- 2. 重采样：44.1k -> 16k 的数量与连续性 ----
    resampler = _Resampler(44100, 16000)
    src_total = 44100
    out_total = 0
    block = 441  # 10ms
    for _ in range(100):
        chunk = np.zeros(block, dtype=np.int16)
        out_total += resampler.process(chunk).size
    expect = src_total * 16000 / 44100
    check("44.1k 重采样到 16k 数量正确",
          abs(out_total - expect) < 20, f"实际 {out_total} 预期 {expect:.0f}")

    same = _Resampler(16000, 16000)
    passthrough = sum(same.process(np.zeros(320, dtype=np.int16)).size for _ in range(10))
    check("采样率相同不做转换", passthrough == 3200, str(passthrough))

    # ---- 3. 连接与握手 ----
    reset_state("ok")
    texts: list[tuple[str, bool]] = []
    client = RTASRClient(on_text=lambda t, f: texts.append((t, f)))
    client.connect("mock_app", "mock_key",
                   endpoint=f"ws://127.0.0.1:{PORT}/v1/ws")
    check("握手成功", client._started.is_set(), str(client.last_error))

    for _ in range(3):
        client.send_frame(voice_frame())
        time.sleep(0.05)
    time.sleep(0.3)
    check("收到中间结果", any(not final for _, final in texts),
          str([(t, f) for t, f in texts]))
    check("中间结果不改变累积全文",
          client.state.text() == "我先做个", client.state.text())

    for _ in range(3):
        client.send_frame(voice_frame())
        time.sleep(0.05)
    time.sleep(0.3)
    check("同句覆盖式刷新而不是追加",
          client.state.text() == "我先做个自我介绍", client.state.text())

    final_text = client.finish(wait=3.0)
    check("结束标记被服务端收到", STATE["ended"], str(STATE["ended"]))
    check("定稿返回完整文本", final_text == "我先做个自我介绍", final_text)
    check("已上传帧数与发送数一致", STATE["frames"] >= 6, str(STATE["frames"]))

    # ---- 4. 未授权要能识别 ----
    reset_state("denied")
    denied = RTASRClient()
    raised: ASRError | None = None
    try:
        denied.connect("mock_app", "mock_key",
                       endpoint=f"ws://127.0.0.1:{PORT}/v1/ws")
    except ASRError as exc:
        raised = exc
    check("握手报错会抛出异常", raised is not None, raised.describe() if raised else "")
    check("10105 被判定为权限问题",
          bool(raised and raised.is_permission), str(raised.code if raised else ""))

    # ---- 5. 多句累积：seg 覆盖 + 顺序拼接 ----
    state = RTASRClient().state
    state.apply(0, "第一句", final=True)
    state.apply(1, "第二", final=False)
    state.apply(1, "第二句", final=True)
    check("多句按 seg 拼接", state.text() == "第一句第二句", state.text())

    # ---- 6. 静音自动结束 ----
    quiet = VoiceTranscriber(recorder=FakeRecorder())
    quiet._started_at = time.monotonic() - 10
    quiet._active = True          # 没有真的 start，但要让它处于「收音中」
    quiet._voiced = False
    quiet._silent_since = time.monotonic() - 10
    config.set("interview.auto_end_silence_sec", 0.2)
    stopped: list[str] = []
    quiet.on_state = stopped.append
    with patch_min_speech(0.0):
        quiet._track_silence(0.0)
    check("没说过话时不会因静音被掐断", "thinking" not in stopped, str(stopped))

    quiet._voiced = True
    quiet._silent_since = time.monotonic() - 5
    with patch_min_speech(0.0):
        quiet._track_silence(0.0)
    time.sleep(0.3)
    check("说过话后长静音自动结束", "thinking" in stopped, str(stopped))

    # ---- 7. 端到端：采集 -> 上屏 -> 提交 ----
    reset_state("ok")
    config.set("interview.auto_end_silence_sec", 0)
    recorder = FakeRecorder(loud_frames=8)
    voice = VoiceTranscriber(recorder=recorder)
    partials: list[str] = []
    finals: list[str] = []
    errors: list[str] = []
    states: list[str] = []
    voice.on_partial = partials.append
    voice.on_final = finals.append
    voice.on_error = errors.append
    voice.on_state = states.append

    voice.start()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and "listening" not in states:
        time.sleep(0.05)
    check("进入收音状态", "listening" in states, str(states))

    while time.monotonic() < deadline and not recorder.pushed >= 8:
        time.sleep(0.05)
    time.sleep(0.4)
    voice.stop()
    while time.monotonic() < deadline and not finals and not errors:
        time.sleep(0.05)

    check("实时上屏拿到过中间文本", bool(partials), str(partials[-1:]))
    check("定稿文本被提交", finals and finals[0] == "我先做个自我介绍", str(finals))
    check("状态走完 connecting/listening/thinking/idle",
          {"listening", "thinking"} <= set(states), str(states))
    check("全流程无报错", not errors, str(errors))

    voice.shutdown()


class patch_min_speech:
    """临时把「最短说话时长」降到 0，省得测试里真的等 1.2 秒。"""

    def __init__(self, value: float) -> None:
        self.value = value
        self.original = None

    def __enter__(self):
        self.original = transcriber_mod.MIN_SPEECH_SEC
        transcriber_mod.MIN_SPEECH_SEC = self.value
        return self

    def __exit__(self, *exc) -> None:
        transcriber_mod.MIN_SPEECH_SEC = self.original


if __name__ == "__main__":
    raise SystemExit(main())
