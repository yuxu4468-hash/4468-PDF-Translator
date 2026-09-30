"""配置读写。

**配置约定**参考了 Saber-Translator（https://github.com/MashiroSaber03/Saber-Translator，
GPL-3.0）的 `shared/config_loader.py` 思路：配置以 JSON 存放在项目 `config/` 目录下，
读取时与内置默认值深合并（新增配置项天然有默认值），写入时先写临时文件再原子替换，
避免半截文件。本文件是重写实现，未复制其代码；另外补了两处：

  - `deep_merge` 忽略值为 `None` 的键（前端常把空输入框传成 null）；
  - 写入前 `flush` + `fsync`，断电时也不会留下空文件。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading

from . import constants
from .path_helpers import CONFIG_DIR, ensure_dirs

logger = logging.getLogger("ConfigLoader")

_lock = threading.RLock()

DEFAULT_CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def config_path(filename: str):
    ensure_dirs()
    return CONFIG_DIR / filename


def _atomic_write_json(path, data) -> bool:
    """先写临时文件再替换，避免写入过程中断电/崩溃留下损坏的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        return True
    except Exception:
        logger.exception("写入配置文件失败: %s", path)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return False


def load_json(filename: str, default=None):
    """读取 JSON 配置；文件缺失或损坏时返回 default。"""
    path = config_path(filename)
    if not path.exists():
        return copy.deepcopy(default)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        logger.error("配置文件 JSON 解析失败，已回退默认值: %s", path)
        return copy.deepcopy(default)
    except Exception:
        logger.exception("读取配置文件失败: %s", path)
        return copy.deepcopy(default)


def save_json(filename: str, data) -> bool:
    return _atomic_write_json(config_path(filename), data)


# ---------------------------------------------------------------------------
# 深合并
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """把 override 深合并进 base 的副本并返回。

    - 字典递归合并
    - 列表与标量整体替换
    - override 中值为 None 的键被忽略（前端常把空输入框传成 null）
    """
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# 应用配置
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """读取用户配置，并与内置默认值合并（保证新增字段有默认值）。"""
    with _lock:
        stored = load_json(DEFAULT_CONFIG_FILE, default={})
        config = deep_merge(constants.DEFAULT_CONFIG, stored if isinstance(stored, dict) else {})
        return config


def save_config(patch: dict) -> dict:
    """把 patch 深合并到当前配置并落盘，返回合并后的完整配置。"""
    with _lock:
        merged = deep_merge(load_config(), patch or {})
        save_json(DEFAULT_CONFIG_FILE, merged)
        logger.info("配置已保存（字段数 %d）", len(merged))
        return merged


def sanitize_config_for_client(config: dict) -> dict:
    """返回给前端的配置（当前为本地单人使用，直接返回明文，便于界面预填）。"""
    return copy.deepcopy(config)


# ---------------------------------------------------------------------------
# 历史记录
# ---------------------------------------------------------------------------

def load_history() -> list:
    data = load_json(HISTORY_FILE, default=[])
    return data if isinstance(data, list) else []


def append_history(entry: dict, max_items: int = None) -> list:
    """追加一条历史记录（最新的在前），并裁剪长度。"""
    limit = max_items or constants.MAX_TASKS_KEPT
    with _lock:
        items = load_history()
        items = [it for it in items if it.get("task_id") != entry.get("task_id")]
        items.insert(0, entry)
        items = items[:limit]
        save_json(HISTORY_FILE, items)
        return items
