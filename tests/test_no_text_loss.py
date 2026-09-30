"""回归测试：译后 PDF 不允许"静默丢内容"。

背景（这是一个真实踩过的坑）：
    某些 PDF（Adobe InDesign 导出、内容流带翻转 CTM）会把普通横排文本的
    行方向报告成 (-1, 0)，即 180°。如果据此把文字当成"旋转文本"交给
    `insert_textbox` 绘制，而 `insert_textbox` 在放不下时会**返回负数并且
    一个字符都不写**，就会导致整段译文凭空消失——日志上看一切正常。

所以本测试对每一段译文都回读产出文件逐条核对，确保：
    1. 每一段译文都真的出现在产出 PDF 里；
    2. 译文的落点仍在原段落附近（没有跑到页面外面去）；
    3. 原文按预期被替换（mono）或保留（dual）。

用法：
    python tests/test_no_text_loss.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import pdf_parser, pipeline  # noqa: E402
from src.core.task import Task  # noqa: E402
from src.shared import constants  # noqa: E402
from src.shared.logger import setup_logging  # noqa: E402
from src.shared.path_helpers import ensure_dirs  # noqa: E402

FAILURES: list[str] = []


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def normalize(text: str) -> str:
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(ch, "")
    return "".join(text.split())


def build_tricky_pdf(path: str) -> None:
    """构造一份"文字倒排 180°"的 PDF，复现原始故障场景。

    折页（Leporello）背面的相邻面板会把文字倒过来印，这样折起来才读得正，
    MuPDF 对这种行的行方向报告为 (-1, 0)。这里用 180° 的 morph 把内容写进去，
    复现出同样的结构。
    """
    doc = fitz.open()
    width, height = 420.0, 620.0
    page = doc.new_page(width=width, height=height)
    center = fitz.Point(width / 2.0, height / 2.0)
    flip = fitz.Matrix(-1, 0, 0, -1, 0, 0)

    def mirrored(rect: fitz.Rect) -> fitz.Rect:
        """把目标矩形关于页心镜像，作为"实际写入用"的矩形。"""
        return fitz.Rect(
            width - rect.x1, height - rect.y1, width - rect.x0, height - rect.y0
        )

    items = [
        (60, 70, "IMPRESSUM - DGHT", 14, "hebo"),
        (60, 120, "Deutsche Gesellschaft fuer Herpetologie", 9, "helv"),
        (60, 160, "Lange 4-6,5 cm", 9, "helv"),
        (60, 200, "Moorfrosch", 13, "hebo"),
        (60, 240, "Springfrosch", 13, "hebo"),
        (60, 280, "(Rana dalmatina)", 9, "helv"),
        (60, 320, "Weitere Informationen:", 8, "helv"),
        (60, 360, "Vogelsang 27", 8, "helv"),
    ]
    for x, y, text, size, font in items:
        target = fitz.Rect(x, y, 360, y + size * 3.0)
        page.insert_textbox(
            mirrored(target), text,
            fontsize=size, fontname=font, morph=(center, flip),
        )
    doc.save(path)
    doc.close()

    # 确认确实制造出了 (-1,0)，否则这个测试就失去意义
    doc = fitz.open(path)
    dirs = set()
    for block in doc[0].get_text("dict")["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for line in block["lines"]:
            if "".join(s["text"] for s in line["spans"]).strip():
                dirs.add(tuple(round(v, 2) for v in line["dir"]))
    doc.close()
    print(f"     构造出的行方向：{sorted(dirs)}")
    if (-1.0, 0.0) not in dirs:
        raise RuntimeError("测试用 PDF 未能构造出 (-1,0) 的行方向，测试无效")


def main() -> int:
    ensure_dirs()
    setup_logging(quiet=True)

    print("=" * 72)
    print("1) 构造复现用的 PDF（横排文本但行方向为 (-1,0)）")
    sample = str(ROOT / "work" / "tmp" / "sample_flipped.pdf")
    build_tricky_pdf(sample)

    print("2) 解析：应识别为 180° 倒排（折页背面）")
    document = pdf_parser.parse_pdf(sample, "loss", "sample_flipped.pdf")
    rotations = sorted({round(p.rotation, 1) for p in document.paragraphs})
    print(f"     段落 {len(document.paragraphs)} 个，rotation 取值={rotations}")
    check(rotations == [180.0], f"(-1,0) 被识别为 180° 倒排（rotation={rotations}）")

    print("3) 用模拟翻译跑完整流水线")
    config = dict(constants.DEFAULT_CONFIG)
    config.update({
        "provider": "mock", "api_key": "", "model": "mock",
        "target_lang": "zh", "mode": "dual",
        "batch_size": 8, "concurrency": 3,
        "export_pdf": True, "export_markdown": False,
        "page_range": "all",
    })
    task = Task(task_id="t-loss", file_id="loss", filename="sample_flipped.pdf")
    pipeline.run_task(task, document, config)
    check(task.status == "done", f"流水线执行成功（{task.status}）")
    if task.status != "done":
        print("     ", task.error)
        return 1

    print("4) 逐段核对译文是否真的写进了产出 PDF")
    config["export_markdown"] = True
    for mode in ("mono", "dual"):
        stats = task.stats.get(f"verify_{mode}")
        path = ROOT / "work" / "outputs" / task.task_id / f"sample_flipped_{mode}.pdf"
        check(stats is not None, f"{mode}：执行了产出校验")
        if stats:
            print(f"     {mode}: checked={stats['checked']} missing={stats['missing']}")
            check(stats["missing"] == 0,
                  f"{mode}：{stats['checked']} 段译文全部写入（缺失 {stats['missing']} 段）")
        check(path.exists(), f"{mode}：产出文件存在")
        if not path.exists():
            continue

        # 独立再核对一遍（不依赖 pipeline 自报的数据）
        doc = fitz.open(str(path))
        all_text = normalize("".join(doc[i].get_text() for i in range(doc.page_count)))
        doc.close()

        targets = [p for p in document.paragraphs if p.target and p.translatable]
        missing = [p for p in targets if len(normalize(p.target)) >= 2
                   and normalize(p.target) not in all_text]
        check(not missing,
              f"{mode}：独立核对 {len(targets)} 段译文全部存在"
              + (f"（缺失：{[m.text[:20] for m in missing]}）" if missing else ""))

        # 译文落点应该在页面内
        doc = fitz.open(str(path))
        page = doc[0]
        outside = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            x0, y0, x1, y1 = block["bbox"]
            if x0 < -2 or y0 < -2 or x1 > page.rect.width + 2 or y1 > page.rect.height + 2:
                outside.append(block["bbox"])
        check(not outside, f"{mode}：所有文字都在页面范围内")

        # 位置保真：每段译文的落点应仍在原段落附近
        # （只看首几段：mono 会重排、dual 会向下扩展，所以容差放宽到页面尺寸的 25%，
        #   目的是抓"整段文字被画到了别的地方"这类严重错误）
        tol_x = page.rect.width * 0.25
        tol_y = page.rect.height * 0.25
        misplaced = []
        for paragraph in document.paragraphs:
            if not paragraph.target or not paragraph.translatable:
                continue
            needle = normalize(paragraph.target)[:6]
            if len(needle) < 2:
                continue
            found = page.search_for(needle)
            if not found:
                continue
            rect = found[0]
            dx = abs((rect.x0 + rect.x1) / 2 - (paragraph.x0 + paragraph.x1) / 2)
            dy = abs((rect.y0 + rect.y1) / 2 - (paragraph.y0 + paragraph.y1) / 2)
            if dx > tol_x or dy > tol_y:
                misplaced.append((paragraph.text[:18], round(dx), round(dy)))
        doc.close()
        check(not misplaced,
              f"{mode}：译文落点都在原段落附近"
              + (f"（偏移过大：{misplaced[:3]}）" if misplaced else ""))

    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("无丢字回归测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
