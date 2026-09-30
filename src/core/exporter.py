"""导出器：把译文导成 Markdown / 纯文本对照稿。

PDF 用于阅读版式，Markdown/TXT 用于复制、检索与二次编辑，
两者互为补充。
"""

from __future__ import annotations

import json
import logging
import time

from .models import KIND_CODE, KIND_FOOTER, KIND_HEADER, KIND_PAGE_NUM, ParsedDocument
from ..shared import constants
from ..shared.text_utils import clean_text

logger = logging.getLogger("Exporter")


def _header_lines(document: ParsedDocument, config: dict, stats: dict) -> list[str]:
    provider = config.get("provider", "")
    model = config.get("model", "")
    target = config.get("target_lang", "")
    return [
        f"# {document.filename} —— 译文对照稿",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 页数：{document.page_count}",
        f"- 段落数：{stats.get('paragraphs', 0)}（成功翻译 {stats.get('translated', 0)} 段）",
        f"- 翻译服务：{provider} / {model}",
        f"- 目标语言：{target}",
        "",
        f"> 由 PDF-translater v{constants.APP_VERSION}（嵌字模式）自动生成，"
        f"原文与译文逐段对照。",
        "",
        "---",
        "",
    ]


def export_markdown(
    document: ParsedDocument, path: str, config: dict, stats: dict, bilingual: bool = True
) -> str:
    """导出 Markdown 对照稿，返回文件路径。"""
    skip_kinds = {KIND_HEADER, KIND_FOOTER, KIND_PAGE_NUM}
    lines = _header_lines(document, config, stats) if bilingual else []

    current_page = -1
    for paragraph in document.paragraphs:
        if paragraph.kind in skip_kinds:
            continue
        source = clean_text(paragraph.text.replace("\n", " "))
        target = clean_text(paragraph.target.replace("\n", " ")) if paragraph.target else ""
        if not source and not target:
            continue

        if paragraph.page != current_page:
            current_page = paragraph.page
            lines.append("")
            lines.append(f"## 第 {current_page + 1} 页")
            lines.append("")

        if paragraph.kind == KIND_CODE and not target:
            lines.append("```")
            lines.append(source)
            lines.append("```")
            lines.append("")
            continue

        if bilingual:
            lines.append(f"**原文**：{source}")
            lines.append("")
            if target:
                lines.append(f"**译文**：{target}")
            else:
                reason = paragraph.error or paragraph.skip_reason or "未翻译"
                lines.append(f"**译文**：（{reason}）")
            lines.append("")
        else:
            lines.append(target or source)
            lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    logger.info("已导出 Markdown：%s", path)
    return path


def export_text(document: ParsedDocument, path: str) -> str:
    """导出纯译文文本（按页分隔），便于直接阅读或喂给其它工具。"""
    skip_kinds = {KIND_HEADER, KIND_FOOTER, KIND_PAGE_NUM, KIND_CODE}
    lines: list[str] = []
    current_page = -1
    for paragraph in document.paragraphs:
        if paragraph.kind in skip_kinds:
            continue
        target = clean_text(paragraph.target.replace("\n", " ")) if paragraph.target else ""
        if not target:
            continue
        if paragraph.page != current_page:
            current_page = paragraph.page
            lines.append("")
            lines.append(f"===== 第 {current_page + 1} 页 =====")
            lines.append("")
        lines.append(target)
        lines.append("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).strip() + "\n")
    logger.info("已导出纯文本：%s", path)
    return path


def export_segments_json(document: ParsedDocument, path: str, config: dict, stats: dict) -> str:
    """导出结构化对照数据（JSON），方便核查漏译或做二次加工。"""
    payload = {
        "filename": document.filename,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "provider": config.get("provider"),
            "model": config.get("model"),
            "target_lang": config.get("target_lang"),
            "mode": config.get("mode"),
        },
        "stats": stats,
        "segments": [
            {
                "id": paragraph.id,
                "page": paragraph.page + 1,
                "kind": paragraph.kind,
                "source": clean_text(paragraph.text.replace("\n", " ")),
                "target": clean_text(paragraph.target.replace("\n", " ")) if paragraph.target else "",
                "translatable": paragraph.translatable,
                "skip_reason": paragraph.skip_reason,
                "error": paragraph.error,
                "bbox": [round(v, 2) for v in paragraph.bbox],
            }
            for paragraph in document.paragraphs
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    logger.info("已导出对照 JSON：%s", path)
    return path
