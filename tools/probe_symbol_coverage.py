"""实测：哪个本机字体覆盖的公式符号最多？（用来决定符号落点的候选顺序）

为什么需要它：第一版排序是"逐个 first-match"，结果落到 4 个字体上
（MS Gothic / JhengHei Light / JhengHei / 雅黑），其中一个还是 Light 细体 ——
落点越分散，产出 PDF 就要多嵌好几份字库，观感也不统一。

这个脚本算一张覆盖矩阵，看有没有"一个字体吃掉大部分符号"的选项。
实测结论：**MS Gothic 一个字体覆盖全部 23 个缺失符号**（全量 63 个也全覆盖），
所以 `_SYMBOL_PREFERENCE` 把它排在第一。

用法（不需要任何语料，直接跑）：
    python tools/probe_symbol_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import fonts  # noqa: E402

MISSING = "∛∓⋅∘∇‱⁻⁺⁰⇒⇔⊆∠⊥∥∅₀₁₂₃₄↗↘"
ALL = "∑∏∫√∛∞≈≠≤≥±∓×÷⋅·∘∂∇ΔδπμΩ°′″‰‱⁻⁺²³¹⁰½¼¾←→↔⇒⇔∈∉⊂⊆∪∩∀∃∠⊥∥∅ⁿ₀₁₂₃₄↗↘"


def main() -> int:
    faces = list(fonts._pack_faces()) + list(fonts.system_faces())
    rows = []
    for face in faces:
        try:
            font = fonts.load_font(face)
        except Exception:
            continue
        missing_hits = sum(1 for c in MISSING if font.has_glyph(ord(c)))
        if missing_hits == 0:
            continue
        all_hits = sum(1 for c in ALL if font.has_glyph(ord(c)))
        rows.append((missing_hits, all_hits, face))

    rows.sort(key=lambda item: (-item[0], -item[1], item[2].name))
    print("覆盖 23 个缺失符号最多的字体（前 20）")
    print("%-30s %-6s %-6s %-8s %-6s %-7s %s"
          % ("字体", "23中", "63中", "可子集", "变量", "细体", "来源"))
    for missing_hits, all_hits, face in rows[:20]:
        print("%-30s %-6d %-6d %-8s %-6s %-7s %s"
              % (face.name, missing_hits, all_hits,
                 "是" if face.subset_safe else "否",
                 "是" if face.variable else "否",
                 "是" if face.light else "否",
                 face.source))

    print()
    # 贪心：反复挑"还没覆盖的符号里覆盖最多"的字体，看几个字体能吃掉全部 23 个
    remaining = set(MISSING)
    picked = []
    while remaining:
        best = None
        for missing_hits, all_hits, face in rows:
            try:
                font = fonts.load_font(face)
            except Exception:
                continue
            hits = {c for c in remaining if font.has_glyph(ord(c))}
            if not hits:
                continue
            score = (len(hits), -len(face.name))
            if best is None or score > best[0]:
                best = (score, face, hits, font)
        if best is None:
            break
        _score, face, hits, _font = best
        picked.append((face.name, len(hits)))
        remaining -= hits

    print("贪心选择（先挑覆盖最多、且非细体的字体）：")
    for name, count in picked:
        print("  %-30s 新增覆盖 %d 个" % (name, count))
    print("  剩余无落点：%s" % ("".join(sorted(remaining)) or "（无）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
