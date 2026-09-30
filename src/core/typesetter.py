"""排版回填（嵌字）：把译文写回 PDF，尽量还原原有版面。

两种模式：

- **mono / 嵌字（仅译文）**：把原文段落擦除，在**原来的位置、原来的基线上**
  写入译文。这是本项目的默认输出，产物看起来就是"原文那份文档变成了中文"。
- **dual（双语对照）**：把原文段落"重排"——原文与译文一起放进一个向下扩展后的
  矩形里（原文沿用原来的断行、译文自动折行），整体字号自动收缩到刚好放入
  该段落与其下方空白之间。这样无论原文排版多紧凑，双语内容都不会互相覆盖，
  也不会溢出页面，代价是紧凑版面下字号会变小。

## 嵌字模式为了"看起来就是原文"做的四件事

1. **字体风格匹配**（见 `fonts.py`）：原文是衬线就配中文字体里的衬线，
   原文加粗就配真实粗体字重。MuPDF 内置的 CJK 字体只有一种字重，直接用会
   把原文所有加粗标题都掉成常规粗细。
2. **基线锚定**：首行基线取自原文首个 span 的 `origin.y`（实测与
   `insert_text` 的基线参数完全一致），而不是段落 bbox 的顶边。用 bbox 顶边
   会因为新字号与原文不同而产生整体上下漂移。
3. **原文基线网格**：多行段落用原文相邻基线的中位间距作为行距，
   让译文的行落在原来那几行上，而不是浮在中间。
4. **字距拉伸**：译文比原文短时，把行内字距拉开填满原文的行宽
   （上限为字号的 `lettering_tracking_max`），避免右边空出一大块。

## 其它关键实现点

  - 文本度量与换行不依赖 `insert_textbox`，而是用 `fitz.Font.text_length`
    自己算行、自己算高，再用 `TextWriter` 按基线逐行写出，因此"能否放下"
    在绘制之前就已知，可以精确地二分搜索最佳字号。
  - 擦除用 `apply_redactions(images=NONE, graphics=NONE)`：只删文字，
    图片与表格线（矢量图）保持不动。
  - 擦除后的填充色从页面渲染图里采样得到，深色/彩色背景也能正确还原；
    若该区域色彩杂乱（说明压在图片上），则不填充，避免遮住插图。
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

from . import fonts as font_lib
from .pdf_parser import page_space
from .models import (
    ALIGN_CENTER,
    ALIGN_JUSTIFY,
    ALIGN_LEFT,
    ALIGN_RIGHT,
    Paragraph,
)
from ..shared.constants import CJK_TARGET_LANGS
from ..shared.text_utils import clean_text

logger = logging.getLogger("Typesetter")

# CJK 及全角字符的 Unicode 区段（这些字符之间可以任意断行）
CJK_RANGES = (
    "\u1100-\u11ff\u2e80-\u2eff\u3000-\u303f\u3040-\u30ff\u3130-\u318f"
    "\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f"
    "\uff00-\uffef"
)

#: 这些标点即使在中文里也应当独立成"可断行单元"。
#: 否则它们会跟着前一个词整体换行，避头尾规则就没机会介入。
PUNCT_RANGES = "\u00b7\u2013\u2014\u2018-\u201f\u2026\u2027"

_TOKEN_RE = re.compile(rf"[{CJK_RANGES}{PUNCT_RANGES}]|[^\s{CJK_RANGES}{PUNCT_RANGES}]+|\s+")

# ---------------------------------------------------------------------------
# 中文排版禁则（避头尾）
# ---------------------------------------------------------------------------

#: **不能出现在行首**的字符：句读点号、闭括号、后引号等。
#: 让它们落在行首是中文排版里最扎眼的瑕疵之一（俗称"顶头点"）。
NO_LINE_START = "、。，．：；？！％‰℃°）〕〉》」』】〗〙〛”’…‥—～·"

#: **不能出现在行末**的字符：开括号、前引号。
NO_LINE_END = "（〔〈《「『【〖〘〚“‘"


def is_no_line_start(token: str) -> bool:
    return bool(token) and token[0] in NO_LINE_START


def is_no_line_end(char: str) -> bool:
    return bool(char) and char in NO_LINE_END


_ALIGN_MAP = {
    ALIGN_LEFT: fitz.TEXT_ALIGN_LEFT,
    ALIGN_CENTER: fitz.TEXT_ALIGN_CENTER,
    ALIGN_RIGHT: fitz.TEXT_ALIGN_RIGHT,
    ALIGN_JUSTIFY: fitz.TEXT_ALIGN_LEFT,  # TextWriter 不支持两端对齐，退化为左对齐
}

# 字体资源
_FONT_CACHE: dict[str, fitz.Font] = {}

#: 来自文件的字体 -> 页面内注册用的 PDF 字体名（insert_textbox 需要）
_CUSTOM_FONT_NAMES: dict[str, str] = {}

#: 字体匹配结果的进程内缓存：同一份文档里成百上千个段落风格往往只有几种
_RENDER_FONT_CACHE: dict[tuple, "RenderFont"] = {}


def get_font(name: str) -> fitz.Font:
    """按名称取得（并缓存）PyMuPDF 内置字体对象。"""
    if name not in _FONT_CACHE:
        _FONT_CACHE[name] = fitz.Font(name)
    return _FONT_CACHE[name]


@dataclass
class RenderFont:
    """一段译文实际使用的字体（含伪粗体标记）。"""

    pair: "FontPair"
    face: font_lib.FontFace
    faux_bold: bool = False

    @property
    def label(self) -> str:
        return self.face.label()

    #: 便捷访问：汉字用的字体对象
    @property
    def font(self) -> fitz.Font:
        return self.pair.cjk


def is_cjk_code(code: int) -> bool:
    """判断码位是否属于"必须用中文字体渲染"的范围。"""
    return (
        0x2E80 <= code <= 0x2EFF        # CJK 部首补充
        or 0x3000 <= code <= 0x303F     # CJK 标点
        or 0x3040 <= code <= 0x30FF     # 假名
        or 0x3130 <= code <= 0x318F     # 谚文字母
        or 0x3400 <= code <= 0x4DBF     # 扩展 A
        or 0x4E00 <= code <= 0x9FFF     # 基本区
        or 0xAC00 <= code <= 0xD7AF     # 谚文音节
        or 0xF900 <= code <= 0xFAFF     # 兼容表意
        or 0xFE30 <= code <= 0xFE4F     # 兼容标点
        or 0xFF00 <= code <= 0xFFEF     # 全角形式
        or 0x20000 <= code <= 0x2FA1F   # 扩展 B~F
    )


#: 中英交界处的额外间隙（占字号比例）。参考 UTR #59「东亚文字间距」。
INTER_SCRIPT_SPACE = 0.25

#: 这些 CJK 标点之后不再加中英间隙（标点本身已经提供了视觉停顿）
_NO_INTER_SPACE_AFTER = "。，、：；！？）》」』】"


@dataclass
class FontPair:
    """一段文字实际使用的**字体对**：CJK 走中文字体，其余走内置拉丁字体。

    ## 为什么要拆成两个字体

    两个原因，一个是观感，一个是**正确性**。

    **观感**：CJK 字库里的拉丁字形是按全角设计的，又宽又呆，中文里夹的
    `DGHT`、`Vipera berus`、`2020` 用中文字体排出来会很松散。

    **正确性**（这个更关键）：不少 CJK 字库把多个 Unicode 码位映射到同一个
    字形，MuPDF 生成 ToUnicode 反查表时会挑错一个，后果不是"看着不对"，
    而是从产出 PDF 里**复制/搜索文字时对不上**。实测：

      - 微软雅黑 / Noto Sans SC：空格 → `U+00A0`（NBSP）→ 搜 `"Vipera berus"` 失败；
      - 等线 / Noto Sans SC / 思源宋体：连字符 → `U+2010` → 搜 `"Baden-Württemberg"` 失败；
      - 微软雅黑 Light：括号 → `U+FE3E/FE3F`。

    内置的 Helvetica / Times 系列**完全没有这个问题**（实测 ASCII 往返 100%），
    而且它们是 base-14 字体：不嵌入、不占体积。
    所以拉丁字符一律交给它们，只有汉字才用匹配出来的中文字体 —— 这也是
    pdf2zh / BabelDOC 的做法（按字符切换字体）。
    """

    cjk: fitz.Font
    cjk_key: str = ""
    latin: fitz.Font = None          # type: ignore[assignment]
    latin_key: str = "helv"
    cjk_is_builtin: bool = True
    latin_is_builtin: bool = True
    #: 两个路由字体都没有字形时，是否允许去本机字体里另找一个（见 `font_for_code`）
    symbol_fallback: bool = True
    #: 码位 -> 实际使用的字体（含符号落点）。按码位缓存，热路径上只是一次查表。
    _char_font: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.latin is None:
            self.latin = get_font("helv")

    # ---- 度量接口：与 fitz.Font 保持一致，这样折行/拟合代码不用改 ----
    @property
    def ascender(self) -> float:
        return max(self.cjk.ascender, self.latin.ascender)

    @property
    def descender(self) -> float:
        return min(self.cjk.descender, self.latin.descender)

    def font_for_code(self, code: int) -> fitz.Font:
        """某个码位实际用哪个字体画。

        三级路由，前一步命中即止：

        1. **路由字体** —— CJK 字符走中文字体，其余走内置拉丁字体；
        2. **符号落点** —— 路由字体没有这个字形时，去本机字体里找第一个有的
           （`fonts.resolve_symbol_face`，按码位缓存）；
        3. 仍然没有 → **返回路由字体**，交给 `sanitize_for_font` 走等价写法兜底
           或丢弃。

        为什么必须在**这里**做：度量（`text_length` / `line_width`）和绘制
        （`runs()`）都从这里取字体，只在绘制处换字体的话，"量出来"和"画出来"
        就会不一致 —— 折行、居中、字距拉伸全部会偏。
        """
        cached = self._char_font.get(code)
        if cached is not None:
            return cached

        font = self.cjk if is_cjk_code(code) else self.latin
        if self.symbol_fallback and not font.has_glyph(code):
            face = font_lib.resolve_symbol_face(code)
            if face is not None:
                font = font_lib.load_font(face)
        self._char_font[code] = font
        return font

    def font_for(self, char: str) -> fitz.Font:
        return self.font_for_code(ord(char))

    def _label(self, font: fitz.Font) -> str:
        """字体在"同字体片段"分组里用的标签（只用于日志与调试，不参与逻辑）。"""
        if font is self.cjk:
            return self.cjk_key
        if font is self.latin:
            return self.latin_key
        return getattr(font, "name", "sym")

    def has_glyph(self, code: int) -> bool:
        return bool(self.font_for_code(code).has_glyph(code))

    def text_length(self, text: str, fontsize: float = 11.0) -> float:
        """混合字体下的文本宽度（含中英交界处的额外间隙）。

        间隙判定统一走 `needs_inter_space`：这里原本另写了一套判断，两套在
        "数字后接中文标点"（`6，体积`）和"汉字后接空格"（`长 5`）上结论相反，
        于是同一段文字 `text_length` 与 `line_width` 会算出两个宽度 ——
        折行与字号拟合分别用这两个数，一差就会把行排歪。
        """
        if not text:
            return 0.0
        total = 0.0
        previous = ""
        for char in text:
            if self.needs_inter_space(previous, char):
                total += INTER_SCRIPT_SPACE * fontsize
            total += self.font_for(char).text_length(char, fontsize=fontsize)
            previous = char
        return total

    def runs(self, text: str) -> list[tuple[str, fitz.Font, str]]:
        """把一行文字按脚本切成若干"同字体片段"：[(文本, 字体, 字体标签)]。

        按**字体对象本身**分组而不是按"是不是 CJK"分组：符号落点字体既不是
        中文字体也不是拉丁字体，必须自己成一段（它只覆盖那几个码位）。
        """
        out: list[tuple[str, fitz.Font, str]] = []
        if not text:
            return out
        buffer: list[str] = []
        current: fitz.Font | None = None
        for char in text:
            font = self.font_for(char)
            if current is not None and font is not current:
                out.append(("".join(buffer), current, self._label(current)))
                buffer = []
            buffer.append(char)
            current = font
        if buffer and current is not None:
            out.append(("".join(buffer), current, self._label(current)))
        return out

    def needs_inter_space(self, before: str, after: str) -> bool:
        """两个相邻字符之间要不要加中英间隙。"""
        if not before or not after:
            return False
        if before in _NO_INTER_SPACE_AFTER or before.isspace() or after.isspace():
            return False
        return is_cjk_code(ord(before)) != is_cjk_code(ord(after))


def pick_font(
    text: str,
    bold: bool,
    target_lang: str,
    font_family: str = "auto",
    source_font: str = "",
    source_bold: bool = False,
    config: dict | None = None,
    ascii_safe: bool = False,
) -> RenderFont:
    """为一段译文选择渲染字体。

    与旧实现的区别：**不再无条件使用 `china-s`**。MuPDF 内置的全部 CJK 字体名
    都指向同一个 Droid Sans Fallback Regular（只有一种字重），所以旧实现里
    `bold` 参数在 CJK 分支被完全忽略，加粗标题会掉成常规粗细。
    现在交给 `fonts` 模块按"原文风格 → 本机字体"做匹配，匹配不到粗体字重时
    用伪粗体（描边）兜底。

    另外，这里返回的是 **CJK + 拉丁的字体对**（见 `FontPair`），
    拉丁字符永远走内置拉丁字体，避免 CJK 字库的 ASCII 反查问题。
    """
    config = config or {}
    style = font_lib.classify_source_font(source_font, bold=bool(bold or source_bold))
    if font_family and font_family != "auto":
        style = font_lib.FontStyle(
            serif=font_family == "serif", bold=style.bold,
            mono=style.mono, italic=style.italic,
        )
    elif not source_font:
        # 原文没有字体信息（例如 OCR 段落）：按目标语言给一个稳妥的默认
        style = font_lib.FontStyle(serif=False, bold=bool(bold))

    # 符号字形降级开关（见 FontPair.font_for_code）
    symbol_fallback = bool(config.get("symbol_fallback", True))
    cache_key = (
        style.serif, style.bold, style.mono, target_lang, ascii_safe,
        symbol_fallback,
        bool(config.get("lettering_use_system_fonts", True)),
        str(config.get("lettering_font_regular") or ""),
        str(config.get("lettering_font_bold") or ""),
    )
    cached = _RENDER_FONT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # 拉丁部分固定用内置字体：无体积开销、ASCII 往返 100% 正确
    latin_key = ("tibo" if style.serif else "tiro") if style.bold else \
                ("tiro" if style.serif else "helv")
    resolved: font_lib.ResolvedFont | None = None
    if target_lang in CJK_TARGET_LANGS:
        resolved = font_lib.resolve(
            style, target_lang, config,
            use_system=bool(config.get("lettering_use_system_fonts", True)),
            ascii_safe=ascii_safe,
        )
        pair = FontPair(
            cjk=resolved.font,
            cjk_key=resolved.face.key(),
            latin=get_font(latin_key),
            latin_key=latin_key,
            cjk_is_builtin=bool(resolved.face.builtin),
            latin_is_builtin=True,
            symbol_fallback=symbol_fallback,
        )
        render = RenderFont(pair=pair, face=resolved.face, faux_bold=resolved.faux_bold)
    else:
        # 非 CJK 目标语言：整句都走内置拉丁字体
        # （内置拉丁字体覆盖 40/63 个公式符号，缺的那些同样走符号落点）
        pair = FontPair(
            cjk=get_font(latin_key), cjk_key=latin_key,
            latin=get_font(latin_key), latin_key=latin_key,
            symbol_fallback=symbol_fallback,
        )
        face = font_lib.FontFace(
            name="MuPDF 内置拉丁字体", builtin=latin_key,
            bold=style.bold, serif=style.serif, source="内置",
        )
        render = RenderFont(pair=pair, face=face, faux_bold=False)

    _RENDER_FONT_CACHE[cache_key] = render
    return render




# ---------------------------------------------------------------------------
# 文本度量
# ---------------------------------------------------------------------------

def natural_height(font: fitz.Font, size: float) -> float:
    """字体的自然行高（不含额外行距）。"""
    span = font.ascender - font.descender
    if span <= 0.1:
        span = 1.2
    return span * size


@dataclass
class Leading:
    """行距策略。

    嵌字时优先贴合**原文的基线网格**：原文相邻两行基线的间距就是译文该用的
    行距，这样译文的行会落在原来那几行上，而不是浮在段落中间。
    `pitch` 为 None 时退化为"按字号算自然行距"（旧行为）。
    """

    spacing: float = 1.05
    pitch: float | None = None

    def natural(self, font: fitz.Font, size: float) -> float:
        return natural_height(font, size) * self.spacing

    def step(self, font: fitz.Font, size: float) -> float:
        """相邻两行基线的间距。"""
        natural = self.natural(font, size)
        if not self.pitch or self.pitch <= 0:
            return natural
        # 下限：不能小于自然行高，否则会叠字；
        # 上限：不超过自然行距的 1.8 倍，否则字号缩小后版面会显得很空。
        return min(max(self.pitch, natural), natural * 1.8)

    def height(self, count: int, font: fitz.Font, size: float) -> float:
        """count 行文本占用的总高度。"""
        if count <= 0:
            return 0.0
        if count == 1:
            return self.natural(font, size)
        return self.natural(font, size) + (count - 1) * self.step(font, size)



def tokenize(text: str) -> list[str]:
    """把文本切成可断行的最小单元（CJK 单字、拉丁词、空白）。"""
    return [token for token in _TOKEN_RE.findall(text) if token]


def wrap_text(text: str, font: fitz.Font, size: float, width: float) -> list[str]:
    """按可用宽度折行，并做中文禁则（避头尾）处理。

    - CJK 字符之间可任意断行；
    - 拉丁单词整体保留，超长单词（如 URL）按字符硬切；
    - 连续空白在行首被丢弃；
    - **中文禁则**：句读点号/闭括号不允许出现在行首（采用"标点悬挂"，
      让它稍微超出边框留在行末，这是中文排版的标准做法）；
      开括号/前引号不允许出现在行末（推到下一行）。
    """
    if width <= 1 or size <= 0:
        return [text]
    lines: list[str] = []
    current = ""

    for token in tokenize(text):
        if token.isspace():
            if current:
                current += " "
            continue

        candidate = current + token
        if font.text_length(candidate, fontsize=size) <= width:
            current = candidate
            continue

        # ---- 禁则 1：标点悬挂 ----
        # 下一个单元是"不能行首"的标点，就让它留在行末、略微出界。
        # 判据是**标点自身**够窄（不超过一个全角宽），而不是整行还剩多少空间 ——
        # 用整行余量判断会在"行正好排满"时失败（差一点点就挂不上），
        # 结果句号/右括号照样被甩到下一行行首。
        if current and is_no_line_start(token):
            if font.text_length(token, fontsize=size) <= size * 1.15:
                current = candidate
                continue

        # 兜底：当前行还是空的（上一行刚好被硬切完），标点就会落到行首。
        # 此时把它挂到上一行末尾 —— 宁可上一行略微出界，也不要"顶头点"。
        if not current and lines and is_no_line_start(token):
            lines[-1] = lines[-1] + token
            continue

        # ---- 禁则 2：开括号/前引号不能留在行末 ----
        carry = ""
        while len(current) > 1 and is_no_line_end(current[-1]):
            carry = current[-1] + carry
            current = current[:-1]

        if current.strip():
            lines.append(current.rstrip())
        current = carry

        candidate = current + token
        if candidate and font.text_length(candidate, fontsize=size) <= width:
            current = candidate
            continue

        # 单个 token 本身就超宽：按字符硬切
        if font.text_length(token, fontsize=size) > width:
            piece = current
            for char in token:
                if font.text_length(piece + char, fontsize=size) <= width:
                    piece += char
                else:
                    if piece:
                        lines.append(piece)
                    piece = char
            current = piece
        else:
            current = token

    if current.strip():
        lines.append(current.rstrip())
    return [line for line in lines if line.strip()] or [""]



def measure(lines: list[str], font: fitz.Font, size: float, line_spacing: float) -> float:
    """给定行数与字号，返回所需高度（自然行距，忽略原文网格）。"""
    return len(lines) * natural_height(font, size) * line_spacing


@dataclass
class FitResult:
    """一次排版拟合的结果。"""

    size: float
    lines: list[str]
    height: float
    fits: bool


def fit_text(
    text: str,
    font: fitz.Font,
    width: float,
    max_height: float,
    max_size: float,
    min_size: float,
    leading: "Leading | float",
) -> FitResult:
    """在给定宽高限制内，二分搜索尽可能大的字号。

    Args:
        leading: `Leading` 对象，或旧式的行距倍数（浮点）。
    """
    if not isinstance(leading, Leading):
        leading = Leading(spacing=float(leading or 1.05))

    if width <= 1:
        return FitResult(min_size, [text], leading.height(1, font, min_size), False)

    max_size = max(min_size, max_size)

    def try_size(size: float) -> FitResult:
        lines = wrap_text(text, font, size, width)
        height = leading.height(len(lines), font, size)
        return FitResult(size, lines, height, height <= max_height + 0.5)

    best = try_size(max_size)
    if best.fits:
        return best

    worst = try_size(min_size)
    if not worst.fits:
        # 即使最小字号也放不下：返回最小字号的排版结果，交由调用方决定是否溢出
        worst.fits = False
        return worst

    low, high = min_size, max_size
    for _ in range(12):
        middle = (low + high) / 2.0
        result = try_size(middle)
        if result.fits:
            low = middle
            best = result
        else:
            high = middle
        if high - low < 0.2:
            break
    result = try_size(low)
    return result if result.fits else best



# ---------------------------------------------------------------------------
# 背景色采样
# ---------------------------------------------------------------------------

_WHITE = (1.0, 1.0, 1.0)


def estimate_background(page: "fitz.Page", rect: fitz.Rect, margin: float = 4.0):
    """采样矩形区域的主色，作为擦除后的填充色。

    Returns:
        (color, uniform)：color 为 (r,g,b) 0~1；uniform 表示该区域颜色是否足够
        一致（不一致说明文字压在图片上，调用方应放弃填充）。
    """
    try:
        clip = fitz.Rect(rect) + (-margin, -margin, margin, margin)
        # 与文字坐标同一个空间（未旋转的 cropbox），否则带 /Rotate 的页面
        # 会裁到错误的区域、取到错误的填充色
        clip = clip & page_space(page)
        if clip.is_empty:
            return _WHITE, True
        zoom = 0.5
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom), clip=clip, colorspace=fitz.csRGB, alpha=False
        )
        samples = pixmap.samples
        if not samples:
            return _WHITE, True
        counter: Counter = Counter()
        # 每 2 个像素取一个，量化到 16 级降低噪声影响
        step = pixmap.n * 2
        for offset in range(0, len(samples) - pixmap.n, step):
            r = samples[offset] >> 4
            g = samples[offset + 1] >> 4
            b = samples[offset + 2] >> 4
            counter[(r, g, b)] += 1
        if not counter:
            return _WHITE, True
        (r, g, b), count = counter.most_common(1)[0]
        total = sum(counter.values())
        share = count / total if total else 0.0
        color = (r / 15.0, g / 15.0, b / 15.0)
        return color, share >= 0.55
    except Exception:
        logger.debug("背景色采样失败，使用白色", exc_info=True)
        return _WHITE, True


def contrast_color(color, fallback=(0.0, 0.0, 0.0)):
    """在给定背景上选择一个可读的文字颜色。"""
    if color is None:
        return fallback
    try:
        r, g, b = float(color[0]), float(color[1]), float(color[2])
    except (TypeError, IndexError):
        return fallback
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return (0.0, 0.0, 0.0) if luminance > 0.55 else (1.0, 1.0, 1.0)


def int_to_rgb(value: int):
    """PyMuPDF 的 span 颜色整数 -> (r,g,b) 浮点三元组。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    return ((value >> 16) & 0xFF) / 255.0, ((value >> 8) & 0xFF) / 255.0, (value & 0xFF) / 255.0


# ---------------------------------------------------------------------------
# 符号字符兜底
# ---------------------------------------------------------------------------

#: 符号字体（Wingdings / Symbol / ZapfDingbats）常用私有区码位 -> 正常 Unicode
SYMBOL_FALLBACKS = {
    0xF0A7: "•", 0xF0B7: "•", 0xF075: "•", 0xF06C: "●", 0xF0A8: "▪",
    0xF0D8: "➢", 0xF0E8: "➢", 0xF0AE: "→", 0xF0AC: "←", 0xF0AD: "↔",
    0xF0FC: "✓", 0xF0FE: "☑", 0xF0A1: "✱", 0xF0E0: "✉", 0xF0B0: "°",
    0xF0B1: "±", 0xF0B3: "≥", 0xF0A3: "≤", 0xF0BB: "≈", 0xF02D: "-",
    0xF020: " ", 0xF02A: "*", 0xF0B4: "×", 0xF0B8: "÷", 0xF0E9: "○",
}


#: 符号的**可渲染等价写法**（63 条，覆盖本项目实测用到的全部公式符号）。
#:
#: 值可以是字符串，也可以是**候选序列**（按优先级）。候选序列用在这种情况：
#: 一个"形近字"比 ASCII 拼写更贴近原意，但它不一定存在于当前字体里 ——
#: 例如 `∅`（工程图纸里的"直径"）写成 `Ø` 比写成 `empty` 好得多，
#: 而 `Ø` 万一没字形，还能退到 `dia`、`empty`。
#:
#: **只在"路由字体和本机符号落点都没有这个字形"时才用**：
#:   - 横排能按字符换字体，实测 23 个缺失符号全都有落点，所以这里几乎不会触发；
#:   - **竖排走 insert_textbox，只能用单一字体**，这才是这张表真正兜底的地方
#:     （实测：鳄类准则里 22 处 `∅ 1,6 m` 都是竖排表头，只能靠这张表）。
SYMBOL_ASCII_FALLBACK = {
    "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~=", "×": "x", "÷": "/",
    "²": "^2", "³": "^3", "¹": "^1",
    "⁰": "^0", "⁻": "^-", "⁺": "^+",
    "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4",
    "∑": ("Σ", "sum"), "∏": ("Π", "prod"), "∫": "int",
    "√": "sqrt", "∞": "inf",
    "±": "+/-", "∓": "-/+", "⋅": "·", "·": "⋅",
    "∘": "o", "∂": "d",
    "∇": "grad", "Δ": "Delta", "δ": "delta",
    "π": "pi", "μ": "u",
    "Ω": "Ohm", "°": "deg", "′": "'", "″": '"',
    "‰": "0/00", "‱": "0/000",
    "←": "<-", "→": "->", "↔": "<->",
    "⇒": "=>", "⇔": "<=>",
    "∈": "in", "∉": "not in", "⊂": "subset", "⊆": "subseteq",
    "∪": "union", "∩": "intersect", "∀": "forall", "∃": "exists",
    "∠": "angle", "⊥": "perp", "∥": "parallel",
    # `∅` 在德语技术文档里是**直径**符号（`∅ 1,6 m`），不是空集 —— 写成 `empty`
    # 会把"直径 1.6 米"变成"空的 1.6 米"。`Ø` 形近且常见，优先用它。
    "∅": ("Ø", "dia", "empty"),
    "ⁿ": "^n", "∛": "cbrt",
    "½": "1/2", "¼": "1/4", "¾": "3/4",
    "↗": "up-right", "↘": "down-right",
}


def _renders(font, text: str) -> bool:
    """这个字体能不能把整段等价写法画出来（每个字符都要有字形）。"""
    return bool(text) and all(font.has_glyph(ord(char)) for char in text)


def _equivalent_forms(char: str) -> tuple[str, ...]:
    """取某个符号的等价写法候选（字符串统一成单元素元组）。"""
    value = SYMBOL_ASCII_FALLBACK.get(char)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def sanitize_for_font(text: str, font: fitz.Font, allow_ascii: bool = False) -> str:
    """把字体里没有字形的字符换成可渲染的等价字符。

    PDF 里经常用 Wingdings / Symbol 这类符号字体，导出的文字会落在 Unicode
    私有使用区（U+E000–U+F8FF），例如项目符号 `\\uf0a7`。内置的中日韩字体
    没有这些字形，**而且 `TextWriter` 遇到缺字形时不会报错**，只会画出一个
    空白方块（notdef），提取出来是 `\\x00` —— 不报错但结果是坏的。
    所以这里主动检查字形，能对应上的换成正常字符，对应不上的直接去掉，
    避免留下空白方块。

    Args:
        allow_ascii: 是否允许用 `SYMBOL_ASCII_FALLBACK` 把数学符号换成等价写法
            （形近字优先，ASCII 兜底）。默认关闭，这样**关掉 `symbol_fallback`
            开关时行为与改造前逐字节一致**；由调用方按配置传入。

    注意 `font` 既可以是 `fitz.Font`，也可以是 `FontPair` —— 后者已经把
    "本机符号落点字体"算进 `has_glyph`，于是有落点的符号会走到绘制层用那个字体画，
    不会在这里被降级（保真度优先）。
    """
    if not text:
        return text
    result: list[str] = []
    for char in text:
        code = ord(char)
        if font.has_glyph(code):
            result.append(char)
            continue
        mapped = SYMBOL_FALLBACKS.get(code)
        # 映射出来的替代字符也得确认字体真有这个字形，否则等于把空白方块
        # 从一个码位搬到另一个码位，问题依旧。
        if mapped is not None and any(font.has_glyph(ord(item)) for item in mapped):
            result.append(mapped)
            continue
        if allow_ascii:
            form = next((item for item in _equivalent_forms(char) if _renders(font, item)), "")
            if form:
                result.append(form)
                continue
        if 0xE000 <= code <= 0xF8FF:
            continue  # 未知私有区字符：直接丢掉
        result.append(char)
    return "".join(result)



# ---------------------------------------------------------------------------
# 排版计划
# ---------------------------------------------------------------------------

@dataclass
class TextBlockPlan:
    """一段文字在页面上的绘制计划。"""

    rect: fitz.Rect
    #: 实际用于渲染的字体对（CJK + 拉丁）；鸭子类型，度量接口与 fitz.Font 一致
    font: "FontPair | fitz.Font"
    fontname: str
    size: float
    lines: list[str]
    color: tuple
    align: int
    line_spacing: float
    rotation: int = 0
    overlay: bool = True
    #: 非空表示字体来自文件（系统字体/自定义字体），旋转绘制时要注册到页面上
    fontfile: str = ""
    #: 首行基线的绝对 y 坐标；None 表示按 rect 顶边推算（旋转/倒排时使用）
    first_baseline: float | None = None
    #: 相邻基线的间距；<=0 表示按字号自然行距
    pitch: float = 0.0
    #: 每行额外增加的字距（pt），用于把行拉伸到原文的行宽
    tracking: list[float] = field(default_factory=list)
    #: 该块需要用描边做伪粗体（字体本身没有粗体字重时）
    faux_bold: bool = False
    #: 旋转文本的排版坐标系（非旋转文本为 None）：
    #: 起点（原文第一行基线起点）、文字前进方向、换行叠放方向
    origin_point: tuple | None = None
    axis_along: tuple | None = None
    axis_stack: tuple | None = None
    #: 沿文字方向可用的长度（竖排时是 bbox 的高）
    run_length: float = 0.0

    def total_height(self) -> float:
        if self.pitch and self.pitch > 0:
            natural = natural_height(self.font, self.size) * self.line_spacing
            return natural + max(0, len(self.lines) - 1) * self.pitch
        return measure(self.lines, self.font, self.size, self.line_spacing)



@dataclass
class PagePlan:
    """一页的完整排版计划。"""

    page_index: int
    redactions: list[tuple[fitz.Rect, tuple | None]] = field(default_factory=list)
    blocks: list[TextBlockPlan] = field(default_factory=list)
    separators: list[tuple[fitz.Rect, tuple]] = field(default_factory=list)
    #: OCR 段落需要遮盖的原图区域（扫描件里文字是图片的一部分，擦不掉）
    covers: list[fitz.Rect] = field(default_factory=list)
    overflow: int = 0
    skipped: int = 0
    #: 双语模式下因空间不足、退化为"仅译文"的段落数
    fallback_mono: int = 0
    #: 因所属**单元格**而收紧了可用高度的段落数
    #: —— 有这个数字才能分清"开关打开了"和"真的收紧了"（本项目的老教训）
    clamped_cell: int = 0
    #: 因所属**表格行**而收紧了可用高度的段落数（三线表靠这条）
    clamped_row: int = 0


def _box_room(box, original: fitz.Rect, is_vertical: bool, flipped: bool) -> float | None:
    """从段落起点算起、到约束框边界为止的可用长度（沿**阅读方向**）。

    横排沿 bbox 的高向下延伸，竖排沿 bbox 的宽叠放，180° 倒排向上延伸 ——
    三种情况的"阅读方向"不同，所以要分别取约束框对应的那条边。
    """
    if not box:
        return None
    if is_vertical:
        return float(box[2]) - original.x0
    if flipped:
        return original.y1 - float(box[1])
    return float(box[3]) - original.y0


def _box_floor(original: fitz.Rect, is_vertical: bool) -> float:
    """可用高度的下限：段落**自己原来的框**。

    原文本身就占着这么大，压到它以下既解决不了问题、又会把一段本来正常的段落
    改小；万一某段被错误地绑到一个偏小的框上（表格识别歪了），压下去就是**误改**。
    宁可漏检不可误改 —— 所以约束只能**收回借来的空间**，不能侵占原文的框。
    """
    return max(4.0, original.width if is_vertical else original.height)


def _clamp_to_box(paragraph: Paragraph, original: fitz.Rect, is_vertical: bool,
                  flipped: bool, max_height: float) -> float:
    """把"可用高度"收紧到所属约束框（单元格或表格行）的边界内。

    只**收紧**，不放大：`min(现值, 到框边的距离)`，下限见 `_box_floor`。

    Returns 收紧后的可用高度；没有约束框信息时原样返回。
    """
    room = _box_room(getattr(paragraph, "constraint_bbox", None),
                     original, is_vertical, flipped)
    if room is None or room <= 0:
        return max_height
    floor = _box_floor(original, is_vertical)
    if room <= floor:
        return max_height
    return min(max_height, room)


def _box_clamped(paragraph: Paragraph, original: fitz.Rect, is_vertical: bool,
                 flipped: bool, max_height: float) -> bool:
    """这一段是否真的被约束框收紧了（用于统计，便于确认功能真的生效）。"""
    room = _box_room(getattr(paragraph, "constraint_bbox", None),
                     original, is_vertical, flipped)
    if room is None or room <= 0:
        return False
    floor = _box_floor(original, is_vertical)
    return room > floor and room < max_height


def _constraint_enabled(paragraph: Paragraph, cell_clamp: bool, row_clamp: bool) -> bool:
    """这一段所属的约束框，对应的开关是不是开着的。

    两个来源各有一个开关，是为了能单独回滚：单元格识别（PyMuPDF 的启发式）
    与横线切行（本项目自己的启发式）出问题的方式不一样，得能分开关。
    """
    kind = getattr(paragraph, "constraint_kind", "")
    if kind == "cell":
        return cell_clamp
    if kind == "row":
        return row_clamp
    return False


def _available_below(paragraph: Paragraph, siblings: list[Paragraph], page_height: float,
                     bottom_margin: float) -> float:
    """计算段落下方可用的空白高度（同栏内、到下一个段落的上沿为止）。

    判断"谁在下面"用**段落中心**而不是 bbox 是否相交：紧凑排版的相邻行
    bbox 常常互相咬合（字体的升降部留白重叠），用相交判断会把紧贴着的
    下一段误判成"不存在"，从而算出过大的可用空间、导致译文压到它身上。
    """
    limit = page_height - bottom_margin
    center = (paragraph.y0 + paragraph.y1) / 2.0
    for other in siblings:
        if other is paragraph:
            continue
        other_center = (other.y0 + other.y1) / 2.0
        if other_center <= center:
            continue
        limit = min(limit, other.y0)
    return max(0.0, limit - paragraph.y1)


def _available_above(paragraph: Paragraph, siblings: list[Paragraph], top_margin: float) -> float:
    """计算段落**上方**可用的空白高度。

    用于倒排文本（180°，折页背面）：这类文字的阅读方向自下而上，
    译文要排在视觉上的上方，所以可用空白也得往上找。
    """
    limit = top_margin
    center = (paragraph.y0 + paragraph.y1) / 2.0
    for other in siblings:
        if other is paragraph:
            continue
        other_center = (other.y0 + other.y1) / 2.0
        if other_center >= center:
            continue
        limit = max(limit, other.y1)
    return max(0.0, paragraph.y0 - limit)


def _safe_erase_rect(
    rect: fitz.Rect, paragraph: Paragraph, page_paragraphs: list[Paragraph]
) -> fitz.Rect:
    """把擦除矩形向内收边，避免连带删掉**保留原文**的相邻段落文字。

    `apply_redactions` 删的是"与该矩形相交"的**全部**文字，而紧凑版式里相邻
    段落的 bbox 常因字体升降部留白而互相咬合（实测折页上有 3~4pt 的纵向重叠）。
    照着段落 bbox 直接擦，就会把邻居的头一行或末一行一起**静默删掉**。

    只对**保留原文**的邻居避让（未翻译的页眉页脚、代码、取不到译文的段落）：

    - 邻居也要被替换时收边毫无意义 —— 它的原文反正会被擦掉重写，
      收边只会让本段的原文残留一小条（更难看的德文碎片）；
    - 邻居保留原文时才是真的会丢内容，必须避让。

    收边后如果矩形退化，则放弃收边（宁可冒险也不能什么都不擦）。
    """
    result = fitz.Rect(rect)
    adjusted = False
    for other in page_paragraphs:
        if other is paragraph or not (other.text or "").strip():
            continue
        if other.target:
            continue  # 邻居也会被擦除重写，无需避让
        other_rect = fitz.Rect(other.bbox)
        if not result.intersects(other_rect):
            continue
        overlap = result & other_rect
        if overlap.width <= 0.3 or overlap.height <= 0.3:
            continue
        if overlap.width >= overlap.height:
            # 上下相邻：把交叠的那条水平边收到邻居的边界上
            if other_rect.y0 > result.y0:
                result.y1 = min(result.y1, other_rect.y0)
            else:
                result.y0 = max(result.y0, other_rect.y1)
        else:
            if other_rect.x0 > result.x0:
                result.x1 = min(result.x1, other_rect.x0)
            else:
                result.x0 = max(result.x0, other_rect.x1)
        adjusted = True

    if adjusted and (result.is_empty or result.width < 1.0 or result.height < 1.0):
        return fitz.Rect(rect)
    return result


def _mark_erase(
    plan: PagePlan,
    paragraph: Paragraph,
    rect: fitz.Rect,
    page_paragraphs: list[Paragraph] | None = None,
) -> None:
    """记录"把原文从页面上抹掉"的方式。

    两种情况要分开处理：

    - **普通 PDF**：原文是真正的文字对象，用 redaction 删掉即可，
      图片与矢量图形（表格线等）会自动保留。
    - **扫描件（段落来自 OCR）**：文字是扫描图像的一部分，没有文字对象可删，
      只能稍后画一块背景色把它盖住。这也是为什么 OCR 段落要单独记录 `covers`。
    """
    if page_paragraphs:
        rect = _safe_erase_rect(rect, paragraph, page_paragraphs)
    if paragraph.from_ocr:
        plan.covers.append(fitz.Rect(rect))
    else:
        plan.redactions.append((fitz.Rect(rect), None))



def _overlaps_any(
    rect: fitz.Rect,
    paragraph: Paragraph,
    page_paragraphs: list[Paragraph],
    tolerance: float = 1.0,
) -> bool:
    """判断矩形是否与页面上其它段落的原始位置相交。

    用于双语模式的兜底判断：与其把"原文+译文"硬挤进一个和邻居咬合的区域
    （结果两边都读不清），不如让这一段退化成"仅译文"。
    """
    for other in page_paragraphs:
        if other is paragraph:
            continue
        if not (other.text or "").strip():
            continue
        intersection = rect & fitz.Rect(other.bbox)
        if intersection.is_empty:
            continue
        if intersection.width > tolerance and intersection.height > tolerance:
            return True
    return False


def _baseline_grid(paragraph: Paragraph, rotation: int) -> tuple[float | None, float | None]:
    """取出原文的基线信息：`(首行基线 y, 相邻基线间距)`。

    只有当排版方向是正常横排（0°）时才有意义：180° 倒排与竖排的 `origin`
    语义不同，而且倒排还要经过 morph 镜像，直接用绝对坐标会错位，
    所以那两种情况返回 (None, None)，调用方退回按矩形顶边推算。

    PyMuPDF 里 span 的 `origin` 就是该段文字的**基线起点**，实测与
    `insert_text(pos, ...)` 的 pos.y 完全一致，因此可以放心当作绝对基线用。
    """
    if rotation != 0:
        return None, None

    baselines: list[float] = []
    for line in paragraph.lines:
        for span in line.spans:
            if span.origin and span.origin[1]:
                baselines.append(float(span.origin[1]))
                break
    if not baselines:
        return None, None

    first = baselines[0]
    if len(baselines) < 2:
        return first, None

    ordered = sorted(baselines)
    gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b - a > 0.5]
    if not gaps:
        return first, None
    gaps.sort()
    middle = len(gaps) // 2
    pitch = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2.0
    return first, pitch


def _rotated_frame(paragraph: Paragraph):
    """从原文数据推出竖排文本的排版坐标系。

    Returns:
        `(origin, along, stack, run_length)` 或 None。含义：

        - `origin`：原文**第一行** span 的起点，也就是基线的起点；
          译文的第一行就落在同一个点上，天然与原文对齐。
        - `along`：文字前进方向的单位向量（例如自下而上是 (0,-1)）。
        - `stack`：换行时的叠放方向，由"第 0 行与第 1 行起点之差"投影到
          法线方向得到 —— 是**从数据里读出来的**，不靠猜朝哪边叠。
        - `run_length`：沿文字方向可用的长度（竖排即 bbox 的高）。

    这样就不需要去猜 `insert_textbox` 的角度约定，也不会出现"上下颠倒"。
    """
    origins: list[tuple[float, float]] = []
    for line in paragraph.lines:
        for span in line.spans:
            if span.origin:
                origins.append((float(span.origin[0]), float(span.origin[1])))
                break
    if not origins:
        return None

    direction = paragraph.lines[0].direction if paragraph.lines else (1.0, 0.0)
    norm = math.hypot(direction[0], direction[1])
    if norm < 1e-6:
        return None
    along = (direction[0] / norm, direction[1] / norm)
    stack = (-along[1], along[0])

    if len(origins) >= 2:
        vx = origins[1][0] - origins[0][0]
        vy = origins[1][1] - origins[0][1]
        if abs(vx) + abs(vy) > 0.01:
            if vx * stack[0] + vy * stack[1] < 0:
                stack = (-stack[0], -stack[1])

    box = fitz.Rect(paragraph.bbox)
    run_length = abs(box.height * along[1]) + abs(box.width * along[0])
    return origins[0], along, stack, max(8.0, run_length)


def compute_tracking(
    lines: list[str],
    font: fitz.Font,
    size: float,
    width: float,
    align: str,
    max_ratio: float,
) -> list[float]:
    """算出每一行需要额外增加的字距，让它填满原文的行宽。

    译文（中文）通常比同义的德文/英文**短**，左对齐时会空出右边一大块，
    一眼就能看出"这是翻译过的"。把字距均匀拉开就自然多了 —— 这也是人工嵌字
    的常规做法。

    三条自我约束，避免拉过头反而更难看：

    - 只对左对齐/两端对齐的行做；居中、右对齐靠自身对齐方式就能处理空档。
    - **最后一行不拉**：段落的末行本来就不满行，拉满反而奇怪。
    - 空档既不能超过行宽的 30%，单字间距也不能超过字号的 `max_ratio` 倍；
      否则会把 `"是"` 这种两字短行摊成一整行，非常突兀。
    """
    tracks = [0.0] * len(lines)
    if not lines or max_ratio <= 0 or width <= 1:
        return tracks
    if align not in (ALIGN_LEFT, ALIGN_JUSTIFY, ALIGN_CENTER):
        return tracks

    last = len(lines) - 1
    for index, line in enumerate(lines):
        if index == last or not line.strip():
            continue
        # 用与绘制同一套的宽度算法（含中英间隙），否则算出来的空档会偏大
        natural = line_width(font, line, size, 0.0)
        slack = width - natural
        if slack <= 0.5 or slack > width * 0.30:
            continue
        # 居中排版的标题拉字距会破坏居中观感，只在空档很小时微调
        if align == ALIGN_CENTER and slack > width * 0.12:
            continue
        gaps = max(1, len(line) - 1)
        per_gap = slack / gaps
        if per_gap > max_ratio * size:
            continue
        tracks[index] = per_gap
    return tracks


def plan_page(
    page_paragraphs: list[Paragraph],
    page_height: float,
    config: dict,
    mode: str,
) -> PagePlan:
    """为一页生成排版计划。

    Args:
        page_paragraphs: 该页**全部**段落（含未翻译的），未翻译的段落不绘制，
            但会作为障碍物参与"下方可用空间"的计算，避免译文覆盖到它们上面。
    """
    plan = PagePlan(page_index=page_paragraphs[0].page if page_paragraphs else 0)
    if not page_paragraphs:
        return plan

    font_scale = float(config.get("font_scale") or 1.0)
    min_size = float(config.get("min_font_size") or 5.0)
    line_spacing = float(config.get("line_spacing") or 1.05)
    font_family = config.get("font_family", "auto")
    target_lang = config.get("target_lang", "zh")
    bottom_margin = max(12.0, page_height * 0.02)
    # 双语模式下原文与译文之间的间隔（相对于字号）
    dual_gap_ratio = float(config.get("dual_gap") or 1.0) * 0.30
    # 嵌字：字距拉伸上限（占字号比例）。设为 0 可关闭拉伸。
    tracking_max = float(config.get("lettering_tracking", 0.08) or 0.0)
    # 符号字形降级（本机字体落点 + ASCII 等价兜底）
    symbol_fallback = bool(config.get("symbol_fallback", True))
    # 表格约束框：单元格（PyMuPDF 表格识别）与表格行（横线切行）各一个开关
    cell_clamp = bool(config.get("table_cell_clamp", True))
    row_clamp = bool(config.get("table_row_clamp", True))


    for paragraph in page_paragraphs:
        source = clean_text(paragraph.text.replace("\n", " "))
        target = clean_text(paragraph.target.replace("\n", " "))
        if not target:
            # 未翻译的段落：不绘制，但仍是障碍物
            continue

        rotation = int(round(paragraph.rotation / 90.0)) * 90 % 360
        # 倒排文本（180°，折页背面等）：阅读方向是自下而上，
        # 因此"紧接着这一段"的位置在视觉上位于其**上方**，可用空白也要向上找。
        flipped = rotation == 180
        # 旋转段落（竖排 ±90°）：横排表格里的列标签、竖向表头都属于这一类
        is_vertical = rotation in (90, 270)
        top_margin = max(12.0, page_height * 0.02)
        original = fitz.Rect(paragraph.bbox)
        max_size = max(min_size, paragraph.size * font_scale)

        siblings = [p for p in page_paragraphs if p.column == paragraph.column]

        if is_vertical:
            # 竖排文字沿 bbox 的**高**方向排列，多行沿 bbox 的**宽**方向叠放。
            # 所以"一行能排多长"是 bbox 的高，"能叠几行"是 bbox 的宽。
            #
            # 这里曾经一律用 bbox 的宽当折行宽度，后果很严重：单元格宽只有
            # 11~21pt，中文被折成每行 1~2 个字；旋转排版需要"行数×字号"的叠高，
            # 远超单元格宽度，于是**所有字号都放不下**，整批回退成水平绘制。
            # 实测一份 36 页文档因此产生 309 处朝向错乱、并压进相邻单元格。
            # 留 2% 余量：最终由 insert_textbox 负责绘制，它会按矩形宽度
            # 自行折行；我们折得略短一点，它就不会二次折行，中文禁则也就保住了。
            line_limit = max(8.0, original.height) * 0.98
            max_height = max(4.0, original.width)
            # 表格单元格四周都是邻居，没有可借的空白
            available = 0.0
        else:
            line_limit = max(8.0, original.width)
            if flipped:
                available = _available_above(paragraph, siblings, top_margin)
            else:
                available = _available_below(paragraph, siblings, page_height, bottom_margin)
            max_height = original.height + available

        # ---- 表格约束框：可用高度不得越出所属单元格 / 表格行 ----
        # 放在模式分支之前：mono 与 dual 用的是同一个 max_height，
        # 框边界对两者都是硬约束（dual 的向下扩展同样会压到下一行）。
        if _constraint_enabled(paragraph, cell_clamp, row_clamp):
            if _box_clamped(paragraph, original, is_vertical, flipped, max_height):
                if getattr(paragraph, "constraint_kind", "") == "cell":
                    plan.clamped_cell += 1
                else:
                    plan.clamped_row += 1
            max_height = _clamp_to_box(paragraph, original, is_vertical, flipped, max_height)

        def place(height: float, offset: float) -> fitz.Rect:
            """按"阅读方向偏移"构造矩形。

            offset 是沿阅读方向、从本段起点算起的距离：
              - 正常横排：起点在上边，向下延伸；
              - 180° 倒排：起点在下边，向上延伸。
            """
            if flipped:
                bottom = original.y1 - offset
                return fitz.Rect(original.x0, bottom - height, original.x1, bottom)
            top = original.y0 + offset
            return fitz.Rect(original.x0, top, original.x1, top + height)

        if mode == "mono":
            # ---- 嵌字：矩形就是原段落框，必要时向阅读方向借用空白 ----
            render = pick_font(
                target, paragraph.bold, target_lang, font_family,
                source_font=paragraph.font, source_bold=paragraph.bold, config=config,
                # 竖排走 insert_textbox，只能用一个字体：必须避开会把 ASCII
                # 反查错的中文字库（等线会把 - 变成 U+2010）
                ascii_safe=is_vertical,
            )
            # 判定字形要用**实际绘制用的那个字体**：
            #   - 横排走 FontPair.runs()，可以按字符换字体，所以判定也用字体对；
            #   - 竖排走 insert_textbox，整段只用一个字体（中文字体），用字体对判定
            #     会得出"有字形"的结论而实际画成方块。
            glyph_font = render.font if is_vertical else render.pair
            target = sanitize_for_font(
                target, glyph_font, allow_ascii=symbol_fallback
            )
            first_baseline, original_pitch = _baseline_grid(paragraph, rotation)
            leading = Leading(spacing=line_spacing, pitch=original_pitch)
            result = fit_text(
                target, render.pair, line_limit, max_height, max_size, min_size, leading
            )

            frame = None
            if is_vertical:
                # 旋转文本：rect 就是原始 bbox，坐标系从原文数据推出来
                # （见 _rotated_frame），绘制时用 TextWriter + 旋转矩阵手工画
                rect = fitz.Rect(original)
                frame = _rotated_frame(paragraph)
                # 竖排不做字距拉伸：旋转框里的行宽已被固定，拉长会溢出单元格
                tracking: list[float] = []
            else:
                # 基线锚定：首行落在原文首行的基线上；没有基线信息时退回按框顶推算
                if first_baseline is not None:
                    top = first_baseline - render.pair.ascender * result.size
                    rect = fitz.Rect(original.x0, top, original.x1,
                                     top + max(original.height, result.height))
                else:
                    rect = place(max(original.height, result.height), 0.0)
                tracking = compute_tracking(
                    result.lines, render.pair, result.size, line_limit,
                    paragraph.align,
                    tracking_max if first_baseline is not None else 0.0,
                )

            if result.height > max_height + 0.5:
                plan.overflow += 1
            _mark_erase(plan, paragraph, original, page_paragraphs)
            plan.blocks.append(
                TextBlockPlan(
                    rect=rect, font=render.pair, fontname=render.face.key(), size=result.size,
                    lines=result.lines, color=int_to_rgb(paragraph.color),
                    align=_ALIGN_MAP.get(paragraph.align, fitz.TEXT_ALIGN_LEFT),
                    line_spacing=line_spacing, rotation=rotation,
                    fontfile=render.face.path,
                    first_baseline=first_baseline,
                    pitch=leading.step(render.pair, result.size),
                    tracking=tracking,
                    faux_bold=render.faux_bold,
                    origin_point=frame[0] if frame else None,
                    axis_along=frame[1] if frame else None,
                    axis_stack=frame[2] if frame else None,
                    run_length=frame[3] if frame else 0.0,
                )
            )
            continue

        # ---- 双语对照：原文 + 译文一起放进扩展后的矩形 ----
        render = pick_font(
            target, paragraph.bold, target_lang, font_family,
            source_font=paragraph.font, source_bold=paragraph.bold, config=config,
        )
        font = render.pair
        source_lines = [clean_text(line.text) for line in paragraph.lines if clean_text(line.text)]
        # 符号字体（Wingdings 等）留下的私有区字符要换成字体真正有的字形，
        # 否则会画成空白方块
        target = sanitize_for_font(target, font, allow_ascii=symbol_fallback)
        source_lines = [sanitize_for_font(line, font, allow_ascii=symbol_fallback)
                        for line in source_lines]
        if not source_lines:
            source_lines = wrap_text(source, font, max_size, line_limit)

        max_height = original.height + available

        def layout_at(size: float) -> tuple[float, list[str]]:
            """给定字号，返回 (总高度, 译文行)。"""
            gap = dual_gap_ratio * size
            source_h = len(source_lines) * natural_height(font, size) * line_spacing
            target_lines = wrap_text(target, font, size, line_limit)
            target_h = measure(target_lines, font, size, line_spacing)
            return source_h + gap + target_h, target_lines

        # 二分搜索最大可用字号
        low, high = min_size, max_size
        best_size = min_size
        best_total, best_lines = layout_at(min_size)
        if best_total <= max_height + 0.5:
            # 尝试放大
            total_at_max, lines_at_max = layout_at(max_size)
            if total_at_max <= max_height + 0.5:
                best_size, best_total, best_lines = max_size, total_at_max, lines_at_max
            else:
                for _ in range(12):
                    middle = (low + high) / 2.0
                    total, lines = layout_at(middle)
                    if total <= max_height + 0.5:
                        low, best_size, best_total, best_lines = middle, middle, total, lines
                    else:
                        high = middle
                    if high - low < 0.2:
                        break

        if best_total > max_height + 0.5:
            # 连最小字号都放不下"原文 + 译文"。
            # 此时**不能**把矩形撑大硬塞——那会让译文压到相邻段落上，
            # 变成谁都读不了。改为对该段退化成"仅译文"：内容完整、绝不重叠，
            # 代价只是这一小段没有保留原文。
            best_total = None

        if best_total is None:
            plan.fallback_mono += 1
            target = sanitize_for_font(target, font, allow_ascii=symbol_fallback)
            result = fit_text(target, font, line_limit, max_height, max_size,
                              min_size, line_spacing)
            _mark_erase(plan, paragraph, original, page_paragraphs)
            plan.blocks.append(
                TextBlockPlan(
                    rect=place(max(original.height, min(result.height, max_height)), 0.0),
                    font=font, fontname=render.face.key(), fontfile=render.face.path,
                    size=result.size,
                    lines=result.lines, color=int_to_rgb(paragraph.color),
                    align=_ALIGN_MAP.get(paragraph.align, fitz.TEXT_ALIGN_LEFT),
                    line_spacing=line_spacing, rotation=rotation,
                    faux_bold=render.faux_bold,
                )
            )
            continue

        rect_height = max(original.height, best_total)

        # 兜底：扩展后的矩形不允许压到别的段落身上。
        # 紧凑排版里相邻段落（例如"学名 + 种名"这种上下紧贴的标签）的 bbox
        # 本来就互相咬合，硬塞双语会让两边都糊在一起。
        if _overlaps_any(place(rect_height, 0.0), paragraph, page_paragraphs):
            plan.fallback_mono += 1
            target = sanitize_for_font(target, font, allow_ascii=symbol_fallback)
            result = fit_text(target, font, line_limit, max_height, max_size,
                              min_size, line_spacing)
            _mark_erase(plan, paragraph, original, page_paragraphs)
            plan.blocks.append(
                TextBlockPlan(
                    rect=place(max(original.height, min(result.height, max_height)), 0.0),
                    font=font, fontname=render.face.key(), fontfile=render.face.path,
                    size=result.size,
                    lines=result.lines, color=int_to_rgb(paragraph.color),
                    align=_ALIGN_MAP.get(paragraph.align, fitz.TEXT_ALIGN_LEFT),
                    line_spacing=line_spacing, rotation=rotation,
                    faux_bold=render.faux_bold,
                )
            )
            continue

        rect = place(rect_height, 0.0)

        _mark_erase(plan, paragraph, original, page_paragraphs)

        gap = dual_gap_ratio * best_size
        source_height = len(source_lines) * natural_height(font, best_size) * line_spacing
        align = _ALIGN_MAP.get(paragraph.align, fitz.TEXT_ALIGN_LEFT)

        # 原文（沿用原有断行），位于阅读方向的起点一侧
        plan.blocks.append(
            TextBlockPlan(
                rect=place(source_height, 0.0),
                font=font, fontname=render.face.key(), fontfile=render.face.path,
                size=best_size, lines=source_lines,
                color=int_to_rgb(paragraph.color), align=align,
                line_spacing=line_spacing, rotation=rotation,
                faux_bold=render.faux_bold,
            )
        )
        # 分隔线
        separator = place(0.35, source_height + gap * 0.45)
        plan.separators.append((separator, (0.86, 0.87, 0.89)))
        # 译文：排在分隔线之后，占据扩展矩形的剩余部分
        target_offset = source_height + gap
        target_height = max(0.0, rect_height - target_offset)
        plan.blocks.append(
            TextBlockPlan(
                rect=place(target_height, target_offset),
                font=font, fontname=render.face.key(), fontfile=render.face.path,
                size=best_size, lines=best_lines,
                color=int_to_rgb(paragraph.color), align=align,
                line_spacing=line_spacing, rotation=rotation,
                faux_bold=render.faux_bold,
            )
        )

    return plan


def draw_plan(page: "fitz.Page", plan: PagePlan) -> None:
    """把排版计划画到页面上。"""
    # ---- 1) 先擦除原文（只删文字，保留图片与矢量图形）----
    if plan.redactions:
        for rect, fill in plan.redactions:
            background, uniform = estimate_background(page, rect)
            use_fill = background if uniform else None
            try:
                page.add_redact_annot(rect, fill=use_fill)
            except TypeError:
                # 老版本 PyMuPDF 不接受 fill=None
                page.add_redact_annot(rect, fill=background if uniform else (1, 1, 1))
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        except TypeError:
            page.apply_redactions()

    # ---- 2) 遮盖扫描件里的原文 ----
    # 扫描件的文字是图像像素，没有文字对象可删，只能画一块与背景同色的矩形盖住它。
    if plan.covers:
        for rect in plan.covers:
            background, uniform = estimate_background(page, rect)
            color = background if uniform else (1.0, 1.0, 1.0)
            try:
                page.draw_rect(rect, color=color, fill=color, width=0, overlay=True)
            except Exception:
                logger.debug("遮盖原文失败", exc_info=True)

    # ---- 3) 画分隔线 ----
    for rect, color in plan.separators:
        try:
            page.draw_rect(rect, color=color, fill=color, width=0)
        except Exception:
            logger.debug("分隔线绘制失败", exc_info=True)

    # ---- 4) 逐块写入文字 ----
    for block in plan.blocks:
        if not any(line.strip() for line in block.lines):
            continue
        if block.rotation in (90, 270):
            # 竖排：TextWriter 不支持旋转，走 insert_textbox（带防丢字兜底）
            _draw_rotated(page, block)
        else:
            # 0° 正常绘制；180° 倒排（折页背面等）用 morph 旋转
            _draw_block(page, block, rotate_180=(block.rotation == 180))


def _rotated_fontname(page: "fitz.Page", block: TextBlockPlan) -> str:
    """为来自文件的字体在页面上注册一个 PDF 字体名，供 insert_textbox 使用。

    内置字体（helv / china-s …）直接用名字即可；系统字体是文件，
    必须先 `insert_font` 注册，否则 insert_textbox 会因为找不到字体而报错。
    """
    if not block.fontfile:
        return block.fontname
    name = _CUSTOM_FONT_NAMES.get(block.fontfile)
    if name is None:
        name = f"PDTL{len(_CUSTOM_FONT_NAMES)}"
        _CUSTOM_FONT_NAMES[block.fontfile] = name
    try:
        page.insert_font(fontname=name, fontfile=block.fontfile)
    except Exception:
        # 重复注册是正常的（同一页多个同字体文本块），忽略即可
        logger.debug("字体注册跳过：%s", block.fontfile, exc_info=True)
    return name


def insert_rotate_of(rotation: int) -> int:
    """把"段落方向角"换算成 `insert_textbox` 的 `rotate` 参数。

    两者**不是**同一个角度，而是相反数关系。实测：

        insert_textbox(rotate=90)  ->  产出行的 dir = (0, -1)   自下而上
        insert_textbox(rotate=270) ->  产出行的 dir = (0,  1)   自上而下

    而 `_rotation_of()` 对 `dir=(0,-1)` 返回 -90（归一化后 270）。
    早先直接把 270 传进去，产出方向就变成了 (0,1) —— **上下颠倒**，
    与原文相反。这正是"表格里文字朝向不一致"的直接原因。
    """
    return (360 - int(rotation)) % 360


def _draw_rotated(page: "fitz.Page", block: TextBlockPlan) -> None:
    """竖排文本（±90°）：用 `insert_textbox` 绘制。

    ## 方向问题（已修）

    `insert_textbox` 的 `rotate` 参数与文本方向**相反**，见 `insert_rotate_of()`。
    早先直接把段落方向角传进去，整列字就上下颠倒了 —— 实测 2113 处朝向错乱。

    ## 字体问题

    `insert_textbox` 只接受**单一个** `fontname`，没法像横排那样"汉字用中文字体、
    拉丁用内置拉丁字体"。所以给竖排段落挑字体时必须**避开会把 ASCII 反查错的字库**：
    等线会把 `-` 变成 `U+2010`（产出里 856 处连字符搜不到）。
    这一条由 `pick_font(ascii_safe=True)` 保证。

    ## 放不下时

    `insert_textbox` 放不下会返回**负数且一个字符都不写**，所以逐级缩小字号；
    仍放不下才退化为水平绘制（宁可方向不完美，也不能丢内容）。
    """
    text = "\n".join(block.lines)
    rect = fitz.Rect(block.rect)
    size = block.size
    min_size = 3.0
    fontname = _rotated_fontname(page, block)
    rotate = insert_rotate_of(block.rotation)

    while size >= min_size:
        try:
            remaining = page.insert_textbox(
                rect,
                text,
                fontname=fontname,
                fontsize=size,
                color=block.color,
                align=block.align,
                rotate=rotate,
                overlay=True,
            )
        except Exception:
            logger.debug("旋转文本写入异常", exc_info=True)
            break
        if remaining is not None and remaining >= 0:
            return
        size -= 0.5

    logger.warning(
        "竖排文本（原文方向 %.0f°，绘制 rotate=%d）在 %.0f×%.0f 的框内放不下，"
        "已回退为水平绘制以免丢失内容：%r",
        block.rotation, rotate, rect.width, rect.height, text[:30],
    )
    _draw_block(page, block, bottom=page_space(page).y1)


def line_width(font, text: str, size: float, tracking: float = 0.0) -> float:
    """一行文本的实际宽度（含中英间隙与字距拉伸）。

    单独抽出来是为了让**测量**与**绘制**共用同一套算法 —— 两者不一致就会出现
    "算着放得下、画出来却出框"，或者居中的行看着偏一边。
    传入字体对（`FontPair`）时按脚本分别度量；传入普通 `fitz.Font` 时退化为
    单字体度量。
    """
    if not text:
        return 0.0
    pair = font if isinstance(font, FontPair) else None
    total = 0.0
    previous = ""
    for char in text:
        if pair is not None and pair.needs_inter_space(previous, char):
            total += INTER_SCRIPT_SPACE * size
        face = pair.font_for(char) if pair is not None else font
        total += face.text_length(char, fontsize=size)
        previous = char
    if tracking and len(text) > 1:
        total += tracking * (len(text) - 1)
    return total


def _append_line(
    writer: "fitz.TextWriter",
    block: TextBlockPlan,
    origin: tuple[float, float],
    line: str,
    tracking: float,
) -> int:
    """写入一行文本，返回实际追加的片段数。

    字体对里的不同脚本要用各自的字体绘制，所以按"同字体片段"分组：
    没有字距拉伸时一段一次 `append`（文字对象更少、提取更连贯）；
    需要拉伸时逐字定位（`TextWriter.append` 没有字距参数）。
    实测逐字追加不影响文本提取的正确性，产物体积也没有变化。
    """
    pair = block.font if isinstance(block.font, FontPair) else None
    size = block.size

    if pair is None:
        if tracking <= 0.01:
            writer.append(origin, line, font=block.font, fontsize=size)
            return 1
        x, y = origin
        for char in line:
            writer.append((x, y), char, font=block.font, fontsize=size)
            x += block.font.text_length(char, fontsize=size) + tracking
        return len(line)

    written = 0
    x, y = origin
    previous = ""
    for text, face, _key in pair.runs(line):
        if tracking > 0.01:
            for char in text:
                if pair.needs_inter_space(previous, char):
                    x += INTER_SCRIPT_SPACE * size
                writer.append((x, y), char, font=face, fontsize=size)
                x += face.text_length(char, fontsize=size) + tracking
                previous = char
                written += 1
        else:
            if text and pair.needs_inter_space(previous, text[0]):
                x += INTER_SCRIPT_SPACE * size
            writer.append((x, y), text, font=face, fontsize=size)
            x += face.text_length(text, fontsize=size)
            previous = text[-1]
            written += 1
    return written


def _draw_block(
    page: "fitz.Page",
    block: TextBlockPlan,
    rotate_180: bool = False,
    bottom: float | None = None,
) -> None:
    """按基线逐行写出文本，行位置由我们自己计算，避免自动折行带来的误差。

    首行基线优先用原文的绝对基线（`block.first_baseline`），这样无论新字号
    比原文大还是小，文字都落在原来那一行的位置上，不会整体上下漂移。

    Args:
        rotate_180: 文本需要倒排（原文 dir 为 (-1,0)，例如折页背面的面板）。
            实现方式是照常在矩形内排版，再对整块文字施加"绕矩形中心的 180° 旋转"
            —— 因为矩形关于自身中心是对称的，旋转后文字仍落在同一个矩形里，
            但方向（含行序）整体翻转，与原文一致。
    """
    try:
        writer = fitz.TextWriter(page.rect, color=block.color)
    except TypeError:
        writer = fitz.TextWriter(page.rect)

    if bottom is None:
        bottom = page_space(page).y1
    ascent = block.font.ascender * block.size
    width = block.rect.width
    if block.pitch and block.pitch > 0:
        lineheight = block.pitch
    else:
        lineheight = natural_height(block.font, block.size) * block.line_spacing

    # 倒排要经过 morph 镜像，绝对基线会被镜像到别处，所以那时只能按框顶推算
    if block.first_baseline is not None and not rotate_180:
        start = block.first_baseline
    else:
        start = block.rect.y0 + ascent

    written = 0
    for index, line in enumerate(block.lines):
        if not line.strip():
            continue
        baseline = start + index * lineheight
        # 底边同样要按文字坐标空间取，不能用 page.rect.y1：
        # 带 /Rotate 90 的页面两者差 175pt，会把页面下半部分的译文全部丢掉
        if baseline - ascent > bottom + 2:
            break
        tracking = block.tracking[index] if index < len(block.tracking) else 0.0
        measured = line_width(block.font, line, block.size, tracking)
        if block.align == fitz.TEXT_ALIGN_CENTER:
            x = block.rect.x0 + max(0.0, (width - measured) / 2.0)
        elif block.align == fitz.TEXT_ALIGN_RIGHT:
            x = block.rect.x1 - measured
        else:
            x = block.rect.x0
        try:
            written += _append_line(writer, block, (x, baseline), line, tracking)
        except Exception:
            logger.debug("文本行追加失败：%r", line[:40], exc_info=True)

    if not written:
        return

    morph = None
    if rotate_180:
        center = fitz.Point(
            (block.rect.x0 + block.rect.x1) / 2.0,
            (block.rect.y0 + block.rect.y1) / 2.0,
        )
        morph = (center, fitz.Matrix(-1, 0, 0, -1, 0, 0))

    # 字体没有粗体字重时，用"填充 + 描边"做伪粗体。
    # 注意**不能**改用"同一位置描两遍"：实测那样文字会被提取两次，
    # 复制粘贴出现重复内容，回读校验也会误判。
    render_mode = 2 if block.faux_bold else 0

    try:
        writer.write_text(page, color=block.color, overlay=True, morph=morph,
                          render_mode=render_mode)
    except TypeError:
        # 老版本 PyMuPDF 不支持 morph / render_mode
        if morph is not None:
            logger.warning("当前 PyMuPDF 不支持 morph，倒排文本将按正向绘制")
        try:
            writer.write_text(page, color=block.color, overlay=True, morph=morph)
        except TypeError:
            try:
                writer.write_text(page, color=block.color, overlay=True)
            except TypeError:
                writer.write_text(page, overlay=True)
    except Exception:
        logger.debug("文本写入失败", exc_info=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def typeset_document(
    doc: "fitz.Document",
    paragraphs: list[Paragraph],
    config: dict,
    mode: str,
    log=None,
    cancel_event=None,
    progress=None,
) -> dict:
    """把译文写回整个文档（就地修改 doc）。

    Args:
        log: 日志回调 `log(message: str)`。
        progress: 进度回调 `progress(page_number: int, total_pages: int)`。

    Returns:
        统计信息 dict：{"pages": n, "blocks": n, "overflow": n, "skipped": n, "fonts": [...]}
    """
    log = log or (lambda message: logger.info(message))

    # 按页归组：包含未翻译段落，用于碰撞检测
    by_page: dict[int, list[Paragraph]] = {}
    for paragraph in paragraphs:
        by_page.setdefault(paragraph.page, []).append(paragraph)

    stats = {"pages": 0, "blocks": 0, "overflow": 0, "skipped": 0,
             "fallback_mono": 0, "clamped_cell": 0, "clamped_row": 0}
    used_fonts: dict[str, int] = {}

    targets = [index for index in sorted(by_page) if any(p.target for p in by_page[index])]
    total = len(targets)

    for position, page_index in enumerate(targets, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        page_paragraphs = by_page[page_index]
        page = doc.load_page(page_index)
        # 页面高度必须取"文字坐标空间"的高度（见 page_space 的说明）
        plan = plan_page(page_paragraphs, float(page_space(page).height), config, mode)
        for block in plan.blocks:
            key = block.fontname
            used_fonts[key] = used_fonts.get(key, 0) + 1
        draw_plan(page, plan)
        stats["pages"] += 1
        stats["blocks"] += len(plan.blocks)
        stats["overflow"] += plan.overflow
        stats["skipped"] += plan.skipped
        stats["fallback_mono"] += plan.fallback_mono
        stats["clamped_cell"] += plan.clamped_cell
        stats["clamped_row"] += plan.clamped_row
        log(f"第 {page_index + 1} 页排版完成（{len(plan.blocks)} 个文本块）")
        if progress is not None:
            try:
                progress(position, total)
            except Exception:
                logger.debug("进度回调异常", exc_info=True)

    stats["fonts"] = _describe_fonts(used_fonts)
    if stats["fonts"]:
        log("嵌字字体：" + "，".join(stats["fonts"]))

    if stats["fallback_mono"]:
        log(
            f"提示：{stats['fallback_mono']} 段因下方空间不足，已自动改为只显示译文"
            f"（避免与相邻段落重叠）；如需完整双语建议增大字号缩放或改用“仅译文”模式"
        )
    if stats["overflow"]:
        log(f"提示：有 {stats['overflow']} 段因版面过于紧凑而缩到最小字号，建议改用“仅译文”模式")
    if stats["clamped_cell"] or stats["clamped_row"]:
        log(f"表格约束：{stats['clamped_cell']} 段收紧到单元格内、"
            f"{stats['clamped_row']} 段收紧到表格行内（避免压到相邻行/格）")
    return stats


def _describe_fonts(used: dict[str, int]) -> list[str]:
    """把"用到了哪些字体"整理成可读的一行行文本。"""
    described: list[str] = []
    for key, count in sorted(used.items(), key=lambda item: -item[1]):
        if not key:
            continue
        if os.sep in key or "/" in key or "\\" in key:
            described.append(f"{Path(key).stem}×{count}")
        else:
            described.append(f"{key}×{count}")
    return described[:6]

