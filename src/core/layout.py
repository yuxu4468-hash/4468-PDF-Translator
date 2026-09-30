"""版面分析：阅读顺序（XY-cut）、对齐方式、段落合并、页眉页脚识别。

这里是“版面还原”质量的关键。核心思路：

1. **XY-cut 阅读顺序**：递归寻找能把页面干净切开的最大空隙。横向空隙优先
   用于确定上下顺序，纵向空隙用于识别分栏；两者比较时取更宽的那个，因此
   双栏文档会把栏间留白识别为分栏，而跨栏标题则通过横向切分保证在正文之前。
2. **段落合并**：部分 PDF 生成器把每一行导出成独立文本块，需要按行距与
   左右边界把它们重新聚合成段落。
3. **页眉页脚/页码识别**：先按区域初筛，再统计跨页重复率确认。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter

from .models import (
    ALIGN_CENTER,
    ALIGN_JUSTIFY,
    ALIGN_LEFT,
    ALIGN_RIGHT,
    KIND_CODE,
    KIND_FOOTER,
    KIND_HEADER,
    KIND_PAGE_NUM,
    KIND_TEXT,
    KIND_TITLE,
    Line,
    Paragraph,
)
from ..shared import constants
from ..shared.text_utils import clean_text, normalize_for_key

# 用于识别页码的正则
_PAGE_NUM_PATTERNS = [
    re.compile(r"^\s*[-–—\[\(]?\s*\d{1,4}\s*[-–—\]\)]?\s*$"),
    re.compile(r"^\s*(?:page|p\.?)\s*\d{1,4}\s*(?:of\s*\d{1,4})?\s*$", re.IGNORECASE),
    re.compile(r"^\s*第?\s*\d{1,4}\s*页?\s*(?:/\s*共?\s*\d{1,4}\s*页?)?\s*$"),
    re.compile(r"^\s*\d{1,4}\s*/\s*\d{1,4}\s*$"),
]


def is_page_number(text: str) -> bool:
    """判断文本是否只是页码。"""
    stripped = clean_text(text)
    if not stripped or len(stripped) > 24:
        return False
    return any(pattern.match(stripped) for pattern in _PAGE_NUM_PATTERNS)


# ---------------------------------------------------------------------------
# 阅读顺序：XY-cut
# ---------------------------------------------------------------------------

def _best_vertical_gap(boxes: list[tuple[float, float, float, float]], min_gap: float):
    """扫描 x 方向，返回能把块集合干净切成左右两组的最大空隙。

    Returns:
        (gap_width, split_x, left_indices, right_indices) 或 None
    """
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i][0], boxes[i][1]))
    best = None
    max_x1 = boxes[order[0]][2]
    for pos in range(1, len(order)):
        index = order[pos]
        x0 = boxes[index][0]
        gap = x0 - max_x1
        if gap >= min_gap:
            left = order[:pos]
            right = order[pos:]
            if left and right and (best is None or gap > best[0]):
                best = (gap, max_x1 + gap / 2.0, left, right)
        max_x1 = max(max_x1, boxes[index][2])
    return best


def _best_horizontal_gap(boxes: list[tuple[float, float, float, float]], min_gap: float):
    """扫描 y 方向，返回能把块集合干净切成上下两组的最大空隙。"""
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i][1], boxes[i][0]))
    best = None
    max_y1 = boxes[order[0]][3]
    for pos in range(1, len(order)):
        index = order[pos]
        y0 = boxes[index][1]
        gap = y0 - max_y1
        if gap >= min_gap:
            top = order[:pos]
            bottom = order[pos:]
            if top and bottom and (best is None or gap > best[0]):
                best = (gap, max_y1 + gap / 2.0, top, bottom)
        max_y1 = max(max_y1, boxes[index][3])
    return best


def xy_cut(
    boxes: list[tuple[float, float, float, float]],
    page_width: float,
    depth: int = 0,
    max_depth: int = 6,
) -> tuple[list[int], int]:
    """递归 XY-cut，返回 (阅读顺序的块下标列表, 估计的分栏数)。

    选择规则：
      - 若纵向空隙足够宽（≥页面宽度 3%）且两侧各有 ≥2 个块，认定为分栏，优先按栏切分；
      - 否则在纵向/横向候选中取最宽者切分；
      - 都不满足或达到递归上限时，按 (y, x) 排序。
    """
    count = len(boxes)
    if count <= 1 or depth >= max_depth:
        return sorted(range(count), key=lambda i: (round(boxes[i][1], 1), boxes[i][0])), 1

    min_v_gap = max(6.0, 0.030 * page_width)
    # 横向最小空隙：至少要接近一行的高度，否则会在段落之间乱切
    heights = [max(1.0, boxes[i][3] - boxes[i][1]) for i in range(count)]
    median_h = statistics.median(heights) if heights else 10.0
    min_h_gap = max(3.0, 0.65 * median_h)

    vertical = _best_vertical_gap(boxes, min_v_gap)
    horizontal = _best_horizontal_gap(boxes, min_h_gap)

    prefer_vertical = (
        vertical is not None
        and len(vertical[2]) >= 2
        and len(vertical[3]) >= 2
        and vertical[0] >= 0.035 * page_width
    )

    if prefer_vertical:
        chosen = vertical
    elif vertical and horizontal:
        chosen = vertical if vertical[0] >= horizontal[0] else horizontal
    else:
        chosen = vertical or horizontal

    if chosen is None:
        return sorted(range(count), key=lambda i: (round(boxes[i][1], 1), boxes[i][0])), 1

    gap, _position, group_a, group_b = chosen
    sub_a = [boxes[i] for i in group_a]
    sub_b = [boxes[i] for i in group_b]
    order_a, cols_a = xy_cut(sub_a, page_width, depth + 1, max_depth)
    order_b, cols_b = xy_cut(sub_b, page_width, depth + 1, max_depth)

    # 判断这次切分是“分栏”还是“上下分段”
    is_column_split = chosen is vertical
    columns = (cols_a + cols_b) if is_column_split else max(cols_a, cols_b)

    return [group_a[i] for i in order_a] + [group_b[i] for i in order_b], columns


def order_blocks(
    boxes: list[tuple[float, float, float, float]],
    page_width: float,
    page_height: float = 0.0,
) -> tuple[list[int], int]:
    """对外接口：返回块的阅读顺序与分栏数。"""
    if not boxes:
        return [], 1
    if len(boxes) == 1:
        return [0], 1
    order, columns = _column_order(boxes, page_width, 0)
    return order, max(1, columns)


# ---------------------------------------------------------------------------
# 分栏检测：覆盖直方图 + 并行度校验
# ---------------------------------------------------------------------------

def _find_gutter(
    boxes: list[tuple[float, float, float, float]], page_width: float
) -> tuple[float, float] | None:
    """寻找竖向栏间留白，返回 (gutter_x0, gutter_x1)；找不到返回 None。

    做法是对 x 方向做"覆盖计数投影"：统计每个 x 位置被多少个文本块压住。
    真正的栏缝处这个计数会明显低于其它位置。相比"必须完全无覆盖"的做法，
    这样能容忍少数横跨栏缝的块（例如宽度超过一栏、又没到通栏的标题），
    这类块随后会被判定为 spanning 单独处理。
    """
    if len(boxes) < 4:
        return None

    left = min(box[0] for box in boxes)
    right = max(box[2] for box in boxes)
    text_width = right - left
    if text_width <= 20:
        return None

    resolution = 1.0
    count = int(text_width / resolution) + 2
    cover = [0] * count
    for x0, _y0, x1, _y1 in boxes:
        start = max(0, int((x0 - left) / resolution))
        end = min(count - 1, int((x1 - left) / resolution))
        for position in range(start, end + 1):
            cover[position] += 1

    # 栏缝只会出现在页面中部
    low = int(count * 0.20)
    high = max(low + 1, int(count * 0.80))
    window = cover[low:high]
    if len(window) < 5:
        return None

    minimum = min(window)
    ordered = sorted(window)
    median = ordered[len(ordered) // 2]

    # 判定条件一：栏缝处几乎不被覆盖（覆盖块数不超过总数的 15%）
    if minimum > max(1.0, 0.15 * len(boxes)):
        return None
    # 判定条件二：栏缝处覆盖度必须明显低于其余位置（排除单栏密排文本）
    if minimum > 0.5 * median:
        return None

    # 取覆盖度最低的最长连续段作为栏缝；并列时取最靠中间的
    threshold = minimum
    best = None
    run_start = None
    center = len(window) / 2.0
    for position in range(len(window) + 1):
        value = window[position] if position < len(window) else threshold + 1
        if value <= threshold:
            if run_start is None:
                run_start = position
            continue
        if run_start is not None:
            length = position - run_start
            distance = abs((run_start + position) / 2.0 - center)
            if best is None or (length, -distance) > (best[0], -best[1]):
                best = (length, distance, run_start, position)
            run_start = None

    if best is None:
        return None
    _length, _distance, span_start, span_end = best
    return (left + (low + span_start) * resolution, left + (low + span_end) * resolution)


def _vertical_overlap_ratio(
    group_a: list[tuple[float, float, float, float]],
    group_b: list[tuple[float, float, float, float]],
) -> float:
    """两组块的纵向重叠程度（用于确认它们确实是并排的两栏）。"""
    if not group_a or not group_b:
        return 0.0
    a0 = min(box[1] for box in group_a)
    a1 = max(box[3] for box in group_a)
    b0 = min(box[1] for box in group_b)
    b1 = max(box[3] for box in group_b)
    overlap = min(a1, b1) - max(a0, b0)
    span = min(a1 - a0, b1 - b0)
    if span <= 0:
        return 0.0
    return max(0.0, overlap) / span


def _column_order(
    boxes: list[tuple[float, float, float, float]], page_width: float, depth: int
) -> tuple[list[int], int]:
    """分栏阅读顺序（可递归处理三栏以上）。"""
    if len(boxes) <= 2 or depth >= 3:
        return sorted(range(len(boxes)), key=lambda i: (round(boxes[i][1], 1), boxes[i][0])), 1

    gutter = _find_gutter(boxes, page_width)
    if gutter is None:
        # 没有分栏，退回 XY-cut（同时也能给出分栏数估计）
        return xy_cut(boxes, page_width)

    gutter_start, gutter_end = gutter

    # 页眉/页脚/页码这类"页面级"的矮块不归属任何一栏，它们参与整页的上下顺序。
    # 否则居中页码会因为 x 位置落在左半边而被排到右栏内容之前。
    text_top = min(box[1] for box in boxes)
    text_bottom = max(box[3] for box in boxes)
    text_span = max(1.0, text_bottom - text_top)

    def is_zone_block(box) -> bool:
        height = box[3] - box[1]
        if height > 0.05 * text_span:
            return False
        return (
            box[1] <= text_top + 0.08 * text_span
            or box[3] >= text_bottom - 0.08 * text_span
        )

    left_group, right_group, spanning = [], [], []
    for index, box in enumerate(boxes):
        if is_zone_block(box):
            spanning.append(index)
        elif box[2] <= gutter_start + 0.5:
            left_group.append(index)
        elif box[0] >= gutter_end - 0.5:
            right_group.append(index)
        else:
            spanning.append(index)

    # 校验：两侧都要有足够的块，且纵向确实并行（否则是误判）
    if len(left_group) < 2 or len(right_group) < 2:
        return xy_cut(boxes, page_width)
    left_boxes = [boxes[i] for i in left_group]
    right_boxes = [boxes[i] for i in right_group]
    if _vertical_overlap_ratio(left_boxes, right_boxes) < 0.45:
        return xy_cut(boxes, page_width)

    order_left, cols_left = _column_order(left_boxes, page_width, depth + 1)
    order_right, cols_right = _column_order(right_boxes, page_width, depth + 1)
    sequence_left = [left_group[i] for i in order_left]
    sequence_right = [right_group[i] for i in order_right]

    # 通栏块按 y 顺序插入：它上方的左右栏内容先读完，再读它本身
    spanning_sorted = sorted(spanning, key=lambda i: boxes[i][1])
    result: list[int] = []
    li = ri = 0
    for block in spanning_sorted:
        top = boxes[block][1]
        while li < len(sequence_left) and boxes[sequence_left[li]][3] <= top:
            result.append(sequence_left[li])
            li += 1
        while ri < len(sequence_right) and boxes[sequence_right[ri]][3] <= top:
            result.append(sequence_right[ri])
            ri += 1
        result.append(block)

    result.extend(sequence_left[li:])
    result.extend(sequence_right[ri:])
    return result, cols_left + cols_right


# ---------------------------------------------------------------------------
# 对齐方式
# ---------------------------------------------------------------------------

def detect_alignment(lines: list[Line], bbox: tuple[float, float, float, float]) -> str:
    """根据各行左右边界推断段落对齐方式。"""
    usable = [line for line in lines if clean_text(line.text)]
    if len(usable) < 2:
        return ALIGN_LEFT

    width = max(1.0, bbox[2] - bbox[0])
    lefts = [line.bbox[0] for line in usable]
    rights = [line.bbox[2] for line in usable]
    centers = [(line.bbox[0] + line.bbox[2]) / 2.0 for line in usable]
    tol = max(1.5, 0.03 * width)

    left_spread = max(lefts) - min(lefts)
    right_spread = max(rights) - min(rights)
    center_spread = max(centers) - min(centers)

    # 居中：左右都不齐但中心齐
    if center_spread <= tol and left_spread > tol:
        return ALIGN_CENTER
    if right_spread <= tol and left_spread > tol:
        return ALIGN_RIGHT

    # 两端对齐：除最后一行外，各行右边界都接近段落右边界
    if len(usable) >= 3:
        body = usable[:-1]
        full = sum(1 for line in body if line.bbox[2] >= bbox[2] - max(2.0, 0.06 * width))
        if full >= len(body) * 0.8 and max(lefts) - min(lefts) <= tol:
            return ALIGN_JUSTIFY
    return ALIGN_LEFT


# ---------------------------------------------------------------------------
# 段落属性推断
# ---------------------------------------------------------------------------

def dominant_size(lines: list[Line]) -> float:
    """按字符数加权求段落主导字号。"""
    counter: Counter = Counter()
    for line in lines:
        for span in line.spans:
            weight = max(1, len(clean_text(span.text)))
            counter[round(span.size, 1)] += weight
    if not counter:
        return 10.0
    return float(counter.most_common(1)[0][0])


def dominant_color(lines: list[Line]) -> int:
    counter: Counter = Counter()
    for line in lines:
        for span in line.spans:
            counter[span.color] += max(1, len(clean_text(span.text)))
    if not counter:
        return 0
    return int(counter.most_common(1)[0][0])


def dominant_font(lines: list[Line]) -> tuple[str, bool]:
    counter: Counter = Counter()
    bold_weight = 0
    total = 0
    for line in lines:
        for span in line.spans:
            weight = max(1, len(clean_text(span.text)))
            counter[span.font] += weight
            total += weight
            if span.is_bold:
                bold_weight += weight
    font = counter.most_common(1)[0][0] if counter else ""
    return font, bool(total and bold_weight / total >= 0.5)


def line_spacing(lines: list[Line]) -> float:
    """估算段落内行距（相邻行基线间距的中位数）。"""
    if len(lines) < 2:
        return 0.0
    gaps = []
    ordered = sorted(lines, key=lambda l: l.bbox[1])
    for prev, curr in zip(ordered, ordered[1:]):
        gap = curr.bbox[1] - prev.bbox[1]
        if gap > 0.5:
            gaps.append(gap)
    return statistics.median(gaps) if gaps else 0.0


# ---------------------------------------------------------------------------
# 跨页重复内容（页眉页脚 / 水印）识别
# ---------------------------------------------------------------------------

def detect_repeated(
    paragraphs_by_page: dict[int, list[Paragraph]],
    page_count: int,
    zone_of=None,
):
    """标记跨页重复出现的段落。

    Args:
        paragraphs_by_page: 页码 -> 该页段落列表。
        page_count: 文档总页数。
        zone_of: 可选回调 `zone_of(paragraph) -> str | None`，返回该段落所在的
            页边区域类型（KIND_HEADER / KIND_FOOTER / KIND_PAGE_NUM）。
            位于页边区域的文本判定阈值更低（页眉页脚通常每页都出现）。

    Returns:
        (重复文本键集合, 空字典) —— 第二个返回值保留是为了向后兼容。
    """
    if page_count <= 0:
        return set(), {}

    occurrences: dict[str, set[int]] = {}
    zones: dict[str, set[str]] = {}

    for page_index, paragraphs in paragraphs_by_page.items():
        for paragraph in paragraphs:
            # 表格里的重复文本（表头行）不算"跨页重复"：那是表格结构，
            # 不是页眉页脚。把它们算进来的话，整张表的列标题都会被跳过。
            if getattr(paragraph, "in_table", False):
                continue
            key = normalize_for_key(paragraph.text)
            if not key or len(key) < 2 or len(key) > 300:
                continue
            occurrences.setdefault(key, set()).add(page_index)
            if zone_of is not None:
                zone = zone_of(paragraph)
                if zone:
                    zones.setdefault(key, set()).add(zone)

    repeated: set[str] = set()
    for key, pages in occurrences.items():
        if key in zones:
            # 页边区域的文字：出现在 2 页以上（或 25% 页面）就足以认定是页眉页脚
            need = max(2, int(page_count * 0.25))
        else:
            # 正文区域的重复：要求更高，避免把正文里偶尔重复的句子误删。
            # **至少 3 页**这个下限很关键：像"物种手册"这类文档，每种动物都有
            # 一组同名小标题（特征 / 受威胁状况 / 分布），它们会"每页都出现"。
            # 原来写成 max(2, page_count*0.6)，两页的文档就退化成"两页都有即算
            # 重复"，于是页面正中间的加粗小标题被判成页眉而**跳过翻译**，
            # 只有 2 个页面的样本本来也不足以支撑"这是页眉"的判断。
            need = max(3, int(page_count * 0.6))
        if len(pages) >= need:
            repeated.add(key)

    return repeated, {}


# ---------------------------------------------------------------------------
# 段落合并（把“一行一块”的 PDF 重新聚合成段落）
# ---------------------------------------------------------------------------

def _rule_segments(page, min_len_ratio: float = 0.06):
    """从页面矢量图里取出"够长的直线段"，分成 (横线, 竖线) 两组。

    坐标与文字同空间。`table_regions()`（判"这一片是表格"）与 `table_rows()`
    （切表格的行）都要读这些线，所以抽成一个函数 —— 一处 owner，
    判据（多长算线、什么算矩形边）只有一份。
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return [], []
    if not drawings:
        return [], []

    try:
        space = page.rect * page.derotation_matrix
    except Exception:
        space = page.rect
    min_len = max(30.0, float(space.width) * min_len_ratio)

    horizontals: list[tuple[float, float, float, float]] = []
    verticals: list[tuple[float, float, float, float]] = []
    for item in drawings:
        for sub in item.get("items", []):
            kind = sub[0]
            if kind == "l":
                p1, p2 = sub[1], sub[2]
                if abs(p2.y - p1.y) < 2 and abs(p2.x - p1.x) >= min_len:
                    horizontals.append((min(p1.x, p2.x), p1.y, max(p1.x, p2.x), p1.y))
                elif abs(p2.x - p1.x) < 2 and abs(p2.y - p1.y) >= min_len:
                    verticals.append((p1.x, min(p1.y, p2.y), p1.x, max(p1.y, p2.y)))
            elif kind == "re":
                rect = sub[1]
                if rect.width >= min_len:
                    horizontals.append((rect.x0, rect.y0, rect.x1, rect.y0))
                    horizontals.append((rect.x0, rect.y1, rect.x1, rect.y1))
                if rect.height >= min_len:
                    verticals.append((rect.x0, rect.y0, rect.x0, rect.y1))
                    verticals.append((rect.x1, rect.y0, rect.x1, rect.y1))
    return horizontals, verticals


def table_regions(page, min_rules: int = 3, min_len_ratio: float = 0.06):
    """从页面矢量图里识别表格区域，返回矩形列表（坐标与文字同空间）。

    为什么要单独识别表格：表格的**表头行**会在每一页重复出现，形态上和
    "页眉"完全一样（页面上方、逐页重复、内容一致），现有的"跨页重复"
    启发式会把它们判成页眉而跳过，于是整张表的列标题保持原文
    —— 实测一份 26 页文档因此漏掉 346 段（`Fläche KL` 一种就 63 次）。

    判据只用线条：表格由成组的横线构成（"三线表"只有横线，没有竖线，
    所以不强制要求竖线）。横线数量达到阈值即认为该页存在表格。
    """
    horizontals, verticals = _rule_segments(page, min_len_ratio)
    runs = horizontals + verticals
    if len(runs) < min_rules:
        return []
    xs = [value for run in runs for value in (run[0], run[2])]
    ys = [value for run in runs for value in (run[1], run[3])]
    return [(min(xs), min(ys), max(xs), max(ys))]


#: 横线聚类容差：同一条线的重复绘制/描边会差零点几 pt
RULE_Y_TOL = 1.5
#: 一条行带至少要有这么高才算"一行"（低于它的当成双重描边）
ROW_MIN_HEIGHT = 4.0


def _merge_rules(horizontals, y_tol: float = RULE_Y_TOL):
    """把横线按 y 聚成"行边界"，每条边界记下自己的左右端点。

    同一条线在 PDF 里常常画好几遍（描边 + 填充、分块绘制），不聚类就会切出
    一堆零高度的假行。
    """
    if not horizontals:
        return []
    ordered = sorted(horizontals, key=lambda item: item[1])
    merged: list[list[float]] = []
    for x0, y, x1, _y1 in ordered:
        if merged and abs(y - merged[-1][1]) <= y_tol:
            current = merged[-1]
            current[0] = min(current[0], x0)
            current[2] = max(current[2], x1)
            continue
        merged.append([x0, y, x1, y])
    return merged


def table_rows(page, min_rules: int = 3, min_len_ratio: float = 0.06,
               min_height: float = ROW_MIN_HEIGHT):
    """只用**横线**切出的表格行带，返回 (x0, y0, x1, y1) 列表。

    为什么需要它：三线表（只有横线、没有竖线）上 `find_tables()` **一个单元格都
    识别不出来**（实测 323 页里 0 个格），阶段 3 的单元格约束在这些表格上完全不
    触发。而横线本来就是表格的行边界 —— 这里只是把它们按 y 排好、两两成行。

    实测（全库 46 份）：能切行的页 160 页、共 893 条行带；落在行带里的可翻译
    段落 2679 段，其中 **801 段（29.9%）的行边界比原有机制更紧**，最极端的几段
    原来能"借"到 443pt（半页），收紧后只有 16pt。

    **行带只给上下边界、不给列边界。** 这对本项目的约束够用：约束只作用在
    **阅读方向（叠放轴）** 上，也就是"别往下压到下一行去"。竖排段落（叠放轴是
    横向）因此受益有限 —— 行带给不出列宽，这一点在文档里写明，不假装能解决。
    """
    horizontals, _verticals = _rule_segments(page, min_len_ratio)
    if len(horizontals) < min_rules:
        return []
    boundaries = _merge_rules(horizontals)
    if len(boundaries) < 2:
        return []

    rows: list[tuple[float, float, float, float]] = []
    for upper, lower in zip(boundaries, boundaries[1:]):
        if lower[1] - upper[1] < min_height:
            continue
        rows.append((
            min(upper[0], lower[0]), float(upper[1]),
            max(upper[2], lower[2]), float(lower[1]),
        ))
    return rows


def in_any_region(bbox, regions, ratio: float = 0.5) -> bool:
    """段落是否落在某个表格区域内（按面积重叠比例判断）。"""
    if not regions:
        return False
    x0, y0, x1, y1 = bbox
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if area <= 0:
        return False
    for rx0, ry0, rx1, ry1 in regions:
        ox = max(0.0, min(x1, rx1) - max(x0, rx0))
        oy = max(0.0, min(y1, ry1) - max(y0, ry0))
        if ox * oy >= area * ratio:
            return True
    return False


# ---------------------------------------------------------------------------
# 表格约束框（T1/T1b：把译文约束在自己的格子里 / 表格行里）
# ---------------------------------------------------------------------------

def table_cells(page, min_side: float = 2.0) -> list[tuple[float, float, float, float]]:
    """用 PyMuPDF 的表格识别取**单元格**矩形列表（坐标与文字同空间）。

    与 `table_regions()` 的分工：那个函数只回答"这一片是表格"（靠线条），
    用于避免表头被当成页眉；这个函数回答"这一格到哪儿为止"，用于把译文
    约束在格内。两者不互相替代。

    **覆盖率的实话**：实测一份 36 页的德文准则，有单元格的只有 25 页
    （共 2463 格），段落中心落在格内的只有 68%。三线表（只有横线、没有竖线）
    上 `find_tables()` 一个格都识别不出来 —— 那部分交给 `table_rows()`。

    识别失败不抛异常，返回空列表 —— 排版不能因为表格识别失败而整体失败。
    """
    try:
        finder = page.find_tables()
    except Exception:
        return []

    seen: set[tuple[int, int, int, int]] = set()
    cells: list[tuple[float, float, float, float]] = []
    for table in getattr(finder, "tables", None) or []:
        for row in getattr(table, "rows", None) or []:
            for cell in getattr(row, "cells", None) or []:
                if cell is None:
                    continue
                try:
                    values = tuple(float(value) for value in cell)
                except (TypeError, ValueError):
                    continue
                if len(values) != 4:
                    continue
                x0, y0, x1, y1 = values
                if x1 - x0 < min_side or y1 - y0 < min_side:
                    continue
                # 合并单元格会在多行里重复出现，去重避免白算
                key = (round(x0), round(y0), round(x1), round(y1))
                if key in seen:
                    continue
                seen.add(key)
                cells.append((x0, y0, x1, y1))
    return cells


def _contains_center(bbox, box) -> bool:
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def cell_for(bbox, cells) -> tuple[float, float, float, float] | None:
    """段落落在哪个单元格里（用段落**中心点**判定）。

    为什么用中心点而不是面积重叠：单元格是硬边界，一段文字必然属于唯一一格；
    面积重叠会让跨格（识别歪了的）段落同时"属于"两格，取哪一个都说不清。
    实测中心点判定的命中率 68%，与面积判定没有实质差别。
    """
    if not cells:
        return None
    for cell in cells:
        if _contains_center(bbox, cell):
            return cell
    return None


def row_for(bbox, rows) -> tuple[float, float, float, float] | None:
    """段落落在哪条表格行带里（同样用中心点判定）。

    取**最扁的一条**：行带是从整页横线两两切出来的，遇到"表 1 的底边到
    表 2 的顶边"这种跨表空档，也可能套住一段正文。真有多条都套住时，
    取高度最小的那条更符合"这一段属于哪一行"的直觉；
    而且就算选错，后果也只是"少借了点空间"，不会把段落压到比原框更小
    （`_clamp_to_box` 有下限保护）。
    """
    if not rows:
        return None
    hits = [row for row in rows if _contains_center(bbox, row)]
    if not hits:
        return None
    return min(hits, key=lambda row: row[3] - row[1])


def constraint_for(bbox, cells, rows) -> tuple[tuple[float, float, float, float] | None, str]:
    """段落该被约束在哪个框里，返回 `(框, 来源)`，来源为 `"cell"` / `"row"` / `""`。

    **单元格优先**：它比行带多了列边界，更精确。识别不到单元格时退到行带
    （三线表就靠这条）。这是"约束框"的**唯一** owner —— 调用方不再自己写
    优先级判断，避免两处逻辑各判一次、判出两个结果。
    """
    cell = cell_for(bbox, cells)
    if cell is not None:
        return cell, "cell"
    row = row_for(bbox, rows)
    if row is not None:
        return row, "row"
    return None, ""


def line_style(line: Line) -> tuple[bool, str, int]:
    """取一行的主导风格：`(是否粗体, 字体名, 四舍五入后的字号)`。

    按字符数加权，避免行首一个上标把整行判成别的字号。
    """
    counter: Counter = Counter()
    total = 0
    for span in line.spans:
        weight = max(1, len(clean_text(span.text)))
        counter[(span.is_bold, span.font, round(span.size, 1))] += weight
        total += weight
    if not counter:
        return False, "", 0
    (bold, font, size), _ = counter.most_common(1)[0]
    return bold, font, int(size * 10)


def _should_merge(prev: Paragraph, curr: Paragraph, column_left: float, column_right: float) -> bool:
    """判断两个相邻段落是否应合并成一段。"""
    if prev.kind != curr.kind or prev.column != curr.column:
        return False
    if prev.kind not in (KIND_TEXT,):
        return False
    if not prev.lines or not curr.lines:
        return False
    # 字号差异明显则不是同一段
    if abs(prev.size - curr.size) > max(0.6, 0.12 * prev.size):
        return False
    # 颜色不同（例如链接/高亮）不合并
    if prev.color != curr.color:
        return False
    # 字重不同不合并：这是"粗体小标题 + 正文"被排进同一个文本框的典型形态。
    # 如果合并回去，小标题就会丢掉粗体、也丢掉它自己的基线位置。
    if prev.bold != curr.bold:
        return False

    gap = curr.y0 - prev.y1
    height = max(prev.size, curr.size)
    if gap < -0.5 * height or gap > 0.85 * height:
        return False

    prev_last = max(prev.lines, key=lambda l: l.bbox[3])
    curr_first = min(curr.lines, key=lambda l: l.bbox[1])

    column_width = max(1.0, column_right - column_left)
    # 上一段最后一行必须“顶到右边”才说明话没说完
    reaches_right = prev_last.bbox[2] >= column_right - max(8.0, 0.06 * column_width)
    # 下一段第一行必须从左边开始
    starts_left = curr_first.bbox[0] <= column_left + max(6.0, 0.05 * column_width)
    return reaches_right and starts_left


def merge_paragraphs(paragraphs: list[Paragraph]) -> list[Paragraph]:
    """在同一栏内合并被拆散的段落，返回新的段落列表（保持阅读顺序）。"""
    if len(paragraphs) < 2:
        return paragraphs

    # 计算每栏的左右边界
    bounds: dict[tuple[int, int], tuple[float, float]] = {}
    for paragraph in paragraphs:
        key = (paragraph.page, paragraph.column)
        left, right = bounds.get(key, (1e9, -1e9))
        bounds[key] = (min(left, paragraph.x0), max(right, paragraph.x1))

    merged: list[Paragraph] = []
    for paragraph in paragraphs:
        if merged:
            prev = merged[-1]
            same_column = (prev.page, prev.column) == (paragraph.page, paragraph.column)
            if same_column:
                left, right = bounds.get((paragraph.page, paragraph.column), (0.0, 1.0))
                if _should_merge(prev, paragraph, left, right):
                    # 合并：行追加、范围扩展、字号取较大者
                    prev.lines = prev.lines + paragraph.lines
                    prev.bbox = (
                        min(prev.x0, paragraph.x0),
                        min(prev.y0, paragraph.y0),
                        max(prev.x1, paragraph.x1),
                        max(prev.y1, paragraph.y1),
                    )
                    prev.size = max(prev.size, paragraph.size)
                    continue
        merged.append(paragraph)

    return merged


# ---------------------------------------------------------------------------
# 段落分类
# ---------------------------------------------------------------------------

def classify_kind(
    paragraph: Paragraph,
    page_height: float,
    body_size: float,
    in_header_zone: bool,
    in_footer_zone: bool,
    repeated: bool = False,
    in_table: bool = False,
) -> str:
    """为段落打类型标签。

    重要：**处在页面上下边缘并不等于就是页眉页脚**。海报、折页、全出血排版的
    正文经常落在页面顶部或底部；这里把"是否在页边区域"只当作候选条件，
    必须同时满足"跨页重复"（或本身就是页码）才判定为页眉页脚，
    否则一律按正文处理，避免误删内容。
    """
    text = clean_text(paragraph.text)
    line_count = len([l for l in paragraph.lines if clean_text(l.text)])
    in_zone = in_header_zone or in_footer_zone

    # 表格里的文字**永远不是页眉页脚**：表格表头行天然会每页重复、
    # 也常常位于页面上方，形态上和页眉一样，只能靠"它在表格里"来区分。
    if in_table:
        return KIND_TITLE if (
            body_size > 0
            and paragraph.size >= body_size * 1.18
            and line_count <= 3
            and len(text) <= 120
        ) else KIND_TEXT

    # 页码：内容本身就能判定，不依赖重复性
    if in_zone and is_page_number(text):
        return KIND_PAGE_NUM

    # 页眉页脚：位于页边区域 **且** 跨页重复
    if in_zone and repeated:
        return KIND_HEADER if in_header_zone else KIND_FOOTER

    # 不在页边区域但跨页重复：水印、固定装饰文字，同样不翻译
    if repeated:
        return KIND_HEADER if paragraph.y1 <= page_height * 0.5 else KIND_FOOTER

    # 标题：字号显著大于正文、行数少、长度短
    if (
        body_size > 0
        and paragraph.size >= body_size * 1.18
        and line_count <= 3
        and len(text) <= 120
    ):
        return KIND_TITLE
    return KIND_TEXT


def median_body_size(paragraphs: list[Paragraph]) -> float:
    """估计正文主体字号（按字符数加权的中位数）。"""
    weighted: list[float] = []
    for paragraph in paragraphs:
        weight = max(1, min(400, len(clean_text(paragraph.text))))
        weighted.extend([paragraph.size] * max(1, weight // 40))
    if not weighted:
        return 10.0
    return statistics.median(weighted)
