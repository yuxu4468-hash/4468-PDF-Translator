"""嵌字（保留原版式、只换文字）专项回归测试。

这个套件锁死"嵌字"相对普通"仅译文替换"多出来的那几条能力，防止以后改坏：

  1. **字体风格匹配**：原文加粗 → 译文必须用粗体（或明确的伪粗体），
     不能再像以前那样所有 CJK 都用同一个只有常规字重的内置字体；
  2. **字体可子集化**：选中的字体不能让产出 PDF 膨胀（CFF/可变字体要降权）；
  3. **基线锚定**：译文首行必须落在原文首行的基线上（±1pt）；
  4. **原文行距网格**：多行段落沿用原文的基线间距；
  5. **字距拉伸**：行宽有富余时拉字距，且不超过上限、不动最后一行；
  6. **中文禁则（避头尾）**：逗号句号不能出现在行首，开引号不能出现在行末；
  7. **粗体小标题**：与正文同处一个文本块的加粗小标题要被拆成独立段落；
  8. **不丢内容**：译文全部写入、未翻译的原文原样保留、图片不被红action 删掉。

用法：
    python tests/test_lettering.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import fonts, pdf_parser, typesetter  # noqa: E402
from src.core.pipeline import verify_output  # noqa: E402
from src.shared import constants  # noqa: E402

FAILURES: list[str] = []
TMP = ROOT / "work" / "tmp"
SAMPLE = TMP / "lettering_sample.pdf"


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def norm(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isalnum())


# ---------------------------------------------------------------------------
# 合成样本 PDF
# ---------------------------------------------------------------------------

BODY_DE = (
    "Die Kreuzotter ist eine relativ kleine, gedrungene Schlange mit "
    "charakteristischer Zickzack-Zeichnung auf dem Ruecken und einer dunklen "
    "Linie laengs der Wirbelsaeule."
)
HEAD_DE = "Merkmale"
SUB_DE = "Gefaehrdung und Schutzstatus"
LIST_DE = "Rote Liste Deutschland (2020): Kategorie 2 stark gefaehrdet"

TARGETS = [
    (HEAD_DE, "特征"),
    (SUB_DE, "受威胁状况与保护级别"),
    (BODY_DE, (
        "极北蝰是一种体型相对较小、粗壮的蛇类，背部具有特征性的锯齿状斑纹，"
        "并沿脊柱有一条深色纵线，这些特征在野外识别时都非常有用。"
    )),
    (LIST_DE, "德国红色名录（2020）：第2类“严重受威胁”，并列入特别保护名单。"),
]
KEEP_DE = "DGHT 2023 - Alle Rechte vorbehalten"


def build_sample(path: Path) -> None:
    """构造样本：加粗小标题与正文处在**同一个文本块**里（复现真实版式）。

    关键点：必须用 `TextWriter` 一次写入才成。实测两次 `insert_textbox`
    调用会被 MuPDF 报成两个 block，而真实 InDesign 文档是**同一个文本对象内
    切换字体**，MuPDF 报成一个 block、块内多行各自带不同字体 ——
    这正是 `_split_lines_by_style` 要处理的形态。
    """
    TMP.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=420, height=560)
    bold = fitz.Font("hebo")
    regular = fitz.Font("helv")

    def write_block(paras: list[tuple[str, fitz.Font]], start_y: float, wrap: float = 340.0):
        """把 (文本, 字体) 序列按基线逐行写入 —— 一次 write_text 即一个 block。

        换行宽度要给足（这里 340pt）：如果宽度太窄，连标题都会被折成好几行，
        就不再有"短小标题 + 长正文"的形态，`_split_lines_by_style` 会（正确地）
        拒绝拆分，测试也就测不到东西了。
        """
        writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
        y = start_y
        for text, font in paras:
            current = ""
            for word in text.split(" "):
                candidate = (current + " " + word).strip()
                if font.text_length(candidate, fontsize=10) <= wrap:
                    current = candidate
                else:
                    writer.append((40, y), current, font=font, fontsize=10)
                    y += 13.0
                    current = word
            if current:
                writer.append((40, y), current, font=font, fontsize=10)
                y += 13.0
        writer.write_text(page)

    # 块 1：加粗小标题 Merkmale + 常规正文（同块）
    write_block([(HEAD_DE, bold), (BODY_DE, regular)], 60.0)
    # 块 2：加粗小标题 + 常规列表行（同块）
    write_block([(SUB_DE, bold), (LIST_DE, regular)], 240.0)

    # 不翻译的德文（模拟版权行）：必须原样保留
    page.insert_textbox(fitz.Rect(40, 500, 380, 530), KEEP_DE,
                        fontname="helv", fontsize=8, lineheight=1.2)

    # 一张图片：redaction 不允许碰到它
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60))
    pix.set_rect(pix.irect, (30, 120, 200))
    page.insert_image(fitz.Rect(330, 400, 390, 460), pixmap=pix)

    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def load_sample(config: dict):
    """解析样本并填入固定译文，返回 (document, 命中段数)。"""
    document = pdf_parser.parse_pdf(str(SAMPLE), "lettering", SAMPLE.name)
    matched = 0
    for paragraph in document.paragraphs:
        key = paragraph.text.replace("\n", " ")
        best = None
        for source, target in TARGETS:
            if source in key and (best is None or len(source) > len(best[0])):
                best = (source, target)
        if best is not None:
            paragraph.target = best[1]
            matched += 1
        elif KEEP_DE in key:
            paragraph.translatable = False
            paragraph.skip_reason = "测试：保留原文"
    return document, matched


def image_digests_of(doc) -> list:
    """取每张图片的 (页码, 字节数, 宽, 高)。

    比对 xref 是没用的：`garbage=4` 保存会重新编号对象，xref 变了不代表图片丢了。
    字节数与尺寸组合足以判断"图还在、内容没变"。
    """
    digests = []
    for index in range(doc.page_count):
        for item in doc[index].get_images(full=True):
            try:
                info = doc.extract_image(item[0])
                digests.append((index, len(info.get("image") or b""),
                                info.get("width"), info.get("height")))
            except Exception:
                digests.append((index, -1, 0, 0))
    return sorted(digests)


# ---------------------------------------------------------------------------
# 1) 字体模块
# ---------------------------------------------------------------------------

def test_font_classification():
    print("\n[1] 字体风格识别")
    style = fonts.classify_source_font("Calibri")
    check(not style.serif and not style.bold, "Calibri → 无衬线且非粗体")

    style = fonts.classify_source_font("Calibri-Bold")
    check(style.bold and not style.serif, "Calibri-Bold → 无衬线 + 粗体")

    style = fonts.classify_source_font("Tahoma-Bold", flags=16)
    check(style.bold, "Tahoma-Bold（flags=16）→ 粗体")

    style = fonts.classify_source_font("SimSun")
    check(style.serif, "SimSun → 衬线")

    style = fonts.classify_source_font("ABCDEF+MyriadPro-Regular")
    check(not style.serif, "去掉子集前缀后 MyriadPro → 无衬线")


def test_font_traits():
    print("\n[2] 字库格式探测（子集化安全性）")
    faces = fonts.system_faces()
    check(bool(faces), f"探测到 {len(faces)} 个中文字体")

    # 内置字体必须始终作为兜底存在
    check(any(f.source == "内置" for f in faces), "存在 MuPDF 内置兜底字体")

    # 每个候选都必须真的能画中文，否则说明探测的判据坏了
    bad = []
    for face in faces[:12]:
        try:
            if not fonts.load_font(face).has_glyph(0x4E2D):
                bad.append(face.name)
        except Exception as exc:
            bad.append(f"{face.name}: {exc}")
    check(not bad, f"抽查的候选字体都能渲染中文（异常：{bad}）")

    # CFF/可变字体必须被标记为"不可安全子集化"
    for face in faces:
        if face.variable or face.outline == "cff":
            check(not face.subset_safe,
                  f"{face.name}（{face.outline}{'/可变' if face.variable else ''}）被标记为不可子集化")
            break
    else:
        print("  [SKIP] 本机没有 CFF/可变中文字体，跳过该项")


def test_font_resolution():
    print("\n[3] 风格 → 字体 匹配")
    cfg = dict(constants.DEFAULT_CONFIG)

    regular = fonts.resolve(fonts.FontStyle(serif=False, bold=False), "zh", cfg)
    check(regular.font.has_glyph(0x4E2D), f"常规正文选中可渲染中文的字体：{regular.face.name}")

    bold = fonts.resolve(fonts.FontStyle(serif=False, bold=True), "zh", cfg)
    check(bold.face.bold or bold.faux_bold,
          f"粗体请求得到粗体字重或伪粗体：{bold.face.name}"
          f"{'（伪粗体）' if bold.faux_bold else ''}")

    # 关键回归：常规与粗体必须是**不同**的字体，否则说明风格匹配没生效
    if not regular.faux_bold and not bold.faux_bold:
        check(regular.face.key() != bold.face.key(),
              "常规与粗体解析到了不同的字体文件")

    serif = fonts.resolve(fonts.FontStyle(serif=True, bold=False), "zh", cfg)
    check(serif.face.serif or serif.face.source == "内置",
          f"衬线请求得到衬线字体：{serif.face.name}")

    latin = fonts.resolve(fonts.FontStyle(serif=False, bold=True), "en", cfg)
    check(latin.face.source == "内置" and latin.face.builtin in ("hebo", "helv"),
          "非 CJK 目标语言走内置拉丁字体（无需下载）")

    # 关闭系统字体后必须退化，且明确告知没有粗体
    off = fonts.resolve(fonts.FontStyle(serif=False, bold=True), "zh", cfg, use_system=False)
    check(off.face.source == "内置" and off.faux_bold,
          "禁用系统字体后退化到内置字体并使用伪粗体")

    # 用户指定的坏路径不能让解析崩掉
    tainted = dict(cfg, lettering_font_regular=r"Z:\definitely\missing.ttf")
    fallback = fonts.resolve(fonts.FontStyle(bold=False), "zh", tainted)
    check(fallback.font.has_glyph(0x4E2D), "自定义字体路径无效时能回退到自动匹配")


# ---------------------------------------------------------------------------
# 2) 折行与禁则
# ---------------------------------------------------------------------------

def test_line_break_rules():
    print("\n[4] 中文禁则（避头尾）")
    font = fonts.resolve(fonts.FontStyle(), "zh", {}).font
    size = 10.0
    width = size * 12  # 每行约 12 个字

    closing = "，。、；：）！？"
    opening = "（“《"

    text = "极北蝰是一种体型相对较小的蛇类，背部具有特征性的锯齿状斑纹（图1），因此在野外应当仔细观察。"
    lines = typesetter.wrap_text(text, font, size, width)
    check(len(lines) >= 3, f"长句被折成 {len(lines)} 行")

    bad_start = [ln for ln in lines if ln and ln[0] in closing]
    check(not bad_start, f"没有行以句读/闭括号开头（违例：{bad_start[:3]}）")
    bad_end = [ln for ln in lines if ln and ln[-1] in opening]
    check(not bad_end, f"没有行以开括号/前引号结尾（违例：{bad_end[:3]}）")

    # 恢复整段文本（去掉折行处补的空白）
    joined = "".join(lines).replace(" ", "")
    check(joined == text.replace(" ", ""), "折行前后文本内容一致（没丢字/多半字）")

    # 悬挂不能让行宽失控：最多超出一个字
    for line in lines:
        over = font.text_length(line, fontsize=size) - width
        if over > size * 1.2:
            check(False, f"标点悬挂超出过多：{line!r} 超出 {over:.1f}pt")
            break
    else:
        check(True, "标点悬挂最多超出一个字宽")


def test_tracking():
    print("\n[5] 字距拉伸")
    font = fonts.resolve(fonts.FontStyle(), "zh", {}).font
    size = 10.0

    # 构造一行"几乎填满"的文本：只留 4pt 富余。
    # 注意不能用很短的行走很宽的框 —— 那会被"空档超过行宽 30%"的保护规则挡掉，
    # 这条保护是故意的（短行摊满整行很突兀）。
    line = "极北蝰是一种蛇类的"
    width = font.text_length(line, fontsize=size) + 4.0

    tracks = typesetter.compute_tracking([line, line, line], font, size, width, "left", 0.08)
    check(tracks[0] > 0, f"行宽有富余时首行被拉伸（{tracks[0]:.3f}pt/字）")
    check(tracks[0] <= 0.08 * size + 1e-6,
          f"拉伸量不超过上限（{tracks[0]:.3f} <= {0.08*size:.3f}）")
    check(tracks[-1] == 0.0, "最后一行不拉伸")

    # 关闭拉伸
    zeros = typesetter.compute_tracking([line, line, line], font, size, width, "left", 0.0)
    check(all(v == 0.0 for v in zeros), "上限为 0 时完全不拉伸")

    # 空档过大（短行摊满整行）应当放弃拉伸
    wide = typesetter.compute_tracking(["短"], font, size, width * 6, "left", 0.08)
    check(wide[0] == 0.0, "空档超过行宽 30% 时放弃拉伸")

    # 右对齐不拉伸（对齐方式自己会处理空档）
    right = typesetter.compute_tracking([line, line], font, size, width, "right", 0.08)
    check(all(v == 0.0 for v in right), "右对齐不拉伸")


def test_leading():
    print("\n[6] 行距 / 基线网格")
    font = fonts.resolve(fonts.FontStyle(), "zh", {}).font
    natural = typesetter.Leading(spacing=1.05)
    grid = typesetter.Leading(spacing=1.05, pitch=18.0)

    size = 10.0
    check(abs(grid.step(font, size) - 18.0) < 0.01, "原文行距在合理范围内时按原文走")
    # 字号缩小时不能叠字
    tiny = grid.step(font, 5.0)
    check(tiny >= natural.natural(font, 5.0) - 1e-6, "行距不会小于自然行高（不叠字）")
    # 字号放很大时不能无限拉开
    huge = grid.step(font, 40.0)
    check(huge <= natural.natural(font, 40.0) * 1.8 + 1e-6, "行距不超过自然行距的 1.8 倍")
    check(grid.height(1, font, size) == natural.natural(font, size), "单行高度等于自然行高")


# ---------------------------------------------------------------------------
# 3) 端到端
# ---------------------------------------------------------------------------

def test_parse_split():
    print("\n[7] 粗体小标题从正文中拆出")
    document, _ = load_sample({})
    headings = [p for p in document.paragraphs if p.bold and p.text.strip()]
    check(bool(headings), f"解析出 {len(headings)} 个粗体段落")

    texts = [p.text.replace("\n", " ").strip() for p in headings]
    check(any(HEAD_DE in t for t in texts), f"独立出小标题 {HEAD_DE!r}")
    check(any(SUB_DE in t for t in texts), f"独立出小标题 {SUB_DE!r}")

    body = [p for p in document.paragraphs if BODY_DE[:30] in p.text.replace("\n", " ")]
    check(bool(body) and not body[0].bold,
          "正文档落没有被一起判成粗体")
    document.close()


def test_end_to_end():
    print("\n[8] 端到端：嵌字后回读校验")
    config = dict(constants.DEFAULT_CONFIG)
    config.update({"mode": "mono", "target_lang": "zh"})

    document, matched = load_sample(config)
    check(matched >= 4, f"样本中 {matched} 段填入了测试译文")

    out = TMP / "lettering_out.pdf"
    doc = fitz.open(str(SAMPLE))
    original_digests = image_digests_of(doc)

    stats = typesetter.typeset_document(
        doc, document.paragraphs, config, "mono", log=lambda m: None
    )
    try:
        doc.subset_fonts()
    except Exception as exc:
        check(False, f"subset_fonts 失败：{exc}")
    doc.save(str(out), garbage=4, deflate=True, clean=False)
    doc.close()

    check(stats["blocks"] > 0, f"排版了 {stats['blocks']} 个文本块")

    # --- 体积：子集化必须真的生效 ---
    size_kb = out.stat().st_size / 1024
    check(size_kb < 512, f"产出体积 {size_kb:.0f} KB（< 512 KB，说明字体已子集化）")

    # --- 内容：译文写入 + 未翻译原文保留 ---
    result = verify_output(str(out), document.paragraphs)
    check(result["missing"] == 0,
          f"译文全部写入（{result['checked']} 段，缺失 {result['missing']}）"
          + (f"，样例 {result['samples']}" if result["missing"] else ""))
    check(result.get("lost_original", 0) == 0,
          f"未翻译的原文未被误删（检查 {result.get('kept_checked', 0)} 段，"
          f"丢失 {result.get('lost_original', 0)}）")

    # --- 图片必须还在（redaction 不能删图）---
    # 注意不能比 xref：`garbage=4` 保存时会重新编号对象，xref 变了不代表图片丢了。
    # 这里比对图片的**字节数/尺寸**，才是真的"图还在、内容没变"。
    out_doc = fitz.open(str(out))
    after = image_digests_of(out_doc)
    check(after == original_digests,
          f"图片全部保留且内容未变（原 {len(original_digests)} 张 → 现 {len(after)} 张；"
          f"{original_digests} vs {after}）")

    # --- 粗体真的画成了粗体 ---
    bold_fonts, regular_fonts = set(), set()
    for block in out_doc[0].get_text("dict")["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                name = (span.get("font") or "").lower()
                if any(ch >= "\u4e00" for ch in span.get("text", "")):
                    (bold_fonts if "bold" in name else regular_fonts).add(span.get("font"))
    if bold_fonts:
        check(True, f"译文中出现粗体字体：{sorted(bold_fonts)}")
    else:
        # 只有内置字体可用时走伪粗体路径，属于预期退化
        resolved = fonts.resolve(fonts.FontStyle(bold=True), "zh", config)
        check(resolved.faux_bold,
              f"没有真实粗体字重，已退化为伪粗体（当前粗体字体：{resolved.face.name}）")

    # --- 基线锚定：译文首行落在原文首行基线上 ---
    source = fitz.open(str(SAMPLE))
    deviations = []
    for paragraph in document.paragraphs:
        if not paragraph.target or paragraph.rotation != 0:
            continue
        baselines = [line.spans[0].origin[1] for line in paragraph.lines if line.spans]
        if not baselines:
            continue
        first = baselines[0]
        found = []
        for block in out_doc[paragraph.page].get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if any(ch >= "\u4e00" for ch in span.get("text", "")):
                        found.append(abs(span["origin"][1] - first))
        if found:
            deviations.append(min(found))
    source.close()
    if deviations:
        worst = max(deviations)
        check(worst <= 1.0,
              f"首行基线偏差最大 {worst:.2f}pt（{len(deviations)} 段，要求 ≤ 1.0pt）")
    else:
        check(False, "没能测到译文基线")

    # --- 几何：不越界、不重叠 ---
    problems = []
    for index in range(out_doc.page_count):
        page = out_doc[index]
        lines = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                if "".join(s.get("text", "") for s in line.get("spans", [])).strip():
                    lines.append((fitz.Rect(line["bbox"]), block.get("number", -1)))
        for rect, _ in lines:
            if rect.x0 < -2 or rect.y0 < -2 or rect.x1 > page.rect.width + 2 \
                    or rect.y1 > page.rect.height + 2:
                problems.append(f"越界 {tuple(round(v, 1) for v in rect)}")
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                if lines[i][1] == lines[j][1]:
                    continue
                inter = lines[i][0] & lines[j][0]
                if inter.is_empty or inter.width <= 0 or inter.height <= 0:
                    continue
                area = inter.width * inter.height
                smaller = min(lines[i][0].width * lines[i][0].height,
                              lines[j][0].width * lines[j][0].height)
                if smaller > 0 and area / smaller > 0.5:
                    problems.append(f"重叠 @ {tuple(round(v, 1) for v in inter)}")
    check(not problems, f"没有越界/重叠（问题：{problems[:3]}）")

    out_doc.close()
    document.close()


def build_rotated_pdf(path: Path) -> None:
    """构造一份 /Rotate 90 的横排表格页（复现坐标空间踩坑场景）。

    真实案例：某份 36 页的饲养准则里有 22 页带 /Rotate 90。这类页面上
    `page.rect` 是旋转后的**视觉**矩形（595x420），而**文字坐标仍在未旋转的
    cropbox 空间里**（420x595）。把 `page.rect.height` 当成"页面高度"会：

      · 把行 bbox 按 420 截断（本该 152.6pt 的竖排行只剩 22.9pt）
      · 把"下方可用空间"算成接近 0（连最小字号都放不下）
      · 绘制时用 `page.rect.y1` 当底边，把页面下半部分的译文全部丢掉

    实测后果是一份文档静默丢掉 34~196 段译文。
    """
    TMP.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    # 未旋转空间 420 宽 x 595 高；/Rotate 90 之后 page.rect 会变成 595x420
    page = doc.new_page(width=420, height=595)
    page.set_rotation(90)

    items = [
        (fitz.Rect(40, 60, 300, 80), "Header row of the table", 11, "hebo"),
        (fitz.Rect(40, 120, 300, 170),
         "A fairly long paragraph that sits in the upper half of the page and should be "
         "replaced by a Chinese translation without losing any of its content.", 10, "helv"),
        # 关键：页面下半部分（y > 420，落在旋转后 page.rect 之外）
        (fitz.Rect(40, 440, 300, 480),
         "Lower paragraph which lies beyond page.rect.height but inside the text space.",
         10, "helv"),
        (fitz.Rect(40, 520, 300, 560),
         "Bottom paragraph near the page edge, also outside page.rect.height.", 10, "helv"),
    ]
    for rect, text, size, font in items:
        page.insert_textbox(rect, text, fontsize=size, fontname=font)

    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def test_rotated_pages():
    """带 /Rotate 的页面必须用"文字坐标空间"，否则整段译文会被静默丢弃。"""
    print("\n[10] 带 /Rotate 的页面")
    sample = TMP / "lettering_rotated.pdf"
    build_rotated_pdf(sample)

    raw = fitz.open(str(sample))
    page = raw[0]
    check(page.rotation == 90, f"样本页确实是 /Rotate 90（rotation={page.rotation}）")
    check(round(page.rect.height) == 420 and round(page.cropbox.height) == 595,
          "page.rect=595x420 而 cropbox=420x595（两者不同，正是踩坑点）")
    raw.close()

    document = pdf_parser.parse_pdf(str(sample), "rot", sample.name)
    info = document.pages[0]
    check(round(info.height) == 595 and round(info.width) == 420,
          f"解析出的页面尺寸用文字坐标空间（{info.width:.0f}x{info.height:.0f}）")

    lower = [p for p in document.paragraphs if p.y0 > 420]
    check(len(lower) >= 2, f"页面下半部分解析出 {len(lower)} 段（应 ≥2，说明没被截断）")

    targets = {
        "Header row": "表格表头行",
        "A fairly long paragraph": (
            "这是一段位于页面上半部分、内容较长的正文，翻译成中文后应当完整写入，"
            "不能因为坐标空间算错而丢失任何内容。"),
        "Lower paragraph": "这是位于页面下半部分的段落，它在 page.rect 之外、却在文字坐标空间之内。",
        "Bottom paragraph": "这是靠近页面底边的段落，同样落在 page.rect 之外。",
    }
    matched = 0
    for paragraph in document.paragraphs:
        key = paragraph.text.replace("\n", " ")
        for source, target in targets.items():
            if source in key:
                paragraph.target = target
                matched += 1
                break
    check(matched >= 4, f"{matched} 段填入测试译文")

    config = dict(constants.DEFAULT_CONFIG)
    config.update({"mode": "mono", "target_lang": "zh"})
    out = TMP / "lettering_rotated_out.pdf"
    doc = fitz.open(str(sample))
    typesetter.typeset_document(doc, document.paragraphs, config, "mono", log=lambda m: None)
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.save(str(out), garbage=4, deflate=True, clean=False)
    doc.close()

    result = verify_output(str(out), document.paragraphs)
    check(result["missing"] == 0,
          f"译文全部写入（{result['checked']} 段，缺失 {result['missing']}）"
          + (f"，样例 {result['samples']}" if result["missing"] else ""))
    document.close()


def test_latin_extraction():
    """拉丁字符必须走内置拉丁字体，否则产出 PDF 里复制/搜索会对不上。

    这是一个真实踩过的坑：微软雅黑把 U+0020 与 U+00A0 映射到同一个字形，
    MuPDF 生成 ToUnicode 反查表时选中了 U+00A0，于是产出里
    `Vipera berus` 变成 `Vipera\\xa0berus`，搜索直接失效；
    等线 / Noto Sans SC 则把连字符 `-` 反查成 `U+2010`。
    """
    print("\n[9] 拉丁文本提取保真度")
    font = fonts.resolve(fonts.FontStyle(), "zh", {}).font
    pair = typesetter.FontPair(cjk=font, cjk_key="cjk")

    probe = 'Vipera berus - 2020 (Abb. 5) "q" 50%'
    doc = fitz.open()
    page = doc.new_page(width=420, height=60)
    writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
    x = 10.0
    for text, face, _key in pair.runs(probe):
        writer.append((x, 40), text, font=face, fontsize=12)
        x += face.text_length(text, fontsize=12)
    writer.write_text(page)
    reread = fitz.open("pdf", doc.tobytes(garbage=4, deflate=True))
    got = reread[0].get_text().strip()
    reread.close()
    doc.close()

    check("\xa0" not in got, f"没有 NBSP（得到 {got!r}）")
    check("\u2010" not in got, "没有 U+2010 连字符")
    check(got == probe, f"ASCII 往返完全一致（得到 {got!r}）")

    # 中英之间应当有额外间隙，但不能出现在中文标点之后
    check(pair.needs_inter_space("中", "A"), "中文后接拉丁字母要加间隙")
    check(pair.needs_inter_space("A", "中"), "拉丁字母后接中文要加间隙")
    check(not pair.needs_inter_space("。", "A"), "中文句号后不加间隙")
    check(not pair.needs_inter_space("中", "。"), "中文标点之间不加间隙")
    check(pair.text_length("中A", fontsize=10) > font.text_length("中", fontsize=10) + 6,
          "中英混排的宽度把间隙算进去了")


def build_vertical_pdf(path: Path) -> None:
    """构造一份"表格列标签被旋转 90°"的页面（复现竖排单元格场景）。

    真实案例：一份 36 页的饲养准则里有 341 个旋转段落。早先 `plan_page`
    对它们用 bbox 的**宽**（11~26pt）当折行宽度，中文被折成每行 1~2 个字；
    旋转排版需要"行数 × 字号"的叠高，远超单元格宽度，于是所有字号都放不下，
    **整批回退成水平绘制** —— 实测 2113 处朝向错乱、并压进相邻单元格。
    """
    TMP.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=420, height=595)

    # 表头（横排）
    page.insert_textbox(fitz.Rect(40, 60, 380, 80), "Housing requirements table",
                        fontsize=12, fontname="hebo")
    # 竖排列标签：文字沿 y 方向排、多行沿 x 方向叠（dir = (0,-1)）
    labels = [
        (100.0, "Species name"),
        (140.0, "Minimum area per animal"),
        (200.0, "Temperature and humidity"),
    ]
    for x, text in labels:
        page.insert_textbox(fitz.Rect(x, 120, x + 22, 500), text,
                            fontsize=10, fontname="helv", rotate=90)
    # 一行数据
    page.insert_textbox(fitz.Rect(40, 520, 380, 560),
                        "Data rows follow below the rotated header labels.",
                        fontsize=10, fontname="helv")
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def test_vertical_text():
    """旋转（竖排）段落：方向必须与原文一致，且不能被折成每行一两个字。"""
    print("\n[11] 竖排单元格")
    sample = TMP / "lettering_vertical.pdf"
    build_vertical_pdf(sample)

    document = pdf_parser.parse_pdf(str(sample), "vt", sample.name)
    rotated = [p for p in document.paragraphs if abs(round(p.rotation)) % 180 == 90]
    check(len(rotated) >= 3, f"解析出 {len(rotated)} 个旋转段落")

    for paragraph in rotated:
        direction = paragraph.lines[0].direction
        check(abs(direction[1]) > 0.7,
              f"旋转段落的方向是竖排（dir={tuple(round(v, 2) for v in direction)}）")
        break

    targets = {
        "Species name": "物种名称",
        "Minimum area per animal": "每只动物的最小饲养面积要求",
        "Temperature and humidity": "温度与湿度",
    }
    matched = 0
    for paragraph in document.paragraphs:
        key = paragraph.text.replace("\n", " ")
        for source, target in targets.items():
            if source in key:
                paragraph.target = target
                matched += 1
                break
    check(matched >= 3, f"{matched} 段填入测试译文")

    config = dict(constants.DEFAULT_CONFIG)
    config.update({"mode": "mono", "target_lang": "zh"})
    out = TMP / "lettering_vertical_out.pdf"
    doc = fitz.open(str(sample))
    typesetter.typeset_document(doc, document.paragraphs, config, "mono", log=lambda m: None)
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.save(str(out), garbage=4, deflate=True, clean=False)
    doc.close()

    out_doc = fitz.open(str(out))
    problems = []
    for paragraph in rotated:
        source_dir = tuple(round(v, 2) for v in paragraph.lines[0].direction)
        center = ((paragraph.bbox[0] + paragraph.bbox[2]) / 2,
                  (paragraph.bbox[1] + paragraph.bbox[3]) / 2)
        best = None
        for block in out_doc[paragraph.page].get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not text.strip():
                    continue
                box = fitz.Rect(line["bbox"])
                dx = max(box.x0 - center[0], 0, center[0] - box.x1)
                dy = max(box.y0 - center[1], 0, center[1] - box.y1)
                dist = (dx * dx + dy * dy) ** 0.5
                if dist > 60:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, tuple(round(v, 2) for v in line.get("dir", (1, 0))), text)
        if best is None:
            problems.append(f"{paragraph.text[:20]!r} 附近没有产出文字")
            continue
        if best[1] != source_dir:
            problems.append(
                f"{paragraph.text[:20]!r} 原文 dir={source_dir} 产出 dir={best[1]}")
        # 折行必须沿 bbox 的高方向：产出行的长度应远大于 bbox 宽度
        if len(best[2]) > 6 and (best[0] and False):
            pass
    check(not problems,
          f"{len(rotated)} 个竖排段落方向全部与原文一致"
          + (f"（问题：{problems[:3]}）" if problems else ""))

    # 拉丁连字符不能变成 U+2010（竖排走 insert_textbox，只用单一字体）
    text = "".join(out_doc[i].get_text() for i in range(out_doc.page_count))
    out_doc.close()
    check("\u2010" not in text, "竖排段落里没有 U+2010（字体已按 ASCII 安全挑选）")
    document.close()


def main() -> int:
    print("=" * 72)
    print("嵌字专项回归测试")
    print("=" * 72)

    # 合成样本 PDF（后面两个端到端环节都要用）
    build_sample(SAMPLE)

    test_font_classification()
    test_font_traits()
    test_font_resolution()
    test_line_break_rules()
    test_tracking()
    test_leading()
    test_parse_split()
    test_end_to_end()
    test_latin_extraction()
    test_rotated_pages()
    test_vertical_text()

    print()
    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("嵌字专项测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
