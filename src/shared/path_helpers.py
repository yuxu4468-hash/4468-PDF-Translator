"""路径与运行目录工具。

目录约定（全部位于项目根目录下，便于绿色部署与整目录搬迁）：

    PDF-translater/
        config/      配置文件
        logs/        日志
        work/        运行时数据
            uploads/ 上传的原始 PDF
            outputs/ 翻译产物（按 task_id 分目录）
            tmp/     中间临时文件
        src/         源码
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

# src/shared/path_helpers.py -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
WORK_DIR = PROJECT_ROOT / "work"
UPLOAD_DIR = WORK_DIR / "uploads"
OUTPUT_DIR = WORK_DIR / "outputs"
TMP_DIR = WORK_DIR / "tmp"
CACHE_DIR = WORK_DIR / "cache"

_ALL_DIRS = (CONFIG_DIR, LOGS_DIR, WORK_DIR, UPLOAD_DIR, OUTPUT_DIR, TMP_DIR, CACHE_DIR)


def ensure_dirs() -> None:
    """确保所有运行目录存在（幂等）。"""
    for directory in _ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def task_output_dir(task_id: str) -> Path:
    """返回某个任务的产物目录，并确保其存在。"""
    safe = safe_name(task_id)
    path = OUTPUT_DIR / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str, fallback: str = "file") -> str:
    """把任意字符串转换成安全的文件名片段。

    去掉路径分隔符、控制字符与 Windows 保留字符，压缩空白。
    """
    if not name:
        return fallback
    # Unicode 规范化，避免全角/组合字符造成的诡异文件名
    name = unicodedata.normalize("NFKC", str(name))
    # 去掉扩展名之外的路径部分
    name = name.replace("\\", "/").split("/")[-1]
    # 去掉 Windows 非法字符
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    # 压缩空白
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return fallback
    # Windows 保留名
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if name.split(".")[0].upper() in reserved:
        name = "_" + name
    # 限制长度，给后缀留出空间
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: 120 - len(ext) - 1] + "." + ext
        else:
            name = name[:120]
    return name


def stem_of(filename: str) -> str:
    """取得不含扩展名的文件名主干（已安全化）。"""
    return safe_name(Path(filename).stem or "document")


def human_size(num_bytes: int | float) -> str:
    """把字节数格式化成可读字符串。"""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def is_relative_to(path: Path, parent: Path) -> bool:
    """兼容 Python 3.8 的 Path.is_relative_to 替代实现。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}
