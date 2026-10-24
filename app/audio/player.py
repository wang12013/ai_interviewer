"""PCM 播放器。

用 sounddevice 的 callback 模式维护一条常开的输出流，外部随时往里追加 PCM。
这样做的原因：

* 每句话各开一次流会在句间产生可听见的爆音和空档，callback + 缓冲区是连续的
* 打断要足够快：清空缓冲区下一个回调周期就静音了（约 20ms），不用等 write 返回
* 采样率变化时重开流，兼容超拟人（24k）与标准合成（16k）混用

回调里只做内存拷贝，绝不做 IO 或加锁很久的操作，否则会爆音。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger("tts")

# 回调块大小：按 24k 算约 26ms，够小以保证打断及时，又不会太碎
BLOCK_FRAMES = 640


class AudioError(Exception):
    """输出设备不可用。"""


class PCMPlayer:
    """16bit 单声道 PCM 的连续播放器。"""

    def __init__(self, sample_rate: int = 24000,
                 on_state: Callable[[bool], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._sample_rate = sample_rate
        self._stream: sd.OutputStream | None = None
        self._fed_frames = 0
        self._played_frames = 0
        self._closed = False
        self._error: str = ""
        self.on_state = on_state

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def error(self) -> str:
        return self._error

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._played_frames < self._fed_frames

    def remaining_seconds(self) -> float:
        with self._lock:
            pending = len(self._buffer) // 2
        return pending / self._sample_rate if self._sample_rate else 0.0

    def feed(self, pcm: bytes, sample_rate: int | None = None) -> None:
        """追加一段 PCM，立刻可以听到（如果缓冲为空）。"""
        if not pcm or self._closed:
            return
        rate = int(sample_rate or self._sample_rate)
        if rate != self._sample_rate:
            self._switch_rate(rate)
        self._ensure_stream()
        with self._lock:
            self._buffer.extend(pcm)
            self._fed_frames += len(pcm) // 2
        self._notify(True)

    def clear(self) -> None:
        """打断：丢掉所有待播数据。"""
        with self._lock:
            self._buffer.clear()
            self._played_frames = self._fed_frames
        self._notify(False)

    def close(self) -> None:
        self._closed = True
        with self._lock:
            self._buffer.clear()
        self._teardown()

    def drain(self, timeout: float = 0.5) -> None:
        """等缓冲清空（用于测试和优雅退出）。"""
        import time

        deadline = time.monotonic() + timeout
        while self.is_playing and time.monotonic() < deadline:
            time.sleep(0.02)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _switch_rate(self, rate: int) -> None:
        self._teardown()
        self._sample_rate = rate
        logger.info("播放采样率切换为 %s", rate)

    def _ensure_stream(self) -> None:
        if self._stream is not None or self._closed:
            return
        try:
            stream = sd.OutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_FRAMES,
                callback=self._callback,
                finished_callback=lambda: None,
            )
            stream.start()
            self._stream = stream
            self._error = ""
        except Exception as exc:  # noqa: BLE001
            self._error = f"无法打开音频输出设备：{exc}"
            logger.error(self._error)
            raise AudioError(self._error) from exc

    def _teardown(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _callback(self, outdata, frames, time_info, status) -> None:
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buffer[:need])
            del self._buffer[:need]
        if chunk:
            samples = np.frombuffer(chunk, dtype=np.int16)
            outdata[: len(samples), 0] = samples
            if len(samples) < frames:
                outdata[len(samples):, 0] = 0
            with self._lock:
                self._played_frames += len(samples)
        else:
            outdata.fill(0)

    def _notify(self, playing: bool) -> None:
        callback = self.on_state
        if callback is None:
            return
        try:
            callback(playing)
        except Exception:  # noqa: BLE001
            pass
