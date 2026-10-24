"""路径定位：同时兼容开发环境与 PyInstaller 打包后的绿色版。"""

from __future__ import annotations

import sys
from pathlib import Path


def _app_root() -> Path:
    """绿色版把数据放在 exe 同级，开发时放在工程根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


APP_ROOT = _app_root()
# PyInstaller 会把 --add-data 的内容解到 _MEIPASS；开发时就是工程根目录
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", APP_ROOT))

DATA_DIR = APP_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
FILE_DIR = DATA_DIR / "files"
AUDIO_DIR = DATA_DIR / "audio"
REPORT_BACKUP_DIR = DATA_DIR / "reports_backup"

CONFIG_FILE = DATA_DIR / "config.json"
SECRET_FILE = DATA_DIR / "secrets.bin"
DB_FILE = DATA_DIR / "app.db"
LOG_FILE = LOG_DIR / "app.log"


def ensure_dirs() -> None:
    for directory in (DATA_DIR, LOG_DIR, FILE_DIR, AUDIO_DIR, REPORT_BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def resource(*parts: str) -> Path:
    return RESOURCE_ROOT.joinpath(*parts)


def audio_dir_for(interview_id: str) -> Path:
    target = AUDIO_DIR / interview_id
    target.mkdir(parents=True, exist_ok=True)
    return target
