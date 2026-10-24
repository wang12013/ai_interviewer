"""播报器：把一段文字变成连续播放的音频。

一段话往往有好几句，如果串行「合成一句 -> 播完 -> 合成下一句」，
句与句之间会有明显的停顿。所以这里分两条流水线：

    分句 -> 合成线程池（可并行 3 句）-> 按 seq 排序 -> 播放缓冲 -> 声卡

合成比播放快得多（一句话 40 字约 8 秒音频，合成只要 0.5 秒），
所以并行合成能把首字延迟压到一次请求的时间，后面几句早已备好。

打断（用户点「跳过播报」或直接开口）时，generation 自增，
所有在途的合成结果到达后会发现代号过期而被丢弃，不会「打断后声音又冒出来」。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .player import AudioError, PCMPlayer
from .splitter import SentenceSplitter
from .tts_client import TTSEngine, TTSError

logger = logging.getLogger("tts")

# 同时合成的句子数。3 句够覆盖一次发言的大多数情况，再多会触发接口限流
MAX_PARALLEL = 3


class Speaker:
    """把文字播出来。所有方法都可从任意线程调用。"""

    def __init__(self, *, engine: TTSEngine | None = None,
                 player_factory: Callable[[Callable[[bool], None]], object] | None = None) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

        self._engine = engine or TTSEngine()
        self._player_factory = player_factory or (
            lambda on_state: PCMPlayer(on_state=on_state)
        )
        self._player = None
        self._audio_broken = False
        self._pool: ThreadPoolExecutor | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        # 待合成/待播放的队列状态
        self._results: dict[int, bytes] = {}
        self._rates: dict[int, int] = {}
        self._next_seq = 0
        self._submit_seq = 0
        self._inflight = 0
        self._generation = 0
        self._total = 0
        # 空闲状态只广播一次，避免心跳每 50ms 都往界面推一遍
        self._idle_announced = True

        self._splitter = SentenceSplitter()
        self._streaming = False

        self.on_state: Callable[[str], None] | None = None   # speaking / idle
        self.on_error: Callable[[str], None] | None = None
        self.on_sentence: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def degraded(self) -> bool:
        return self._engine.degraded

    @property
    def degraded_reason(self) -> str:
        return self._engine.degraded_reason

    @property
    def voice_notice(self) -> str:
        """当前音色不可用、被自动换成兜底音色时的提示（否则为空）。"""
        return self._engine.voice_notice

    @property
    def voice(self) -> str:
        return self._engine.voice()

    def reset_voice(self) -> None:
        self._engine.reset_voice()

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            busy = self._inflight > 0 or self._results or (
                self._player.is_playing if self._player else False
            )
        return bool(busy)

    def ready(self) -> tuple[bool, str]:
        return self._engine.client.ready()

    def set_protocol(self, protocol: str) -> None:
        self._engine.set_protocol(protocol)

    # ------------------------------------------------------------------
    # 播报
    # ------------------------------------------------------------------
    def say(self, text: str, *, blocking: bool = False, timeout: float = 90.0) -> None:
        """播报一段完整文本。blocking=True 时等到播完再返回（试听用）。"""
        if not (text or "").strip():
            return
        self._ensure_started()
        self.stop(silent=True)  # 新的一句进来，先掐掉上一句的残留

        with self._cond:
            self._generation += 1
            generation = self._generation
            self._reset_locked()
            self._streaming = False
            self._total = 0

        sentences = self._splitter.feed(text)
        sentences.extend(self._splitter.flush())
        for sentence in sentences:
            self._submit(sentence, generation)

        with self._cond:
            self._total = self._submit_seq
            self._cond.notify_all()

        if blocking:
            self.wait(timeout)

    def stream_begin(self) -> None:
        """开始流式播报（大模型逐字输出时用）。"""
        self._ensure_started()
        self.stop()
        with self._cond:
            self._generation += 1
            self._reset_locked()
            self._streaming = True
            self._total = 0
        self._splitter.reset()

    def stream_feed(self, delta: str) -> None:
        """喂入一段新文字，凑成句的立刻送合成。"""
        with self._lock:
            if not self._streaming:
                return
            generation = self._generation
        for sentence in self._splitter.feed(delta):
            self._submit(sentence, generation)

    def stream_end(self) -> None:
        """文字流结束，把最后半句也合成掉。"""
        with self._lock:
            if not self._streaming:
                return
            generation = self._generation
            self._streaming = False
        for sentence in self._splitter.flush():
            self._submit(sentence, generation)
        with self._cond:
            self._total = self._submit_seq
            self._cond.notify_all()

    def stop(self, *, silent: bool = False) -> None:
        """打断。正在合成的结果会被丢弃，正在播的声音立刻停。

        silent=True 表示后面紧接着要播新内容，不必先把界面切成空闲，
        否则「说完一句马上接下一句」时状态灯会闪一下。
        """
        with self._cond:
            self._generation += 1
            self._reset_locked()
            self._streaming = False
            self._cond.notify_all()
        self._splitter.reset()
        if self._player is not None:
            self._player.clear()
        if not silent:
            self._emit_state("idle")

    def wait(self, timeout: float = 90.0) -> bool:
        """等本轮播报结束，返回是否正常播完。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                pending = self._inflight or self._results
                if not pending and not (self._player and self._player.is_playing):
                    return True
            time.sleep(0.03)
        return False

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._running = False
            pool, self._pool = self._pool, None
            player, self._player = self._player, None
            thread, self._thread = self._thread, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if player is not None:
            player.close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _ensure_started(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL,
                                            thread_name_prefix="tts")
            # 音频设备在播放线程里打开：声卡初始化可能要几十毫秒，
            # 放在调用线程（界面线程）会卡住界面。
            self._thread = threading.Thread(target=self._play_loop, name="tts-play",
                                            daemon=True)
            self._thread.start()

    def _reset_locked(self) -> None:
        self._results.clear()
        self._rates.clear()
        self._next_seq = 0
        self._submit_seq = 0
        self._inflight = 0
        self._total = 0
        self._idle_announced = True

    def _submit(self, sentence: str, generation: int) -> None:
        sentence = (sentence or "").strip()
        if not sentence:
            return
        with self._cond:
            seq = self._submit_seq
            self._submit_seq += 1
            self._inflight += 1
            self._idle_announced = False
            pool = self._pool
        if pool is None:
            return
        pool.submit(self._synthesize, sentence, seq, generation)
        self._emit_state("speaking")

    def _synthesize(self, sentence: str, seq: int, generation: int) -> None:
        try:
            result = self._engine.synthesize(sentence)
        except TTSError as exc:
            logger.warning("第 %s 句合成失败：%s", seq, exc.describe())
            self._finish_one(seq, generation, None, None)
            self._report_error(exc.describe())
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("合成异常")
            self._finish_one(seq, generation, None, None)
            self._report_error(f"语音合成异常：{exc}")
            return
        self._finish_one(seq, generation, result.pcm, result.sample_rate)
        if self.on_sentence:
            try:
                self.on_sentence(sentence)
            except Exception:  # noqa: BLE001
                pass

    def _finish_one(self, seq: int, generation: int, pcm: bytes | None,
                    rate: int | None) -> None:
        with self._cond:
            self._inflight = max(0, self._inflight - 1)
            logger.debug("合成返回 seq=%s gen=%s/%s bytes=%s", seq, generation,
                         self._generation, len(pcm or b""))
            if generation != self._generation:
                logger.debug("seq=%s 属于已打断的轮次，丢弃", seq)
                return  # 已被打断，结果作废
            if pcm:
                self._results[seq] = pcm
                self._rates[seq] = int(rate or 24000)
            else:
                # 失败的句子也要占位，否则后面的句子永远等不到它
                self._results[seq] = b""
                self._rates[seq] = 24000
            self._cond.notify_all()

    def _play_loop(self) -> None:
        """按 seq 顺序把合成结果喂进播放缓冲。"""
        logger.debug("播放线程启动")
        # 先备好播放器，之后的消费才有地方写数据
        try:
            player = self._player_factory(self._on_player_state)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._audio_broken = True
                self._player = None
            self._report_error(f"无法打开音频输出设备：{exc}")
            self._emit_state("idle")
            player = None
        else:
            with self._lock:
                self._player = player

        while True:
            item = None
            with self._cond:
                if not self._running:
                    return
                if self._next_seq in self._results:
                    item = self._take_locked()
                else:
                    self._cond.wait(0.05)
                    if self._next_seq in self._results:
                        item = self._take_locked()
            if item is None:
                # 没数据可放时借这个心跳检查是否该切回空闲，
                # 播放器本身不会主动汇报「播完了」
                self._maybe_idle()
                continue

            pcm, rate, seq_done = item
            logger.debug("播放线程取出 seq=%s bytes=%s", seq_done, len(pcm))
            if pcm and self._player is not None:
                try:
                    self._player.feed(pcm, rate)
                except AudioError as exc:
                    # 没有输出设备（静音、远程桌面、耳机被拔）不该让整场面试挂掉，
                    # 报一次错后关掉播放，文字照常走。
                    if not self._audio_broken:
                        self._audio_broken = True
                        self._report_error(str(exc))
                    with self._lock:
                        self._player = None
                    self._emit_state("idle")

    def _take_locked(self) -> tuple[bytes, int, int]:
        pcm = self._results.pop(self._next_seq)
        rate = self._rates.pop(self._next_seq, 24000)
        seq = self._next_seq
        self._next_seq += 1
        return pcm, rate, seq

    def _maybe_idle(self) -> None:
        """没有在途任务、没有待播数据、也没在出声时，宣布空闲。"""
        with self._lock:
            if self._idle_announced:
                return
            if self._inflight or self._results:
                return
            if self._total and self._next_seq < self._total:
                return
            player = self._player
        if player is not None and player.is_playing:
            return
        with self._lock:
            if self._idle_announced:
                return
            self._idle_announced = True
        self._emit_state("idle")

    def _on_player_state(self, playing: bool) -> None:
        if not playing:
            with self._lock:
                if self._inflight or self._results:
                    return
            self._emit_state("idle")

    def _emit_state(self, state: str) -> None:
        callback = self.on_state
        if callback is None:
            return
        try:
            callback(state)
        except Exception:  # noqa: BLE001
            pass

    def _report_error(self, message: str) -> None:
        callback = self.on_error
        if callback is None:
            logger.warning("播报失败：%s", message)
            return
        try:
            callback(message)
        except Exception:  # noqa: BLE001
            pass


class SpeakerStub:
    """不发声的替身，用于测试和无音频环境。"""

    degraded = False
    degraded_reason = ""
    voice_notice = ""
    voice = ""
    is_speaking = False

    def ready(self) -> tuple[bool, str]:
        return True, ""

    def set_protocol(self, protocol: str) -> None:
        pass

    def reset_voice(self) -> None:
        pass

    def say(self, text: str, *, blocking: bool = False, timeout: float = 90.0) -> None:
        self.is_speaking = True
        self.is_speaking = False

    def stream_begin(self) -> None:
        pass

    def stream_feed(self, delta: str) -> None:
        pass

    def stream_end(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wait(self, timeout: float = 90.0) -> bool:
        return True

    def close(self) -> None:
        pass
