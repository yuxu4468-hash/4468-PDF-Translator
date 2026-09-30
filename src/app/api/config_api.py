"""配置相关接口：读写配置、服务商列表、连通性测试。"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, request

from .responses import fail, ok
from ...interfaces import ProviderError, create_provider, provider_choices
from ...shared import constants
from ...shared.config_loader import load_config, sanitize_config_for_client, save_config

logger = logging.getLogger("ConfigAPI")

bp = Blueprint("config_api", __name__, url_prefix="/api")

# 允许通过接口修改的字段白名单（避免前端塞入任意键污染配置）
EDITABLE_KEYS = set(constants.DEFAULT_CONFIG) | {"glossary"}


def filter_editable(payload: dict) -> dict:
    """只保留白名单内的字段。"""
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items() if key in EDITABLE_KEYS}


@bp.get("/config")
def get_config():
    return ok(config=sanitize_config_for_client(load_config()),
              defaults=constants.DEFAULT_CONFIG)


@bp.post("/config")
def post_config():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return fail("请求体必须是 JSON 对象")
    patch = filter_editable(payload)
    if not patch:
        return fail("没有可保存的字段")
    try:
        config = save_config(patch)
    except Exception as exc:
        logger.exception("保存配置失败")
        return fail(f"保存配置失败：{exc}", 500)
    return ok(config=sanitize_config_for_client(config))


@bp.get("/providers")
def get_providers():
    return ok(providers=provider_choices(), languages=constants.LANGUAGES)


@bp.post("/test-connection")
def test_connection():
    payload = request.get_json(silent=True) or {}
    saved = load_config()

    # 用请求里的值覆盖已保存配置；api_key 为空时回退到已保存的密钥
    merged = dict(saved)
    for key in ("provider", "api_key", "base_url", "model", "timeout", "temperature"):
        if payload.get(key):
            merged[key] = payload[key]
    merged["timeout"] = min(60, int(merged.get("timeout") or 30))
    merged["max_retries"] = 1  # 测试连接不重试，快速给出结论

    if not merged.get("api_key"):
        preset = constants.PROVIDER_MAP.get(merged.get("provider", ""), {})
        if preset.get("needs_key", True):
            return fail("请先填写 API Key")

    provider = None
    try:
        provider = create_provider(merged)
        started = time.time()
        response = provider.test_connection()
        latency = int((time.time() - started) * 1000)
        reply = (response.text or "").strip().replace("\n", " ")[:80]
        logger.info("连接测试成功：%s/%s（%dms）", merged.get("provider"), merged.get("model"), latency)
        return ok(latency_ms=latency, reply=reply or "（模型返回空内容，但连接正常）", model=response.model)
    except ProviderError as exc:
        logger.warning("连接测试失败：%s", exc)
        return fail(str(exc))
    except Exception as exc:
        logger.exception("连接测试异常")
        return fail(f"测试失败：{type(exc).__name__}: {exc}", 500)
    finally:
        if provider is not None:
            provider.close()
