"""Flask 应用工厂。

目录约定（与 Saber-Translator 保持一致的 app/api/templates/static 结构）：

    src/app/
        __init__.py     创建应用
        routes.py       页面路由
        api/            REST 接口
        templates/      HTML 模板
        static/         CSS / JS
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from ..shared import constants
from ..shared.logger import setup_logging
from ..shared.path_helpers import ensure_dirs
from . import routes
from .api import config_bp, fonts_bp, ocr_bp, system_bp, translate_bp

logger = logging.getLogger("App")


def create_app(testing: bool = False) -> Flask:
    """创建并配置 Flask 应用。"""
    ensure_dirs()
    setup_logging(quiet=testing)

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = constants.MAX_UPLOAD_SIZE
    app.config["JSON_AS_ASCII"] = False
    app.config["TESTING"] = testing
    # 中文 JSON 不转义，便于调试
    try:
        app.json.ensure_ascii = False
    except AttributeError:
        pass

    app.register_blueprint(routes.bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(translate_bp)
    app.register_blueprint(ocr_bp)
    app.register_blueprint(fonts_bp)
    app.register_blueprint(system_bp)

    _register_error_handlers(app)

    logger.info("%s v%s 已就绪", constants.APP_NAME, constants.APP_VERSION)
    return app


def _register_error_handlers(app: Flask) -> None:
    """统一错误响应格式，前端只需处理 {ok:false,error}。"""

    @app.errorhandler(404)
    def handle_404(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "接口不存在"}), 404
        return error, 404

    @app.errorhandler(413)
    def handle_413(error):
        limit = constants.MAX_UPLOAD_SIZE // (1024 * 1024)
        return jsonify({"ok": False, "error": f"文件过大，上限为 {limit} MB"}), 413

    @app.errorhandler(500)
    def handle_500(error):
        logger.exception("服务器内部错误：%s", error)
        return jsonify({"ok": False, "error": f"服务器内部错误：{error}"}), 500

    @app.errorhandler(Exception)
    def handle_uncaught(error):
        from werkzeug.exceptions import HTTPException

        if isinstance(error, HTTPException):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": error.description}), error.code
            return error
        logger.exception("未捕获异常")
        return jsonify({"ok": False, "error": f"{type(error).__name__}: {error}"}), 500
