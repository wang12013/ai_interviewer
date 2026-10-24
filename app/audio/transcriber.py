"""语音回答编排：麦克风 -> 节流上传 -> 实时上屏 -> 定稿。

一条语音回答的生命周期：

    点按钮 --(后台握手)--> listening --(说话/沉默)--> 点结束或静音自动结束
                                                        |
                                                 thinking（等服务端吐最后一帧）

三个容易做错的地方：

1. **上传必须匀速**。文档明确说发送过快会让引擎出错，而声卡回调的节奏会
   抖动，所以上传线程按「上一帧时间 + 40ms」对齐，而不是收到就发。
2. **静音结束要在「说过话之后」才计时**。刚点开按钮就计时的话，用户稍微
   想一下就被判结束，体验极差。
3. **停止是异步的**。service finish 要等最多 4 秒（服务端把剩余结果推完），
   绝不能占着界面线程。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import config
from .asr_client import ASRError, RTASRClient, RTASR_ENDPOINT
from .recorder import FRAME_BYTES, TARGET_RATE, AudioInputError, MicrophoneRecorder

logger = logging.getLogger("asr")

# 静音判定：16bit PCM 的 RMS 门槛。安静环境约 100~300，正常说话 1000 以上
SILENCE_RMS = 350
# 点开按钮后至少录这么久才允许静音结束，避免"想一下就被掐"
MIN_SPEECH_SEC = 1.2
# 单条回答上限，防止用户忘点结束一直占着连接
MAX_ANSWER_SEC = 90.0
# 队列上限：约 2 秒音频，超了说明上传跟不上，丢最旧的
QUEUE_MAX = 50


class VoiceTranscriber:
    """一次「语音回答」的完整流程。所有回调都在后台线程里触发。"""

    def __init__(self, *, device: int | str | None = None,
                 recorder: MicrophoneRecorder | None = None) -> None:
        self._device = device
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_MAX)
        # 允许注入替身，测试时就不必真的占麦克风
        self._recorder = recorder or MicrophoneRecorder(device=device)
        self._worker: threading.Thread | None = None
        self._finisher: threading.Thread | None = None
        self._active = False
        self._stopping = False
        self._submit = True
        self._lock = threading.Lock()

        self._voiced = False
        self._silent_since: float | None = None
        self._started_at = 0.0
        self._frames_uploaded = 0
        self._audio = bytearray()

        # 界面钩子
        self.on_partial: Callable[[str], None] | None = None
        self.on_final: Callable[[str], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_state: Callable[[str], None] | None = None   # connecting/listening/thinking/idle
        self.on_level: Callable[[float], None] | None = None
        self.audio_dir: Path | None = None
        self.last_audio_path: Path | None = None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    def ready(self) -> tuple[bool, str]:
        missing = [
            name for key, name in (("xfyun.app_id", "AppID"), ("xfyun.api_key", "APIKey"))
            if not str(config.get(key) or "").strip()
        ]
        if missing:
            return False, "缺少讯飞" + "、".join(missing) + "，无法转写"
        return True, ""

    # ------------------------------------------------------------------
    # 开关
    # ------------------------------------------------------------------
    def start(self) -> None:
        """开始收音。握手与开设备都在后台，界面立刻能返回。"""
        with self._lock:
            if self._active:
                return
            ok, reason = self.ready()
            if not ok:
                self._emit_error(reason)
                return
            self._active = True
            self._stopping = False
            self._voiced = False
            self._silent_since = None
            self._started_at = time.monotonic()
            self._frames_uploaded = 0
            self._audio.clear()
            self._drain_queue()
            self.last_audio_path = None

        self._emit_state("connecting")
        self._worker = threading.Thread(target=self._run, name="asr-worker", daemon=True)
        self._worker.start()

    def stop(self, *, submit: bool = True) -> None:
        """结束收音。submit=False 表示放弃这段（例如面试官开始说话）。"""
        with self._lock:
            if not self._active or self._stopping:
                return
            self._stopping = True
            self._submit = submit
        self._emit_state("thinking")
        # 关设备很快，但 finish 要等服务端，必须放到后台
        self._finisher = threading.Thread(target=self._finish, args=(submit,),
                                          name="asr-finish", daemon=True)
        self._finisher.start()

    def shutdown(self) -> None:
        try:
            self._recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        self._active = False
        self._stopping = True
        self._drain_queue()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def _run(self) -> None:
        app_id = str(config.get("xfyun.app_id") or "").strip()
        api_key = str(config.get("xfyun.api_key") or "").strip()
        endpoint = str(config.get("xfyun.asr.endpoint") or RTASR_ENDPOINT)
        lang = str(config.get("xfyun.asr.lang") or "cn")

        client = RTASRClient(on_text=self._on_text, on_error=self._on_client_error)
        try:
            client.connect(app_id, api_key, endpoint=endpoint, lang=lang)
        except ASRError as exc:
            logger.warning("转写握手失败：%s", exc.describe())
            self._fail(exc.describe())
            return

        try:
            self._recorder.start(self._on_frame)
        except AudioInputError as exc:
            client.abort()
            self._fail(str(exc))
            return

        self._emit_state("listening")
        self._upload_loop(client)

        try:
            self._recorder.stop()
        except Exception:  # noqa: BLE001
            pass

        # finish 会等服务端把最后几句推完，最多 4 秒
        try:
            text = client.finish(wait=4.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("收取最终转写结果失败：%s", exc)
            text = ""

        with self._lock:
            submit = getattr(self, "_submit", True)
            self._active = False
            self._stopping = False
        self._emit_state("idle")

        text = (text or "").strip()
        if not submit:
            return
        if not text:
            self._emit_error("这段没有识别到内容，请再说一次或直接打字")
            return
        self._save_audio()
        self._emit_final(text)

    def _upload_loop(self, client: RTASRClient) -> None:
        """按 40ms 节奏把队列里的帧送出去。"""
        frame_sec = FRAME_BYTES / 2 / TARGET_RATE
        next_at = time.monotonic()
        while True:
            # 说话时队列一直有货，超时检查必须放在取队列之前
            if time.monotonic() - self._started_at > MAX_ANSWER_SEC:
                logger.info("单条回答超过 %.0f 秒，自动结束", MAX_ANSWER_SEC)
                self._auto_stop()
                break
            try:
                frame = self._queue.get(timeout=0.15)
            except queue.Empty:
                if self._stopping:
                    break
                continue
            if frame is None:
                break

            now = time.monotonic()
            if next_at > now:
                time.sleep(next_at - now)
            next_at = max(next_at + frame_sec, time.monotonic())

            try:
                client.send_frame(frame)
            except ASRError as exc:
                logger.warning("上传中断：%s", exc.describe())
                self._fail(exc.describe())
                break
            self._frames_uploaded += 1

    def _finish(self, submit: bool) -> None:
        # 先停录音，让队列不再有新帧进来，再塞结束哨兵
        try:
            self._recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._queue.put(None, timeout=0.5)
        except queue.Full:
            self._drain_queue()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        worker = self._worker
        if worker is not None and worker.is_alive():
            # worker 发完队列里剩余的帧才退出，给它一点时间但别无限等
            worker.join(6.0)

    # ------------------------------------------------------------------
    # 采集回调
    # ------------------------------------------------------------------
    def _on_frame(self, frame: bytes) -> None:
        if self._stopping or not frame:
            return

        samples = np.frombuffer(frame, dtype="<i2")
        if samples.size:
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        else:
            rms = 0.0

        self._track_silence(rms)
        if not self._stopping:
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                # 上传跟不上就丢最旧的，宁可少一段也别让延迟无限增长
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(frame)
                except queue.Empty:
                    pass
            if len(self._audio) < TARGET_RATE * 2 * MAX_ANSWER_SEC:
                self._audio.extend(frame)

        if self.on_level is not None and self._frames_uploaded % 5 == 0:
            try:
                self.on_level(min(1.0, rms / 6000.0))
            except Exception:  # noqa: BLE001
                pass

    def _track_silence(self, rms: float) -> None:
        now = time.monotonic()
        if rms >= SILENCE_RMS:
            self._voiced = True
            self._silent_since = None
            return
        if self._silent_since is None:
            self._silent_since = now
            return

        try:
            silence_sec = float(config.get("interview.auto_end_silence_sec", 2.0) or 0)
        except (TypeError, ValueError):
            silence_sec = 0.0
        if silence_sec <= 0:
            return
        # 只有「说过话」之后才允许静音结束，且整体不能太短
        if not self._voiced:
            return
        if now - self._started_at < MIN_SPEECH_SEC:
            return
        if now - self._silent_since >= silence_sec:
            logger.info("静音 %.1f 秒，自动结束本轮回答", silence_sec)
            self._auto_stop()

    def _auto_stop(self) -> None:
        """从采集回调线程触发停止，必须避免在回调里做耗时操作。"""
        threading.Thread(target=self.stop, kwargs={"submit": True},
                         name="asr-autostop", daemon=True).start()

    # ------------------------------------------------------------------
    # 回调转发
    # ------------------------------------------------------------------
    def _on_text(self, text: str, final: bool) -> None:
        if final:
            return   # final 统一在 worker 收尾时用 client.finish() 的全文
        if not self._stopping:
            self._emit(self.on_partial, text)

    def _on_client_error(self, error: ASRError) -> None:
        # 服务端中途报错，把原因交给界面，用户可退回去打字
        if not self._stopping:
            self._emit_error(error.describe())

    def _fail(self, message: str) -> None:
        with self._lock:
            self._active = False
            self._stopping = False
        try:
            self._recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        self._emit_state("idle")
        self._emit_error(message)

    # ------------------------------------------------------------------
    def _save_audio(self) -> None:
        """把这段回答的 PCM 落成 wav，供复盘回放。"""
        if not config.get("interview.save_audio", True):
            return
        if len(self._audio) < TARGET_RATE:
            return
        try:
            target_dir = self.audio_dir
            if target_dir is None:
                return
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"answer-{int(time.time() * 1000)}.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(TARGET_RATE)
                handle.writeframes(bytes(self._audio))
            self.last_audio_path = path
            logger.info("回答录音已保存：%s（%.1f 秒）",
                        path.name, len(self._audio) / 2 / TARGET_RATE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存回答录音失败：%s", exc)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _emit(self, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001
            pass

    def _emit_state(self, state: str) -> None:
        self._emit(self.on_state, state)

    def _emit_final(self, text: str) -> None:
        self._emit(self.on_final, text)

    def _emit_error(self, message: str) -> None:
        self._emit(self.on_error, message)
