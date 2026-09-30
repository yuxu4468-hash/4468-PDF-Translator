"""OCR 相关接口：模型状态、下载、单页试识别。"""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, request

from .responses import fail, ok
from ...core import ocr as ocr_pkg
from ...shared import constants

logger = logging.getLogger("OCRAPI")

bp = Blueprint("ocr_api", __name__, url_prefix="/api/ocr")

# 下载是耗时操作，放到后台线程并记录状态
_download_state = {
    "running": False,
    "lang": "",
    "message": "",
    "error": "",
    "done": False,
}
_download_lock = threading.Lock()


def _engine_available() -> tuple[bool, str]:
    """检查推理引擎依赖是否齐备。"""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False, "未安装 numpy"
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "未安装 onnxruntime（pip install onnxruntime）"
    return True, ""


@bp.get("/status")
def ocr_status():
    available, reason = _engine_available()
    lang = request.args.get("lang") or constants.DEFAULT_CONFIG["ocr_lang"]
    det = request.args.get("det") or constants.DEFAULT_CONFIG["ocr_det_version"]
    try:
        info = ocr_pkg.status(lang, det)
    except Exception as exc:
        logger.exception("查询 OCR 状态失败")
        return fail(f"查询失败：{exc}", 500)

    with _download_lock:
        info["download"] = dict(_download_state)
    info["engine_available"] = available
    info["engine_reason"] = reason
    info["modes"] = [
        {"id": mode, "label": constants.OCR_MODE_LABELS[mode]}
        for mode in constants.OCR_MODES
    ]
    return ok(info)


@bp.post("/download")
def ocr_download():
    payload = request.get_json(silent=True) or {}
    lang = payload.get("lang") or constants.DEFAULT_CONFIG["ocr_lang"]
    det = payload.get("det") or constants.DEFAULT_CONFIG["ocr_det_version"]
    if lang not in ocr_pkg.REC_MODELS:
        return fail(f"不支持的语言：{lang}")

    available, reason = _engine_available()
    if not available:
        return fail(reason)

    with _download_lock:
        if _download_state["running"]:
            return ok(message="已有下载任务在进行中", **dict(_download_state))
        _download_state.update(
            {"running": True, "lang": lang, "message": "开始下载…", "error": "", "done": False}
        )

    def worker():
        def log(message: str) -> None:
            with _download_lock:
                _download_state["message"] = str(message)
            logger.info("[OCR下载] %s", message)

        try:
            ocr_pkg.ensure_models(lang, det, log=log)
            with _download_lock:
                _download_state.update({"running": False, "done": True, "message": "下载完成"})
        except Exception as exc:
            logger.exception("OCR 模型下载失败")
            with _download_lock:
                _download_state.update(
                    {"running": False, "done": False, "error": str(exc), "message": "下载失败"}
                )

    threading.Thread(target=worker, name="ocr-download", daemon=True).start()
    return ok(message="已开始下载", lang=lang)


@bp.post("/test")
def ocr_test():
    """对已上传文档的某一页做一次试识别，方便确认语言与效果。"""
    from ...core.store import document_store
    from ...core.ocr import scanner as ocr_scanner
    from ...core.ocr.engine import PPOCREngine

    payload = request.get_json(silent=True) or {}
    file_id = payload.get("file_id")
    document = document_store.get(file_id) if file_id else None
    if document is None:
        return fail("找不到该文档，请先上传 PDF", 404)

    try:
        page_number = int(payload.get("page") or 1)
    except (TypeError, ValueError):
        return fail("页码必须是整数")
    if page_number < 1 or page_number > document.page_count:
        return fail(f"页码超出范围（1~{document.page_count}）")

    lang = payload.get("lang") or constants.DEFAULT_CONFIG["ocr_lang"]
    available, reason = _engine_available()
    if not available:
        return fail(reason)

    doc = None
    try:
        ocr_pkg.ensure_models(lang, log=lambda m: logger.info("[OCR] %s", m))
        doc = document_store.open_document(document.path)
        engine = PPOCREngine(lang=lang)
        engine.load()
        page = doc.load_page(page_number - 1)
        needed, why = ocr_scanner.page_needs_ocr(page)
        array, _scale = ocr_scanner.render_page(page, dpi=int(payload.get("dpi") or 300))
        result = engine.recognize_page(array)
        return ok(
            page=page_number,
            needed=needed,
            reason=why,
            lang=lang,
            elapsed=round(result.elapsed, 2),
            image={"width": result.width, "height": result.height},
            lines=[line.to_dict() for line in result.lines[:200]],
            text=result.text,
        )
    except Exception as exc:
        logger.exception("OCR 试识别失败")
        return fail(f"识别失败：{exc}", 500)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
