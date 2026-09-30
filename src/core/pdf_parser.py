"""PDF 解析：把 PDF 转成结构化的中间表示。

只依赖 PyMuPDF（fitz），不引入 OCR / 版面模型等重型依赖，因此启动快、体积小。

流程（逐页）：
    get_text("dict") -> 文本块/行/span -> XY-cut 阅读顺序 -> 段落属性推断
    -> 跨页统计（页眉页脚/重复内容）-> 段落合并 -> 可翻译性判定
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter, defaultdict

import fitz  # PyMuPDF

from . import layout
from .models import (
    KIND_CODE,
    KIND_EMPTY,
    KIND_FOOTER,
    KIND_HEADER,
    KIND_PAGE_NUM,
    KIND_TEXT,
    KIND_TITLE,
    Line,
    PageInfo,
    Paragraph,
    ParsedDocument,
    Span,
)
from ..shared import constants
from ..shared.text_utils import clean_text, has_target_language, is_translatable, looks_like_code

logger = logging.getLogger("PDFParser")


# ---------------------------------------------------------------------------
# 低层：把 fitz 的 dict 输出转成 IR
# ---------------------------------------------------------------------------

def page_space(page: "fitz.Page") -> "fitz.Rect":
    """返回该页的**文字/绘制坐标空间**（未旋转的 cropbox）。

    这是本项目里一个关键约定：PyMuPDF 的
    `get_text()`、`insert_textbox()`、`add_redact_annot()`、`TextWriter`
    全部按**未旋转**坐标工作，只有 `page.rect` 是旋转之后的视觉矩形。
    对 `/Rotate 90|270` 的页面（横排表格、折页很常见）两者宽高互换，
    一旦混用就会静默丢掉整段译文。

    用 `page.rect * page.derotation_matrix` 取，顺带正确处理 cropbox 原点偏移。
    """
    try:
        return page.rect * page.derotation_matrix
    except Exception:
        return page.rect


def _build_lines(raw_block: dict, page_height: float) -> list[Line]:
    """把 fitz 的 block 字典转成 Line 列表（已丢弃空白行）。"""
    lines: list[Line] = []
    for raw_line in raw_block.get("lines", []):
        spans: list[Span] = []
        for raw_span in raw_line.get("spans", []):
            text = raw_span.get("text", "")
            if not text:
                continue
            spans.append(
                Span(
                    text=text,
                    bbox=tuple(float(v) for v in raw_span.get("bbox", (0, 0, 0, 0))),
                    font=raw_span.get("font", "") or "",
                    size=float(raw_span.get("size", 10.0) or 10.0),
                    flags=int(raw_span.get("flags", 0) or 0),
                    color=int(raw_span.get("color", 0) or 0),
                    origin=tuple(float(v) for v in raw_span.get("origin", (0, 0))),
                )
            )
        if not spans:
            continue
        if not clean_text("".join(span.text for span in spans)):
            continue
        direction = raw_line.get("dir", (1.0, 0.0)) or (1.0, 0.0)
        bbox = tuple(float(v) for v in raw_line.get("bbox", raw_block.get("bbox", (0, 0, 0, 0))))
        # 行 bbox 有时会越出页面，做一次裁剪保护
        bbox = (bbox[0], max(0.0, bbox[1]), bbox[2], min(page_height, bbox[3]))
        lines.append(
            Line(
                bbox=bbox,
                spans=spans,
                wmode=int(raw_line.get("wmode", 0) or 0),
                direction=(float(direction[0]), float(direction[1])),
            )
        )
    return lines


def _rotation_of(lines: list[Line]) -> float:
    """由行方向向量判断文本的排版方向，返回 0 / 180 / ±90（度）。

    三种情况要分清：

    - `dir ≈ (1, 0)`  横排，不需要旋转；
    - `dir ≈ (-1, 0)` 基线从右端开始、向左推进，是**真正的 180° 倒排**。
      折页（Leporello）背面的相邻面板、双面印刷的某些版式就是故意倒排的，
      这样折起来之后才读得正。判断依据很可靠：这类行的 span origin.x 恰好
      落在 bbox 的右边缘（实测某本 InDesign 折页有 14/16 段如此）。
    - `|dy| > |dx|`   竖排文本，按 ±90 处理。
    """
    for line in lines:
        dx, dy = line.direction
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue
        if abs(dy) > abs(dx):
            return round(math.degrees(math.atan2(dy, dx)), 2)
        if dx < 0:
            return 180.0
    return 0.0


def _assign_columns(paragraphs: list[Paragraph], page_width: float) -> int:
    """按"栏缝"把段落分到各栏，返回栏数。

    做法是**覆盖计数投影**：统计每个 x 位置被多少个文本块压住，覆盖度极低的
    连续区段就是栏缝；栏缝之间的区间就是一栏。

    这比"按 x 起点依次扫、遇到空隙就换栏"可靠得多。后者在**栏宽不一致**时
    会失效：一个较宽的块（x1 伸进下一栏起点）会把 max_x1 顶过去，导致后面的
    栏全被并进同一栏。折页、多栏画册这类版面很容易踩到，而且后果不只是
    阅读顺序错乱 —— 所有段落被当成"同一栏"之后，段落合并步骤会把不同栏的
    文字拼成一段（实测拼出过 2046pt 宽的怪块）。
    """
    if not paragraphs:
        return 1

    left = min(p.x0 for p in paragraphs)
    right = max(p.x1 for p in paragraphs)
    text_width = right - left
    if text_width <= 40:
        for paragraph in paragraphs:
            paragraph.column = 0
        return 1

    resolution = 2.0
    count = int(text_width / resolution) + 2
    cover = [0] * count
    for paragraph in paragraphs:
        start = max(0, int((paragraph.x0 - left) / resolution))
        end = min(count - 1, int((paragraph.x1 - left) / resolution))
        for position in range(start, end + 1):
            cover[position] += 1

    # 容许多达 8% 的块跨栏（例如通栏标题），所以不要求覆盖度严格为 0
    threshold = max(1, int(0.08 * len(paragraphs)))
    min_gap = max(10.0, 0.008 * page_width)

    gutters: list[tuple[int, int]] = []
    run_start = None
    for position in range(count):
        if cover[position] <= threshold:
            if run_start is None:
                run_start = position
            continue
        if run_start is not None:
            if (position - run_start) * resolution >= min_gap and run_start > 0:
                gutters.append((run_start, position))
            run_start = None

    if not gutters:
        for paragraph in paragraphs:
            paragraph.column = 0
        return 1

    # 栏缝之间的区间即各栏
    bounds: list[tuple[float, float]] = []
    cursor = left
    for gutter_start, gutter_end in gutters:
        edge = left + gutter_start * resolution
        if edge - cursor > 4:
            bounds.append((cursor, edge))
        cursor = left + gutter_end * resolution
    if right - cursor > 4:
        bounds.append((cursor, right))
    if len(bounds) <= 1:
        for paragraph in paragraphs:
            paragraph.column = 0
        return 1

    starts = [item[0] for item in bounds]
    for paragraph in paragraphs:
        center = (paragraph.x0 + paragraph.x1) / 2.0
        # 找到中心所在的栏；落在栏缝里就归到最近的一栏
        column = 0
        for index, (start, end) in enumerate(bounds):
            if start <= center <= end:
                column = index
                break
            if center > end:
                column = index
        paragraph.column = column
    return len(bounds)


# ---------------------------------------------------------------------------
# 单页解析
# ---------------------------------------------------------------------------

def _split_lines_by_cluster(lines: list[Line], page_width: float) -> list[list[Line]]:
    """把一个文本块的行按水平位置拆成若干组。

    极少数 PDF（尤其是 InDesign 做的折页/多栏版面）会把**分处不同栏**的文字
    归进同一个"块"，块的最小外接矩形因此横跨整页。这种块绝不能当成一段处理：
    擦除矩形会横扫整页，把别的栏的文字一起抹掉（实测一份折页上出现过
    2046pt 宽的块）。

    做法：按行起点排序后依次归组，一旦"下一行的起点"离"当前组已覆盖到的最右端"
    超过一个阈值，就认为跨到另一个栏了，另起一组。
    正常情况下同段的行互相重叠、起点也接近，永远不会被拆开。
    """
    if len(lines) <= 1:
        return [lines]

    min_gap = max(15.0, 0.015 * page_width)
    ordered = sorted(lines, key=lambda line: (line.bbox[0], line.bbox[1]))
    clusters: list[list[Line]] = [[ordered[0]]]
    rightmost = ordered[0].bbox[2]

    for line in ordered[1:]:
        if line.bbox[0] - rightmost > min_gap:
            clusters.append([line])
            rightmost = line.bbox[2]
        else:
            clusters[-1].append(line)
            rightmost = max(rightmost, line.bbox[2])

    # 组内按 y 重新排好（阅读顺序）
    for cluster in clusters:
        cluster.sort(key=lambda line: line.bbox[1])
    return clusters


def _split_lines_by_style(lines: list[Line], max_groups: int = 4) -> list[list[Line]]:
    """把一个文本块的行按**行级风格**（粗体/字号）切成若干组。

    有些排版软件会把"加粗小标题"和它下面的正文放进**同一个文本框**，
    PyMuPDF 于是把它们报成同一个 block。如果照单全收当成一段：

    - 段落的主导字重按字符数投票，正文占多数 → 整段被判成**常规字重**，
      小标题的**粗体**就丢了（这是"嵌字"里最容易被一眼看出来的瑕疵）；
    - 整段按**首行基线**重排，小标题原本独立的一行位置也没了。

    实测某份折页里 `Merkmale` / `Gefährdung und Schutzstatus` / `Verbreitung`
    三个小标题都是 `Calibri-Bold flags=16`，却和正文同块，共 23 处。

    但切分是**有风险**的：切错地方会把一句话从中间截断，翻译质量反而变差。
    所以加了三道约束，任何一条不满足就整块原样返回：

    1. 组数不超过 `max_groups`（再多就说明是表格那种隔行换风格的形态）；
    2. 相邻两组必须**在粗体或字号上有差异** —— 仅仅换了字体名不算标题
       （实测 `Ein Informationsblatt der Deutschen` / `Gesellschaft für …`
       这两行只是字体不同，照切会把副标题从中间断开）；
    3. 相邻两组里至少有一组**像标题**：不超过 3 行，且最宽的一行不超过
       整块宽度的 72%。
    """
    if len(lines) < 2:
        return [lines]

    ordered = sorted(lines, key=lambda line: line.bbox[1])
    styles = [layout.line_style(line) for line in ordered]

    groups: list[list[Line]] = [[ordered[0]]]
    for index in range(1, len(ordered)):
        if styles[index] == styles[index - 1]:
            groups[-1].append(ordered[index])
        else:
            groups.append([ordered[index]])

    if len(groups) < 2 or len(groups) > max_groups:
        return [lines]

    block_width = max((line.bbox[2] - line.bbox[0]) for line in ordered) or 1.0

    def heading_like(group: list[Line]) -> bool:
        if len(group) > 3:
            return False
        widest = max(line.bbox[2] - line.bbox[0] for line in group)
        return widest <= block_width * 0.72

    for previous, following in zip(groups, groups[1:]):
        style_a = layout.line_style(previous[0])
        style_b = layout.line_style(following[0])
        # 约束 2：粗体与字号都没变 → 只是换了字体，不算标题
        if style_a[0] == style_b[0] and style_a[2] == style_b[2]:
            return [lines]
        # 约束 3：总得有一侧像标题
        if not (heading_like(previous) or heading_like(following)):
            return [lines]

    return groups


def parse_page(page: "fitz.Page", page_index: int, paragraph_id_start: int):

    """解析一页，返回 (PageInfo, Paragraph 列表, 下一个可用 id)。"""
    # 坐标空间：PyMuPDF 的文字坐标**与绘制坐标**都在"未旋转的 cropbox 空间"里，
    # 而 `page.rect` 反映 /Rotate。两者在带旋转的页面上并不相同
    # （实测 /Rotate 90 的一页：page.rect = 595x420，文字却分布在 420x595 内）。
    # 混用会导致行 bbox 被截断、可用空间算成 0、译文整段丢失，所以统一用前者。
    space = page_space(page)
    page_width, page_height = float(space.width), float(space.height)
    # 先从矢量图里识别表格区域：表格的表头行会每页重复、又常位于页面上方，
    # 形态与页眉完全一致，必须靠"它在表格里"才能避免被当成页眉跳过。
    tables = layout.table_regions(page)
    # 表格**约束框**：单元格优先（多了列边界，更精确），识别不到时退到
    # 由横线切出的**行带** —— 三线表（只有横线）上 find_tables() 一个格都
    # 识别不出来，实测 323 页里 0 个格，那部分只能靠行带。
    cells = layout.table_cells(page)
    rows = layout.table_rows(page)
    info = PageInfo(
        page=page_index,
        width=page_width,
        height=page_height,
        rotation=int(page.rotation or 0),
        tables=tables,
        cells=cells,
        rows=rows,
    )

    raw = page.get_text("dict")
    blocks: list[list[Line]] = []
    boxes = []
    for raw_block in raw.get("blocks", []):
        if raw_block.get("type", 0) != 0:
            continue  # 图片块跳过
        lines = _build_lines(raw_block, page_height)
        if not lines:
            continue
        # 把"横跨多栏"的怪块拆开，避免擦除时误伤别的栏；
        # 再把"粗体小标题 + 正文"同块的形态按行级风格拆开，让标题拿回粗体与基线
        for cluster in _split_lines_by_cluster(lines, page_width):
            for group in _split_lines_by_style(cluster):
                bbox = (
                    min(line.bbox[0] for line in group),
                    min(line.bbox[1] for line in group),
                    max(line.bbox[2] for line in group),
                    max(line.bbox[3] for line in group),
                )
                if bbox[2] - bbox[0] <= 0.5 or bbox[3] - bbox[1] <= 0.5:
                    continue
                blocks.append(group)
                boxes.append(bbox)


    if not blocks:
        info.paragraphs = 0
        return info, [], paragraph_id_start

    order, columns = layout.order_blocks(boxes, page_width)
    info.columns = columns

    paragraphs: list[Paragraph] = []
    next_id = paragraph_id_start
    for position in order:
        lines = blocks[position]
        bbox = boxes[position]
        text = "\n".join(line.text for line in lines)
        paragraph = Paragraph(
            id=next_id,
            page=page_index,
            bbox=bbox,
            lines=lines,
            text=text,
            size=layout.dominant_size(lines),
            color=layout.dominant_color(lines),
            align=layout.detect_alignment(lines, bbox),
            rotation=_rotation_of(lines),
        )
        paragraph.font, paragraph.bold = layout.dominant_font(lines)
        paragraph.in_table = layout.in_any_region(bbox, tables)
        paragraph.constraint_bbox, paragraph.constraint_kind = layout.constraint_for(
            bbox, cells, rows
        )
        paragraphs.append(paragraph)
        next_id += 1

    _assign_columns(paragraphs, page_width)
    info.paragraphs = len(paragraphs)
    return info, paragraphs, next_id


# ---------------------------------------------------------------------------
# 整篇解析
# ---------------------------------------------------------------------------

def parse_pdf(path: str, file_id: str, filename: str = "") -> ParsedDocument:
    """解析整个 PDF 文件。"""
    document = ParsedDocument(
        file_id=file_id,
        path=str(path),
        filename=filename or str(path).split("\\")[-1].split("/")[-1],
    )

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise RuntimeError(f"无法打开 PDF：{exc}") from exc

    if doc.needs_pass:
        document.encrypted = True
        document.close()
        doc.close()
        raise RuntimeError("该 PDF 已加密，请先用其它工具去除密码保护。")

    document.doc = doc
    document.encrypted = False
    if doc.is_repaired:
        document.warnings.append("PDF 结构有轻微损坏，已自动修复后解析。")

    all_paragraphs: list[Paragraph] = []
    next_id = 0
    for page_index in range(doc.page_count):
        try:
            page = doc.load_page(page_index)
            info, paragraphs, next_id = parse_page(page, page_index, next_id)
        except Exception as exc:  # 单页失败不应导致整篇失败
            logger.warning("第 %d 页解析失败：%s", page_index + 1, exc)
            document.warnings.append(f"第 {page_index + 1} 页解析失败：{exc}")
            info = PageInfo(page=page_index)
        document.pages.append(info)
        all_paragraphs.extend(paragraphs)

    document.paragraphs = all_paragraphs
    _post_process(document)

    # 解析完成后立即释放文件句柄：后续预览/排版都按需重新打开，
    # 避免同时上传很多文档时句柄泄漏。
    document.doc = None
    try:
        doc.close()
    except Exception:
        pass

    logger.info(
        "解析完成：%s 共 %d 页 / %d 段落（可翻译 %d 段）",
        document.filename,
        document.page_count,
        len(document.paragraphs),
        len(document.translatable_paragraphs()),
    )
    return document


def _post_process(document: ParsedDocument) -> None:
    """整篇级别的后处理：分类、重复识别、合并、可翻译性判定。"""
    from ..shared.text_utils import normalize_for_key

    paragraphs = document.paragraphs
    if not paragraphs:
        return

    body_size = layout.median_body_size(paragraphs)

    # 1) 先算出每段是否落在页眉/页脚区域（仅是"候选"，不直接定型）
    def zone_of(paragraph: Paragraph) -> str | None:
        page_info = document.pages[paragraph.page]
        height = page_info.height or 842.0
        if paragraph.y1 <= height * constants.HEADER_ZONE:
            return KIND_HEADER
        if paragraph.y0 >= height * constants.FOOTER_ZONE:
            return KIND_FOOTER
        return None

    # 2) 跨页重复内容统计（水印、页眉页脚、固定装饰文字）
    by_page: dict[int, list[Paragraph]] = defaultdict(list)
    for paragraph in paragraphs:
        by_page[paragraph.page].append(paragraph)
    repeated, _ = layout.detect_repeated(by_page, document.page_count, zone_of=zone_of)

    # 3) 定稿分类：页边区域只是候选，必须"跨页重复"或"本身是页码"才降级为页眉页脚
    for paragraph in paragraphs:
        page_info = document.pages[paragraph.page]
        height = page_info.height or 842.0
        zone = zone_of(paragraph)
        paragraph.kind = layout.classify_kind(
            paragraph,
            height,
            body_size,
            zone == KIND_HEADER,
            zone == KIND_FOOTER,
            repeated=normalize_for_key(paragraph.text) in repeated,
            in_table=paragraph.in_table,
        )

    # 4) 段落合并（可选）
    merged = layout.merge_paragraphs(paragraphs)
    if len(merged) != len(paragraphs):
        logger.info("段落合并：%d -> %d", len(paragraphs), len(merged))
    document.paragraphs = merged


def mark_translatable(
    document: ParsedDocument,
    target_lang: str = "zh",
    skip_headers_footers: bool = True,
    skip_repeated: bool = True,
    skip_non_translatable: bool = True,
) -> None:
    """根据配置标记每个段落是否参与翻译（就地修改）。"""
    for paragraph in document.paragraphs:
        paragraph.translatable = True
        paragraph.skip_reason = ""

        text = clean_text(paragraph.text.replace("\n", " "))

        if not text:
            paragraph.translatable = False
            paragraph.kind = KIND_EMPTY
            paragraph.skip_reason = "空白"
            continue

        if skip_headers_footers and paragraph.kind in (KIND_HEADER, KIND_FOOTER, KIND_PAGE_NUM):
            paragraph.translatable = False
            paragraph.skip_reason = {
                KIND_HEADER: "页眉",
                KIND_FOOTER: "页脚",
                KIND_PAGE_NUM: "页码",
            }[paragraph.kind]
            continue

        if skip_non_translatable:
            if looks_like_code(text):
                paragraph.translatable = False
                paragraph.kind = KIND_CODE
                paragraph.skip_reason = "代码/公式"
                continue
            if not is_translatable(text):
                paragraph.translatable = False
                paragraph.kind = KIND_CODE
                paragraph.skip_reason = "非文本内容"
                continue
            if has_target_language(text, target_lang):
                paragraph.translatable = False
                paragraph.skip_reason = "已是目标语言"
                continue

        paragraph.kind = paragraph.kind if paragraph.kind != KIND_EMPTY else KIND_TEXT


# ---------------------------------------------------------------------------
# 诊断信息
# ---------------------------------------------------------------------------

def document_statistics(document: ParsedDocument) -> dict:
    """汇总文档统计信息，用于界面展示。"""
    kinds = Counter(paragraph.kind for paragraph in document.paragraphs)
    sizes = [paragraph.size for paragraph in document.paragraphs if paragraph.translatable]
    from_ocr = sum(1 for p in document.paragraphs if p.from_ocr)
    return {
        "pages": document.page_count,
        "paragraphs": len(document.paragraphs),
        "translatable": len(document.translatable_paragraphs()),
        "characters": sum(len(clean_text(p.text)) for p in document.translatable_paragraphs()),
        "kinds": dict(kinds),
        "columns": max((page.columns for page in document.pages), default=1),
        "body_size": round(statistics.median(sizes), 1) if sizes else 0.0,
        "from_ocr": from_ocr,
        "ocr_pages": len(getattr(document, "ocr_pages", []) or []),
        "warnings": list(document.warnings),
    }


def append_ocr_paragraphs(document: ParsedDocument, paragraphs: list[Paragraph]) -> None:
    """把 OCR 得到的段落并入文档，并按阅读顺序重新排好。"""
    if not paragraphs:
        return
    document.paragraphs.extend(paragraphs)
    document.paragraphs.sort(key=lambda p: (p.page, round(p.y0, 1), p.x0))
    # 重新编号，保证 id 连续唯一
    for index, paragraph in enumerate(document.paragraphs):
        paragraph.id = index
    _post_process(document)
