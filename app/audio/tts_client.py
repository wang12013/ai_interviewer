"""讯飞语音合成客户端。

讯飞的「超拟人合成」和「在线语音合成」是两套完全不同的协议：

| | 超拟人（x4/x5/x6 系列发音人） | 标准合成 |
|---|---|---|
| 地址 | cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6 | tts-api.xfyun.cn/v2/tts |
| 请求体 | header / parameter / payload 三段 | common / business / data 三段 |
| 返回体 | header.code + payload.audio | 顶层 code + data.audio |
| 权限 | 需要单独开通「超拟人语音合成」，且**按发音人逐个授权** | 创建应用后默认有免费额度 |

两者的签名算法相同（hmac-sha256 + host/date/request-line）。

两个容易踩的坑：

1. ``parameter.oral``（口语化配置）只有 x4 系列认。x5/x6 音色带上它会被
   参数校验拦下，所以这里按音色前缀决定发不发。
2. 音频编码：主站文档推荐 `raw`（PCM），而超拟人 V3 文档的参数表里只有
   lame/speex/opus。不赌单一编码，按 `raw` -> `lame`（本地解码）-> 标准合成
   逐级降级。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import quote, urlparse

import websocket

from ..config import (
    FALLBACK_STANDARD_VCN,
    FALLBACK_SUPER_VCN,
    ORAL_VCN_PREFIXES,
    VOICE_PRESETS,
    config,
    is_super_vcn,
    voice_short,
)

logger = logging.getLogger("tts")

SUPER_ENDPOINT = "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6"
STANDARD_ENDPOINT = "wss://tts-api.xfyun.cn/v2/tts"

# 超拟人只支持这三种采样率，24000 听感最好
SUPER_RATES = (16000, 8000, 24000)
STANDARD_RATES = (16000, 8000)

# 11200 功能未授权 / 11201 当日限额 / 10010 未授权许可
PERMISSION_CODES = {11200, 11201, 10010, 100001, 100002, 100003, 100004}
# 10139 参数错误 / 10009 输入非法 / 10163 参数校验失败
PARAM_CODES = {10139, 10009, 10160, 10161, 10163}


class TTSError(Exception):
    """合成失败。code 为讯飞返回码，网络层失败时为 None。"""

    def __init__(self, message: str, *, code: int | None = None,
                 protocol: str = "", stage: str = "service") -> None:
        super().__init__(message)
        self.code = code
        self.protocol = protocol
        self.stage = stage

    @property
    def is_permission(self) -> bool:
        return self.code in PERMISSION_CODES

    @property
    def is_param(self) -> bool:
        return self.code in PARAM_CODES

    def describe(self) -> str:
        if self.stage == "network":
            return f"网络不通：{self}"
        if self.is_permission:
            return f"未开通权限（{self.code}）：{self}"
        if self.code is not None:
            return f"合成失败（{self.code}）：{self}"
        return str(self)


@dataclass
class TTSResult:
    """一段合成结果。pcm 一律是 16bit 单声道小端裸数据。"""

    pcm: bytes
    sample_rate: int
    protocol: str
    encoding: str
    cost_ms: int = 0

    @property
    def frames(self) -> int:
        return len(self.pcm) // 2

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def signed_url(endpoint: str, api_key: str, api_secret: str) -> str:
    """按讯飞 ws 鉴权文档生成带签名的连接地址。"""
    parsed = urlparse(endpoint)
    host = parsed.netloc
    path = parsed.path
    # 必须是 RFC 1123 的 GMT 格式，和服务端校验的时间戳格式一致
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    digest = hmac.new(api_secret.encode("utf-8"), origin.encode("utf-8"),
                      hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("utf-8")
    auth_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(auth_origin.encode("utf-8")).decode("utf-8")
    return f"{endpoint}?authorization={authorization}&date={quote(date)}&host={host}"


class XfyunTTS:
    """一次调用合成一句话。线程安全，可在多个合成线程里共用。"""

    def __init__(self) -> None:
        self._mp3_ready: bool | None = None

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def settings(self) -> dict:
        return {
            "app_id": str(config.get("xfyun.app_id") or "").strip(),
            "api_key": str(config.get("xfyun.api_key") or "").strip(),
            "api_secret": str(config.get("xfyun.api_secret") or "").strip(),
            "vcn": str(config.get("xfyun.tts.vcn") or FALLBACK_SUPER_VCN),
            "speed": _clamp(config.get("xfyun.tts.speed", 50)),
            "volume": _clamp(config.get("xfyun.tts.volume", 60)),
            "pitch": _clamp(config.get("xfyun.tts.pitch", 50)),
            "endpoint_super": str(config.get("xfyun.tts.endpoint_super") or SUPER_ENDPOINT),
            "endpoint_std": str(config.get("xfyun.tts.endpoint_std") or STANDARD_ENDPOINT),
        }

    def ready(self) -> tuple[bool, str]:
        cfg = self.settings()
        missing = [
            name for key, name in (
                ("app_id", "AppID"), ("api_key", "APIKey"), ("api_secret", "APISecret"),
            ) if not cfg[key]
        ]
        if missing:
            return False, "缺少讯飞" + "、".join(missing)
        return True, ""

    def synthesize(self, text: str, *, protocol: str = "super",
                   encoding: str = "raw", timeout: float = 12.0,
                   vcn: str | None = None) -> TTSResult:
        """合成一段文字。

        ``vcn`` 为空时用配置里的音色；显式传入则临时覆盖，
        供「音色探测」和「音色自动兜底」使用，不必来回改全局配置。
        """
        ok, reason = self.ready()
        if not ok:
            raise TTSError(reason, stage="config")
        text = (text or "").strip()
        if not text:
            raise TTSError("待合成文本为空", stage="config")

        voice = (vcn or self.settings()["vcn"] or "").strip()
        if protocol == "super":
            return self._synthesize_super(text, encoding=encoding, timeout=timeout,
                                          vcn=voice)
        return self._synthesize_standard(text, timeout=timeout, vcn=voice)

    # ------------------------------------------------------------------
    # 超拟人
    # ------------------------------------------------------------------
    def _synthesize_super(self, text: str, *, encoding: str, timeout: float,
                          vcn: str) -> TTSResult:
        cfg = self.settings()
        rate = 24000
        tts_params = {
            "vcn": vcn,
            "speed": cfg["speed"],
            "volume": cfg["volume"],
            "pitch": cfg["pitch"],
            "bgs": 0,
            "reg": 0,
            "rdn": 0,
            "rhy": 0,
            "audio": {
                "encoding": encoding,
                "sample_rate": rate,
                "channels": 1,
                "bit_depth": 16,
                "frame_size": 0,
            },
        }
        parameter: dict = {"tts": tts_params}
        # 口语化配置是 x4 系列的专属参数，x5/x6 音色带上会触发参数校验失败
        if vcn.startswith(ORAL_VCN_PREFIXES):
            parameter["oral"] = {"oral_level": "mid"}
        else:
            logger.debug("音色 %s 不是 x4 系列，跳过 oral 参数", vcn)

        body = {
            "header": {"app_id": cfg["app_id"], "status": 2},
            "parameter": parameter,
            "payload": {
                "text": {
                    "encoding": "utf8",
                    "compress": "raw",
                    "format": "plain",
                    "status": 2,
                    "seq": 0,
                    "text": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
                },
            },
        }

        started = time.monotonic()
        chunks: list[tuple[int, bytes]] = []
        sample_rate = rate
        for message in self._stream(cfg["endpoint_super"], body, protocol="super",
                                     timeout=timeout):
            header = message.get("header") or {}
            code = header.get("code", 0)
            if code not in (0, None):
                raise TTSError(str(header.get("message") or "合成失败"),
                               code=int(code), protocol="super")
            audio = (message.get("payload") or {}).get("audio") or {}
            sample_rate = int(audio.get("sample_rate") or sample_rate)
            data = audio.get("audio")
            if data:
                chunks.append((int(audio.get("seq") or 0), _b64decode(data)))
            if int(audio.get("status") or 0) == 2 or int(header.get("status") or 0) == 2:
                break

        pcm = b"".join(piece for _, piece in sorted(chunks, key=lambda item: item[0]))
        if not pcm:
            raise TTSError("接口没有返回音频数据", protocol="super")

        cost = int((time.monotonic() - started) * 1000)
        if encoding == "lame":
            pcm, sample_rate = self._decode_mp3(pcm, sample_rate)
        return TTSResult(pcm=pcm, sample_rate=sample_rate, protocol="super",
                         encoding=encoding, cost_ms=cost)

    # ------------------------------------------------------------------
    # 标准合成
    # ------------------------------------------------------------------
    def _synthesize_standard(self, text: str, *, timeout: float, vcn: str) -> TTSResult:
        cfg = self.settings()
        rate = 16000
        # 超拟人音色（x4/x5/x6）在标准接口里不存在，硬塞会被判参数错误
        std_vcn = vcn if not is_super_vcn(vcn) else FALLBACK_STANDARD_VCN
        if std_vcn != vcn:
            logger.info("标准合成不支持音色 %s，改用 %s", vcn, std_vcn)
        body = {
            "common": {"app_id": cfg["app_id"]},
            "business": {
                "aue": "raw",
                "auf": f"audio/L16;rate={rate}",
                "vcn": std_vcn,
                "speed": cfg["speed"],
                "volume": cfg["volume"],
                "pitch": cfg["pitch"],
                "tte": "UTF8",
                "sfl": 1,
            },
            "data": {
                "status": 2,
                "text": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
            },
        }

        started = time.monotonic()
        pieces: list[bytes] = []
        for message in self._stream(cfg["endpoint_std"], body, protocol="standard",
                                     timeout=timeout):
            code = message.get("code", 0)
            if code not in (0, None):
                raise TTSError(str(message.get("message") or "合成失败"),
                               code=int(code), protocol="standard")
            data = message.get("data") or {}
            audio = data.get("audio")
            if audio:
                pieces.append(_b64decode(audio))
            if int(data.get("status") or 0) == 2:
                break

        pcm = b"".join(pieces)
        if not pcm:
            raise TTSError("接口没有返回音频数据", protocol="standard")
        return TTSResult(pcm=pcm, sample_rate=rate, protocol="standard",
                         encoding="raw",
                         cost_ms=int((time.monotonic() - started) * 1000))

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------
    def _stream(self, endpoint: str, body: dict, *, protocol: str,
                timeout: float):
        """建立连接并逐帧 yield 服务端 JSON。"""
        cfg = self.settings()
        try:
            url = signed_url(endpoint, cfg["api_key"], cfg["api_secret"])
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"签名失败：{exc}", protocol=protocol, stage="config") from exc

        ws = None
        try:
            ws = websocket.create_connection(url, timeout=timeout)
            ws.send(json.dumps(body))
            idle_since = time.monotonic()
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException as exc:
                    raise TTSError("等待音频超时", protocol=protocol,
                                   stage="network") from exc
                if not raw:
                    break
                idle_since = time.monotonic()
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if time.monotonic() - idle_since > timeout:
                    raise TTSError("合成超时", protocol=protocol, stage="network")
        except TTSError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"{type(exc).__name__}: {exc}", protocol=protocol,
                           stage="network") from exc
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass

    def _decode_mp3(self, data: bytes, fallback_rate: int) -> tuple[bytes, int]:
        """lame 编码时在本地解回 PCM，保住超拟人的音色。"""
        if self._mp3_ready is False:
            raise TTSError("本地缺少 mp3 解码器（miniaudio）", protocol="super",
                           stage="decode")
        try:
            import miniaudio  # noqa: PLC0415 - 可选依赖，延迟导入

            decoded = miniaudio.decode(
                data,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=fallback_rate,
            )
            self._mp3_ready = True
            return bytes(decoded.samples), int(decoded.sample_rate or fallback_rate)
        except ImportError as exc:
            self._mp3_ready = False
            raise TTSError("本地缺少 mp3 解码器（miniaudio）", protocol="super",
                           stage="decode") from exc
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"mp3 解码失败：{exc}", protocol="super",
                           stage="decode") from exc


class TTSEngine:
    """在 ``XfyunTTS`` 之上做两级兜底：先换音色，再换协议。

    讯飞超拟人是**按发音人逐个授权**的，没开通的音色会直接回 11200，
    而用户在控制台看到「已开通」列表后很容易以为选哪个都行。所以遇到
    权限/参数错误时先拿默认免费音色重试一次——面试官必须能开口，
    不能因为一个音色名写错就整场哑掉。

    降级结果会被记住，之后的合成直接走可行路径，
    避免每句话都要先失败一次。
    """

    def __init__(self) -> None:
        self.client = XfyunTTS()
        self.protocol = str(config.get("xfyun.tts.mode") or "super")
        self._super_encoding = "raw"
        self.degraded = False
        self.degraded_reason = ""
        # 音色不可用后换成了哪个，以及给界面的提示语
        self._voice_override: str | None = None
        self.voice_notice = ""

    # ------------------------------------------------------------------
    # 音色
    # ------------------------------------------------------------------
    def voice(self) -> str:
        if self._voice_override:
            return self._voice_override
        return str(config.get("xfyun.tts.vcn") or FALLBACK_SUPER_VCN)

    def reset_voice(self) -> None:
        """设置页换了音色时调用，清掉上一次的兜底决定。"""
        self._voice_override = None
        self.voice_notice = ""

    def set_protocol(self, protocol: str) -> None:
        if protocol in ("super", "standard"):
            self.protocol = protocol
            self.degraded = False
            self.degraded_reason = ""
            # 模式变了，编码策略重新从 raw 开始试
            self._super_encoding = "raw"

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------
    def synthesize(self, text: str) -> TTSResult:
        # 超拟人优先，标准合成永远垫底（创建应用就有额度，几乎不可能失败）
        order = ["super", "standard"] if self.protocol == "super" else ["standard"]

        last: TTSError | None = None
        for protocol in order:
            try:
                if protocol == "super":
                    return self._attempt_super(text, self._super_encoding)
                return self.client.synthesize(text, protocol="standard",
                                               encoding="raw", vcn=self.voice())
            except TTSError as exc:
                last = exc
                logger.warning("TTS 失败 protocol=%s encoding=%s voice=%s code=%s: %s",
                               protocol, self._super_encoding, self.voice(),
                               exc.code, exc)
                if protocol == "super":
                    self.degraded = True
                    self.degraded_reason = self._diagnose(exc)

        raise last or TTSError("语音合成不可用", stage="config")

    # ------------------------------------------------------------------
    def _attempt_super(self, text: str, encoding: str) -> TTSResult:
        voice = self.voice()
        try:
            return self.client.synthesize(text, protocol="super", encoding=encoding,
                                          vcn=voice)
        except TTSError as first:
            # 第一嫌疑：这个音色没在控制台开通。换成默认免费音色再试一次，
            # 编码保持不变——音色授权和音频编码是两件不相干的事。
            auth_like = first.is_permission or first.is_param
            if auth_like and voice != FALLBACK_SUPER_VCN:
                try:
                    result = self.client.synthesize(
                        text, protocol="super", encoding=encoding,
                        vcn=FALLBACK_SUPER_VCN,
                    )
                except TTSError as second:
                    # 兜底音色也不行，说明问题不在音色上，继续往下试编码
                    logger.warning("兜底音色 %s 也失败：%s",
                                   FALLBACK_SUPER_VCN, second)
                else:
                    self._voice_override = FALLBACK_SUPER_VCN
                    self.voice_notice = (
                        f"音色「{voice_short(voice)}」不可用（{first.code}），"
                        f"已自动改用「{voice_short(FALLBACK_SUPER_VCN)}」"
                    )
                    logger.warning(self.voice_notice)
                    return result

            # 第二嫌疑：raw 编码被拒。文档对 raw 的支持说法不一致，
            # 换 lame 在本地解码，音色不变。
            if first.is_param and encoding == "raw":
                self._super_encoding = "lame"
                try:
                    return self.client.synthesize(text, protocol="super",
                                                  encoding="lame", vcn=voice)
                except TTSError as second:
                    logger.warning("lame 也失败：%s", second)
                    raise second from None
            raise

    def _diagnose(self, exc: TTSError) -> str:
        voice = voice_short(self.voice())
        if exc.is_permission:
            # 兜底音色都报未授权，说明不是单个音色的问题，而是服务本身没开
            if self.voice() == FALLBACK_SUPER_VCN:
                return (
                    f"超拟人合成未授权（{exc.code}）：请到讯飞控制台确认已开通"
                    f"「超拟人语音合成」，并勾选发音人「{voice}」"
                )
            return (
                f"音色「{voice}」未开通（{exc.code}）：请在控制台开通该发音人，"
                f"或换用聆小璇等默认免费音色"
            )
        if exc.is_param:
            return f"音色参数被拒绝（{exc.code}）：{exc}"
        return exc.describe()


@dataclass
class VoiceProbe:
    """一次音色探测的结果。"""

    vcn: str
    name: str
    ok: bool
    code: int | None = None
    message: str = ""
    cost_ms: int = 0

    def describe(self) -> str:
        if self.ok:
            return f"{self.name}（{self.vcn}）可用 · {self.cost_ms}ms"
        return f"{self.name}（{self.vcn}）失败：{self.message}"


# 探测用的短文本：只花 12 个字符的额度
PROBE_TEXT = "你好，我是今天的面试官。"


def probe_voices(vcns: list[str] | None = None, *, text: str = PROBE_TEXT,
                 timeout: float = 10.0,
                 client: XfyunTTS | None = None) -> list[VoiceProbe]:
    """逐个尝试音色，告诉用户到底哪个能用。

    只做检测，不影响配置——设置页的「检测全部音色」和命令行工具都用它。
    """
    target = vcns or [item.vcn for item in VOICE_PRESETS]
    worker = client or XfyunTTS()
    names = {item.vcn: item.name for item in VOICE_PRESETS}

    report: list[VoiceProbe] = []
    for vcn in target:
        name = names.get(vcn, vcn)
        started = time.monotonic()
        protocol = "super" if is_super_vcn(vcn) else "standard"
        try:
            result = worker.synthesize(text, protocol=protocol, encoding="raw",
                                       timeout=timeout, vcn=vcn)
        except TTSError as exc:
            report.append(VoiceProbe(
                vcn=vcn, name=name, ok=False, code=exc.code,
                message=exc.describe(),
                cost_ms=int((time.monotonic() - started) * 1000),
            ))
            continue
        except Exception as exc:  # noqa: BLE001
            report.append(VoiceProbe(
                vcn=vcn, name=name, ok=False, message=f"{type(exc).__name__}: {exc}",
                cost_ms=int((time.monotonic() - started) * 1000),
            ))
            continue
        report.append(VoiceProbe(
            vcn=vcn, name=name, ok=bool(result.pcm), cost_ms=result.cost_ms,
            message="" if result.pcm else "接口未返回音频",
        ))
    return report


def _b64decode(data: str) -> bytes:
    try:
        return base64.b64decode(data)
    except Exception:  # noqa: BLE001
        return b""


def _clamp(value, default: int = 50) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default
