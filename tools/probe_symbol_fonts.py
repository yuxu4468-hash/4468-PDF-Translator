"""实测：本机字体对公式符号的覆盖情况（这是"符号字形降级"功能的依据）。

回答两个问题，都靠实测而不是猜：

1. 译文里那些内置拉丁字体**没有字形**的符号，能在本机字体里找到落点吗？
   落点越集中越好 —— 每多一个字体，产出 PDF 就要多嵌一份字库。
2. 用落点字体把这些符号画出来，能**原样提取**回来吗？
   （不能提取就又是一次静默损坏：画面正常、复制/搜索对不上，等于白做。）

实测结论（Windows 11 + MS Gothic）：本机 23 个缺失符号**一个字体全覆盖**，
且逐个写-读回全部一致 —— 这正是 `fonts.resolve_symbol_face()` 的排序依据。

用法（不需要任何语料，直接跑）：
    python tools/probe_symbol_fonts.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import fonts  # noqa: E402

#: 设计文档 §1.2 实测出的 23 个缺失符号
MISSING = "∛∓⋅∘∇‱⁻⁺⁰⇒⇔⊆∠⊥∥∅₀₁₂₃₄↗↘"
#: 全量 63 个公式符号
ALL = "∑∏∫√∛∞≈≠≤≥±∓×÷⋅·∘∂∇ΔδπμΩ°′″‰‱⁻⁺²³¹⁰½¼¾←→↔⇒⇔∈∉⊂⊆∪∩∀∃∠⊥∥∅ⁿ₀₁₂₃₄↗↘"


def main() -> int:
    print("=" * 78)
    print("M1-a  23 个缺失符号会落到哪个字体上")
    print("      （直接问生产代码 fonts.resolve_symbol_face，不在这里重写一套排序）")
    print("=" * 78)
    chosen: dict[str, str] = {}
    picked_fonts: dict[str, fitz.Font] = {}
    for char in MISSING:
        face = fonts.resolve_symbol_face(ord(char))
        if face is None:
            continue
        chosen[char] = face.name
        picked_fonts[face.name] = fonts.load_font(face)

    for char in MISSING:
        print("  %s -> %s" % (char, chosen.get(char, "（无落点）")))

    counts = Counter(chosen.values())
    print("\n  用到的字体数：%d" % len(counts))
    for name, count in counts.most_common():
        print("    %-28s 覆盖 %d 个符号" % (name, count))
    print("  未找到落点的符号：%s"
          % ("".join(c for c in MISSING if c not in chosen) or "（无）"))

    print("\n" + "=" * 78)
    print("M1-b  这些符号用落点字体写出来，能否原样提取回来")
    print("=" * 78)
    bad = []
    for name, font in picked_fonts.items():
        chars = "".join(c for c in MISSING if chosen.get(c) == name)
        scratch = fitz.open()
        page = scratch.new_page(width=420, height=60)
        writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
        writer.append((14, 40), chars, font=font, fontsize=18)
        writer.write_text(page)
        probe = fitz.open("pdf", scratch.tobytes(garbage=4, deflate=True))
        got = probe[0].get_text().strip()
        probe.close()
        scratch.close()
        ok = got == chars
        print("  %-28s 写入 %d 个 -> 读回 %r  %s"
              % (name, len(chars), got, "OK" if ok else "不一致"))
        if not ok:
            bad.append((name, chars, got))

    print("\n" + "=" * 78)
    print("M1-c  同一批符号，改用内置拉丁字体（现状路径）作对照")
    print("=" * 78)
    helv = fitz.Font("helv")
    have = [c for c in MISSING if helv.has_glyph(ord(c))]
    print("  内置 Helvetica 覆盖 23 个里的 %d 个：%s" % (len(have), "".join(have)))
    print("  内置 Helvetica 对全量 63 个符号的覆盖：%d/63"
          % sum(1 for c in ALL if helv.has_glyph(ord(c))))

    print("\n  结论：落点字体数 %d，提取失败 %d 个符号"
          % (len(counts), len(bad)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
