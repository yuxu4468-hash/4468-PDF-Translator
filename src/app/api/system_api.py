"""系统信息、历史记录与日志查看。"""

from __future__ import annotations

import logging
import platform
import socket
import sys

import fitz  # PyMuPDF
from flask import Blueprint, request

from .responses import fail, ok
from ...core.task import task_manager
from ...shared import constants
from ...shared.config_loader import load_history
from ...shared.logger import get_recent_logs
from ...shared.path_helpers import WORK_DIR

logger = logging.getLogger("SystemAPI")

bp = Blueprint("system_api", __name__, url_prefix="/api")

# 结果缓存：网络探测比较慢，缓存一段时间
_network_cache = {"checked_at": 0.0, "value": True}


def check_network(timeout: float = 2.0) -> bool:
    """探测是否能连上公网（用于界面提示，失败不影响功能）。"""
    import time

    now = time.time()
    if now - _network_cache["checked_at"] < 30:
        return _network_cache["value"]
    reachable = False
    for host, port in (("api.deepseek.com", 443), ("1.1.1.1", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reachable = True
                break
        except OSError:
            continue
    _network_cache.update({"checked_at": now, "value": reachable})
    return reachable


@bp.get("/system/info")
def system_info():
    try:
        cjk_font = fitz.Font("china-s").name
    except Exception:
        cjk_font = ""

    # OCR 状态（失败不影响系统信息返回）
    ocr_info = {}
    try:
        from ...core import ocr as ocr_pkg

        info = ocr_pkg.status()
        ocr_info = {
            "ready": info.get("ready", False),
            "lang": info.get("lang", ""),
            "dir": info.get("dir", ""),
            "size": info.get("size", 0),
        }
        try:
            import onnxruntime  # noqa: F401

            ocr_info["engine"] = True
        except ImportError:
            ocr_info["engine"] = False
    except Exception:
        logger.debug("读取 OCR 状态失败", exc_info=True)

    return ok(
        app_name=constants.APP_NAME,
        app_version=constants.APP_VERSION,
        # 应用名是纯 ASCII，拿它当"中文能正确往返"的样本会失效；
        # 这里带上中文标语，供界面显示，也供测试当编码样本。
        app_slogan=constants.APP_SLOGAN,
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        pymupdf=fitz.version[0] if hasattr(fitz, "version") else fitz.__doc__,
        platform=f"{platform.system()}-{platform.release()}",
        cjk_font=cjk_font,
        cjk_ok=bool(cjk_font),
        work_dir=str(WORK_DIR),
        port=constants.PORT,
        network=check_network(),
        providers=[item["id"] for item in constants.PROVIDERS],
        ocr=ocr_info,
    )


@bp.get("/history")
def history():
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    items = load_history()[:limit]
    return ok(items=items)


@bp.get("/logs")
def logs():
    try:
        lines = max(10, min(1000, int(request.args.get("lines", 200))))
    except (TypeError, ValueError):
        lines = 200
    return ok(text=get_recent_logs(lines), lines=lines)
