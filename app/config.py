"""配置读写。

非敏感项存 data/config.json，密钥单独用 Windows DPAPI 加密后存 data/secrets.bin。
DPAPI 不可用时降级为 base64（并标记 insecure），保证程序仍能启动。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from copy import deepcopy
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import CONFIG_FILE, SECRET_FILE, ensure_dirs

# 以点号路径声明哪些字段属于密钥
SECRET_PATHS = ("llm.api_key", "xfyun.api_secret", "xfyun.api_key")

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": 0.7,
        "max_tokens": 1024,
        "timeout": 60,
    },
    "xfyun": {
        "app_id": "",
        "asr": {
            "endpoint": "wss://rtasr.xfyun.cn/v1/ws",
            "lang": "cn",
            "sample_rate": 16000,
            "chunk_ms": 40,
        },
        "tts": {
            "enabled": True,
            "mode": "super",
            "endpoint_super": "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6",
            "endpoint_std": "wss://tts-api.xfyun.cn/v2/tts",
            "vcn": "x6_lingxiaoxuan_pro",
            "speed": 50,
            "volume": 60,
            "pitch": 50,
            "sample_rate": 16000,
            "encoding": "raw",
        },
    },
    "interview": {
        "planned_minutes": 20,
        "style": "friendly",
        "question_count": 18,
        "spare_count": 12,
        "max_followups": 3,
        "auto_end_silence_sec": 2.0,
        "save_audio": True,
        "show_reference": False,
    },
    "ui": {"theme": "light", "font_size": 13},
}

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus"},
    "spark": {"base_url": "https://spark-api-open.xf-yun.com/v1", "model": "generalv3.5"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "custom": {"base_url": "", "model": ""},
}

@dataclass(frozen=True)
class VoicePreset:
    """一个发音人。

    ``opened`` 记录「控制台里是否已开通」——讯飞的超拟人按发音人单独授权，
    没开通的音色调用会直接回 11200，界面要能提示这一点。
    ``language`` 只是展示用的语种；``mode`` 决定这条音色走哪套协议。
    """

    vcn: str
    name: str
    gender: str
    language: str = "普通话"
    mode: str = "super"
    opened: bool = True
    note: str = ""

    @property
    def caption(self) -> str:
        parts = [self.name, self.gender, self.language]
        if self.note:
            parts.append(self.note)
        return " · ".join(parts)


# 超拟人音色一律是 x4_/x5_/x6_ 前缀，标准合成则是 aisjiuxu 这类拼音名。
# 注意 「chars.total」不是发音人而是控制台的计费项，不能放进列表。
VOICE_PRESETS: list[VoicePreset] = [
    VoicePreset("x6_lingxiaoxuan_pro", "聆小璇", "女声", note="交互聊天 · 默认免费"),
    VoicePreset("x6_lingfeiyi_pro", "聆飞逸", "男声", note="交互聊天 · 默认免费"),
    VoicePreset("x6_lingxiaoyue_pro", "聆小玥", "女声", note="交互聊天 · 默认免费"),
    VoicePreset("x6_lingyuyan_pro", "聆玉言", "女声", note="交互聊天 · 默认免费"),
    VoicePreset("x5_lingxiaotang_flow", "聆小糖", "女声", note="语音助手"),
    VoicePreset("x6_gaolengnanshen_pro", "高冷男神", "男声", note="角色配音"),
    # 标准合成只作兜底：超拟人全部不可用时至少还能出声
    VoicePreset("aisjiuxu", "许久", "男声", mode="standard", note="标准合成兜底"),
    VoicePreset("aisxping", "小萍", "女声", mode="standard", note="标准合成兜底"),
]

# 超拟人全部失败时自动换到的音色（讯飞默认免费，开通率最高）
FALLBACK_SUPER_VCN = "x6_lingxiaoxuan_pro"
# 标准合成的兜底音色，创建应用即可用，无需单独开通
FALLBACK_STANDARD_VCN = "aisjiuxu"

# 只有 x4 系列支持口语化参数 oral，x5/x6 带上会报参数错误
ORAL_VCN_PREFIXES = ("x4_",)


def is_super_vcn(vcn: str) -> bool:
    """判断音色属于超拟人（x4/x5/x6 前缀）。"""
    return str(vcn or "").startswith(("x4_", "x5_", "x6_"))


def voice_caption(vcn: str) -> str:
    """把 vcn 翻译成可读名字，未知音色原样返回。"""
    for item in VOICE_PRESETS:
        if item.vcn == vcn:
            return item.caption
    return vcn or "未设置"


def voice_short(vcn: str) -> str:
    """只要名字，用在提示语里（「音色「聆小璇」未开通」）。"""
    for item in VOICE_PRESETS:
        if item.vcn == vcn:
            return item.name
    return vcn or "未设置"


# --------------------------------------------------------------------------
# DPAPI
# --------------------------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_encrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _keep = _to_blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _keep = _to_blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError("CryptUnprotectData 调用失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect(payload: dict[str, str]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if os.name == "nt":
        try:
            return b"DPAPI1" + _dpapi_encrypt(raw)
        except Exception:
            pass
    return b"B64__1" + base64.b64encode(raw)


def unprotect(blob: bytes) -> dict[str, str]:
    if blob.startswith(b"DPAPI1"):
        raw = _dpapi_decrypt(blob[6:])
    elif blob.startswith(b"B64__1"):
        raw = base64.b64decode(blob[6:])
    else:
        return {}
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}****{value[-4:]}"


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _dig(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _bury(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class Config:
    """全局配置单例的载体。所有读写都走点号路径，方便和表单双向绑定。"""

    def __init__(self) -> None:
        self._data: dict[str, Any] = deepcopy(DEFAULT_CONFIG)
        self._secrets: dict[str, str] = {}
        self.encrypted = True
        self.voice_migrated = False
        self.load()

    # -- 读写 ------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        try:
            return _dig(self._data, dotted)
        except KeyError:
            return default

    def set(self, dotted: str, value: Any, *, save: bool = False) -> None:
        _bury(self._data, dotted, value)
        if save:
            self.save()

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # -- 持久化 ----------------------------------------------------------
    def load(self) -> None:
        ensure_dirs()
        if CONFIG_FILE.exists():
            try:
                stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self._data = _deep_merge(DEFAULT_CONFIG, stored)
            except Exception:
                self._data = deepcopy(DEFAULT_CONFIG)

        if SECRET_FILE.exists():
            try:
                self._secrets = unprotect(SECRET_FILE.read_bytes())
                self.encrypted = True
            except Exception:
                self._secrets = {}
                self.encrypted = False

        # 密钥回填到 _data，业务代码取值不用关心存储形式
        for path in SECRET_PATHS:
            if path in self._secrets:
                _bury(self._data, path, self._secrets[path])

        self._migrate_voice()

    def _migrate_voice(self) -> None:
        """把历史配置里的 x4 音色换掉。

        早期版本默认 ``x4_lingxiaoxuan_oral``，但 x4 系列在控制台要单独
        开通，而 x6 系列是默认免费音色。老配置留着旧值会让每次合成都
        回 11200，升级后自动迁移一次。
        """
        vcn = str(self.get("xfyun.tts.vcn") or "")
        if vcn.startswith("x4_"):
            _bury(self._data, "xfyun.tts.vcn", FALLBACK_SUPER_VCN)
            self.voice_migrated = True

    def save(self) -> None:
        ensure_dirs()
        plain = deepcopy(self._data)
        secrets: dict[str, str] = {}
        for path in SECRET_PATHS:
            try:
                value = _dig(plain, path)
            except KeyError:
                continue
            if value:
                secrets[path] = str(value)
            # 配置文件里绝不留明文
            try:
                _bury(plain, path, "")
            except KeyError:
                pass

        CONFIG_FILE.write_text(
            json.dumps(plain, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if secrets:
            SECRET_FILE.write_bytes(protect(secrets))
            self._secrets = secrets

    # -- 校验 ------------------------------------------------------------
    def missing_items(self) -> list[str]:
        checks = [
            ("llm.base_url", "大模型 Base URL"),
            ("llm.model", "大模型模型名"),
            ("llm.api_key", "大模型 API Key"),
            ("xfyun.app_id", "讯飞 AppID"),
            ("xfyun.api_secret", "讯飞 APISecret"),
            ("xfyun.api_key", "讯飞 APIKey"),
        ]
        return [label for path, label in checks if not str(self.get(path) or "").strip()]

    def is_ready(self) -> bool:
        return not self.missing_items()

    def parsed_seconds(self) -> int:
        try:
            return int(self.get("interview.planned_minutes", 20)) * 60
        except (TypeError, ValueError):
            return 1200


config = Config()
