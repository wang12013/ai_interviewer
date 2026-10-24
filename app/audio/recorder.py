"""麦克风采集。

实时转写对音频格式的要求很死：16kHz / 16bit / 单声道 / 每 40ms 一帧 1280 字节。
但声卡往往只肯按自己的默认采样率开流（这台机器是 44.1kHz），所以这里做了两件事：

* 优先按 16000 直接开流；PortAudio 支持的话就不用任何转换
* 开不了就退回设备默认采样率，自己线性插值重采样到 16k

重采样必须**跨块连续**：如果每块独立插值，块与块之间会丢一个样本，
一分钟下来累积几十毫秒的错位，识别结果就会莫名丢字。所以位置指针和
上一个样本都通过 ``_Resampler`` 保存在流上。

回调里只做内存拷贝和入队，绝不做 IO——回调超时会直接导致爆音和丢帧。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger("asr")

TARGET_RATE = 16000
FRAME_MS = 40
# 40ms @ 16k 16bit 单声道 = 16000 * 0.04 * 2
FRAME_BYTES = int(TARGET_RATE * FRAME_MS / 1000) * 2


class AudioInputError(Exception):
    """麦克风不可用。"""


class _Resampler:
    """把任意采样率的 int16 单声道块连续地重采样到目标采样率。"""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.step = src_rate / float(dst_rate)
        # 从 1.0 起步：x[0] 是上一块的尾巴（首块是补的零），
        # 真正的第一个可插值样本在 x[1]，从这里开始才不会多吐一个样本
        self._pos = 1.0
        self._prev = np.zeros(1, dtype=np.float64)

    def process(self, block: np.ndarray) -> np.ndarray:
        x = np.concatenate((self._prev, block.astype(np.float64)))
        total = len(x)
        if total < 2:
            self._prev = x
            return np.empty(0, dtype=np.int16)

        count = int(np.floor((total - 1 - self._pos) / self.step)) + 1
        if count <= 0:
            self._pos -= total - 1
            self._prev = x[-1:]
            return np.empty(0, dtype=np.int16)

        idx = self._pos + np.arange(count) * self.step
        out = np.interp(idx, np.arange(total), x)
        # 位置换算到「以 x 末尾为原点」，下一块拼上来就能接上
        self._pos += count * self.step - (total - 1)
        self._prev = x[-1:]
        return np.clip(out, -32768, 32767).astype(np.int16)


class MicrophoneRecorder:
    """按帧回调 PCM 的录音器。"""

    def __init__(self, *, device: int | str | None = None,
                 frame_ms: int = FRAME_MS) -> None:
        self._device = device
        self._frame_bytes = int(TARGET_RATE * frame_ms / 1000) * 2
        self._stream: sd.InputStream | None = None
        self._running = False
        self._buffer = bytearray()
        self._resampler: _Resampler | None = None
        self._on_frame: Callable[[bytes], None] | None = None
        self._lock = threading.Lock()
        self.dropped = 0
        self.source_rate = TARGET_RATE

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def start(self, on_frame: Callable[[bytes], None]) -> None:
        """打开输入流并开始回调。打不开会抛 AudioInputError。"""
        if self._running:
            return
        self._on_frame = on_frame
        self._buffer.clear()
        self.dropped = 0

        stream = self._open(TARGET_RATE)
        if stream is None:
            # 声卡不认 16k，退回默认采样率自己转
            default_rate = self._default_rate()
            stream = self._open(default_rate)
            if stream is None:
                raise AudioInputError(
                    "无法打开麦克风，请检查系统录音权限与输入设备"
                )
            self.source_rate = default_rate
            self._resampler = _Resampler(default_rate, TARGET_RATE)
            logger.info("麦克风以 %s Hz 打开，将重采样到 %s Hz",
                        default_rate, TARGET_RATE)
        else:
            self.source_rate = TARGET_RATE
            self._resampler = None

        self._stream = stream
        self._running = True
        try:
            stream.start()
        except Exception as exc:  # noqa: BLE001
            self._running = False
            self._stream = None
            raise AudioInputError(f"麦克风启动失败：{exc}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        self._running = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭输入流时出错：%s", exc)
        with self._lock:
            self._buffer.clear()

    # ------------------------------------------------------------------
    def _default_rate(self) -> int:
        try:
            info = sd.query_devices(self._device, "input")
            return int(info["default_samplerate"] or 48000)
        except Exception:  # noqa: BLE001
            return 48000

    def _open(self, rate: int) -> sd.InputStream | None:
        try:
            # blocksize 取 20ms：既能让回调足够频繁，又不至于碎到拖累 CPU
            block = max(160, int(rate * 0.02))
            stream = sd.InputStream(
                samplerate=rate, channels=1, dtype="int16",
                blocksize=block, device=self._device, callback=self._callback,
            )
            # 必须在 start() 之前打开，才能确认设备真的支持这个采样率
            stream.start()
            stream.stop()
            return stream
        except Exception as exc:  # noqa: BLE001
            logger.debug("以 %s Hz 打开输入设备失败：%s", rate, exc)
            return None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            logger.debug("输入流状态：%s", status)
        if not self._running:
            return

        # indata 是 (frames, 1) 的 int16，展平后按目标采样率处理
        block = np.asarray(indata, dtype=np.int16).reshape(-1)
        if self._resampler is not None:
            block = self._resampler.process(block)
        if block.size == 0:
            return

        with self._lock:
            self._buffer.extend(block.astype("<i2", copy=False).tobytes())
            ready: list[bytes] = []
            while len(self._buffer) >= self._frame_bytes:
                ready.append(bytes(self._buffer[:self._frame_bytes]))
                del self._buffer[:self._frame_bytes]

        callback = self._on_frame
        if callback is None:
            return
        for frame in ready:
            try:
                callback(frame)
            except Exception:  # noqa: BLE001
                # 回调里绝不能往外抛，否则 PortAudio 会中止整条流
                self.dropped += 1


def list_input_devices() -> list[tuple[int, str]]:
    """列出可用的录音设备，设置页做选择用。"""
    result: list[tuple[int, str]] = []
    try:
        for index, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels") or 0) > 0:
                result.append((index, str(info.get("name") or f"设备 {index}")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("枚举录音设备失败：%s", exc)
    return result
