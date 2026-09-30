"""JSON 响应封装与通用工具。"""

from __future__ import annotations

from flask import jsonify


def ok(data=None, **extra):
    """成功响应：{"ok": true, "data": {...}}"""
    payload = dict(data or {})
    payload.update(extra)
    return jsonify({"ok": True, "data": payload})


def fail(message: str, status: int = 400, **extra):
    """失败响应：{"ok": false, "error": "..."}"""
    body = {"ok": False, "error": str(message)}
    if extra:
        body.update(extra)
    return jsonify(body), status
