"""讯飞实时语音转写（RTASR）客户端。

和 TTS 那套签名完全不同，别混用：

| | RTASR | TTS |
|---|---|---|
| 签名 | signa = Base64(HmacSHA1(MD5(appid+ts), apiKey)) | HMAC-SHA256 over host/date/request-line |
| 位置 | URL query（appid / ts / signa） | Authorization 头（base64 后再放进 query） |
| 密钥 | 用 **APIKey** | 用 **APISecret** |
| 数据 | 每 40ms 一帧 1280 字节二进制 PCM | JSON 里塞 base64 文本 |

返回结构是二段 JSON：外层 ``{"action":"result","data":"<json字符串>"}``，
``data`` 还得再解析一次。句子按 ``seg_id`` 分组，``st.type`` 为 ``0`` 表示这句定稿、
``1`` 表示中间结果（会把整句重发，不是增量）。

服务端还有一条硬约束：**15 秒内没有音频上传就会主动断开**。所以录音停止后
必须尽快发结束标记，不能晾着连接。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import websocket

logger = logging.getLogger("asr")

RTASR_ENDPOINT = "wss://rtasr.xfyun.cn/v1/ws"

# 10105 无权限 / 10110 无授权 / 10800 超并发 / 10106 参数错 / 10107 非法参数
PERMISSION_CODES = {"10105", "10110", "10800"}
PARAM_CODES = {"10106", "10107"}

END_MARKER = b'{"end": true}'


class ASRError(Exception):
    """转写失败。code 为讯飞返回码，本地故障时为 None。"""

    def __init__(self, message: str, *, code: str | None = None,
                 stage: str = "service") -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage

    @property
    def is_permission(self) -> bool:
        return self.code in PERMISSION_CODES

    @property
    def is_param(self) -> bool:
        return self.code in PARAM_CODES

    def describe(self) -> str:
        if self.stage == "network":
            return f"转写连接失败：{self}"
        if self.stage == "audio":
            return f"麦克风不可用：{self}"
        if self.is_permission:
            return f"未开通实时语音转写（{self.code}）：{self}"
        if self.code:
            return f"转写失败（{self.code}）：{self}"
        return str(self)


def signa_for(app_id: str, api_key: str, ts: int) -> str:
    """signa = Base64(HmacSHA1(MD5(appid + ts), apiKey))。"""
    base = f"{app_id}{ts}"
    digest = hashlib.md5(base.encode("utf-8")).hexdigest()
    mac = hmac.new(api_key.encode("utf-8"), digest.encode("utf-8"),
                   hashlib.sha1).digest()
    return base64.b64encode(mac).decode("utf-8")


def signed_url(app_id: str, api_key: str, endpoint: str = RTASR_ENDPOINT,
               *, lang: str = "cn", ts: int | None = None) -> str:
    ts = int(ts if ts is not None else time.time())
    signa = signa_for(app_id, api_key, ts)
    # signa 里带 + / = 三个特殊字符，必须完整转义，否则服务端算出的是另一个串
    url = f"{endpoint}?appid={quote(app_id)}&ts={ts}&signa={quote(signa)}"
    if lang:
        url += f"&lang={quote(lang)}"
    return url


@dataclass
class TranscriptState:
    """累积的转写文本，按 seg_id 归并。"""

    sentences: dict[int, str] = field(default_factory=dict)
    finals: set[int] = field(default_factory=set)

    def apply(self, seg: int, text: str, final: bool) -> str:
        self.sentences[seg] = text
        if final:
            self.finals.add(seg)
        return self.text()

    def text(self) -> str:
        # seg_id 理论上有序到达，但网络乱序时按数值排一次更保险
        keys = sorted(self.sentences, key=lambda k: (isinstance(k, str), k))
        return "".join(self.sentences[k] for k in keys).strip()

    def reset(self) -> None:
        self.sentences.clear()
        self.finals.clear()


class RTASRClient:
    """一条转写会话。收到文本就回调，不关心音频从哪来。"""

    def __init__(self, on_text=None, on_error=None) -> None:
        self.on_text = on_text
        self.on_error = on_error
        self.state = TranscriptState()
        self.last_error: ASRError | None = None
        self._ws: websocket.WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._sent_frames = 0
        self._last_final_text = ""

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def connect(self, app_id: str, api_key: str, *, endpoint: str = RTASR_ENDPOINT,
                lang: str = "cn", timeout: float = 8.0) -> None:
        if not app_id or not api_key:
            raise ASRError("缺少讯飞 AppID 或 APIKey", stage="config")
        url = signed_url(app_id, api_key, endpoint, lang=lang)
        try:
            self._ws = websocket.create_connection(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"{type(exc).__name__}: {exc}", stage="network") from exc

        self._thread = threading.Thread(target=self._recv_loop, name="asr-recv",
                                        daemon=True)
        self._thread.start()

        # 等握手结果：服务端回 started 才能开始推音频
        if not self._started.wait(timeout):
            self.abort()
            raise ASRError("握手超时，请检查网络或密钥", stage="network")
        if self.last_error is not None:
            error = self.last_error
            self.abort()
            raise error

    def _recv_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as exc:  # noqa: BLE001
                    if not self._stop.is_set():
                        logger.debug("转写连接中断：%s", exc)
                    break
                if not raw:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "ignore")
                self._handle_message(raw)
        finally:
            self._closed.set()

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("转写返回不是 JSON：%r", raw[:120])
            return

        action = str(message.get("action") or "")
        if action == "started":
            self._started.set()
            return
        if action == "error":
            code = str(message.get("code") or "")
            error = ASRError(str(message.get("desc") or "转写服务返回错误"),
                             code=code or None)
            self.last_error = error
            logger.warning("转写服务报错 code=%s desc=%s", code, error)
            self._started.set()   # 让 connect() 及时返回错误而不是干等
            self._report(error)
            return
        if action != "result":
            return

        payload = message.get("data")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        if not isinstance(payload, dict):
            return

        seg, text, final = self._parse(payload)
        if seg is None or not text:
            return
        with self._lock:
            full = self.state.apply(seg, text, final)
            if final:
                self._last_final_text = full
        self._emit(full, final)

    @staticmethod
    def _parse(payload: dict) -> tuple[int | None, str, bool]:
        """从 cn.st.rt[].ws[].cw[0].w 里拼出这句文本。"""
        cn = payload.get("cn")
        if not isinstance(cn, dict):
            # 翻译模式下 data 是扁平的，这里只认转写结构
            return None, "", False
        st = cn.get("st") or {}
        seg_raw = payload.get("seg_id", payload.get("segId", 0))
        try:
            seg = int(seg_raw)
        except (TypeError, ValueError):
            seg = 0
        # type: 0 最终结果 / 1 中间结果
        final = str(st.get("type", "1")) == "0"

        parts: list[str] = []
        for item in st.get("rt") or []:
            for word in item.get("ws") or []:
                for candidate in word.get("cw") or []:
                    piece = candidate.get("w")
                    if piece:
                        parts.append(str(piece))
        return seg, "".join(parts), final

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send_frame(self, frame: bytes) -> None:
        ws = self._ws
        if ws is None or not frame:
            return
        try:
            ws.send_binary(frame)
            self._sent_frames += 1
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"音频上传失败：{exc}", stage="network") from exc

    def finish(self, *, wait: float = 4.0) -> str:
        """发送结束标记并等服务端把剩余结果推完。"""
        with self._lock:
            self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.send_binary(END_MARKER)
            except Exception as exc:  # noqa: BLE001
                logger.debug("发送结束标记失败：%s", exc)

        thread = self._thread
        if thread is not None:
            thread.join(wait)
        # 还没收到任何定稿结果时，退回到已累积的最新全文
        text = self._last_final_text or self.state.text()
        self.close()
        return text

    def abort(self) -> None:
        self._stop.set()
        self.close()

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(1.0)
        self._thread = None

    # ------------------------------------------------------------------
    @property
    def sent_frames(self) -> int:
        return self._sent_frames

    def _emit(self, text: str, final: bool) -> None:
        callback = self.on_text
        if callback is None:
            return
        try:
            callback(text, final)
        except Exception:  # noqa: BLE001
            pass

    def _report(self, error: ASRError) -> None:
        callback = self.on_error
        if callback is None:
            return
        try:
            callback(error)
        except Exception:  # noqa: BLE001
            pass
