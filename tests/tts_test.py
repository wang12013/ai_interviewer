"""TTS 语音合成链路测试。

本地起一个 WebSocket 服务模拟讯飞接口，覆盖两条协议、编码降级、错误码、
并发合成的顺序保证以及打断行为。不需要真实密钥，也不依赖声卡。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402

from app.audio.speaker import Speaker  # noqa: E402
from app.audio.splitter import split_now  # noqa: E402
from app.audio.tts_client import (  # noqa: E402
    TTSEngine,
    TTSError,
    XfyunTTS,
    probe_voices,
    signed_url,
)
from app.config import FALLBACK_STANDARD_VCN, FALLBACK_SUPER_VCN, config  # noqa: E402

PORT = 18765
RATE = 24000

STATE: dict = {
    "mode": "ok",
    "super_encodings": [],
    "texts": [],
    "vcns": [],
    "orals": [],
    "std_vcns": [],
    "slow_first": False,
    "delay": 0.0,
}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------------------
# 假音频：按字数生成正弦波，时长接近真实朗读速度
# ---------------------------------------------------------------------------
def sine_pcm(chars: int, rate: int = RATE) -> bytes:
    duration = max(0.35, chars * 0.16)
    frames = int(rate * duration)
    buf = bytearray(frames * 2)
    for i in range(frames):
        struct.pack_into("<h", buf, i * 2,
                         int(8000 * math.sin(2 * math.pi * 420 * i / rate)))
    return bytes(buf)


# ---------------------------------------------------------------------------
# 模拟服务
# ---------------------------------------------------------------------------
async def handler(ws):
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        request = json.loads(raw)
        if "header" in request:
            await handle_super(ws, request)
        else:
            await handle_standard(ws, request)
    except (asyncio.TimeoutError, websockets.ConnectionClosed, json.JSONDecodeError):
        pass


async def handle_super(ws, request: dict) -> None:
    text = base64.b64decode(request["payload"]["text"]["text"]).decode("utf-8")
    parameter = request.get("parameter") or {}
    tts_block = parameter.get("tts") or {}
    audio_cfg = tts_block.get("audio") or {}
    encoding = audio_cfg.get("encoding", "raw")
    rate = int(audio_cfg.get("sample_rate", RATE))
    vcn = str(tts_block.get("vcn") or "")
    STATE["super_encodings"].append(encoding)
    STATE["vcns"].append(vcn)
    STATE["orals"].append("oral" in parameter)

    if STATE["mode"] == "noauth":
        await ws.send(json.dumps({
            "header": {"code": 11200, "message": "功能未授权", "sid": "mock"},
        }))
        return

    # 模拟「只开通了聆小璇」的账号：其他音色一律回未授权
    if STATE["mode"] == "voice_denied" and vcn != "x6_lingxiaoxuan_pro":
        await ws.send(json.dumps({
            "header": {"code": 11200, "message": "该发音人未授权", "sid": "mock"},
        }))
        return

    if STATE["mode"] == "raw_rejected" and encoding == "raw":
        await ws.send(json.dumps({
            "header": {"code": 10139, "message": "参数校验失败", "sid": "mock"},
        }))
        return

    if STATE["mode"] == "raw_rejected":
        # 假装接受 lame，但返回的不是合法 mp3，逼出解码失败后的降级路径
        await ws.send(json.dumps({
            "header": {"code": 0, "message": "success", "sid": "mock", "status": 2},
            "payload": {"audio": {
                "encoding": "lame", "sample_rate": rate, "channels": 1,
                "bit_depth": 16, "status": 2, "seq": 0,
                "audio": base64.b64encode(b"\x00not-a-real-mp3\x00").decode(),
            }},
        }))
        return

    await emit_audio(ws, text, rate, encoding, protocol="super")


async def handle_standard(ws, request: dict) -> None:
    text = base64.b64decode(request["data"]["text"]).decode("utf-8")
    STATE["std_vcns"].append(str((request.get("business") or {}).get("vcn") or ""))
    if STATE["mode"] == "all_fail":
        await ws.send(json.dumps({"code": 11200, "message": "功能未授权", "sid": "mock"}))
        return
    await emit_audio(ws, text, 16000, "raw", protocol="standard")


async def emit_audio(ws, text: str, rate: int, encoding: str, *, protocol: str) -> None:
    index = len(STATE["texts"])
    STATE["texts"].append(text)
    # 第一句故意慢，用来验证「后发出的句子先合成完」时播放顺序仍然正确
    delay = STATE["delay"] or (0.35 if (STATE["slow_first"] and index == 0) else 0.0)

    pcm = sine_pcm(len(text), rate)
    half = max(2, len(pcm) // 2)
    parts = [pcm[:half], pcm[half:]]

    for seq, chunk in enumerate(parts):
        if delay:
            await asyncio.sleep(delay)
            delay = 0.0
        audio_block = {
            "encoding": encoding, "sample_rate": rate, "channels": 1,
            "bit_depth": 16, "status": 2 if seq == len(parts) - 1 else 1,
            "seq": seq, "audio": base64.b64encode(chunk).decode(),
        }
        # 两套协议连响应体结构都不一样：超拟人套在 payload.audio 里，
        # 标准合成是顶层 code + data
        if protocol == "super":
            message = {
                "header": {"code": 0, "message": "success", "sid": "mock", "status": 1},
                "payload": {"audio": audio_block},
            }
        else:
            message = {"code": 0, "message": "success", "sid": "mock",
                       "data": audio_block}
        await ws.send(json.dumps(message))


class MockServer:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self._stopper: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main() -> None:
            async with websockets.serve(handler, "127.0.0.1", PORT):
                self._stopper = asyncio.Event()
                self.ready.set()
                await self._stopper.wait()

        try:
            self.loop.run_until_complete(main())
        except Exception as exc:  # noqa: BLE001
            print("mock server 退出：", exc)

    def start(self) -> None:
        self._thread.start()
        if not self.ready.wait(8):
            raise RuntimeError("mock 服务启动超时")

    def stop(self) -> None:
        if self._stopper is not None:
            self.loop.call_soon_threadsafe(self._stopper.set)
        self._thread.join(3)


# ---------------------------------------------------------------------------
# 假播放器：记录收到的 PCM，不碰声卡
# ---------------------------------------------------------------------------
class FakePlayer:
    """按真实时长模拟播放，不碰声卡。

    is_playing 依据「已喂入的音频总时长」与「经过的时间」比较，
    这样 Speaker 的播完检测才能被真实验证。
    """

    def __init__(self, on_state=None) -> None:
        self.on_state = on_state
        self.chunks: list[bytes] = []
        self.rates: list[int] = []
        self.clears = 0
        self.closed = False
        self._fed_seconds = 0.0
        self._started = time.monotonic()

    @property
    def total_bytes(self) -> int:
        return sum(len(c) for c in self.chunks)

    @property
    def played_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    @property
    def is_playing(self) -> bool:
        return self._fed_seconds > self.played_seconds + 0.01

    def feed(self, pcm: bytes, rate: int | None = None) -> None:
        if not pcm:
            return
        if not self.is_playing:
            # 上一段已经播完，重新计时
            self._started = time.monotonic()
            self._fed_seconds = 0.0
        rate = int(rate or RATE)
        self.chunks.append(bytes(pcm))
        self.rates.append(rate)
        self._fed_seconds += len(pcm) / 2 / rate
        if self.on_state:
            self.on_state(True)

    def clear(self) -> None:
        self.clears += 1
        self._fed_seconds = 0.0
        self._started = time.monotonic()
        if self.on_state:
            self.on_state(False)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
def setup_config() -> dict:
    backup = {
        "app_id": config.get("xfyun.app_id"),
        "api_key": config.get("xfyun.api_key"),
        "api_secret": config.get("xfyun.api_secret"),
        "mode": config.get("xfyun.tts.mode"),
        "endpoint_super": config.get("xfyun.tts.endpoint_super"),
        "endpoint_std": config.get("xfyun.tts.endpoint_std"),
        "vcn": config.get("xfyun.tts.vcn"),
    }
    config.set("xfyun.app_id", "mock_app")
    config.set("xfyun.api_key", "mock_key")
    config.set("xfyun.api_secret", "mock_secret")
    config.set("xfyun.tts.mode", "super")
    config.set("xfyun.tts.endpoint_super", f"ws://127.0.0.1:{PORT}/super")
    config.set("xfyun.tts.endpoint_std", f"ws://127.0.0.1:{PORT}/std")
    config.set("xfyun.tts.vcn", "x6_lingxiaoxuan_pro")
    return backup


def restore_config(backup: dict) -> None:
    config.set("xfyun.app_id", backup["app_id"] or "")
    config.set("xfyun.api_key", backup["api_key"] or "")
    config.set("xfyun.api_secret", backup["api_secret"] or "")
    config.set("xfyun.tts.mode", backup["mode"] or "super")
    config.set("xfyun.tts.endpoint_super", backup["endpoint_super"])
    config.set("xfyun.tts.endpoint_std", backup["endpoint_std"])
    config.set("xfyun.tts.vcn", backup["vcn"] or FALLBACK_SUPER_VCN)


def reset_state(mode: str = "ok", slow_first: bool = False) -> None:
    STATE["mode"] = mode
    STATE["super_encodings"].clear()
    STATE["texts"].clear()
    STATE["vcns"].clear()
    STATE["orals"].clear()
    STATE["std_vcns"].clear()
    STATE["slow_first"] = slow_first


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
    # ---- 1. 签名地址 ----
    url = signed_url(f"ws://127.0.0.1:{PORT}/super", "key1", "secret1")
    signed_ok = "authorization=" in url and "date=" in url and "host=" in url
    check("签名地址包含三个必要参数", signed_ok, url.split("?")[1][:40] + "…")

    # ---- 2. 分句器 ----
    parts = split_now("先做个自我介绍。重点讲讲你最近的项目，以及你在里面做了什么决策。")
    check("分句器按句末标点切分", len(parts) == 2, " / ".join(parts))

    # ---- 3. 超拟人合成成功 ----
    reset_state("ok")
    client = XfyunTTS()
    result = client.synthesize("你好，我是今天的面试官。", protocol="super")
    expect = sine_pcm(12, RATE)
    check("超拟人返回 PCM", len(result.pcm) > 0 and result.sample_rate == RATE,
          f"{len(result.pcm)} 字节 / {result.sample_rate}Hz / {result.cost_ms}ms")
    check("分帧按 seq 拼接后与原文长度一致",
          abs(len(result.pcm) - len(expect)) < 2048,
          f"实际 {len(result.pcm)} vs 预期 {len(expect)}")
    check("超拟人默认使用 raw 编码", STATE["super_encodings"] == ["raw"],
          str(STATE["super_encodings"]))

    # ---- 3.5 音色与 oral 参数：x6 不能带 oral，x4 才带 ----
    reset_state("ok")
    client.synthesize("你好。", protocol="super", vcn="x6_lingxiaoxuan_pro")
    check("x6 音色不下发 oral 参数", STATE["orals"] == [False], str(STATE["orals"]))
    check("x6 音色按配置原样下发", STATE["vcns"] == ["x6_lingxiaoxuan_pro"],
          str(STATE["vcns"]))

    reset_state("ok")
    client.synthesize("你好。", protocol="super", vcn="x4_lingxiaoxuan_oral")
    check("x4 音色仍然下发 oral 参数", STATE["orals"] == [True], str(STATE["orals"]))

    reset_state("ok")
    standard = client.synthesize("你好。", protocol="standard",
                                 vcn="x6_lingfeiyi_pro")
    check("标准合成不硬塞超拟人音色",
          STATE["std_vcns"] == [FALLBACK_STANDARD_VCN] and standard.protocol == "standard",
          str(STATE["std_vcns"]))

    # ---- 4. 超拟人未授权 → 自动降级标准合成 ----
    reset_state("noauth")
    engine = TTSEngine()
    engine.set_protocol("super")
    result = engine.synthesize("为什么选择这个方案？")
    check("超拟人未授权时降级到标准合成",
          result.protocol == "standard" and len(result.pcm) > 0,
          f"protocol={result.protocol} rate={result.sample_rate}")
    check("降级被标记出来", engine.degraded and bool(engine.degraded_reason),
          engine.degraded_reason)
    check("降级原因指明是音色问题",
          "音色" in engine.degraded_reason or "开通" in engine.degraded_reason,
          engine.degraded_reason)

    # ---- 4.5 音色未开通 → 自动换成默认免费音色（协议不变）----
    reset_state("voice_denied")
    config.set("xfyun.tts.vcn", "x6_gaolengnanshen_pro")
    engine = TTSEngine()
    engine.set_protocol("super")
    result = engine.synthesize("先做个自我介绍吧。")
    check("音色未开通时自动换兜底音色仍然出声",
          result.protocol == "super" and len(result.pcm) > 0,
          f"protocol={result.protocol}")
    check("换音色的过程对用户可解释",
          bool(engine.voice_notice) and "聆小璇" in engine.voice_notice,
          engine.voice_notice)
    check("换音色不算协议降级", engine.degraded is False, str(engine.degraded))
    check("重试顺序是先原音色后兜底",
          STATE["vcns"] == ["x6_gaolengnanshen_pro", "x6_lingxiaoxuan_pro"],
          str(STATE["vcns"]))

    reset_state("voice_denied")
    engine.synthesize("第二个问题。")
    check("兜底音色被记住，不再重复失败",
          STATE["vcns"] == ["x6_lingxiaoxuan_pro"], str(STATE["vcns"]))
    check("对外报出的是实际使用的音色",
          engine.voice() == FALLBACK_SUPER_VCN, engine.voice())
    # 还原配置，后面的用例还要用它
    config.set("xfyun.tts.vcn", FALLBACK_SUPER_VCN)

    # ---- 5. raw 被拒 → 试 lame → 解码失败 → 降级标准 ----
    reset_state("raw_rejected")
    engine = TTSEngine()
    engine.set_protocol("super")
    result = engine.synthesize("系统上线后做了哪些监控？")
    check("raw 被拒后尝试 lame", "lame" in STATE["super_encodings"],
          str(STATE["super_encodings"]))
    check("lame 解码失败后仍能出声（降级标准）",
          result.protocol == "standard" and len(result.pcm) > 0,
          f"protocol={result.protocol}")

    # ---- 6. 全部失败要抛错 ----
    reset_state("all_fail")
    engine = TTSEngine()
    engine.set_protocol("standard")
    raised = False
    try:
        engine.synthesize("这段文字合不出来")
    except TTSError as exc:
        raised = True
        check("错误对象带可读描述", "未授权" in exc.describe() or exc.code == 11200,
              exc.describe())
    check("全部路径失败时抛出 TTSError", raised)

    # ---- 7. 播报器：多句并行合成，顺序不能乱 ----
    reset_state("ok", slow_first=True)
    holder: dict = {}

    def factory(on_state):
        holder["player"] = FakePlayer(on_state)
        return holder["player"]

    speaker = Speaker(player_factory=factory)
    text = "我们聊聊你的项目。订单中台重构里分片键为什么这么选？说说当时的取舍。"
    sentences = split_now(text)
    speaker.say(text, blocking=True, timeout=20)
    time.sleep(0.3)
    player = holder["player"]
    total = player.total_bytes
    expect_total = sum(len(sine_pcm(len(s), RATE)) for s in sentences)
    check("播报覆盖全部句子",
          abs(total - expect_total) < 4096,
          f"{len(sentences)} 句 / 实际 {total} 字节 / 预期 {expect_total} 字节")
    expect_first = len(sine_pcm(len(sentences[0]), RATE))
    check("第一句合成更慢时播放顺序仍然正确",
          len(player.chunks) >= 2 and abs(len(player.chunks[0]) - expect_first) < 256,
          f"首块 {len(player.chunks[0])} 字节 / 第一句应为 {expect_first}")

    # ---- 8. 播到一半打断 ----
    reset_state("ok")
    STATE["delay"] = 0.25
    holder2: dict = {}

    def factory2(on_state):
        holder2["player"] = FakePlayer(on_state)
        return holder2["player"]

    speaker2 = Speaker(player_factory=factory2)
    long_text = "这是第一句话。这是第二句话。这是第三句话。这是第四句话。"
    speaker2.say(long_text)
    time.sleep(0.45)
    mid = holder2["player"].total_bytes
    speaker2.stop()
    time.sleep(0.7)
    after_stop = holder2["player"].total_bytes
    check("播到一半打断后不再追加音频",
          mid > 0 and after_stop == mid,
          f"打断时 {mid} 字节，之后 {after_stop} 字节")
    check("打断会清空播放缓冲", holder2["player"].clears >= 1,
          f"clear 调用 {holder2['player'].clears} 次")
    STATE["delay"] = 0.0

    speaker.close()
    speaker2.close()

    # ---- 9. 音色探测：能分清哪个开通了 ----
    reset_state("voice_denied")
    probes = probe_voices(["x6_lingxiaoxuan_pro", "x6_gaolengnanshen_pro"])
    check("探测能区分可用与未开通",
          [item.ok for item in probes] == [True, False],
          " / ".join(item.describe() for item in probes))
    check("探测结果带返回码", probes[1].code == 11200, str(probes[1].code))

    # ---- 10. 历史 x4 音色自动迁移 ----
    config.set("xfyun.tts.vcn", "x4_lingxiaoxuan_oral")
    config._migrate_voice()
    check("历史 x4 音色迁移到默认免费音色",
          config.get("xfyun.tts.vcn") == FALLBACK_SUPER_VCN,
          str(config.get("xfyun.tts.vcn")))


if __name__ == "__main__":
    raise SystemExit(main())
