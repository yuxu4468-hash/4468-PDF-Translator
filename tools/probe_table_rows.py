"""实测：按横线切行，到底能收紧多少段？（先量，再决定要不要做）

背景：`find_tables()` 在三线表（只有横线、没有竖线）上一个单元格都识别不出来，
所以单元格约束在这些表格上完全不触发。候选方案是**用横线切行** ——
横线本来就是表格的行边界，而且 `table_regions()` 已经在读它们了。

但是"能切出行"不等于"能收紧东西"。真正要回答的问题是：

  按行边界算出的可用空间，比现在（按相邻段落文字算出的可用空间）**更紧**吗？
  更紧的占几成？

如果不更紧，做这个机制就是白费功夫。所以先量。

实测结论（作者的语料：46 份两爬类文档）：能切行 160 页 / 893 条行带；
落在行带里的可翻译段落 2679 段，其中 **801 段（29.9%）** 的行边界比原机制更紧。
据此才决定实现 `layout.table_rows()`。你在自己的语料上跑一遍，数字可能不同。

用法：
    python tools/probe_table_rows.py --base "D:\\我的PDF目录"
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import layout, pdf_parser, typesetter as ts  # noqa: E402

#: 横线聚类容差：同一条线的重复绘制会差零点几 pt
Y_TOL = 1.5
#: 一条"表格横线"至少要有这么长（占页面宽比例）
MIN_LEN_RATIO = 0.06
#: 至少这么多条横线才算表格（沿用 table_regions 的口径）
MIN_RULES = 3


def horizontal_ys(page, space) -> list[float]:
    """页面上所有"够长的横线"的 y 坐标（聚类去重）。"""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    min_len = max(30.0, float(space.width) * MIN_LEN_RATIO)
    ys: list[float] = []
    for item in drawings:
        for sub in item.get("items", []):
            kind = sub[0]
            if kind == "l":
                p1, p2 = sub[1], sub[2]
                if abs(p2.y - p1.y) < 2 and abs(p2.x - p1.x) >= min_len:
                    ys.append((p1.y + p2.y) / 2.0)
            elif kind == "re":
                rect = sub[1]
                if rect.width >= min_len:
                    ys.append(float(rect.y0))
                    ys.append(float(rect.y1))
    if not ys:
        return []
    ys.sort()
    merged = [ys[0]]
    for value in ys[1:]:
        if value - merged[-1] > Y_TOL:
            merged.append(value)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="按横线切行的收益实测")
    parser.add_argument("--base", default=".", help="PDF 语料所在目录（会递归找 *.pdf）")
    parser.add_argument("--limit", type=int, default=0, help="最多看多少份（0 = 全部）")
    args = parser.parse_args()

    base = Path(args.base).expanduser().resolve()
    if not base.is_dir():
        print("找不到语料目录：%s" % base)
        return 1
    docs = sorted(base.rglob("*.pdf"))
    if args.limit:
        docs = docs[: args.limit]
    print("语料目录：%s（%d 份 PDF）" % (base, len(docs)))

    tallies = Counter()
    detail: list[tuple] = []

    for path in docs:
        # 跳过本程序自己产出的译文（否则会把自己的输出当成语料）
        if "译文" in str(path) or "中文" in path.name:
            continue
        try:
            doc = fitz.open(str(path))
        except Exception:
            continue
        try:
            parsed = pdf_parser.parse_pdf(str(path), "r", path.name)
            try:
                for page_index in range(doc.page_count):
                    page = doc[page_index]
                    cells = layout.table_cells(page)
                    regions = layout.table_regions(page)
                    if not regions:
                        continue
                    tallies["有表格区域的页"] += 1
                    if cells:
                        tallies["网格路径已覆盖（不必切行）"] += 1
                        continue
                    tallies["只有线条、没有单元格的页"] += 1

                    space = ts.page_space(page)
                    ys = horizontal_ys(page, space)
                    if len(ys) < MIN_RULES:
                        tallies["横线不足 3 条（不判为表格）"] += 1
                        continue
                    bands = [(ys[i], ys[i + 1]) for i in range(len(ys) - 1)
                             if ys[i + 1] - ys[i] > 4.0]
                    tallies["可以切出的行带"] += len(bands)
                    if not bands:
                        continue
                    tallies["能切行的页"] += 1

                    # 段落：中心落在某个行带里，且行边界比"邻居让出的空间"更紧？
                    paragraphs = [p for p in parsed.paragraphs if p.page == page_index]
                    by_column: dict[int, list] = {}
                    for paragraph in paragraphs:
                        by_column.setdefault(paragraph.column, []).append(paragraph)
                    for paragraph in paragraphs:
                        if not paragraph.translatable:
                            continue
                        center = (paragraph.y0 + paragraph.y1) / 2.0
                        band = next((b for b in bands if b[0] <= center <= b[1]), None)
                        if band is None:
                            continue
                        tallies["落在行带里的段落"] += 1
                        vertical = abs(paragraph.rotation) in (90, 270)
                        if vertical:
                            continue
                        available = ts._available_below(
                            paragraph, by_column.get(paragraph.column, []),
                            space.height, max(12.0, space.height * 0.02),
                        )
                        current = paragraph.height + available
                        room = band[1] - paragraph.y0
                        if room < current - 0.5 and room > paragraph.height:
                            tallies["行边界确实更紧（会被收紧）"] += 1
                            detail.append((path.name, page_index + 1, paragraph.id,
                                           round(current, 1), round(room, 1),
                                           round(current - room, 1)))
                        else:
                            tallies["行边界不更紧"] += 1
            finally:
                parsed.close()
        finally:
            doc.close()

    print("=" * 78)
    print("按横线切行：可行性实测")
    print("=" * 78)
    width = max(len(key) for key in tallies) if tallies else 10
    for key, value in tallies.most_common():
        print("  %-*s %d" % (width, key, value))

    if tallies.get("落在行带里的段落"):
        rate = 100.0 * tallies["行边界确实更紧（会被收紧）"] / tallies["落在行带里的段落"]
        print("\n  会被收紧的比例：%.1f%%" % rate)

    if detail:
        print("\n  收紧幅度最大的 15 段：")
        print("  %-46s %-4s %-5s %-8s %-8s %s"
              % ("文档", "页", "段", "现在可用", "行边界", "收紧"))
        for row in sorted(detail, key=lambda item: -item[5])[:15]:
            print("  %-46s %-4d %-5d %-8.1f %-8.1f -%.1f" % row)

    print("\n" + "=" * 78)
    if tallies.get("落在行带里的段落") and tallies["行边界确实更紧（会被收紧）"] == 0:
        print("结论：行边界**从不**比现有机制更紧 —— 做了也不会有任何效果。")
    elif tallies.get("行边界确实更紧（会被收紧）"):
        print("结论：行边界在部分段落上确实更紧，值得做。")
    else:
        print("结论：样本里没有可翻译段落落在行带里，无法判断。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
