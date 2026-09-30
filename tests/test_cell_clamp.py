"""表格单元格内约束回归测试（阶段 3／T1）。

锁住四件事：

  1. **网格路径**：能识别出单元格时，长译文的可用高度被收紧到格子边界内，
     不会压到下一行。
  2. **退化路径**：三线表（只有横线）识别不出单元格，原有机制照旧生效，
     译文仍然不会压到下一行 —— 本项目的实际情况是**退化路径才是主力**。
  3. **只收紧、不放大**：没有单元格信息（`constraint_bbox is None`）或格子比原段落框
     还小时，可用高度**一点不变** —— 误判的代价是把一段正常译文改坏。
  4. **开关**：`table_cell_clamp=false` 时完全不介入。

用法：
    python tests/test_cell_clamp.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import layout, typesetter as ts  # noqa: E402
from src.core.models import Paragraph  # noqa: E402
from src.shared.constants import DEFAULT_CONFIG  # noqa: E402

FAILURES: list[str] = []
TOL = 1.5      # 几何断言容差（pt）


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 造页面
# ---------------------------------------------------------------------------

def _text(page, x, y, text, size=9.0):
    page.insert_text((x, y), text, fontname="helv", fontsize=size)


def build_grid_pdf() -> fitz.Document:
    """一张**完整网格**表：2 行 2 列，横竖线都有。

    关键布局：左列第一行是一小段德文，**左列第二行是空的**，
    而右列第二行有文字。于是"下方可用空间"的既有算法会一路让到右列
    第三行去（同栏没有别的段落挡着），译文就会长到下一行、压住右边的文字。
    单元格约束必须在这里把它拦住。
    """
    doc = fitz.open()
    page = doc.new_page(width=420, height=300)
    left, mid, right = 40.0, 230.0, 400.0
    top, row1, row2, bottom = 60.0, 100.0, 160.0, 220.0
    for y in (top, row1, row2, bottom):
        page.draw_line((left, y), (right, y), color=(0, 0, 0), width=0.8)
    for x in (left, mid, right):
        page.draw_line((x, top), (x, bottom), color=(0, 0, 0), width=0.8)
    _text(page, left + 6, row1 - 8, "Kurzer Text")
    _text(page, mid + 6, row2 - 8, "Rechte Zelle")
    return doc


def build_threeline_pdf() -> fitz.Document:
    """三线表：只有三条横线，没有竖线（DGHT瑞士分会那份文档就是这个形态）。"""
    doc = fitz.open()
    page = doc.new_page(width=420, height=300)
    left, right = 40.0, 400.0
    top, row1, row2, bottom = 60.0, 100.0, 160.0, 200.0
    for y in (top, row1, bottom):
        page.draw_line((left, y), (right, y), color=(0, 0, 0), width=0.8)
    _text(page, left + 6, row1 - 8, "Kurzer Text")
    _text(page, left + 6, row2 - 8, "Zweite Zeile")
    return doc


def paragraphs_of(doc: fitz.Document, targets: dict[int, str]) -> list[Paragraph]:
    """解析页面段落，并按**自上而下的序号**逐段指定译文。

    一定要能指定"哪一段用长译文"：如果每一段都塞同一段长译文，
    下一行的原文就被替换掉了，就没法再验证"有没有压到下一行"。
    """
    import tempfile

    from src.core import pdf_parser

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "grid.pdf"
        doc.save(str(path))
        parsed = pdf_parser.parse_pdf(str(path), "t", "grid.pdf")

    ordered = sorted((p for p in parsed.paragraphs if p.translatable),
                     key=lambda p: (p.y0, p.x0))
    for index, paragraph in enumerate(ordered):
        paragraph.target = targets.get(index, f"第{index + 1}行短译文")
    return list(parsed.paragraphs)


LONG_TARGET = "这是一段故意写得很长的中文译文，用来验证它会不会越出所属单元格。"


def typeset(doc: fitz.Document, paragraphs,
            cell_clamp: bool = True, row_clamp: bool = True) -> tuple[dict, fitz.Document]:
    config = dict(DEFAULT_CONFIG)
    config.update({"target_lang": "zh", "mode": "mono",
                   "table_cell_clamp": cell_clamp, "table_row_clamp": row_clamp})
    work = fitz.open("pdf", doc.tobytes())
    stats = ts.typeset_document(work, paragraphs, config, mode="mono")
    return stats, work


def drawn_lines(page: fitz.Page) -> list[tuple[str, tuple[float, float, float, float]]]:
    """读出页面上实际画出来的文字行（文本, bbox）。"""
    out = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                out.append((text, tuple(line["bbox"])))
    return out


def find_line(page: fitz.Page, needle: str):
    for text, bbox in drawn_lines(page):
        if needle in text:
            return text, bbox
    return None, None


def band_bottom(page: fitz.Page, y_from: float, y_to: float) -> float | None:
    """某一行带内所有**画出来的文字**的最低下沿。

    不用"找某个词"来断言：译文会被折行，一个词可能跨两行，按词找会找不到。
    直接看几何 —— 带内的文字最远画到了哪儿。
    """
    bottoms = []
    for _text, bbox in drawn_lines(page):
        center = (bbox[1] + bbox[3]) / 2.0
        if y_from <= center <= y_to:
            bottoms.append(bbox[3])
    return max(bottoms) if bottoms else None


# ---------------------------------------------------------------------------
# 1) 单元：只收紧、不放大
# ---------------------------------------------------------------------------

def test_unit_clamp():
    print("\n[1] 单元：可用高度只收紧、不放大")
    original = fitz.Rect(40, 60, 200, 100)          # 高 40
    paragraph = Paragraph(id=1, page=0, bbox=tuple(original))

    # 没有单元格信息 → 原样返回
    got = ts._clamp_to_box(paragraph, original, False, False, 500.0)
    check(got == 500.0, f"无单元格信息时不变（{got}）")
    check(not ts._box_clamped(paragraph, original, False, False, 500.0),
          "无单元格信息时不计入收紧统计")

    # 格子下边界 160 → 距起点 100，比现值 500 紧 → 收紧到 100
    paragraph.constraint_bbox = (40.0, 60.0, 400.0, 160.0)
    got = ts._clamp_to_box(paragraph, original, False, False, 500.0)
    check(got == 100.0, f"收紧到格边（{got}）")
    check(ts._box_clamped(paragraph, original, False, False, 500.0),
          "真的收紧了才计入统计")

    # 现值本来就比格边紧 → 不放大
    got = ts._clamp_to_box(paragraph, original, False, False, 80.0)
    check(got == 80.0, f"现值更紧时保持现值（{got}）")
    check(not ts._box_clamped(paragraph, original, False, False, 80.0),
          "没收紧就不计入统计")

    # 格子比原段落框还小 → 不动（否则会把正常段落改坏）
    paragraph.constraint_bbox = (40.0, 60.0, 400.0, 80.0)   # 只比起点低 20，原框高 40
    got = ts._clamp_to_box(paragraph, original, False, False, 500.0)
    check(got == 500.0, f"格子小于原框时不动（{got}）")

    # 180° 倒排：阅读方向朝上，约束落在格子的**上**边
    paragraph.constraint_bbox = (40.0, 20.0, 400.0, 100.0)
    got = ts._clamp_to_box(paragraph, original, True and False, True, 500.0)
    check(got == 80.0, f"倒排时按格子上边收紧（{got}）")

    # 竖排：叠放轴是 bbox 的宽，所以约束落在格子的**右**边
    # （竖排段落的框本来就是窄的，宽就是它能叠几行的高度）
    narrow = fitz.Rect(100, 60, 120, 140)
    vertical = Paragraph(id=2, page=0, bbox=tuple(narrow))
    vertical.constraint_bbox = (100.0, 60.0, 130.0, 160.0)
    got = ts._clamp_to_box(vertical, narrow, True, False, 500.0)
    check(got == 30.0, f"竖排时按格子右边收紧（{got}）")
    check(not ts._box_clamped(vertical, narrow, True, False, 20.0),
          "竖排时不放大")
    # 竖排的格子比原框宽还窄 → 不动
    vertical.constraint_bbox = (100.0, 60.0, 105.0, 160.0)
    got = ts._clamp_to_box(vertical, narrow, True, False, 500.0)
    check(got == 500.0, f"竖排格子窄于原框时不动（{got}）")


def test_binding():
    print("\n[2] 单元：段落按中心点绑定到单元格")
    cells = [(40.0, 60.0, 200.0, 100.0), (200.0, 60.0, 400.0, 100.0)]
    left = layout.cell_for((50.0, 65.0, 150.0, 95.0), cells)
    check(left == cells[0], f"左侧段落绑到左格（{left}）")
    right = layout.cell_for((250.0, 65.0, 350.0, 95.0), cells)
    check(right == cells[1], f"右侧段落绑到右格（{right}）")
    none = layout.cell_for((50.0, 200.0, 150.0, 260.0), cells)
    check(none is None, "格外的段落不绑定")
    check(layout.cell_for((0, 0, 10, 10), []) is None, "没有单元格时不绑定")


# ---------------------------------------------------------------------------
# 3) 网格路径：真的把译文拦在格内
# ---------------------------------------------------------------------------

def test_grid_path():
    print("\n[3] 网格路径：译文被拦在所属单元格内")
    doc = build_grid_pdf()
    try:
        cells = layout.table_cells(doc[0])
        check(len(cells) >= 2, f"完整网格能识别出单元格（{len(cells)} 个）")

        paragraphs = paragraphs_of(doc, {0: LONG_TARGET})
        stats, work = typeset(doc, paragraphs)
        check(stats.get("clamped_cell", 0) >= 1,
              f"单元格约束确实生效（收紧 {stats.get('clamped_cell', 0)} 段）")

        page = work[0]
        bottom = band_bottom(page, 60.0, 100.0)
        check(bottom is not None, "译文确实画出来了")
        if bottom is not None:
            check(bottom <= 100.0 + TOL,
                  f"译文没有越过第一行的下边界 y=100（实际 y1={bottom:.1f}）")
            check(bottom <= 160.0,
                  f"更没有压到第二行（实际 y1={bottom:.1f}）")

        # 关掉单元格开关：不再收紧
        paragraphs_off = paragraphs_of(doc, {0: LONG_TARGET})
        stats_off, _ = typeset(doc, paragraphs_off, cell_clamp=False)
        check(stats_off.get("clamped_cell", 0) == 0,
              "关闭单元格开关后完全不介入")
        work.close()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 4) 三线表：按横线切行，把译文拦在行内
# ---------------------------------------------------------------------------

def layout_signature(page: fitz.Page) -> list[tuple[str, tuple]]:
    """页面文字的"排版指纹"，用来比较两次排版是否逐块一致。"""
    return [(text, tuple(round(value, 2) for value in bbox))
            for text, bbox in drawn_lines(page)]


def test_threeline_path():
    print("\n[4] 三线表：识别不出单元格，改用横线切行")
    doc = build_threeline_pdf()
    try:
        cells = layout.table_cells(doc[0])
        check(cells == [], f"三线表识别不出单元格（{len(cells)} 个）—— 这正是实测情况")

        rows = layout.table_rows(doc[0])
        check(len(rows) >= 2, f"由横线切出了行带（{len(rows)} 条）")

        paragraphs = paragraphs_of(doc, {0: LONG_TARGET, 1: "第2行"})
        kinds = {p.constraint_kind for p in paragraphs if p.translatable}
        check(kinds == {"row"}, f"段落被绑到行带而不是单元格（{kinds}）")

        stats, work = typeset(doc, paragraphs)
        check(stats.get("clamped_row", 0) >= 1,
              f"行约束确实生效（收紧 {stats.get('clamped_row', 0)} 段）")

        bottom = band_bottom(work[0], 60.0, 100.0)
        check(bottom is not None, "译文确实画出来了")
        if bottom is not None:
            check(bottom <= 100.0 + TOL,
                  f"译文被拦在行内，没有越过横线 y=100（实际 y1={bottom:.1f}）")

        # 打开与关闭的排版结果必须不同 —— 证明约束真的起了作用（不是恰好放得下）
        paragraphs_off = paragraphs_of(doc, {0: LONG_TARGET, 1: "第2行"})
        _stats_off, work_off = typeset(doc, paragraphs_off, row_clamp=False)
        same = layout_signature(work[0]) == layout_signature(work_off[0])
        check(not same, "关掉行约束后排版结果不同（约束确实在起作用）")
        bottom_off = band_bottom(work_off[0], 60.0, 100.0)
        if bottom_off is not None:
            print(f"    · 关掉行约束时译文下沿 {bottom_off:.1f}（越过横线 y=100）")

        # 下一行的译文必须还在（说明第一段没有把它顶掉）
        _text2, row2 = find_line(work[0], "第2")
        check(row2 is not None, "下一行的译文仍然画出来了")
        work.close()
        work_off.close()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 5) 开关默认值
# ---------------------------------------------------------------------------

def test_switch_default():
    print("\n[5] 开关")
    check(DEFAULT_CONFIG.get("table_cell_clamp") is True,
          "table_cell_clamp 默认开启")
    check(DEFAULT_CONFIG.get("table_row_clamp") is True,
          "table_row_clamp 默认开启")
    check(DEFAULT_CONFIG.get("symbol_fallback") is True,
          "symbol_fallback 默认开启")


def main() -> int:
    print("=" * 72)
    print("表格约束框（单元格 / 表格行）回归测试")
    print("=" * 72)
    test_unit_clamp()
    test_binding()
    test_grid_path()
    test_threeline_path()
    test_switch_default()
    print()
    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("表格约束框测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
