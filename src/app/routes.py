"""页面路由。"""

from __future__ import annotations

import logging

from flask import Blueprint, render_template

logger = logging.getLogger("Routes")

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    """主界面（单页应用，所有交互走 /api/*）。"""
    return render_template("index.html")


@bp.get("/favicon.ico")
def favicon():
    # 没有图标文件时返回 204，避免浏览器控制台报 404
    return "", 204
