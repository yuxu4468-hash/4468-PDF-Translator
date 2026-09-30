"""文本处理与通用小工具。

包含：页码范围解析、文本规范化、可翻译性启发式判断、稳定哈希、术语表解析。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

# ---------------------------------------------------------------------------
# 页码范围
# ---------------------------------------------------------------------------

def parse_page_range(spec, total: int) -> list[int]:
    """把 "all" / "1-5,8,11-13" 解析成 0 基页码列表（已排序去重）。

    Args:
        spec: 字符串或列表；None / "" / "all" 表示全部页面。
        total: 文档总页数。

    Returns:
        0 基页码列表，永远落在 [0, total) 内。
    """
    if total <= 0:
        return []
    if spec is None or spec == "" or (isinstance(spec, str) and spec.strip().lower() == "all"):
        return list(range(total))

    if isinstance(spec, (list, tuple, set)):
        tokens = [str(x) for x in spec]
    else:
        tokens = re.split(r"[,，;；\s]+", str(spec))

    pages: set[int] = set()
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token.lower() == "all":
            return list(range(total))
        match = re.fullmatch(r"(\d+)\s*[-~至]\s*(\d+)", token)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                start, end = end, start
            for page in range(start, end + 1):
                if 1 <= page <= total:
                    pages.add(page - 1)
            continue
        if token.isdigit():
            page = int(token)
            if 1 <= page <= total:
                pages.add(page - 1)
            continue
        # 无法识别的记号直接忽略
    return sorted(pages)


def format_page_range(pages: list[int]) -> str:
    """把 0 基页码列表压缩成 "1-5,8" 形式，用于界面展示。"""
    if not pages:
        return "-"
    ordered = sorted(set(p + 1 for p in pages))
    chunks = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        chunks.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = page
    chunks.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(chunks)


# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

# 连字符换行： "trans-\nlation" -> "translation"（仅当两侧都是拉丁字母时）
_HYPHEN_BREAK = re.compile(r"(?<=[A-Za-z])[-\u2010\u2011]\s*\n\s*(?=[a-z])")
# 行尾无连字符的折行：英文行尾接下一行首
_SOFT_BREAK = re.compile(r"(?<=[A-Za-z,;:])\n(?=[a-z])")
# CJK 之间的换行应直接去掉
_CJK_BREAK = re.compile(r"(?<=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef])\n")
# 连续空白
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")


def clean_text(text: str) -> str:
    """去掉零宽字符、压缩空白、统一换行。"""
    if not text:
        return ""
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_SPACE.sub(" ", text)
    return text.strip()


def join_lines(lines: list[str]) -> str:
    """把 PDF 中同一段落的若干行拼接成连续文本。

    处理英文连字符断词与中日韩换行。
    """
    if not lines:
        return ""
    joined = "\n".join(lines)
    joined = _HYPHEN_BREAK.sub("", joined)
    joined = _CJK_BREAK.sub("", joined)
    # 英文句子在同一段内换行，用空格连接
    joined = _SOFT_BREAK.sub(" ", joined)
    joined = re.sub(r"[ \t]*\n[ \t]*", " ", joined)
    return _MULTI_SPACE.sub(" ", joined).strip()


def normalize_for_key(text: str) -> str:
    """生成用于去重/比对的归一化文本（去空白、统一大小写、全角转半角）。"""
    text = unicodedata.normalize("NFKC", clean_text(text))
    return re.sub(r"\s+", "", text).lower()


# ---------------------------------------------------------------------------
# 可翻译性判断
# ---------------------------------------------------------------------------

_URL_ONLY = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)
_EMAIL_ONLY = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
_NUMBER_ONLY = re.compile(r"""^[\s\d.,:%/\\()\[\]{}+\-_=*#&|<>~^°'"`–—]+$""")
_CODE_LIKE = re.compile(
    r"^\s*(?:#include|import\s+\w|from\s+\w+\s+import|def\s+\w+\s*\(|class\s+\w+|"
    r"function\s+\w+\s*\(|public\s+|private\s+|SELECT\s|INSERT\s+INTO|"
    r"</?\w+[^>]*>|\{|\}|=>)\s*",
    re.IGNORECASE,
)

_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def is_translatable(text: str, min_chars: int = 2) -> bool:
    """判断一段文本是否值得送去翻译。

    过滤：空串、纯数字/符号、纯 URL/邮箱、明显的代码片段、过短片段。
    """
    stripped = clean_text(text)
    if len(stripped) < min_chars:
        return False
    if _URL_ONLY.match(stripped) or _EMAIL_ONLY.match(stripped):
        return False
    if _NUMBER_ONLY.match(stripped):
        return False
    # 必须含有至少一个字母或 CJK 字符
    if not _HAS_LETTER.search(stripped):
        return False
    # 去掉所有非字母字符后仍然很短，说明是符号堆
    letters = re.sub(r"[^\w]", "", stripped, flags=re.UNICODE)
    if len(letters) < 2:
        return False
    return True


def looks_like_code(text: str) -> bool:
    """粗略判断是否像代码/配置片段（默认不翻译）。"""
    stripped = clean_text(text)
    if not stripped:
        return False
    if _CODE_LIKE.match(stripped):
        return True
    # 括号/分号密度异常高
    symbols = sum(stripped.count(ch) for ch in "{}[]();=<>")
    if len(stripped) > 8 and symbols / len(stripped) > 0.18:
        return True
    return False


def has_target_language(text: str, target_lang: str) -> bool:
    """判断文本是否已经基本是目标语言（避免把中文再翻一遍）。"""
    if target_lang not in {"zh", "zh-TW", "ja", "ko"}:
        return False
    stripped = clean_text(text)
    if not stripped:
        return False
    cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in stripped if ch.isascii() and ch.isalpha())
    # 中文字符占主导
    return cjk > 0 and cjk >= latin


# ---------------------------------------------------------------------------
# 数值抽取与比对（译文里的数字保护）
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

#: 汉字数字
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"万": 10 ** 4, "亿": 10 ** 8}
_CN_NUM_CHARS = set(_CN_DIGITS) | set(_CN_UNITS) | set(_CN_BIG)

#: 允许跟在汉字数词后面的量词。数词串只有被它界定，才认为"独立成词"。
#:
#: 取表的准则是**按误改代价权衡**：
#:   - 漏收一个量词 → 该数词不转换 → 原文的阿拉伯数字在译文里找不到 →
#:     **误判失配，把一段正确译文退回原文**（这正是本功能最该避免的代价）；
#:   - 多收一个量词 → 可能把固定词里的字当成数词（`十分`→10）→ 凭空造出数字 →
#:     只有在原文恰好也有那个数字时才可能掩盖漏译。
#:
#: 两边不对称，所以**宁可放宽**。但 `分 / 成 / 时 / 刻 / 番 / 折 / 部` 仍然排除：
#: 它们在「十分」「一成」「一时」「一刻钟」「一部分」里都不是数词，
#: 且这些词极常见，收进来会显著提高掩盖漏译的概率。
_CN_TRAILERS = set(
    "条类款项级种章节次组号"      # 第 X 条 / 第 X 类
    "个只头尾匹株枚份名位岁龄"    # 动物与个体量词
    "年月日周天期届"              # 时间
    "米厘尺丈升毫升克斤吨度"      # 度量单位（厘米由「厘」界定）
    "倍层批台套件张片块根支颗粒"
    "本册篇段页"
)


def _is_cjk_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _canonical_number(value: float) -> str:
    """把数值格式化成可比较的规范形式（去掉无意义的尾零：1.0 → 1、1.50 → 1.5）。"""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return ("%f" % value).rstrip("0").rstrip(".")


def _number_candidates(token: str) -> set[str]:
    """一个数字串可能的归一化形式。

    两件事要处理：

    1. **千分位与小数点存在真实歧义**：德语 `1.000` 是「一千」，英语 `1.000` 是
       「一点零」，只凭字符串无法判定。所以两种读法都产出，比对时命中任一即算通过。
    2. **比较数值而不是数字串**：`1.000`（小数读法）与 `1.0` 数值相同，
       按字符串比会误判成失配。规范成数值形式后二者都是 `1`。

    两条都是为压住误报 —— 误报的代价是**把一段正确译文退回原文**。
    """
    token = (token or "").strip()
    if not token:
        return set()

    if "," in token and "." in token:
        # 同时出现时，靠后的那个是小数点，另一个是千分位
        if token.rfind(",") > token.rfind("."):
            variants = {token.replace(".", "").replace(",", ".")}
        else:
            variants = {token.replace(",", "")}
    else:
        sep = "," if "," in token else ("." if "." in token else "")
        if not sep:
            variants = {token}
        else:
            parts = token.split(sep)
            variants = {parts[0] + "." + "".join(parts[1:])}
            if all(len(part) == 3 for part in parts[1:]):
                variants.add("".join(parts))

    cleaned: set[str] = set()
    for item in variants:
        digits = item.replace(".", "")
        if len(digits) > 15:
            # 超长数字串（编号、卡号）用浮点会丢精度，原样保留
            cleaned.add(item)
            continue
        try:
            cleaned.add(_canonical_number(float(item)))
        except ValueError:
            continue
    return cleaned



def _chinese_to_int(run: str) -> int | None:
    """把「二十五」「两百」「二〇二〇」这类汉字数词转成整数；无法解析返回 None。"""
    if not run:
        return None
    # 纯数字序列（含位值零「〇」「零」）按位读：二〇二〇 → 2020。
    # 不能走下面的累加逻辑 —— 那样只会留下最后一个数位（得到 0）。
    if all(char in _CN_DIGITS for char in run):
        digits = "".join(str(_CN_DIGITS[char]) for char in run)
        return int(digits) if digits else None

    total = 0
    section = 0
    number = 0
    for char in run:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[char]
            number = 0
        elif char in _CN_BIG:
            section = (section + number) * _CN_BIG[char]
            total += section
            section = 0
            number = 0
        else:
            return None
    return total + section + number



def chinese_numerals(text: str) -> list[int]:
    """抽取**独立成词**的汉字数词。

    保守是刻意的：若在汉字词组内部乱转（`一般`→1、`十分`→10），会凭空造出
    原文没有的数字，反而可能掩盖真正的漏译。所以数词串必须满足

    - 前一个字符不是汉字，**或者是「第」**（`第五条`）；
    - 后一个字符不是汉字，**或者属于量词表**（`二十三种`）。
    """
    values: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] not in _CN_NUM_CHARS:
            index += 1
            continue
        start = index
        while index < length and text[index] in _CN_NUM_CHARS:
            index += 1
        run = text[start:index]
        prev_char = text[start - 1] if start > 0 else ""
        next_char = text[index] if index < length else ""
        prev_ok = (not prev_char) or (not _is_cjk_char(prev_char)) or prev_char == "第"
        next_ok = (not next_char) or (not _is_cjk_char(next_char)) or next_char in _CN_TRAILERS
        if prev_ok and next_ok:
            value = _chinese_to_int(run)
            if value is not None:
                values.append(value)
    return values


def extract_numbers(text: str) -> set[str]:
    """抽取文本里所有数值（阿拉伯数字 + 独立成词的汉字数词），返回归一化形式集合。"""
    if not text:
        return set()
    found: set[str] = set()
    for token in _NUMBER_RE.findall(text):
        found |= _number_candidates(token)
    for value in chinese_numerals(text):
        found.add(str(value))
    return found


#: 「数字 + 小数点/逗号 + 空白 + 数字」—— PDF 提取把一个小数劈成两半的样子
_GLUED_NUMBER_RE = re.compile(r"(\d[.,])\s+(?=\d)")


def _reglue_numbers(text: str) -> str:
    """把被空格劈开的小数重新粘回去：`0, 5` → `0,5`。

    实测 BMEL 语料里有 `0,75 x 0, 5 x 0,75 über 1,5 m`：正文里那个空格是
    PDF 提取的产物，不是真的分隔。严格读法会把源数字读成 `0` 和 `5`，
    译文的 `0.5` 就对不上 —— 一次误判，代价是**把一段正确译文退回原文**。

    只用于"严格比对失败之后的第二次判定"，严格路径完全不受影响。
    """
    return _GLUED_NUMBER_RE.sub(r"\1", text)


def missing_numbers(source: str, target: str) -> list[str]:
    """返回**源里出现、译文里找不到**的数值。

    **单向包含**：不检查译文多出来的数值。译文里的数字比原文多通常是正常的 ——
    `1. Juni 2018` 正确译成中文是 `2018年6月1日`，月份名词 `Juni` 转成了数字 `6`。
    若做对称比较，这份正确译文会被误杀。

    严格比对失败后还有**一次**宽松判定（见 `_reglue_numbers`）：PDF 提取常在
    德式小数逗号后插一个空格（`0, 5`），严格读法会把源数字读成 `0` 和 `5`，
    于是译文里的 `0.5` 对不上。宽松读法把 `0,5` 粘回去再判一次 ——
    只有"严格失败、宽松通过"才放行，等于**只会减少误判，不会放松真正的漏译判定
    之外的任何东西**。
    """
    if not source:
        return []
    missing = _missing_strict(source, target)
    if not missing:
        return []
    relaxed = _reglue_numbers(source)
    if relaxed != source and not _missing_strict(relaxed, target):
        return []
    return missing


def _missing_strict(source: str, target: str) -> list[str]:
    """纯严格读法的失配列表（不做任何宽松解释）。"""
    target_numbers = extract_numbers(target)

    missing: list[str] = []
    seen: set[str] = set()
    for token in _NUMBER_RE.findall(source):
        candidates = _number_candidates(token)
        if not candidates or (candidates & target_numbers):
            continue
        if token not in seen:
            seen.add(token)
            missing.append(token)
    for value in chinese_numerals(source):
        key = str(value)
        if key not in target_numbers and key not in seen:
            seen.add(key)
            missing.append(key)
    return missing


# ---------------------------------------------------------------------------
# 哈希与估算
# ---------------------------------------------------------------------------

def stable_hash(*parts) -> str:
    """对任意可 JSON 序列化的输入计算稳定短哈希。"""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（CJK 约 1 字 1 token，拉丁约 4 字符 1 token）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    return int(cjk * 1.05 + other / 3.6) + 1


# ---------------------------------------------------------------------------
# 术语表
# ---------------------------------------------------------------------------

def parse_glossary(raw) -> dict:
    """把多种形式的术语表输入统一成 {原文: 译文}。

    支持：
      - dict
      - 字符串，每行 "原文=译文" / "原文：译文" / "原文,译文"
      - 列表，元素为 [原文, 译文] 或 "原文=译文"
    """
    if not raw:
        return {}
    result: dict[str, str] = {}

    def _add(src, dst):
        src = clean_text(str(src))
        dst = clean_text(str(dst))
        if src and dst:
            result[src] = dst

    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (list, tuple)) and len(value) >= 1:
                _add(key, value[0])
            else:
                _add(key, value)
        return result

    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                _add(item[0], item[1])
            elif isinstance(item, str):
                _parse_glossary_line(item, _add)
        return result

    if isinstance(raw, str):
        for line in raw.splitlines():
            _parse_glossary_line(line, _add)
    return result


def _parse_glossary_line(line: str, add) -> None:
    line = line.strip()
    if not line or line.startswith("#"):
        return
    for sep in ("=>", "=", "→", ":", "：", "\t", ","):
        if sep in line:
            src, _, dst = line.partition(sep)
            add(src, dst)
            return


def format_glossary_block(glossary: dict) -> str:
    """把术语表渲染成提示词里的约束段落。"""
    if not glossary:
        return ""
    lines = [f"- {src} → {dst}" for src, dst in list(glossary.items())[:200]]
    return "【术语表（必须严格遵守，优先级高于一切）】\n" + "\n".join(lines)
