"""嵌字字体接口：本机字体探测、风格匹配预览、缓存刷新。

界面上的"嵌字用哪个字体"完全由后端决定（原文风格 → 本机字体匹配），
所以这里把探测结果和"不同原文风格会选中谁"直接暴露出来，用户不用猜。
"""

from __future__ import annotations

import logging

from flask import Blueprint, request

from .responses import fail, ok
from ...core import fonts as font_lib
from ...shared import constants

logger = logging.getLogger("FontsAPI")

bp = Blueprint("fonts_api", __name__, url_prefix="/api/fonts")


@bp.get("/status")
def fonts_status():
    """列出本机可用的中文字体（含是否可安全子集化的标记）。"""
    try:
        force = request.args.get("refresh", "").lower() in {"1", "true", "yes"}
        if force:
            font_lib.invalidate()
        info = font_lib.status()
    except Exception as exc:
        logger.exception("字体探测失败")
        return fail(f"字体探测失败：{exc}")

    target = request.args.get("target_lang") or constants.DEFAULT_CONFIG["target_lang"]
    config = {
        "lettering_font_regular": request.args.get("regular", ""),
        "lettering_font_bold": request.args.get("bold", ""),
        "lettering_font_family": request.args.get("family", "auto"),
        "lettering_use_system_fonts": request.args.get("use_system", "1") != "0",
    }
    try:
        info["preview"] = font_lib.preview_choices(target, config)
    except Exception:
        logger.debug("字体匹配预览失败", exc_info=True)
        info["preview"] = []
    return ok(**info)


@bp.post("/reload")
def fonts_reload():
    """清空缓存并重新探测（用户刚装了新字体时用）。"""
    font_lib.invalidate()
    try:
        info = font_lib.status()
    except Exception as exc:
        return fail(f"重新探测失败：{exc}")
    return ok(**info)
