"""OCR 功能测试：扫描版 PDF 的识别与翻译。

构造"扫描件"的方式：先用 PyMuPDF 生成带文字的页面，渲染成位图，
再把这些位图放进一个**只有图片、没有文本层**的新 PDF —— 这正是扫描件的特征。
然后验证整条链路：扫描页判定 -> OCR -> 翻译 -> 排版 -> 导出。

用法：
    python tests/test_ocr.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.core import ocr as ocr_pkg  # noqa: E402
from src.core import pdf_parser, pipeline  # noqa: E402
from src.core.ocr import scanner as ocr_scanner  # noqa: E402
from src.core.ocr.engine import PPOCREngine  # noqa: E402
from src.core.task import Task  # noqa: E402
from src.shared import constants  # noqa: E402
from src.shared.logger import setup_logging  # noqa: E402
from src.shared.path_helpers import ensure_dirs  # noqa: E402

FAILURES: list[str] = []

# 测试用的"扫描"内容：(文本, 与下一行的额外间距)
# 真实排版里段落之间会多留一些空白，这里如实模拟，
# 否则所有行等距排列，肉眼也分不出段落。
SCAN_BLOCKS = [
    ("Amphibien Deutschlands", 58),
    ("The dominant sequence transduction models are based on", 30),
    ("complex recurrent or convolutional neural networks.", 52),
    ("Länge 4,5-6,5 cm", 52),
    ("(Pelophylax lessonae) Kleiner Wasserfrosch", 52),
    ("Contact: gs@dght.de https://www.dght.de", 0),
]
SCAN_LINES = [text for text, _gap in SCAN_BLOCKS]


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def build_scanned_pdf(path: str, pages: int = 1) -> None:
    """生成一份只有图像、没有文本层的 PDF（模拟扫描件）。

    注意：拉丁文必须用拉丁字体渲染。如果拿 CJK 字体（如 china-s）去画英文，
    字形会变成全角样式，OCR 会把它认成全角字符并插入多余空格 —— 这是
    "测试样本不像真实扫描件"导致的假失败，不是识别引擎的问题。
    """
    source = fitz.open()
    for index in range(pages):
        page = source.new_page(width=595, height=520)
        page.insert_textbox(
            fitz.Rect(50, 40, 545, 80), SCAN_BLOCKS[0][0],
            fontsize=22, fontname="hebo", align=1,
        )
        y = 110.0
        for text, gap in SCAN_BLOCKS[1:]:
            has_cjk = any("\u3000" <= ch <= "\u9fff" for ch in text)
            page.insert_textbox(
                fitz.Rect(50, y, 545, y + 40), text,
                fontsize=13, fontname="china-s" if has_cjk else "helv",
            )
            y += 26 + gap

    # 渲染成位图，再放进一个全新的、纯图片的 PDF
    scanned = fitz.open()
    for index in range(pages):
        pixmap = source[index].get_pixmap(matrix=fitz.Matrix(2.6, 2.6), alpha=False)
        new_page = scanned.new_page(width=source[index].rect.width,
                                    height=source[index].rect.height)
        new_page.insert_image(new_page.rect, stream=pixmap.tobytes("png"))
    scanned.save(path)
    scanned.close()
    source.close()


def main() -> int:
    ensure_dirs()
    setup_logging(quiet=True)

    print("=" * 72)
    print("1) 准备 OCR 模型")
    try:
        info = ocr_pkg.ensure_models("ch", log=lambda m: print(f"     {m}"))
        print(f"     模型目录：{info['dir']}")
        check(info["ready"], "OCR 模型就绪")
    except Exception as exc:
        print(f"     无法准备模型：{exc}")
        print("     请联网后重试：python -m src.main --download-ocr ch")
        return 1

    print("2) 构造模拟扫描件")
    sample = str(ROOT / "work" / "tmp" / "sample_scanned.pdf")
    (ROOT / "work" / "tmp").mkdir(parents=True, exist_ok=True)
    build_scanned_pdf(sample)

    doc = fitz.open(sample)
    layer_chars = sum(len(doc[i].get_text().strip()) for i in range(doc.page_count))
    images = sum(len(doc[i].get_images(full=True)) for i in range(doc.page_count))
    needed, reason = ocr_scanner.page_needs_ocr(doc[0])
    doc.close()
    print(f"     文本层字符={layer_chars}  图片={images}  判定需要OCR={needed}")
    print(f"     判定理由：{reason}")
    check(layer_chars == 0, "构造出的 PDF 确实没有文本层（是扫描件）")
    check(needed, "扫描页判定为需要 OCR")

    print("3) 单页 OCR 识别")
    engine = PPOCREngine(lang="ch")
    engine.load()
    doc = fitz.open(sample)
    array, _scale = ocr_scanner.render_page(doc[0], dpi=300)
    result = engine.recognize_page(array)
    doc.close()
    print(f"     识别 {len(result.lines)} 行，耗时 {result.elapsed:.2f}s")
    for line in result.lines:
        print(f"       [{line.confidence:.2f}] {line.text}")

    recognized = " ".join(line.text for line in result.lines)
    for expected in ("Amphibien", "Deutschlands", "sequence", "convolutional",
                     "dght.de", "Wasserfrosch"):
        check(expected.lower() in recognized.lower(), f"识别出关键词 “{expected}”")
    check("Länge 4,5-6,5 cm" in recognized or "Länge 4,5" in recognized,
          "识别出德文变音与数字（Länge 4,5-6,5 cm）")
    check(any(line.confidence > 0.85 for line in result.lines), "置信度正常（>0.85）")

    print("4) 整条流水线（OCR + 翻译 + 排版）")
    document = pdf_parser.parse_pdf(sample, "ocrtest", "sample_scanned.pdf")
    config = dict(constants.DEFAULT_CONFIG)
    config.update({
        "provider": "mock", "api_key": "", "model": "mock",
        "target_lang": "zh", "mode": "dual",
        "ocr_mode": "auto",
        "ocr_lang": "ch",
        "ocr_dpi": 300,
        "ocr_min_confidence": 0.5,
        "batch_size": 8, "concurrency": 2,
        "export_pdf": True, "export_markdown": True,
        "page_range": "all",
    })
    task = Task(task_id="t-ocrtest", file_id="ocrtest", filename="sample_scanned.pdf")
    pipeline.run_task(task, document, config)
    for line in task.logs:
        print(f"       | {line}")
    check(task.status == "done", f"流水线执行成功（{task.status}）")
    if task.status != "done":
        print("     错误：", task.error)
        return 1

    ocr_paragraphs = [p for p in document.paragraphs if p.from_ocr]
    print(f"     OCR 段落 {len(ocr_paragraphs)} 个，可翻译 {len(document.translatable_paragraphs())} 段")
    for paragraph in ocr_paragraphs:
        print(f"       · {paragraph.text[:60]!r}")
    check(len(ocr_paragraphs) >= 4, f"OCR 产出 {len(ocr_paragraphs)} 个段落（应把不同内容分开）")
    check(task.stats.get("translated", 0) >= 4, f"翻译了 {task.stats.get('translated', 0)} 段")
    # 关键：不同性质的文本不应被并成一段
    merged_badly = [
        p for p in ocr_paragraphs
        if "Contact:" in p.text and len(p.text) > 90
    ]
    check(not merged_badly, "联系方式没有被错误地并进正文段落")
    check(document.ocr_pages == [0], "记录了 OCR 页码")

    print("5) 校验产出")
    for entry in task.files:
        path = ROOT / "work" / "outputs" / task.task_id / entry["name"]
        check(path.exists() and entry["size"] > 300,
              f"{entry['name']}（{entry['label']}，{entry['size']} 字节）")

    mono = ROOT / "work" / "outputs" / task.task_id / "sample_scanned_mono.pdf"
    if mono.exists():
        out = fitz.open(str(mono))
        text = "\n".join(out[i].get_text() for i in range(out.page_count))
        out.close()
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        check(cjk > 10, f"译后 PDF 含中文（{cjk} 字）")
        check("Amphibien" not in text, "仅译文模式下原英文已被遮盖/替换")

    print("6) Web 接口")
    from src.app import create_app

    app = create_app(testing=True)
    client = app.test_client()
    status = client.get("/api/ocr/status").get_json()
    check(status["ok"], "GET /api/ocr/status 正常")
    check(status["data"]["ready"], "接口报告模型已就绪")
    check(len(status["data"]["modes"]) == 3, "返回三种 OCR 模式")
    check(status["data"]["engine_available"], "推理引擎依赖可用")

    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("OCR 功能测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
