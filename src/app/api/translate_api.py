"""文档上传、页面预览、翻译任务与结果下载。"""

from __future__ import annotations

import io
import logging
import os
import threading

import fitz  # PyMuPDF
from flask import Blueprint, abort, request, send_file

from .responses import fail, ok
from ...core import pipeline, pdf_parser
from ...core.store import document_store
from ...core.task import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_RUNNING,
    task_manager,
)
from ...shared import constants
from ...shared.config_loader import load_config
from ...shared.path_helpers import safe_name, task_output_dir

logger = logging.getLogger("TranslateAPI")

bp = Blueprint("translate_api", __name__, url_prefix="/api")


# ---------------------------------------------------------------------------
# 上传与解析
# ---------------------------------------------------------------------------

@bp.post("/upload")
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return fail("没有收到文件，请选择 PDF 后重试")

    if not file.filename.lower().endswith(constants.ALLOWED_UPLOAD_EXT):
        return fail("只支持 PDF 文件（.pdf）")

    try:
        file_id, path = document_store.save_upload(file.stream, file.filename)
    except ValueError as exc:
        return fail(str(exc))
    except Exception as exc:
        logger.exception("保存上传文件失败")
        return fail(f"保存文件失败：{exc}", 500)

    size = os.path.getsize(path)

    try:
        document = document_store.register(file_id, path, file.filename)
    except Exception as exc:
        logger.exception("解析 PDF 失败")
        try:
            os.unlink(path)
        except OSError:
            pass
        return fail(f"解析 PDF 失败：{exc}")

    # 上传时先按默认配置标记一次可翻译性，让界面能立刻显示原文
    try:
        config = load_config()
        pdf_parser.mark_translatable(
            document,
            target_lang=config.get("target_lang", "zh"),
            skip_headers_footers=bool(config.get("skip_headers_footers", True)),
            skip_repeated=bool(config.get("skip_repeated", True)),
            skip_non_translatable=bool(config.get("skip_non_translatable", True)),
        )
    except Exception:
        logger.warning("预标记可翻译性失败，不影响后续翻译", exc_info=True)

    stats = pdf_parser.document_statistics(document)
    message = f"已解析 {document.page_count} 页，识别到 {stats['translatable']} 个可翻译段落"
    if stats["translatable"] == 0:
        message += "（未发现可提取的文本，可能是扫描版 PDF，需要先做 OCR）"

    logger.info("上传成功：%s -> %s（%d 字节）", file.filename, file_id, size)
    return ok(
        file_id=file_id,
        filename=document.filename,
        pages=document.page_count,
        size=size,
        encrypted=document.encrypted,
        text_pages=document.text_page_count,
        paragraphs=stats["translatable"],
        columns=stats["columns"],
        warnings=document.warnings,
        message=message,
    )


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------

def _render_page(path: str, page_number: int, zoom: float) -> bytes:
    """把某一页渲染成 PNG 字节流。"""
    doc = fitz.open(path)
    try:
        if page_number < 1 or page_number > doc.page_count:
            raise IndexError(f"页码 {page_number} 超出范围（1~{doc.page_count}）")
        page = doc.load_page(page_number - 1)
        zoom = max(0.4, min(3.0, float(zoom)))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pixmap.tobytes("png")
    finally:
        doc.close()


def _parse_zoom(default: float = 1.4) -> float:
    try:
        return float(request.args.get("zoom", default))
    except (TypeError, ValueError):
        return default


def _parse_page(page: str, total: int) -> int:
    try:
        value = int(page)
    except (TypeError, ValueError):
        raise ValueError("页码必须是整数")
    if value < 1 or value > total:
        raise ValueError(f"页码 {value} 超出范围（1~{total}）")
    return value


@bp.get("/preview/<file_id>/<page>")
def preview(file_id: str, page: str):
    document = document_store.get(file_id)
    if document is None:
        return fail("找不到该文档，请重新上传", 404)
    try:
        page_number = _parse_page(page, document.page_count)
        data = _render_page(document.path, page_number, _parse_zoom())
    except ValueError as exc:
        return fail(str(exc), 404)
    except Exception as exc:
        logger.exception("渲染预览失败")
        return fail(f"渲染失败：{exc}", 500)
    return send_file(io.BytesIO(data), mimetype="image/png", max_age=0)


@bp.get("/outpreview/<file_id>/<page>")
def out_preview(file_id: str, page: str):
    """译后页面预览：读取最近一次翻译产生的 PDF。"""
    document = document_store.get(file_id)
    if document is None:
        return fail("找不到该文档，请重新上传", 404)

    outputs = getattr(document, "outputs", None) or {}
    if not outputs:
        return fail("该文档还没有翻译结果，请先开始翻译", 404)

    kind = request.args.get("kind") or ""
    if kind not in outputs:
        # 未指定或指定的变体不存在时，取第一个可用的
        kind = next(iter(outputs))
    path = outputs.get(kind)
    if not path or not os.path.exists(path):
        return fail("译文文件已被删除，请重新翻译", 404)

    try:
        doc = fitz.open(path)
        total = doc.page_count
        doc.close()
        page_number = _parse_page(page, total)
        data = _render_page(path, page_number, _parse_zoom())
    except ValueError as exc:
        return fail(str(exc), 404)
    except Exception as exc:
        logger.exception("渲染译文预览失败")
        return fail(f"渲染失败：{exc}", 500)
    return send_file(io.BytesIO(data), mimetype="image/png", max_age=0)


@bp.get("/document/<file_id>/info")
def document_info(file_id: str):
    document = document_store.get(file_id)
    if document is None:
        return fail("找不到该文档，请重新上传", 404)

    paragraphs = request.args.get("paragraphs", "1") not in ("0", "false", "no")
    payload = {
        "file_id": document.file_id,
        "filename": document.filename,
        "pages": [page.to_dict() for page in document.pages],
        "statistics": pdf_parser.document_statistics(document),
    }
    if paragraphs:
        payload["paragraphs"] = [p.to_dict() for p in document.paragraphs]
    return ok(payload)


# ---------------------------------------------------------------------------
# 翻译任务
# ---------------------------------------------------------------------------

@bp.post("/translate/start")
def start_translate():
    payload = request.get_json(silent=True) or {}
    file_id = payload.get("file_id")
    if not file_id:
        return fail("缺少 file_id")

    document = document_store.get(file_id)
    if document is None:
        return fail("找不到该文档，请重新上传", 404)

    # 合并配置：已保存的配置 -> 请求中的覆盖项
    config = load_config()
    for key in (
        "provider", "api_key", "base_url", "model", "target_lang", "source_lang",
        "mode", "prompt", "glossary", "batch_size", "concurrency", "temperature",
        "timeout", "max_retries", "page_range", "export_pdf", "export_markdown",
        "font_scale", "min_font_size", "line_spacing", "dual_gap",
        "merge_paragraphs", "skip_headers_footers", "skip_repeated",
        "skip_non_translatable", "font_family",
        # OCR
        "ocr_mode", "ocr_lang", "ocr_dpi", "ocr_min_confidence",
        "ocr_det_version", "ocr_use_cls", "ocr_cover_original",
    ):
        if key in payload and payload[key] is not None:
            config[key] = payload[key]

    if config.get("mode") not in constants.VALID_MODES:
        config["mode"] = constants.MODE_DUAL
    if config.get("target_lang") not in constants.LANGUAGE_LABELS:
        return fail(f"不支持的目标语言：{config.get('target_lang')}")

    preset = constants.PROVIDER_MAP.get(config.get("provider", ""))
    if preset is None:
        return fail(f"未知的翻译服务商：{config.get('provider')}")
    if preset.get("needs_key", True) and not config.get("api_key"):
        return fail("请先填写 API Key")
    # 模拟翻译在本地生成结果，不需要接口地址
    if preset.get("kind") != "mock" and not config.get("base_url"):
        return fail("请先填写接口地址（Base URL）")
    if not config.get("model"):
        return fail("请先选择或填写模型名称")

    try:
        config["batch_size"] = max(1, min(40, int(config.get("batch_size") or 12)))
        config["concurrency"] = max(1, min(16, int(config.get("concurrency") or 4)))
    except (TypeError, ValueError):
        config["batch_size"], config["concurrency"] = 12, 4

    # 同一文档若已有任务在跑，直接复用，避免重复计费
    for existing in task_manager.all():
        if existing.file_id == file_id and existing.status in (STATUS_RUNNING, "pending"):
            return fail("该文档已有翻译任务正在执行，请等待完成或先取消", 409)

    task = task_manager.create(file_id, document.filename, options={
        "mode": config["mode"],
        "target_lang": config["target_lang"],
        "provider": config["provider"],
        "model": config["model"],
        "page_range": config.get("page_range", "all"),
    })

    # 预估段落数，让前端可以立即显示分母
    try:
        from ...shared.text_utils import parse_page_range

        pages = set(parse_page_range(config.get("page_range", "all"), document.page_count))
        estimate = sum(1 for p in document.paragraphs if p.translatable and p.page in pages)
    except Exception:
        estimate = len(document.translatable_paragraphs())
    task.paragraph_total = estimate
    task.page_total = document.page_count

    worker = threading.Thread(
        target=pipeline.run_task,
        args=(task, document, config),
        name=f"task-{task.task_id}",
        daemon=True,
    )
    worker.start()

    logger.info("任务已启动：%s（%s -> %s，%d 段）",
                task.task_id, document.filename, config.get("target_lang"), estimate)
    return ok(task_id=task.task_id, paragraphs=estimate,
              mode=config["mode"], message="任务已启动")


@bp.get("/translate/progress/<task_id>")
def translate_progress(task_id: str):
    task = task_manager.get(task_id)
    if task is None:
        return fail("找不到该任务", 404)
    return ok(task.to_dict())


@bp.post("/translate/cancel/<task_id>")
def cancel_translate(task_id: str):
    task = task_manager.get(task_id)
    if task is None:
        return fail("找不到该任务", 404)
    if task.status in (STATUS_DONE, STATUS_ERROR, "cancelled"):
        return ok(status=task.status, message="任务已经结束")
    task.cancel_event.set()
    task.message = "正在取消…"
    task.log("收到取消请求，将在当前批次结束后停止")
    return ok(status="cancelling", message="已发送取消请求")


@bp.delete("/translate/task/<task_id>")
def delete_task(task_id: str):
    task = task_manager.get(task_id)
    if task is None:
        return fail("找不到该任务", 404)
    task.cancel_event.set()

    removed = 0
    directory = task_output_dir(task_id)
    if directory.exists():
        for item in directory.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    removed += 1
            except OSError:
                logger.warning("删除文件失败：%s", item, exc_info=True)
    task_manager.remove(task_id)
    return ok(removed=removed, message=f"已删除任务与 {removed} 个文件")


@bp.get("/translate/result/<task_id>")
def translate_result(task_id: str):
    task = task_manager.get(task_id)
    if task is None:
        return fail("找不到该任务", 404)
    data = task.to_dict()
    # 只保留产物列表、统计与状态，避免把日志整段重复返回
    return ok(
        task_id=task.task_id,
        filename=task.filename,
        status=data["status"],
        stage=data["stage"],
        elapsed=data["elapsed"],
        files=data["files"],
        stats=data["stats"],
        error=task.error,
    )


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

@bp.get("/download/<task_id>/<path:name>")
def download(task_id: str, name: str):
    task = task_manager.get(task_id)
    if task is None:
        return fail("找不到该任务", 404)

    safe = safe_name(name)
    directory = task_output_dir(task_id)
    path = directory / safe

    # 防目录穿越：解析后的真实路径必须仍在任务目录内
    try:
        if not path.resolve().is_relative_to(directory.resolve()):
            abort(403)
    except AttributeError:  # Python < 3.9
        if not str(path.resolve()).startswith(str(directory.resolve())):
            abort(403)

    if not path.exists() or not path.is_file():
        return fail("文件不存在或已被清理", 404)

    return send_file(str(path), as_attachment=True, download_name=safe)
