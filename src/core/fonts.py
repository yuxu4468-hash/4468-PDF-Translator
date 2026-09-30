"""嵌字字体解析：为译文挑选与原文字体风格最接近的中文字体。

## 为什么需要这个模块

MuPDF 内置的 9 个 CJK 字体名（`china-s` / `china-ss` / `china-t` / `japan` /
`korea` …）**全部**指向同一个 `Droid Sans Fallback Regular` —— 只有一种字重、
一种风格（实测确认）。也就是说，只用内置字体的话：

- 原文里所有**加粗**标题在译文里都会掉成正常粗细；
- 原文是衬线体时也无法还原。

这是"嵌字"最显眼的质量缺陷，所以这里做三件事：

1. 从原文 span 的字体名与 flags 推断风格（衬线 / 无衬线 / 粗体）；
2. 在本机字体里找风格匹配、且**能被 MuPDF 正确子集化**的中文字体；
3. 实在找不到粗体字重时，用 `render_mode=2`（填充 + 描边）做伪粗体兜底。

## 三条实测得出的硬约束

**1. CFF/OTF 字库没法子集化。** 例如 `Noto Sans SC (TrueType).otf`
（其实是 CFF 轮廓），`Document.subset_fonts()` 对它**静默失败**（只在 stderr
打印 `MuPDF error: format error: Index bounds`，不抛异常），输出 PDF 会从
几十 KB 膨胀到 **7.3 MB**。TrueType(glyf) 字库则能压到 8–80 KB。
所以候选排序必须强烈偏向 glyf 字库 —— 见 `_sfnt_tables()`。

**2. 可变字体（VF）不能用。** MuPDF 会取第一个命名实例，实测
`NotoSansSC-VF.ttf` 解析出来是 `Noto Sans SC Thin`、`NotoSerifSC-VF.ttf`
是 `Noto Serif SC ExtraLight`，字重严重偏细，直接排除。

**3. 伪粗体不能"同一位置描两遍"。** 那样文字会被**提取两次**：复制粘贴会
得到重复内容，回读校验也会出错。只能用 `render_mode=2`（实测墨水 +71%，
且文本只出现一次）。

本模块不下载任何东西：字体全部来自本机已安装的字体（Windows 注册表 /
macOS / Linux 字体目录）。找不到就用 MuPDF 内置字体兜底。
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

from ..shared.constants import CJK_TARGET_LANGS

logger = logging.getLogger("Fonts")

# ---------------------------------------------------------------------------
# 风格识别
# ---------------------------------------------------------------------------

#: 判断顺序很重要：先 mono，再 serif，最后 sans（无衬线是绝大多数正文的默认）
_MONO_HINTS = ("mono", "courier", "consolas", "menlo", "monaco", "typewriter")

_SERIF_HINTS = (
    "song", "sung", "simsun", "nsimsun", "sun-ext", "serif", "times", "georgia",
    "garamond", "minion", "sabon", "baskerville", "caslon", "didot", "bodoni",
    "book", "ming", "sungti", "songti", "fangsong", "kai", "kaiti", "stsong",
    "宋", "明朝", "仿宋", "楷", "楷体", "思源宋", "source han serif",
    "noto serif", "sourcehanserif",
)

_SANS_HINTS = (
    "hei", "heiti", "sans", "gothic", "grotesk", "grotesque", "arial",
    "helvetica", "calibri", "tahoma", "verdana", "segoe", "myriad", "frutiger",
    "univers", "roboto", "open sans", "lato", "din", "franklin", "candara",
    "corbel", "optima", "futura", "gill", "avenir", "黑", "雅黑", "等线",
    "思源黑", "source han sans", "puhuiti", "harmony", "dengxian", "jhenghei",
    "yahei",
)

#: 名字里出现这些词说明是粗体（除了 flags 的 bit 16 之外的补充判据）
_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "extrabold", "ultrabold")

#: 名字里出现这些词说明是细体，要排除（Light / Thin 不是我们想要的常规字重）
_LIGHT_HINTS = ("light", "thin", "extralight", "ultralight", "hairline")


@dataclass(frozen=True)
class FontStyle:
    """原文文本的风格特征，用来挑匹配的译文字体。"""

    serif: bool = False
    bold: bool = False
    mono: bool = False
    italic: bool = False
    light: bool = False

    def describe(self) -> str:
        parts = []
        parts.append("衬线" if self.serif else "无衬线")
        if self.mono:
            parts.append("等宽")
        if self.bold:
            parts.append("粗体")
        if self.italic:
            parts.append("斜体")
        return "+".join(parts)


def classify_source_font(font_name: str, flags: int = 0, bold: bool = False) -> FontStyle:
    """由原文 span 的字体名与 flags 推断风格。"""
    name = (font_name or "").lower()
    # 去掉子集前缀，例如 "ABCDEF+Calibri-Bold"
    if "+" in name:
        name = name.split("+", 1)[1]

    mono = any(hint in name for hint in _MONO_HINTS)
    serif = any(hint in name for hint in _SERIF_HINTS)
    sans = any(hint in name for hint in _SANS_HINTS)
    if sans and not serif:
        serif = False
    elif serif and not sans:
        serif = True
    elif serif and sans:
        # 两边都命中（例如 "SimSun" 命中 serif、"Songti SC" 命中 serif+sans 之外的组合）
        # 以更靠前的 serif 判据为准
        serif = True
    else:
        # 完全没线索：绝大多数现代文档正文是无衬线，按无衬线处理
        serif = False

    is_bold = bool(bold) or bool(flags & 16) or any(hint in name for hint in _BOLD_HINTS)
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    return FontStyle(serif=serif, bold=is_bold, mono=mono, italic=italic)


# ---------------------------------------------------------------------------
# 字库格式探测
# ---------------------------------------------------------------------------

def sfnt_tables(path: str | os.PathLike) -> set[str]:
    """读取 sfnt 表目录，返回表标签集合（不依赖 fonttools）。

    TTC（字体集合）会先跳到第一个 face 的表目录。
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
            if len(header) < 12:
                return set()
            offset = 0
            if header[:4] == b"ttcf":
                handle.seek(12)
                count_raw = handle.read(4)
                if len(count_raw) < 4:
                    return set()
                count = struct.unpack(">I", count_raw)[0]
                if count == 0:
                    return set()
                first = handle.read(4)
                if len(first) < 4:
                    return set()
                offset = struct.unpack(">I", first)[0]
            handle.seek(offset)
            head = handle.read(12)
            if len(head) < 12:
                return set()
            num_tables = struct.unpack(">H", head[4:6])[0]
            if not 0 < num_tables <= 512:
                return set()
            data = handle.read(16 * num_tables)
    except OSError:
        return set()

    tags: set[str] = set()
    for index in range(num_tables):
        chunk = data[index * 16 : (index + 1) * 16]
        if len(chunk) < 4:
            break
        tags.add(chunk[:4].decode("latin-1", "replace"))
    return tags


def font_traits(path: str | os.PathLike) -> dict:
    """判断字库是否可安全使用：轮廓格式 / 是否可变字体 / 是否子集友好。"""
    tags = sfnt_tables(path)
    has_cff = "CFF " in tags or "CFF2" in tags
    has_glyf = "glyf" in tags
    variable = "fvar" in tags
    if has_glyf:
        outline = "glyf"
    elif has_cff:
        outline = "cff"
    else:
        outline = "unknown"
    return {
        "outline": outline,
        "variable": variable,
        # 只有 glyf 字库实测能被 subset_fonts() 正确压缩
        "subset_safe": outline == "glyf" and not variable,
    }


# ---------------------------------------------------------------------------
# 字体发现
# ---------------------------------------------------------------------------

#: 注册表里名字带这些词的才算候选中文字体（只是初筛，真正判据是字形检查）
_CJK_NAME_HINTS = re.compile(
    r"yahei|jhenghei|simsun|nsimsun|simhei|simkai|simfang|dengxian|fangsong|kaiti|"
    r"heiti|songti|stxihei|stkaiti|stsong|stsong|noto\s*sans\s*sc|noto\s*serif\s*sc|"
    r"source\s*han|sourcesans|wenquanyi|wqy|pingfang|hiragino|mingliu|pmingliu|"
    r"malgun|meiryo|yu\s*gothic|ms\s*gothic|puhuiti|harmonyos|alibaba|"
    r"思源|黑体|宋体|楷体|雅黑|等线|仿宋|"
    r"hei|song|ming|kai|gothic|cjk|han",
    re.IGNORECASE,
)

#: 内置兜底字体（MuPDF 自带，实测全部指向 Droid Sans Fallback Regular）
_BUILTIN_CJK = "china-s"

#: 简体覆盖度下限：低于它就不允许用于中文输出
SIMPLIFIED_MIN_COVERAGE = 0.90

_FONT_EXT = (".ttf", ".ttc", ".otf", ".otc", ".ttc2")


@dataclass
class FontFace:
    """一个候选中文字体。"""

    name: str
    path: str = ""
    builtin: str = ""
    bold: bool = False
    serif: bool = False
    cjk: bool = True
    source: str = "系统"
    subset_safe: bool = True
    variable: bool = False
    outline: str = "glyf"
    #: 空格能否被正确提取（见 `space_extracts_correctly`）
    space_ok: bool = True
    #: 细体（Light / Thin）。细体是独立字重，**不是**"常规"，
    #: 用它渲染正文会明显偏细，所以不满足"常规字重"的请求。
    light: bool = False
    #: 汉字能否按**标准码位**提取（见 `cjk_roundtrip_ok`）
    cjk_ok: bool = True
    #: ASCII 能否按**原码位**提取（见 `ascii_roundtrip_ok`）。
    #: 竖排文本只能用一个字体，拉丁字符也会走中文字体，所以这一项很关键。
    ascii_ok: bool = True
    #: 简体汉字覆盖度（0~1）。日文/韩文字体常常覆盖不全，用它渲染简体译文
    #: 会出现缺字 -> notdef -> 产出里提取为空。
    simplified_coverage: float = 1.0

    def key(self) -> str:
        return self.builtin or self.path

    def label(self) -> str:
        weight = "粗体" if self.bold else "常规"
        style = "衬线" if self.serif else "无衬线"
        return f"{self.name}（{style}/{weight}）"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label(),
            "path": self.path,
            "bold": self.bold,
            "serif": self.serif,
            "source": self.source,
            "subset_safe": self.subset_safe,
            "variable": self.variable,
            "outline": self.outline,
            "space_ok": self.space_ok,
            "light": self.light,
            "cjk_ok": self.cjk_ok,
            "ascii_ok": self.ascii_ok,
            "simplified_coverage": round(self.simplified_coverage, 3),
        }


#: 首选项排序（越靠前越优先）。挑的都是中文文档里的常见正文与标题字体。
_PREFERRED_SANS = (
    "microsoft yahei", "微软雅黑", "msyh",
    "dengxian", "等线",
    "source han sans", "noto sans sc", "思源黑体", "sourcesans",
    "pingfang sc", "harmonyos sans", "alibaba puhuiti",
    "wenquanyi micro hei", "hiragino sans", "heiti sc", "stxihei",
    "microsoft jhenghei", "simhei", "黑体",
)

_PREFERRED_SERIF = (
    "source han serif", "noto serif sc", "思源宋体", "sourceserif",
    "songti sc", "simsun", "宋体", "stsong", "fangsong", "仿宋",
    "kaiti", "楷体", "mingliu", "pmingliu",
)


def _preference_rank(name: str, serif: bool) -> int:
    """越小越优先；没有命中偏好表就给一个较大的基础分。"""
    lowered = (name or "").lower()
    table = _PREFERRED_SERIF if serif else _PREFERRED_SANS
    for index, token in enumerate(table):
        if token in lowered:
            return index
    return 500


_FACES: list[FontFace] | None = None
_FACES_LOCK = threading.Lock()
_LOADED: dict[str, fitz.Font] = {}
_LOAD_LOCK = threading.Lock()
#: 空格提取探测结果缓存（key -> 是否正常）
_SPACE_OK: dict[str, bool] = {}
#: 汉字码位往返探测结果缓存（key -> 是否正常）
_CJK_OK: dict[str, bool] = {}
#: ASCII 码位往返探测结果缓存（key -> 是否正常）
_ASCII_OK: dict[str, bool] = {}
#: 简体覆盖度缓存（key -> 0~1）
_SIMPLIFIED_OK: dict[str, float] = {}
#: 符号落点缓存（码位 -> 字体；值为 None 表示本机确实没有该字形）
_SYMBOL_FACE: dict[int, "FontFace | None"] = {}
_SYMBOL_LOCK = threading.Lock()

#: 探测结果磁盘缓存（字体探测要加载几十个字库，跨进程复用能省 1~3 秒）
_CACHE_VERSION = 7


def _cache_path() -> Path:
    from ..shared.path_helpers import CACHE_DIR

    return CACHE_DIR / f"system_fonts_v{_CACHE_VERSION}.json"


def load_font(face: FontFace) -> fitz.Font:
    """取得（并缓存）字体对象。"""
    key = face.key()
    with _LOAD_LOCK:
        cached = _LOADED.get(key)
        if cached is not None:
            return cached
        if face.builtin:
            font = fitz.Font(face.builtin)
        else:
            font = fitz.Font(fontfile=face.path)
        _LOADED[key] = font
        return font


def space_extracts_correctly(face: "FontFace") -> bool:
    """探测这个字库的空格能否被**正确提取**（而不是变成不换行空格 NBSP）。

    有些字库把 U+0020 与 U+00A0 映射到同一个字形，MuPDF 生成反查表时会选中
    U+00A0。后果不是"看着不对"，而是从产出 PDF 里**复制/搜索文字时全部失配**：
    实测微软雅黑输出 `Vipera\\xa0berus`，于是搜 `"Vipera berus"` 搜不到。
    等线 / 黑体 / 宋体 / 内置字体都没有这个问题。

    探测方式最直接：真写一小段带空格的文字，再读回来比对。
    """
    key = face.key()
    cached = _SPACE_OK.get(key)
    if cached is not None:
        return cached
    result = True
    try:
        scratch = fitz.open()
        page = scratch.new_page(width=160, height=40)
        font = load_font(face)
        writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
        writer.append((10, 25), "A B", font=font, fontsize=12)
        writer.write_text(page)
        probe = fitz.open("pdf", scratch.tobytes(garbage=4, deflate=True))
        try:
            result = probe[0].get_text().strip() == "A B"
        finally:
            probe.close()
        scratch.close()
    except Exception:
        # 探测本身失败时不惩罚该字体（宁可用了它，也不要无谓地把它排到最后）
        logger.debug("空格提取探测失败：%s", face.name, exc_info=True)
        result = True
    _SPACE_OK[key] = result
    if not result:
        logger.info("字体 %s 的空格会变成 NBSP，已降权", face.name)
    return result


#: 用于探测"汉字能否按标准码位提取"的样本：CJK 兼容表意文字对应的标准汉字。
#: 这些字**正是**容易在两个码位之间被反查错的那一批。
_CJK_PROBE = "".join(chr(code) for code in range(0xF900, 0xFB00))  # 占位，运行时替换


def _compat_probe_text() -> str:
    """构造探测串：把 U+F900–U+FAFF 的兼容字符用 NFKC 还原成标准汉字。"""
    import unicodedata

    out = []
    for code in range(0xF900, 0xFB00):
        normalized = unicodedata.normalize("NFKC", chr(code))
        if len(normalized) == 1 and normalized != chr(code) and "\u4e00" <= normalized <= "\u9fff":
            out.append(normalized)
    return "".join(out)


def cjk_roundtrip_ok(face: "FontFace") -> bool:
    """探测汉字能否按**标准码位**提取回来。

    CJK 兼容表意文字（U+F900–U+FAFF）与统一表意文字是同一个字形的两个码位
    （例如 `利` U+5229 与 U+F9DD）。字库若同时收录两者，MuPDF 生成 ToUnicode
    反查表时可能挑中兼容码位，结果是：**画面完全正常，但从产出 PDF 里复制/
    搜索出来的字对不上**（搜"权利"搜不到，因为拿到的是"权利"）。

    实测 Noto Sans SC 有 107 个字、思源宋体 Heavy 有 66 个字会这样；
    SimSun / SimHei / 等线 / 微软雅黑 / 内置字体则完全干净。
    这是 pdf2zh 系列项目同样踩到的坑（BabelDOC 专门发了 PR 把兼容码位转回统一汉字）。
    """
    key = face.key()
    cached = _CJK_OK.get(key)
    if cached is not None:
        return cached

    result = True
    try:
        probe = _compat_probe_text()
        scratch = fitz.open()
        page = scratch.new_page(width=520, height=420)
        font = load_font(face)
        writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
        chunk = 28
        for index in range(0, len(probe), chunk):
            writer.append((10, 30 + (index // chunk) * 18), probe[index:index + chunk],
                          font=font, fontsize=14)
        writer.write_text(page)
        reread = fitz.open("pdf", scratch.tobytes(garbage=4, deflate=True))
        try:
            got = reread[0].get_text().replace("\n", "").replace("\xa0", " ").strip()
        finally:
            reread.close()
        scratch.close()
        remaining = list(got)
        result = all(char in remaining and not remaining.remove(char) for char in probe)
    except Exception:
        logger.debug("汉字码位探测失败：%s", face.name, exc_info=True)
        result = True

    _CJK_OK[key] = result
    if not result:
        logger.info("字体 %s 会把部分汉字提取成兼容字符，已降权", face.name)
    return result


#: ASCII 往返探测样本：覆盖竖排表格里最常见的那批符号
_ASCII_PROBE = "A B-C.D,E/F:G(H)1 2.5% +-*=_#@& 2020"


def ascii_roundtrip_ok(face: "FontFace") -> bool:
    """探测字库能否把 ASCII 字符按**原码位**还原出来。

    为什么需要：竖排文本走 `insert_textbox`，它只接受**单一字体**，
    拉丁字符也会用中文字体渲染。而部分中文字库把多个码位映射到同一字形，
    MuPDF 反查时挑错，产出里就成了"看着对、复制/搜索对不上"：

      - 等线：`-` -> `U+2010`（`Baden‐Württemberg` 搜不到）
      - 微软雅黑 / Noto Sans SC：空格 -> `U+00A0`

    实测 SimSun / SimHei / 华文宋体 / 内置字体都能完整还原。
    """
    key = face.key()
    cached = _ASCII_OK.get(key)
    if cached is not None:
        return cached

    result = True
    try:
        scratch = fitz.open()
        page = scratch.new_page(width=420, height=60)
        font = load_font(face)
        writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
        writer.append((10, 40), _ASCII_PROBE, font=font, fontsize=12)
        writer.write_text(page)
        reread = fitz.open("pdf", scratch.tobytes(garbage=4, deflate=True))
        try:
            got = reread[0].get_text().strip().replace("\n", "")
        finally:
            reread.close()
        scratch.close()
        result = got == _ASCII_PROBE
    except Exception:
        logger.debug("ASCII 往返探测失败：%s", face.name, exc_info=True)
        result = True

    _ASCII_OK[key] = result
    if not result:
        logger.info("字体 %s 会改变部分 ASCII 字符的码位，竖排排版将避开它", face.name)
    return result


#: 简体专用字样本：这些字与繁体字形不同，日文/韩文字体通常**没有**。
#: 用它们做覆盖度探测，能有效区分"能排简体"和"只能排日韩文"的字体。
_SIMPLIFIED_SAMPLE = (
    "们时说对现别类极图鉴检议护级种区发记录结果处理动保农业东车马鸟鱼虫贝页风飞"
    "书样点线让认识论试验证据标准规饲温湿设构规模细许关环务员产业长门问间闻"
    "见观规视觉词汇译术语买卖货质贝账赐则刚创删剂剑刘则们"
    "龙龟鳄蛙蛇蜥蝾螈蟾蜍鲵鳍鳞壳齿颚肾肠胆脑颅"
)


def simplified_coverage(face: "FontFace") -> float:
    """探测该字体对**简体汉字**的覆盖度（0~1）。

    为什么要单独探：日文（MS Gothic）、韩文（Malgun Gothic）字体同样收录大量
    汉字、ASCII 也干净，但**简体字形收录不全**。拿它们排简体译文时缺字会被画成
    notdef，产出 PDF 里提取为空 —— 表现为"整段译文没能写入"
    （实测某份 75 页文档因此丢了 130 段窄表头）。
    """
    key = face.key()
    cached = _SIMPLIFIED_OK.get(key)
    if cached is not None:
        return cached

    coverage = 1.0
    try:
        font = load_font(face)
        total = len(_SIMPLIFIED_SAMPLE)
        hit = sum(1 for char in _SIMPLIFIED_SAMPLE if font.has_glyph(ord(char)))
        coverage = hit / total if total else 1.0
    except Exception:
        logger.debug("简体覆盖度探测失败：%s", face.name, exc_info=True)
        coverage = 1.0

    _SIMPLIFIED_OK[key] = coverage
    if coverage < SIMPLIFIED_MIN_COVERAGE:
        logger.info("字体 %s 的简体覆盖度只有 %.0f%%，中文排版将避开它",
                    face.name, coverage * 100)
    return coverage


def _windows_font_entries() -> list[tuple[str, Path]]:
    """从注册表读取 (显示名, 文件路径)。显示名比文件名可靠得多。"""
    try:
        import winreg
    except ImportError:
        return []

    entries: list[tuple[str, Path]] = []
    roots = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Microsoft" / "Windows" / "Fonts")

    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            handle = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(handle)[1]
            for index in range(count):
                try:
                    display, value, _ = winreg.EnumValue(handle, index)
                except OSError:
                    break
                if not isinstance(value, str) or not value:
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    for root in roots:
                        probe = root / value
                        if probe.exists():
                            candidate = probe
                            break
                if candidate.suffix.lower() in _FONT_EXT and candidate.exists():
                    entries.append((display, candidate))
        finally:
            winreg.CloseKey(handle)
    return entries


def _posix_font_entries() -> list[tuple[str, Path]]:
    """macOS / Linux：扫描常见字体目录，名字取文件名（无注册表可用）。"""
    homes = [Path.home() / ".fonts", Path.home() / "Library" / "Fonts"]
    system = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]
    entries: list[tuple[str, Path]] = []
    for root in homes + system:
        if not root.is_dir():
            continue
        try:
            for path in list(root.rglob("*"))[:6000]:
                if path.is_file() and path.suffix.lower() in _FONT_EXT:
                    entries.append((path.stem, path))
        except OSError:
            continue
    return entries


def _display_to_face(display: str, path: Path) -> FontFace | None:
    """把一个 (显示名, 路径) 变成 FontFace；失败返回 None。"""
    name = re.sub(r"\((?:TrueType|OpenType|All res)\)", "", display).strip()
    name = name.split("&")[0].strip() or path.stem
    lowered = name.lower()

    traits = font_traits(str(path))
    face = FontFace(
        name=name,
        path=str(path),
        bold=any(hint in lowered for hint in _BOLD_HINTS),
        serif=classify_source_font(name).serif,
        source="系统",
        subset_safe=traits["subset_safe"],
        variable=traits["variable"],
        outline=traits["outline"],
        light=any(hint in lowered for hint in _LIGHT_HINTS),
    )
    try:
        font = load_font(face)
    except Exception:
        logger.debug("字体加载失败：%s", path, exc_info=True)
        return None
    # 真正的判据：能不能画中文
    if not font.has_glyph(0x4E2D):
        return None
    face.space_ok = space_extracts_correctly(face)
    face.cjk_ok = cjk_roundtrip_ok(face)
    face.ascii_ok = ascii_roundtrip_ok(face)
    face.simplified_coverage = simplified_coverage(face)
    return face


def _discover_uncached() -> list[FontFace]:
    entries = _windows_font_entries()
    if not entries:
        entries = _posix_font_entries()

    faces: list[FontFace] = []
    seen: set[str] = set()
    for display, path in entries:
        if not _CJK_NAME_HINTS.search(display):
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        face = _display_to_face(display, path)
        if face is not None:
            faces.append(face)

    # 内置兜底永远放在最后
    try:
        builtin_font = fitz.Font(_BUILTIN_CJK)
        if builtin_font.has_glyph(0x4E2D):
            builtin_face = FontFace(
                name=builtin_font.name or "MuPDF 内置 CJK 字体",
                builtin=_BUILTIN_CJK,
                serif=False,
                source="内置",
                subset_safe=True,
            )
            builtin_face.space_ok = space_extracts_correctly(builtin_face)
            builtin_face.cjk_ok = cjk_roundtrip_ok(builtin_face)
            builtin_face.ascii_ok = ascii_roundtrip_ok(builtin_face)
            builtin_face.simplified_coverage = simplified_coverage(builtin_face)
            faces.append(builtin_face)
    except Exception:
        logger.debug("内置 CJK 字体不可用", exc_info=True)

    logger.info("发现 %d 个可用中文字体", len(faces))
    return faces


def _read_cache() -> list[FontFace] | None:
    try:
        path = _cache_path()
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        faces = [
            FontFace(
                name=item["name"],
                path=item.get("path", ""),
                builtin=item.get("builtin", ""),
                bold=bool(item.get("bold")),
                serif=bool(item.get("serif")),
                source=item.get("source", "系统"),
                subset_safe=bool(item.get("subset_safe", True)),
                variable=bool(item.get("variable")),
                outline=item.get("outline", "glyf"),
                space_ok=bool(item.get("space_ok", True)),
                light=bool(item.get("light", False)),
                cjk_ok=bool(item.get("cjk_ok", True)),
                ascii_ok=bool(item.get("ascii_ok", True)),
                simplified_coverage=float(item.get("simplified_coverage", 1.0)),
            )
            for item in payload.get("faces", [])
        ]
        # 路径失效（换机器/卸载字体）时整体作废
        for face in faces:
            if face.path and not Path(face.path).exists():
                return None
        return faces or None
    except Exception:
        logger.debug("字体缓存读取失败", exc_info=True)
        return None


def _write_cache(faces: list[FontFace]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "faces": [
                {
                    "name": face.name,
                    "path": face.path,
                    "builtin": face.builtin,
                    "bold": face.bold,
                    "serif": face.serif,
                    "source": face.source,
                    "subset_safe": face.subset_safe,
                    "variable": face.variable,
                    "outline": face.outline,
                    "space_ok": face.space_ok,
                    "light": face.light,
                    "cjk_ok": face.cjk_ok,
                    "ascii_ok": face.ascii_ok,
                    "simplified_coverage": face.simplified_coverage,
                }
                for face in faces
            ]
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        logger.debug("字体缓存写入失败", exc_info=True)


def system_faces(force: bool = False) -> list[FontFace]:
    """本机可用的中文字体列表（进程内缓存 + 磁盘缓存）。"""
    global _FACES
    with _FACES_LOCK:
        if _FACES is not None and not force:
            return _FACES
        if not force:
            cached = _read_cache()
            if cached:
                _FACES = cached
                return _FACES
        _FACES = _discover_uncached()
        _write_cache(_FACES)
        return _FACES


def invalidate() -> None:
    """清空字体缓存（改了配置或装了新字体时用）。"""
    global _FACES
    with _FACES_LOCK:
        _FACES = None
    with _SYMBOL_LOCK:
        _SYMBOL_FACE.clear()


# ---------------------------------------------------------------------------
# 符号落点：当前字体没有字形时，去哪找个有的
# ---------------------------------------------------------------------------

#: 符号字体偏好（越靠前越优先）。
#:
#: 挑的是**数学/箭头/下标类符号覆盖好**的字体，而不是中文字体观感表那一套。
#: `MS Gothic` 排第一是实测结论：本机 23 个"内置 Helvetica 缺失"的符号它**一个
#: 字体全覆盖**（全量 63 个也全覆盖）。落点集中很要紧 —— 每换一个字体就要在产出
#: 里多嵌一份字库，而 `subset_fonts()` 至少要留一份完整的字体程序。
_SYMBOL_PREFERENCE = (
    "ms gothic", "yu gothic", "malgun gothic",
    "microsoft jhenghei", "microsoft yahei", "dengxian",
    "simsun", "simhei", "heiti", "songti", "kaiti",
    "source han", "noto sans", "思源", "黑体", "宋体",
)


def _symbol_preference_rank(name: str) -> int:
    lowered = (name or "").lower()
    for index, token in enumerate(_SYMBOL_PREFERENCE):
        if token in lowered:
            return index
    return 500


#: 码位 -> 落点字体（None 表示本机确实没有）；按码位缓存，见 `_SYMBOL_FACE`


def _symbol_candidates() -> list[FontFace]:
    """符号落点候选，按"能正确嵌入 + 观感正常 + 落点集中"排序。

    与 `_score()` 的区别：这里要的是**单个字形**，所以不提字重、不要衬线匹配，
    但把细体（Light/Thin）排后面 —— 数学符号用细体画会明显比周围正文轻。
    """
    faces = list(_pack_faces()) + list(system_faces())

    def order(face: FontFace):
        return (
            0 if face.subset_safe else 1,
            0 if not face.variable else 1,
            0 if not face.light else 1,
            0 if face.cjk_ok else 1,
            0 if face.ascii_ok else 1,
            _symbol_preference_rank(face.name),
            face.name,
        )

    faces.sort(key=order)
    return faces


def resolve_symbol_face(code: int) -> "FontFace | None":
    """为"当前字体没有字形"的码位找第一个本机有该字形的字体。

    只在字符真正缺字形时才被调用，结果按码位缓存 —— 常规排版不会因此变慢。
    找不到返回 None，调用方继续走 ASCII 等价兜底。
    """
    with _SYMBOL_LOCK:
        if code in _SYMBOL_FACE:
            return _SYMBOL_FACE[code]

    found: FontFace | None = None
    for face in _symbol_candidates():
        try:
            if load_font(face).has_glyph(code):
                found = face
                break
        except Exception:
            continue

    with _SYMBOL_LOCK:
        _SYMBOL_FACE[code] = found
    if found is not None:
        logger.debug("码位 U+%04X 的符号落点：%s", code, found.name)
    return found


# ---------------------------------------------------------------------------
# 解析：风格 -> 具体字体
# ---------------------------------------------------------------------------

@dataclass
class ResolvedFont:
    """最终用于绘制的字体。"""

    face: FontFace
    font: fitz.Font
    faux_bold: bool = False
    reason: str = ""

    @property
    def needs_stroke(self) -> bool:
        """是否需要用 render_mode=2 做伪粗体。"""
        return self.faux_bold

    def label(self) -> str:
        suffix = "（伪粗体）" if self.faux_bold else ""
        return f"{self.face.name}｜{self.face.source}{suffix}"


def _user_face(path: str, name: str, bold: bool) -> FontFace | None:
    probe = Path(path)
    if not probe.exists() or not probe.is_file():
        return None
    traits = font_traits(str(probe))
    face = FontFace(
        name=name, path=str(probe), bold=bold,
        serif=classify_source_font(name).serif,
        source="自定义", subset_safe=traits["subset_safe"],
        variable=traits["variable"], outline=traits["outline"],
        light=any(hint in name.lower() for hint in _LIGHT_HINTS),
    )
    try:
        if not load_font(face).has_glyph(0x4E2D):
            return None
    except Exception:
        return None
    face.space_ok = space_extracts_correctly(face)
    face.cjk_ok = cjk_roundtrip_ok(face)
    return face


def _pack_faces() -> list[FontFace]:
    """可选字体包（models/fonts/），用户自行放入即可优先使用。"""
    from ..shared.path_helpers import PROJECT_ROOT

    directory = PROJECT_ROOT / "models" / "fonts"
    if not directory.is_dir():
        return []
    faces: list[FontFace] = []
    for path in sorted(directory.glob("*")):
        if path.suffix.lower() not in _FONT_EXT:
            continue
        name = path.stem
        lowered = name.lower()
        traits = font_traits(str(path))
        face = FontFace(
            name=name, path=str(path), source="字体包",
            bold=any(hint in lowered for hint in _BOLD_HINTS),
            serif=classify_source_font(name).serif,
            subset_safe=traits["subset_safe"], variable=traits["variable"],
            outline=traits["outline"],
            light=any(hint in lowered for hint in _LIGHT_HINTS),
        )
        try:
            if load_font(face).has_glyph(0x4E2D):
                face.space_ok = space_extracts_correctly(face)
                face.cjk_ok = cjk_roundtrip_ok(face)
                face.ascii_ok = ascii_roundtrip_ok(face)
                face.simplified_coverage = simplified_coverage(face)
                faces.append(face)
        except Exception:
            continue
    return faces


def _score(face: FontFace, style: FontStyle) -> tuple:
    """给候选字体打分，返回可排序元组（越小越好）。

    顺序即优先级：
      1. 不是可变字体（MuPDF 只取第一个命名实例，字重会离谱地细）；
      2. 能被正确子集化（glyf 字库；CFF 会让产出从几十 KB 膨胀到好几 MB）；
      3. 字重匹配（粗体请求必须有粗体字重）；
      4. 衬线/无衬线匹配；
      5. 空格能被正确提取（否则产出 PDF 里搜不到多词短语）；
      6. 常见度/观感偏好；内置字体垫底。
    """
    # 简体覆盖不足的字体直接淘汰到末尾：用它们排简体译文会缺字
    coverage_bad = 0 if face.simplified_coverage >= SIMPLIFIED_MIN_COVERAGE else 1
    return (
        coverage_bad,
        0 if not face.variable else 1,
        0 if face.subset_safe else 1,
        # 汉字能否按标准码位提取 —— 这是**正确性**问题，排在字重（观感）之前：
        # 有些字库（Noto 系列、思源宋体 Heavy）把 CJK 兼容表意文字与统一汉字
        # 指向同一字形，MuPDF 反查时选中兼容码位，产出 PDF 里复制出来的是
        # U+F9xx 而不是标准汉字，搜索/引用都会对不上。
        0 if face.cjk_ok else 1,
        0 if face.bold == style.bold else 1,
        # 细体（Light/Thin）不是"常规"：它是**更细**的独立字重，
        # 拿它当正文会明显偏细。实测微软雅黑的 Light 因为空格正常、
        # 偏好排名靠前，一度盖过了等线 Regular。
        0 if (face.light == style.light) else 1,
        0 if face.serif == style.serif else 1,
        0 if face.space_ok else 1,
        _preference_rank(face.name, style.serif),
        0 if face.source != "内置" else 1,
        face.name,
    )


def resolve(
    style: FontStyle,
    target_lang: str,
    config: dict | None = None,
    use_system: bool = True,
    ascii_safe: bool = False,
) -> ResolvedFont:
    """为给定风格挑选译文渲染字体。

    Args:
        style: 原文风格（衬线/粗体）。
        target_lang: 目标语言；非 CJK 语言直接用内置拉丁字体。
        config: 配置字典，支持 `lettering_font_regular` / `lettering_font_bold`。
        use_system: 是否允许使用本机字体（关闭则退回内置字体）。
        ascii_safe: 是否要求字库能原样还原 ASCII。竖排文本（走 insert_textbox，
            只能用单一字体）必须打开，否则等线这类字库会把拉丁连字符变成
            `U+2010`，产出里搜不到。

    Returns:
        ResolvedFont。找不到匹配字重时会带 `faux_bold=True`。
    """
    config = config or {}

    # ---- 非 CJK 目标：内置拉丁字体已经覆盖常规/粗体两档，无需任何匹配 ----
    if target_lang not in CJK_TARGET_LANGS:
        builtin = "hebo" if style.bold else ("tibo" if style.serif else "helv")
        face = FontFace(
            name="MuPDF 内置拉丁字体", builtin=builtin,
            bold=style.bold, serif=style.serif, source="内置",
        )
        return ResolvedFont(face=face, font=load_font(face), reason="拉丁目标语言使用内置字体")

    # ---- 1) 用户显式指定的字体文件优先 ----
    regular_path = str(config.get("lettering_font_regular") or "").strip()
    bold_path = str(config.get("lettering_font_bold") or "").strip()
    if regular_path or bold_path:
        chosen = None
        if style.bold and bold_path:
            chosen = _user_face(bold_path, Path(bold_path).stem, True)
        if chosen is None and regular_path:
            chosen = _user_face(regular_path, Path(regular_path).stem, False)
        if chosen is not None:
            faux = style.bold and not chosen.bold
            return ResolvedFont(
                face=chosen, font=load_font(chosen), faux_bold=faux,
                reason="使用配置指定的字体文件",
            )
        logger.warning("配置指定的字体文件不可用，回退到自动匹配：%s / %s", regular_path, bold_path)

    family = str(config.get("lettering_font_family") or "auto").lower()
    if family == "serif":
        style = FontStyle(serif=True, bold=style.bold, mono=style.mono, italic=style.italic)
    elif family in ("sans", "sans-serif"):
        style = FontStyle(serif=False, bold=style.bold, mono=style.mono, italic=style.italic)

    if not use_system:
        face = FontFace(name="MuPDF 内置 CJK 字体", builtin=_BUILTIN_CJK,
                        bold=False, serif=False, source="内置")
        return ResolvedFont(face=face, font=load_font(face), faux_bold=style.bold,
                            reason="已禁用系统字体，使用内置字体")

    candidates: list[FontFace] = []
    candidates.extend(_pack_faces())
    candidates.extend(system_faces())

    if not candidates:
        face = FontFace(name="MuPDF 内置 CJK 字体", builtin=_BUILTIN_CJK, source="内置")
        return ResolvedFont(face=face, font=load_font(face), faux_bold=style.bold,
                            reason="未发现本机中文字体")

    def order(face: FontFace):
        base = _score(face, style)
        if not ascii_safe:
            return base
        # 竖排：把"能原样还原 ASCII"提到字重之前（正确性优先于观感）
        return (base[0], base[1], base[2], 0 if face.ascii_ok else 1) + base[3:]

    candidates.sort(key=order)
    best = candidates[0]
    faux = style.bold and not best.bold
    if faux:
        # 想要粗体但没有可用粗体字重：优先换成同族有粗体的（排序已保证风格匹配）
        logger.info("未找到匹配的粗体中文字体，将使用伪粗体（描边）渲染：%s", best.name)
    return ResolvedFont(
        face=best,
        font=load_font(best),
        faux_bold=faux,
        reason=f"{style.describe()} → {best.label()}",
    )


# ---------------------------------------------------------------------------
# 诊断 / 界面展示
# ---------------------------------------------------------------------------

def status() -> dict:
    """返回本机字体探测结果，供接口与界面展示。"""
    faces = system_faces()
    usable = [face for face in faces if face.subset_safe]
    return {
        "count": len(faces),
        "usable": len(usable),
        "faces": [face.to_dict() for face in faces],
        "warnings": [
            f"{len(faces) - len(usable)} 个字体无法被正确子集化（CFF/可变字体），已降权"
            if len(faces) != len(usable) else ""
        ],
    }


def preview_choices(target_lang: str = "zh", config: dict | None = None) -> list[dict]:
    """列出"不同原文风格会选中哪个字体"，便于界面上直观确认。"""
    config = config or {}
    out = []
    for label, style in (
        ("正文（无衬线）", FontStyle(serif=False, bold=False)),
        ("标题（无衬线粗体）", FontStyle(serif=False, bold=True)),
        ("正文（衬线）", FontStyle(serif=True, bold=False)),
        ("标题（衬线粗体）", FontStyle(serif=True, bold=True)),
    ):
        resolved = resolve(style, target_lang, config)
        out.append({
            "style": label,
            "font": resolved.face.name,
            "source": resolved.face.source,
            "faux_bold": resolved.faux_bold,
            "subset_safe": resolved.face.subset_safe,
        })
    return out
