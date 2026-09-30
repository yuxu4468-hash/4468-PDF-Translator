"""符号字形降级回归测试（阶段 2）。

锁住三件事：

  1. **23 个"内置拉丁字体缺字形"的数学符号不再画成空白方块** ——
     写入后必须能原样提取回来（空白方块提取出来是空的，所以"非空且一致"
     就是判据）。
  2. **降级链的顺序**：先保留原符号（本机落点字体），实在没有才退成 ASCII；
     开关关闭时**退回改造前的行为**（原字符保留、不换 ASCII）。
  3. **度量与绘制一致** —— 符号换字体后，`text_length` 与逐片段实际宽度仍相等，
     否则折行／居中／字距拉伸会算出错误的位置。

用法：
    python tests/test_symbol_glyph.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import typesetter as ts  # noqa: E402
from src.shared.constants import DEFAULT_CONFIG  # noqa: E402

FAILURES: list[str] = []

#: 设计文档 §1.2 实测出的 23 个缺失符号（内置 Helvetica 全部没有字形）
MISSING = "∛∓⋅∘∇‱⁻⁺⁰⇒⇔⊆∠⊥∥∅₀₁₂₃₄↗↘"
#: 实测的公式符号全集（63 个）
ALL_SYMBOLS = ("∑∏∫√∛∞≈≠≤≥±∓×÷⋅·∘∂∇ΔδπμΩ°′″‰‱⁻⁺²³¹⁰½¼¾"
               "←→↔⇒⇔∈∉⊂⊆∪∩∀∃∠⊥∥∅ⁿ₀₁₂₃₄↗↘")

#: 假字体默认"有"的字符：可打印 ASCII
_ASCII = "".join(chr(code) for code in range(0x20, 0x7F))


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def render_roundtrip(text: str, pair) -> str:
    """按实际绘制方式写出这段文字，再读回来。"""
    doc = fitz.open()
    page = doc.new_page(width=900, height=90)
    writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
    x, y = 12.0, 56.0
    for part, face, _key in pair.runs(text):
        writer.append((x, y), part, font=face, fontsize=18)
        x += face.text_length(part, fontsize=18)
    writer.write_text(page)
    probe = fitz.open("pdf", doc.tobytes(garbage=4, deflate=True))
    try:
        return probe[0].get_text().strip()
    finally:
        probe.close()
        doc.close()


# ---------------------------------------------------------------------------
# 1) 23 个缺失符号：写进去要能读回来
# ---------------------------------------------------------------------------

def test_missing_symbols():
    print("\n[1] 23 个缺失符号写-读回（有落点字体，必须原样保留）")
    config = dict(DEFAULT_CONFIG)
    render = ts.pick_font("测试", False, "zh", config=config)
    pair = render.pair

    got = render_roundtrip(MISSING, pair)
    check(got == MISSING, f"23 个符号全部原样提取（得到 {got!r}）")
    check(all(ch in got for ch in MISSING), "逐个都在，没有丢字")

    # 逐个单独写：定位"到底哪个符号坏了"，混在一起测会让报告看不出是哪个
    bad = []
    for char in MISSING:
        single = render_roundtrip(char, pair)
        if single != char:
            bad.append((char, single))
    check(not bad, f"逐个写入都能读回（坏的：{bad}）")

    # 落点字体必须真的被用上（而不是恰好内置字体有字形）
    fonts_used = {pair.font_for(char).name for char in MISSING}
    check(all(name != "Helvetica" for name in fonts_used),
          f"缺失符号确实换到了本机字体（{sorted(fonts_used)}）")


# ---------------------------------------------------------------------------
# 2) 降级链：原符号优先，ASCII 只作兜底
# ---------------------------------------------------------------------------

class _NoGlyphFont:
    """一个"除 ASCII 外什么字形都没有"的假字体，用来逼出 ASCII 兜底分支。"""

    name = "无字形字体"
    ascender = 0.8
    descender = -0.2

    def __init__(self, keep: str = _ASCII):
        self.keep = set(keep)

    def has_glyph(self, code: int) -> bool:
        return chr(code) in self.keep

    def text_length(self, text: str, fontsize: float = 11.0) -> float:
        return len(text) * fontsize * 0.5


def test_ascii_fallback():
    print("\n[2] 等价写法兜底（降级链第 3 步）")
    blank = _NoGlyphFont()
    got = ts.sanitize_for_font("a ≤ b", blank, allow_ascii=True)
    check(got == "a <= b", f"≤ 换成 <=（得到 {got!r}）")
    got = ts.sanitize_for_font("5 °C ± 1", blank, allow_ascii=True)
    check(got == "5 degC +/- 1", f"° ± 换成 ASCII（得到 {got!r}）")
    got = ts.sanitize_for_font("x₂", blank, allow_ascii=True)
    check(got == "x_2", f"下标换成 _2（得到 {got!r}）")

    # 形近字优先于 ASCII 拼写：`∅` 在德语技术文档里是**直径**符号，
    # 写成 `empty` 会把"直径 1.6 米"变成"空的 1.6 米"。
    lookalike = _NoGlyphFont(keep=_ASCII + "ØΣΠ")
    got = ts.sanitize_for_font("∅ 1,6 m", lookalike, allow_ascii=True)
    check(got == "Ø 1,6 m", f"∅ 优先换成形近的 Ø（得到 {got!r}）")
    got = ts.sanitize_for_font("∑x", lookalike, allow_ascii=True)
    check(got == "Σx", f"∑ 优先换成形近的 Σ（得到 {got!r}）")
    # 形近字也没有字形时，退到下一个候选（纯 ASCII 拼写）
    got = ts.sanitize_for_font("∅ 1,6 m", blank, allow_ascii=True)
    check(got == "dia 1,6 m", f"形近字缺失时退到下个候选（得到 {got!r}）")
    got = ts.sanitize_for_font("∑x", blank, allow_ascii=True)
    check(got == "sumx", f"形近字缺失时 ∑ 退到 sum（得到 {got!r}）")

    # 兜底字符本身也得有字形，否则等于把方块从一个码位搬到另一个码位
    got = ts.sanitize_for_font("≤", _NoGlyphFont(keep="<"), allow_ascii=True)
    check(got == "≤", f"等价写法的字符也缺字形时保持原样（得到 {got!r}）")

    # 关闭开关：与改造前一致 —— 缺字形的字符原样保留
    got = ts.sanitize_for_font("a ≤ b", blank, allow_ascii=False)
    check(got == "a ≤ b", f"关闭开关时不换等价写法（得到 {got!r}）")

    # 有落点字体时**不允许**降级：保真度优先
    pair = ts.pick_font("测试", False, "zh", config=dict(DEFAULT_CONFIG)).pair
    got = ts.sanitize_for_font("∛8", pair, allow_ascii=True)
    check(got == "∛8", f"有落点时保留原符号，不降级（得到 {got!r}）")


# ---------------------------------------------------------------------------
# 3) 测试符号全集与落点覆盖
# ---------------------------------------------------------------------------

def test_full_symbol_set():
    print("\n[3] 63 个公式符号的整体覆盖")
    config = dict(DEFAULT_CONFIG)
    pair = ts.pick_font("测试", False, "zh", config=config).pair

    got = render_roundtrip(ALL_SYMBOLS, pair)
    # 63 个符号排成一行会超出测试页宽，提取时被当成多行并插入换行 —— 去掉空白再比
    check(got.replace("\n", "").replace(" ", "") == ALL_SYMBOLS,
          f"63 个符号全部可渲染且可提取（得到 {got!r}）")

    no_face = [char for char in ALL_SYMBOLS
               if not pair.font_for(char).has_glyph(ord(char))]
    check(not no_face, f"没有任何符号落到'无字形'（{no_face}）")


# ---------------------------------------------------------------------------
# 4) 度量与绘制一致
# ---------------------------------------------------------------------------

def test_metrics_consistency():
    print("\n[4] 换字体后的度量仍然与绘制一致")
    pair = ts.pick_font("测试", False, "zh", config=dict(DEFAULT_CONFIG)).pair
    text = "全长 5 ≤ 6，体积 ∛8 °C，密度 ₁₀ ↗ 上限"
    size = 13.0

    total = pair.text_length(text, fontsize=size)
    manual = 0.0
    previous = ""
    for char in text:
        if pair.needs_inter_space(previous, char):
            manual += ts.INTER_SCRIPT_SPACE * size
        manual += pair.font_for(char).text_length(char, fontsize=size)
        previous = char
    check(abs(total - manual) < 1e-6,
          f"text_length 与逐字符累加一致（{total:.4f} vs {manual:.4f}）")

    # line_width 走的是另一条实现，也要与之一致
    line_total = ts.line_width(pair, text, size, 0.0)
    check(abs(line_total - manual) < 1e-6,
          f"line_width 与逐字符累加一致（{line_total:.4f} vs {manual:.4f}）")


# ---------------------------------------------------------------------------
# 5) 同字体片段分组
# ---------------------------------------------------------------------------

def test_runs_grouping():
    print("\n[5] 符号自成一个绘制片段")
    pair = ts.pick_font("测试", False, "zh", config=dict(DEFAULT_CONFIG)).pair
    # 用 ∛（内置 Helvetica **没有**字形）才能触发换字体；≤ 是 Helvetica 自带的，
    # 换了反而测不到东西
    runs = pair.runs("5 ∛ 6")
    check(len(runs) >= 3, f"含落点符号的行被切成多个片段（{len(runs)} 段）")
    joined = "".join(part for part, _face, _key in runs)
    check(joined == "5 ∛ 6", f"片段拼回来与原文一致（{joined!r}）")

    # 同一行里连着两个同字体符号应共用同一个片段（不逐字切碎）
    runs = pair.runs("∛₁₀")
    check(len(runs) == 1, f"同字体的相邻符号合并为一段（{len(runs)} 段）")


# ---------------------------------------------------------------------------
# 6) 开关关闭 = 改造前行为
# ---------------------------------------------------------------------------

def test_switch_off():
    print("\n[6] symbol_fallback=false 时回到改造前的行为")
    config = dict(DEFAULT_CONFIG)
    config["symbol_fallback"] = False
    pair = ts.pick_font("测试", False, "zh", config=config).pair

    for char in "∛₁₀↗":
        font = pair.font_for(char)
        check(font is pair.latin or font is pair.cjk,
              f"{char} 不再去本机字体找落点（{font.name}）")

    # 缺字形的字符原样保留（sanitize 不动它）——与改造前一致
    got = ts.sanitize_for_font("a ≤ b ∛", pair, allow_ascii=False)
    check(got == "a ≤ b ∛", f"缺字形字符原样保留（得到 {got!r}）")

    check(DEFAULT_CONFIG.get("symbol_fallback") is True,
          "symbol_fallback 默认开启")


def main() -> int:
    print("=" * 72)
    print("符号字形降级回归测试")
    print("=" * 72)
    test_missing_symbols()
    test_ascii_fallback()
    test_full_symbol_set()
    test_metrics_consistency()
    test_runs_grouping()
    test_switch_off()
    print()
    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("符号字形降级测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
