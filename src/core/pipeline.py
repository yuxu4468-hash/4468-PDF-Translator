"""端到端流水线：解析 -> 翻译 -> 排版 -> 导出。

`run_task()` 在后台线程里执行，通过 `Task` 对象对外暴露进度与日志。
"""

from __future__ import annotations

import logging
import time
import unicodedata
from pathlib import Path

from . import exporter, pdf_parser, typesetter
from .models import ParsedDocument
from .task import (
    STAGE_EXPORTING,
    STAGE_FINISHED,
    STAGE_OCR,
    STAGE_PARSING,
    STAGE_TRANSLATING,
    STAGE_TYPESETTING,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_RUNNING,
    Task,
)
from .translator import TranslationCache, Translator
from ..shared import constants
from ..shared.path_helpers import CACHE_DIR, safe_name, stem_of, task_output_dir
from ..shared.text_utils import parse_page_range

logger = logging.getLogger("Pipeline")

# 生成哪些 PDF 变体（顺序即结果列表顺序，第一个是主模式）
# 嵌字版放第一位：它是"看起来就是原文那份文档、只是变成了中文"的产物，
# 也就是这个工具的主交付物；双语对照版作为辅助。
ALL_MODES = ("mono", "dual")
MODE_LABELS = dict(constants.MODE_LABELS)



class CancelledError(RuntimeError):
    """用户取消任务。"""


def _check_cancel(task: Task) -> None:
    if task.cancel_event.is_set():
        raise CancelledError("任务已被用户取消")


def run_task(task: Task, document: ParsedDocument, config: dict) -> None:
    """执行一次完整的翻译任务（就地修改 task 与 document）。"""
    task.status = STATUS_RUNNING
    task.started_at = time.time()
    task.log(f"开始处理《{document.filename}》：{document.page_count} 页")

    try:
        _translate(task, document, config)
        _typeset(task, document, config)
        _export(task, document, config)
    except CancelledError:
        task.status = STATUS_CANCELLED
        task.stage = STAGE_FINISHED
        task.message = "任务已取消"
        task.log("任务已取消")
    except Exception as exc:
        logger.exception("任务 %s 失败", task.task_id)
        task.status = STATUS_ERROR
        task.error = str(exc) or type(exc).__name__
        task.message = f"任务失败：{task.error}"
        task.log(f"任务失败：{task.error}")
    finally:
        task.finished_at = time.time()
        task.refresh_clock()
        _record_history(task)


# ---------------------------------------------------------------------------
# 阶段 0：OCR（扫描版 PDF）
# ---------------------------------------------------------------------------

def _run_ocr(task: Task, document: ParsedDocument, config: dict, selected_pages: set[int]) -> None:
    """按配置对扫描页做 OCR，并把识别结果作为段落并入文档。"""
    from . import ocr as ocr_pkg
    from .ocr import scanner as ocr_scanner

    mode = config.get("ocr_mode", constants.OCR_MODE_AUTO)
    if mode == constants.OCR_MODE_OFF:
        return

    doc = None
    try:
        doc = _open_document(document)

        # 1) 找出需要 OCR 的页面
        candidates: list[int] = []
        reasons: dict[int, str] = {}
        for page_index in sorted(selected_pages):
            page = doc.load_page(page_index)
            if mode == constants.OCR_MODE_ALWAYS:
                candidates.append(page_index)
                reasons[page_index] = "按设置对所有页面执行 OCR"
                continue
            needed, reason = ocr_scanner.page_needs_ocr(page)
            if needed:
                candidates.append(page_index)
                reasons[page_index] = reason

        if not candidates:
            task.log("未发现需要 OCR 的页面（都有文本层），跳过 OCR")
            return

        for page_index in candidates:
            task.log(f"第 {page_index + 1} 页：{reasons[page_index]}")

        # 2) 确保模型就绪（缺失则下载）
        lang = config.get("ocr_lang", constants.DEFAULT_CONFIG["ocr_lang"])
        det = config.get("ocr_det_version", constants.DEFAULT_CONFIG["ocr_det_version"])
        task.message = "正在准备 OCR 模型"
        task.stage = STAGE_OCR
        try:
            ocr_pkg.ensure_models(lang, det, log=task.log)
        except Exception as exc:
            task.log(f"OCR 模型准备失败：{exc}")
            document.warnings.append(
                f"OCR 模型未就绪（{exc}），已跳过 OCR；"
                f"可执行 python -m src.main --download-ocr {lang} 手动下载"
            )
            return

        # 3) 逐页识别
        engine = ocr_pkg.PPOCREngine(lang=lang, det_version=det,
                                     use_cls=bool(config.get("ocr_use_cls", False)))
        task.log(f"正在加载 OCR 模型（{ocr_pkg.REC_MODELS[lang]['label']}）…")
        engine.load(log=task.log)

        def on_progress(done: int, total: int, page_index: int) -> None:
            task.paragraph_done = done
            task.paragraph_total = total
            task.message = f"正在 OCR 第 {done}/{total} 页（原第 {page_index + 1} 页）"

        paragraphs, infos = ocr_scanner.scan_document(
            doc,
            candidates,
            engine,
            dpi=int(config.get("ocr_dpi") or 200),
            min_confidence=float(config.get("ocr_min_confidence") or 0.0),
            log=task.log,
            cancel_event=task.cancel_event,
            progress=on_progress,
            lang=lang,
        )

        if not paragraphs:
            task.log("OCR 未识别到任何文字")
            return

        pdf_parser.append_ocr_paragraphs(document, paragraphs)
        document.ocr_pages = [info.page for info in infos]
        document.ocr_info = [info.__dict__ for info in infos]
        task.log(
            f"OCR 共识别 {len(paragraphs)} 段（{len(infos)} 页），"
            f"已并入待翻译内容"
        )
        task.stats = {**task.stats, "ocr_pages": len(infos), "ocr_paragraphs": len(paragraphs)}
    except Exception as exc:
        logger.exception("OCR 阶段失败")
        task.log(f"OCR 失败，将只翻译文本层内容：{exc}")
        document.warnings.append(f"OCR 失败：{exc}")
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 阶段 1：翻译
# ---------------------------------------------------------------------------

def _translate(task: Task, document: ParsedDocument, config: dict) -> None:
    task.stage = STAGE_PARSING
    task.message = "正在筛选可翻译段落"

    page_range = config.get("page_range", "all")
    selected_pages = set(parse_page_range(page_range, document.page_count))

    # 先做 OCR（如果需要），把它识别出来的段落并进文档
    _run_ocr(task, document, config, selected_pages)

    # 按页面范围与可翻译规则重新标记
    pdf_parser.mark_translatable(
        document,
        target_lang=config.get("target_lang", "zh"),
        skip_headers_footers=bool(config.get("skip_headers_footers", True)),
        skip_repeated=bool(config.get("skip_repeated", True)),
        skip_non_translatable=bool(config.get("skip_non_translatable", True)),
    )

    paragraphs = [
        p for p in document.paragraphs
        if p.translatable and p.page in selected_pages
    ]
    # 不在范围内的段落标记为跳过，便于导出时说明
    for paragraph in document.paragraphs:
        if paragraph.page not in selected_pages and paragraph.translatable:
            paragraph.translatable = False
            paragraph.skip_reason = "不在所选页面范围"

    task.paragraph_total = len(paragraphs)
    task.page_total = len(selected_pages)
    task.log(
        f"共 {len(paragraphs)} 段需要翻译"
        f"（已跳过页眉页脚/非文本等 {len(document.paragraphs) - len(paragraphs)} 段）"
    )

    if not paragraphs:
        task.stats = {"paragraphs": 0, "translated": 0, "failed": 0, "skipped": len(document.paragraphs)}
        task.log("没有需要翻译的段落，可能该 PDF 是扫描件或已选定页范围内没有文字")
        task.message = "没有需要翻译的段落"
        return

    _check_cancel(task)
    task.stage = STAGE_TRANSLATING
    task.message = "正在调用大模型翻译"

    cache = TranslationCache(CACHE_DIR / "translations.json", enabled=True)

    def on_progress(done: int, total: int) -> None:
        task.batch_done = done
        task.batch_total = total
        # 段落完成数按批次比例估算，用于界面展示
        task.paragraph_done = int(task.paragraph_total * done / total) if total else 0
        task.message = f"正在翻译第 {done}/{total} 批"

    translator = Translator(config, cache=cache, log=task.log, progress=on_progress)
    try:
        stats = translator.translate_paragraphs(paragraphs, cancel_event=task.cancel_event)
    finally:
        translator.close()

    _check_cancel(task)

    translated = sum(1 for p in paragraphs if p.target)
    failed = sum(1 for p in paragraphs if not p.target)
    task.paragraph_done = task.paragraph_total
    task.batch_done = task.batch_total
    task.stats = {
        "paragraphs": len(paragraphs),
        "translated": translated,
        "failed": failed,
        "skipped": len(document.paragraphs) - len(paragraphs),
        "tokens": stats.prompt_tokens + stats.completion_tokens,
        "cache_hits": stats.cache_hits,
        "requests": stats.requests,
        "retries": stats.retries,
        "translate_seconds": round(stats.elapsed, 1),
        "numeric_rejected": stats.numeric_rejected,
        "numeric_retries": stats.numeric_retries,
        "numeric_mismatched": stats.numeric_mismatched,
    }
    task.log(
        f"翻译完成：成功 {translated} 段，失败 {failed} 段，"
        f"耗时 {stats.elapsed:.1f}s，消耗 token 约 {stats.prompt_tokens + stats.completion_tokens}"
    )
    if stats.numeric_mismatched:
        fixed = stats.numeric_mismatched - stats.numeric_rejected
        task.log(
            f"数值校验：初检 {stats.numeric_mismatched} 段译文的数字与原文不符，"
            f"经定向重试修正 {fixed} 段"
            + (f"，另有 {stats.numeric_rejected} 段无法修正、已回退为保留原文" if stats.numeric_rejected else "")
        )
    if failed:
        task.log(f"警告：有 {failed} 段未取得译文，这些段落将保留原文")


# ---------------------------------------------------------------------------
# 阶段 2：排版
# ---------------------------------------------------------------------------

def _typeset(task: Task, document: ParsedDocument, config: dict) -> None:
    if not config.get("export_pdf", True):
        task.log("已按设置跳过 PDF 排版")
        return

    _check_cancel(task)
    task.stage = STAGE_TYPESETTING
    task.message = "正在把译文写回 PDF"
    task.paragraph_done = 0

    out_dir = task_output_dir(task.task_id)
    stem = stem_of(document.filename)
    primary_mode = config.get("mode", constants.MODE_MONO)
    if primary_mode not in ALL_MODES:
        primary_mode = constants.MODE_MONO
    modes = [primary_mode] + [m for m in ALL_MODES if m != primary_mode]

    produced: dict[str, str] = {}

    for mode in modes:
        _check_cancel(task)

        def on_page(index: int, total: int) -> None:
            task.paragraph_done = index
            task.message = f"正在排版第 {index}/{total} 页（{MODE_LABELS[mode]}）"

        out_path = out_dir / f"{stem}_{mode}.pdf"
        doc = None
        try:
            doc = _open_document(document)
            stats = typesetter.typeset_document(
                doc,
                document.paragraphs,
                config,
                mode,
                log=task.log,
                cancel_event=task.cancel_event,
                progress=on_page,
            )
            # 排版统计要落到 task.stats：只打日志是不够的 —— 日志会被轮转、
            # 被 MAX_LOG_LINES 截断，而"这功能到底生效了没有"必须能被程序读到。
            # （踩过一次：验证脚本读 stats["cell_clamped"] 一直是 0，
            #   实际是键根本没透出来，却差点被当成"约束没生效"。）
            task.stats = {**task.stats, f"typeset_{mode}": {
                key: value for key, value in stats.items() if key != "fonts"
            }}
            # 内嵌的字体默认是完整字库（好几 MB），子集化后只保留用到的字形，
            # 输出体积可以缩小两个数量级。
            subset_ok = True
            try:
                doc.subset_fonts()
            except Exception:
                subset_ok = False
                logger.debug("字体子集化失败，跳过", exc_info=True)

            # garbage=4 会做对象去重与无用对象清理，保持 clean=False 以尽量
            # 不改动原有内容流，最大限度保留原始版式与矢量图形。
            doc.save(str(out_path), garbage=4, deflate=True, clean=False)
            produced[mode] = str(out_path)
            task.log(
                f"{MODE_LABELS[mode]} PDF 已生成：{out_path.name}"
                f"（{stats['pages']} 页 / {stats['blocks']} 个文本块）"
            )
            _warn_if_bloated(task, out_path, document, subset_ok)

            # 回读产出文件，确认译文真的都写进去了。
            # 排版库在极端情况下会"静默不写"（例如 insert_textbox 放不下时
            # 返回负数且一个字符都不写），必须主动校验，不能只看日志。
            check = verify_output(str(out_path), document.paragraphs)
            task.stats = {**task.stats, f"verify_{mode}": check}
            if check["missing"]:
                task.log(
                    f"警告：{MODE_LABELS[mode]} PDF 中有 {check['missing']}/{check['checked']} "
                    f"段译文未能写入（示例：{check['samples']}）"
                )
            else:
                task.log(f"{MODE_LABELS[mode]} PDF 校验通过：{check['checked']} 段译文全部写入")
            # 没翻译的段落必须原样留在页面上（页眉页脚、代码、取不到译文的段落）。
            # 擦除是按矩形删文字，矩形稍微压到邻居就会连它一起删掉，所以也要校验。
            if check.get("lost_original"):
                task.log(
                    f"警告：{MODE_LABELS[mode]} PDF 中有 {check['lost_original']} 段"
                    f"**未翻译**的原文被误删（示例：{check['lost_samples']}）"
                )
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    document.outputs = produced
    task.stats = {**task.stats, "outputs": produced}


def _open_document(document: ParsedDocument):
    import fitz

    doc = fitz.open(document.path)
    if doc.needs_pass:
        doc.close()
        raise RuntimeError("PDF 需要密码，无法排版")
    return doc


def _normalize_for_match(text: str) -> str:
    """比对用归一化：先做 NFKC，再只保留字母/数字/中日韩字符。

    做两件事：

    1. **丢掉所有标点与符号**。排版阶段会把字体里没有字形的符号
       （例如 Wingdings 的私有区项目符号 \\uf0a7）换成等价字符或直接去掉，
       逐字比对会因为标点差异误报"译文没写进去"。这个校验的目的是确认
       **整段内容有没有落盘**，而不是逐字一致，所以比对"文字骨架"更合适。

    2. **NFKC 归一化**。有些字库（Noto 系列、思源宋体 Heavy）把 CJK 兼容
       表意文字与统一汉字指向同一字形，MuPDF 反查时选中兼容码位：产出 PDF
       里画面完全正常，但提取出来的是 `U+F9DD` 而不是 `利`（U+5229）。
       不归一化就会把这种"码位差异"误报成"译文缺失"。NFKC 会把
       `U+F9DD` 映射回 `U+5229`，两者就能对上了。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    result: list[str] = []
    for char in text:
        if char.isalnum():
            result.append(char)
        elif "\u4e00" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff":
            result.append(char)
    return "".join(result)


def verify_output(path: str, paragraphs: list) -> dict:
    """回读译后 PDF，确认译文都写进去了、且该保留的原文还在。

    两类问题都要查，因为**两类都可能静默发生**：

    1. **译文没写进去**：排版库在矩形放不下时会不报错地什么都不画
       （`insert_textbox` 返回负数；`TextWriter` 遇到缺字形只画空白方块）。
    2. **不该删的原文被删了**：擦除是按矩形删除"与之相交的文字"，
       矩形稍微压到相邻段落就会把邻居一起删掉。没有译文的段落
       （页眉页脚、代码、取不到译文的段落）本该原样保留。

    Returns:
        {"checked": n, "missing": n, "samples": [...],
         "lost_original": n, "lost_samples": [...]}
    """
    import fitz

    targets = [
        (p, _normalize_for_match(p.target.replace("\n", " ")))
        for p in paragraphs
        if getattr(p, "target", "") and p.translatable
    ]
    keeps = [
        (p, _normalize_for_match(p.text.replace("\n", " ")))
        for p in paragraphs
        if p.translatable and not getattr(p, "target", "") and not getattr(p, "from_ocr", False)
    ]
    result = {
        "checked": len(targets), "missing": 0, "samples": [],
        "kept_checked": len(keeps), "lost_original": 0, "lost_samples": [],
    }
    if not targets and not keeps:
        return result

    try:
        doc = fitz.open(path)
    except Exception:
        logger.warning("校验时无法打开产出文件：%s", path, exc_info=True)
        return result

    try:
        page_text = {
            index: _normalize_for_match(doc[index].get_text())
            for index in range(doc.page_count)
        }
    finally:
        doc.close()

    def haystack_for(page_index: int) -> str:
        text = page_text.get(page_index, "")
        return text or "".join(page_text.values())

    for paragraph, needle in targets:
        # 单字符译文（例如 "—"）比对意义不大，跳过
        if len(needle) < 2:
            continue
        if needle not in haystack_for(paragraph.page):
            result["missing"] += 1
            if len(result["samples"]) < 3:
                result["samples"].append(paragraph.text.replace("\n", " ")[:24])

    for paragraph, needle in keeps:
        if len(needle) < 4:
            continue
        if needle not in haystack_for(paragraph.page):
            result["lost_original"] += 1
            if len(result["lost_samples"]) < 3:
                result["lost_samples"].append(paragraph.text.replace("\n", " ")[:24])

    return result


def _warn_if_bloated(task: Task, out_path, document: ParsedDocument, subset_ok: bool) -> None:
    """产出体积异常时给出可操作的提示。

    CFF/OTF 字库（例如 Noto Sans SC 的 .otf 版）会让 `subset_fonts()` **静默失败**，
    整份字库被原样嵌进 PDF，输出会从几十 KB 膨胀到好几 MB。这种情况必须提示，
    否则用户只会觉得"生成的文件怎么这么大"而不知道原因。
    """
    import os

    try:
        size = os.path.getsize(out_path)
        source = os.path.getsize(document.path)
    except OSError:
        return
    if size < 3 * 1024 * 1024 or size < source * 2:
        return
    tasks_hint = "建议在“嵌字字体”里指定一个 TrueType(.ttf) 中文字体"
    if not subset_ok:
        task.log(f"警告：字体子集化未成功，产出文件为 {size / 1048576:.1f} MB；{tasks_hint}")
    else:
        task.log(f"提示：产出文件为 {size / 1048576:.1f} MB，偏大；{tasks_hint}")



# ---------------------------------------------------------------------------
# 阶段 3：导出
# ---------------------------------------------------------------------------

def _export(task: Task, document: ParsedDocument, config: dict) -> None:
    _check_cancel(task)
    task.stage = STAGE_EXPORTING
    task.message = "正在导出结果文件"

    out_dir = task_output_dir(task.task_id)
    stem = stem_of(document.filename)
    stats = {
        "paragraphs": task.stats.get("paragraphs", 0),
        "translated": task.stats.get("translated", 0),
        "failed": task.stats.get("failed", 0),
    }
    files: list[dict] = []

    # PDF 变体
    for mode, path in (task.stats.get("outputs") or {}).items():
        files.append(_file_entry(path, kind=mode, task_id=task.task_id, label=MODE_LABELS.get(mode, mode)))

    # 对照稿
    if config.get("export_markdown", True):
        try:
            md_path = out_dir / f"{stem}_bilingual.md"
            exporter.export_markdown(document, str(md_path), config, stats, bilingual=True)
            files.append(_file_entry(str(md_path), kind="markdown", task_id=task.task_id, label="Markdown 对照稿"))
        except Exception:
            logger.exception("Markdown 导出失败")
            task.log("Markdown 对照稿导出失败（不影响 PDF 结果）")

        try:
            txt_path = out_dir / f"{stem}_translation.txt"
            exporter.export_text(document, str(txt_path))
            files.append(_file_entry(str(txt_path), kind="text", task_id=task.task_id, label="纯译文文本"))
        except Exception:
            logger.exception("纯文本导出失败")

        try:
            json_path = out_dir / f"{stem}_segments.json"
            exporter.export_segments_json(document, str(json_path), config, stats)
            files.append(_file_entry(str(json_path), kind="json", task_id=task.task_id, label="分段对照数据"))
        except Exception:
            logger.exception("JSON 导出失败")

    task.files = files
    task.status = STATUS_DONE
    task.stage = STAGE_FINISHED
    task.message = "全部完成"
    task.paragraph_done = task.paragraph_total
    task.log(f"全部完成，共生成 {len(files)} 个文件")


def _file_entry(path: str, kind: str, task_id: str, label: str = "") -> dict:
    import os

    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {
        "name": name,
        "kind": kind,
        "label": label or kind,
        "size": size,
        "url": f"/api/download/{task_id}/{name}",
    }


# ---------------------------------------------------------------------------
# 历史记录
# ---------------------------------------------------------------------------

def _record_history(task: Task) -> None:
    try:
        from ..shared.config_loader import append_history

        append_history(task.history_entry())
    except Exception:
        logger.debug("写入历史记录失败", exc_info=True)
