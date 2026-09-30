"""扫描页桥接：判断哪些页需要 OCR，并把识别结果变成可翻译的段落。

流程：

    页面 --渲染成位图(DPI)--> PP-OCR 识别 --> 文本行 --> 按行距聚合成段落
         --> Paragraph（坐标为 PDF 点，带 from_ocr 标记）

坐标换算：位图是按 DPI 渲染的，1 点 = dpi/72 像素，所以
    pdf_点坐标 = 像素坐标 * 72 / dpi
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

import fitz  # PyMuPDF

from ..models import ALIGN_LEFT, KIND_TEXT, Line, Paragraph, Span
from .engine import OCRResult, PPOCREngine

logger = logging.getLogger("OCRScanner")

# 判定"这页需要 OCR"时，文本层字符数下限
TEXT_LAYER_MIN_CHARS = 40


@dataclass
class PageOCRInfo:
    """单页的 OCR 情况。"""

    page: int
    needed: bool
    reason: str = ""
    text_chars: int = 0
    image_count: int = 0
    lines: int = 0
    elapsed: float = 0.0
    lang: str = ""


def page_text_length(page: "fitz.Page") -> int:
    """页面文本层里的有效字符数（忽略纯空白）。"""
    try:
        return len("".join(page.get_text().split()))
    except Exception:
        return 0


def page_image_area_ratio(page: "fitz.Page") -> float:
    """图片覆盖面积占页面的比例，用于区分"扫描页"和"纯文字页"。"""
    try:
        page_area = max(1.0, page.rect.width * page.rect.height)
        covered = 0.0
        for info in page.get_image_info():
            bbox = fitz.Rect(info["bbox"])
            covered += max(0.0, bbox.width) * max(0.0, bbox.height)
        return min(1.0, covered / page_area)
    except Exception:
        return 0.0


def page_needs_ocr(page: "fitz.Page", min_chars: int = TEXT_LAYER_MIN_CHARS) -> tuple[bool, str]:
    """判断某页是否需要 OCR。

    需要 OCR 的典型特征：**几乎没有文本层，但页面被图片覆盖**（扫描件/影印件）。
    """
    chars = page_text_length(page)
    ratio = page_image_area_ratio(page)
    if chars >= min_chars:
        return False, "已有文本层"
    if ratio < 0.35 and chars == 0:
        # 既没文字也没图片：空白页
        return False, "空白页"
    if ratio >= 0.35:
        return True, f"文本层仅 {chars} 字符、图片占页面 {ratio:.0%}，判定为扫描页"
    return False, f"文本层仅 {chars} 字符，但图片覆盖不足，跳过"


def render_page(page: "fitz.Page", dpi: int = 200) -> tuple["object", float]:
    """把页面渲染成 RGB numpy 数组。

    Returns:
        (RGB 数组, 每像素对应的 PDF 点数)
    """
    import numpy as np

    dpi = max(72, min(400, int(dpi)))
    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    return array.copy(), 72.0 / dpi


# ---------------------------------------------------------------------------
# 文本行 -> 段落
# ---------------------------------------------------------------------------

def group_lines(
    lines: list,
    pitch_ratio: float = 1.35,
    overlap_ratio: float = 0.45,
    short_line_ratio: float = 0.68,
    size_change_ratio: float = 0.35,
):
    """把 OCR 文本行按版面线索聚合成段落。

    用到三个线索，任何一个成立就断段：

    1. **行距变大**：同一段内行距基本恒定，段间会明显变大；
    2. **上一行明显偏短**：一段的最后一行通常填不满整栏，
       所以"短行之后"往往就是新段落的开始；
    3. **行高突变**：标题/图注的字号与正文不同。

    另外横向重叠太少、或左边距差异过大（缩进的新段落）也会断段。
    """
    if not lines:
        return []

    heights = sorted(max(1.0, line.box.height) for line in lines)
    median_height = heights[len(heights) // 2]

    widths = sorted(max(1.0, line.box.width) for line in lines)
    # 用 85 分位当"满行宽度"，比最大值稳（避免个别超长行拉高阈值）
    body_width = widths[min(len(widths) - 1, int(len(widths) * 0.85))]

    pitches = []
    for previous, current in zip(lines, lines[1:]):
        pitch = current.box.center_y - previous.box.center_y
        if pitch > 1.0:
            pitches.append(pitch)
    # 基线用 25 分位而不是中位数：段内行距是"最小"的那一档，
    # 如果取中位数，当文档里单行段落很多时，中位数会被段间距带偏，
    # 结果所有行都被并成一大段（实测踩过这个坑）。
    if pitches:
        ordered_pitches = sorted(pitches)
        baseline = ordered_pitches[max(0, int(len(ordered_pitches) * 0.25) - 1)]
    else:
        baseline = median_height * 1.4
    break_pitch = max(baseline * pitch_ratio, median_height * 1.25)

    groups: list[list] = [[lines[0]]]
    for previous, current in zip(lines, lines[1:]):
        pitch = current.box.center_y - previous.box.center_y

        overlap = min(previous.box.x1, current.box.x1) - max(previous.box.x0, current.box.x0)
        narrower = max(1.0, min(previous.box.width, current.box.width))
        overlap_ok = overlap / narrower >= overlap_ratio

        same_size = (
            abs(current.box.height - median_height) <= size_change_ratio * median_height
        )
        previous_is_short = previous.box.width < short_line_ratio * body_width

        same_paragraph = (
            0 < pitch <= break_pitch
            and overlap_ok
            and same_size
            and not previous_is_short
            and abs(current.box.x0 - previous.box.x0) <= 0.9 * median_height
        )
        if same_paragraph:
            groups[-1].append(current)
        else:
            groups.append([current])
    return groups


def lines_to_paragraphs(
    groups: list[list],
    page_index: int,
    point_scale: float,
    paragraph_id_start: int = 0,
) -> list[Paragraph]:
    """把行分组转换成 `Paragraph`（坐标单位换算成 PDF 点）。"""
    paragraphs: list[Paragraph] = []
    next_id = paragraph_id_start

    for group in groups:
        spans_by_line: list[Line] = []
        texts: list[str] = []
        for ocr_line in group:
            box = ocr_line.box
            bbox = (box.x0 * point_scale, box.y0 * point_scale,
                    box.x1 * point_scale, box.y1 * point_scale)
            spans_by_line.append(
                Line(
                    bbox=bbox,
                    spans=[Span(text=ocr_line.text, bbox=bbox, font="ocr",
                                size=max(4.0, box.height * point_scale * 0.78))],
                )
            )
            texts.append(ocr_line.text)

        if not texts:
            continue

        x0 = min(line.bbox[0] for line in spans_by_line)
        y0 = min(line.bbox[1] for line in spans_by_line)
        x1 = max(line.bbox[2] for line in spans_by_line)
        y1 = max(line.bbox[3] for line in spans_by_line)

        heights = [line.bbox[3] - line.bbox[1] for line in spans_by_line]
        typical_height = statistics.median(heights) if heights else 10.0
        # 扫描件里字形高度约等于字号的 0.78 倍（不含行距）
        font_size = max(4.0, typical_height * 0.78)

        confidences = [getattr(item, "confidence", 1.0) for item in group]
        paragraph = Paragraph(
            id=next_id,
            page=page_index,
            bbox=(x0, y0, x1, y1),
            lines=spans_by_line,
            text="\n".join(texts),
            kind=KIND_TEXT,
            size=font_size,
            align=ALIGN_LEFT,
            from_ocr=True,
        )
        paragraph.ocr_confidence = sum(confidences) / len(confidences) if confidences else 1.0
        paragraphs.append(paragraph)
        next_id += 1

    return paragraphs


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------

def scan_document(
    doc: "fitz.Document",
    page_indices: list[int],
    engine: PPOCREngine,
    dpi: int = 200,
    min_confidence: float = 0.0,
    paragraph_id_start: int = 0,
    log=None,
    cancel_event=None,
    progress=None,
    lang: str = "",
) -> tuple[list[Paragraph], list[PageOCRInfo]]:
    """对指定页面做 OCR，返回 (段落列表, 每页信息)。"""
    log = log or (lambda message: logger.info(message))
    all_paragraphs: list[Paragraph] = []
    infos: list[PageOCRInfo] = []
    next_id = paragraph_id_start

    for position, page_index in enumerate(page_indices, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        page = doc.load_page(page_index)
        chars = page_text_length(page)
        array, point_scale = render_page(page, dpi=dpi)
        result: OCRResult = engine.recognize_page(array, min_confidence=min_confidence)

        groups = group_lines(result.lines)
        paragraphs = lines_to_paragraphs(groups, page_index, point_scale, next_id)
        next_id += len(paragraphs)
        all_paragraphs.extend(paragraphs)

        info = PageOCRInfo(
            page=page_index,
            needed=True,
            reason=f"识别 {len(result.lines)} 行 / {len(paragraphs)} 段",
            text_chars=chars,
            image_count=len(page.get_images(full=True)),
            lines=len(result.lines),
            elapsed=result.elapsed,
            lang=result.used_lang or lang,
        )
        infos.append(info)
        log(
            f"第 {page_index + 1} 页 OCR 完成：识别 {len(result.lines)} 行，"
            f"合并为 {len(paragraphs)} 段（{result.elapsed:.1f}s）"
        )
        if progress is not None:
            try:
                progress(position, len(page_indices), page_index)
            except Exception:
                logger.debug("OCR 进度回调异常", exc_info=True)

    return all_paragraphs, infos
