"""离线端到端自测：解析 -> 翻译（模拟）-> 排版 -> 导出。

不联网、不调用真实大模型，用于验证整条链路是否正常。
用法：
    python tests/test_pipeline.py
生成物在 work/outputs/ 下，另外会在 tests/ 目录输出校对用的文档分布统计。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core import pdf_parser, pipeline, store  # noqa: E402
from src.core.task import Task  # noqa: E402
from src.shared import constants  # noqa: E402
from src.shared.path_helpers import ensure_dirs  # noqa: E402


def build_sample_pdf(path: str) -> str:
    """生成一份带有多栏、页眉页脚、标题的测试 PDF。"""
    import fitz

    doc = fitz.open()
    body = (
        "This section describes the method used in our experiment. "
        "We collected a large corpus of documents and divided it into training and test splits. "
        "The model was trained for twenty epochs using a batch size of thirty two. "
        "Evaluation shows a clear improvement over the baseline system."
    )
    for index in range(3):
        page = doc.new_page()
        width, height = page.rect.width, page.rect.height

        # 页眉（每页重复，应被识别为页眉并跳过）
        page.insert_textbox(
            fitz.Rect(72, 30, width - 72, 50),
            "Journal of Document Processing, Vol. 12",
            fontsize=8, fontname="helv",
        )
        # 页脚页码
        page.insert_textbox(
            fitz.Rect(72, height - 45, width - 72, height - 25),
            f"{index + 1}",
            fontsize=9, fontname="helv", align=1,
        )
        # 跨栏标题
        page.insert_textbox(
            fitz.Rect(60, 60, width - 60, 90),
            f"{index + 1}. System Architecture and Design",
            fontsize=15, fontname="hebo",
        )
        # 左栏
        page.insert_textbox(
            fitz.Rect(60, 100, 290, 420),
            f"Left column paragraph {index + 1}. {body}",
            fontsize=9.5, fontname="helv",
        )
        page.insert_textbox(
            fitz.Rect(60, 430, 290, 720),
            f"Left column second paragraph {index + 1}. {body}",
            fontsize=9.5, fontname="helv",
        )
        # 右栏
        page.insert_textbox(
            fitz.Rect(310, 100, width - 60, 420),
            f"Right column paragraph {index + 1}. {body}",
            fontsize=9.5, fontname="helv",
        )
        page.insert_textbox(
            fitz.Rect(310, 430, width - 60, 720),
            f"Right column second paragraph {index + 1}. {body}",
            fontsize=9.5, fontname="helv",
        )

    doc.save(path)
    doc.close()
    return path


def make_config(**overrides) -> dict:
    config = dict(constants.DEFAULT_CONFIG)
    config.update(
        {
            "provider": "mock",
            "api_key": "",
            "model": "mock",
            "target_lang": "zh",
            "mode": "dual",
            "batch_size": 4,
            "concurrency": 3,
            "skip_headers_footers": True,
            "skip_repeated": True,
            "skip_non_translatable": True,
            "merge_paragraphs": True,
            "export_pdf": True,
            "export_markdown": True,
            "page_range": "all",
        }
    )
    config.update(overrides)
    return config


def main() -> int:
    ensure_dirs()
    from src.shared.logger import setup_logging

    setup_logging(quiet=True)

    failures: list[str] = []

    def check(condition, label):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}")
        if not condition:
            failures.append(label)

    # ------------------------------------------------------------------
    print("=" * 72)
    print("1) 生成测试 PDF")
    sample = str(ROOT / "work" / "tmp" / "sample_multicolumn.pdf")
    build_sample_pdf(sample)
    check(Path(sample).exists(), f"测试文件已生成（{os.path.getsize(sample)} 字节）")

    # ------------------------------------------------------------------
    print("2) 解析与版面分析")
    document = pdf_parser.parse_pdf(sample, "testfile", "sample_multicolumn.pdf")
    stats = pdf_parser.document_statistics(document)
    print(f"     页数={stats['pages']} 段落={stats['paragraphs']} 分栏={stats['columns']}")
    check(stats["pages"] == 3, "解析出 3 页")
    check(stats["columns"] == 2, "识别出双栏版面")

    kinds = stats["kinds"]
    print(f"     段落类型分布={kinds}")
    check(kinds.get("header", 0) > 0, "识别出页眉")
    check(kinds.get("page_num", 0) > 0 or kinds.get("footer", 0) > 0, "识别出页脚/页码")
    check(kinds.get("title", 0) > 0, "识别出标题")

    # 阅读顺序：第 1 页左栏第二段应排在右栏第一段之前
    page_one = [p for p in document.paragraphs if p.page == 0]
    texts = [p.text[:24] for p in page_one]
    print("     第 1 页阅读顺序：")
    for order, text in enumerate(texts):
        print(f"       {order}: {text!r}")
    left_second = next(
        (i for i, t in enumerate(texts) if t.startswith("Left column second")), None
    )
    right_first = next(
        (i for i, t in enumerate(texts) if t.startswith("Right column paragraph")), None
    )
    check(
        left_second is not None and right_first is not None and left_second < right_first,
        "双栏阅读顺序正确（左栏读完再读右栏）",
    )

    # ------------------------------------------------------------------
    print("3) 可翻译性筛选")
    pdf_parser.mark_translatable(document, "zh")
    translatable = document.translatable_paragraphs()
    skipped = [p for p in document.paragraphs if not p.translatable]
    print(f"     可翻译 {len(translatable)} 段 / 跳过 {len(skipped)} 段")
    reasons = {}
    for paragraph in skipped:
        reasons[paragraph.skip_reason] = reasons.get(paragraph.skip_reason, 0) + 1
    print(f"     跳过原因={reasons}")
    check(len(translatable) >= 12, "正文段落被正确标记为可翻译")
    check(all(p.skip_reason for p in skipped), "被跳过的段落都有跳过原因")

    # ------------------------------------------------------------------
    print("4) 运行完整流水线（模拟翻译）")
    config = make_config()
    task = Task(task_id="t-selftest", file_id="testfile", filename="sample_multicolumn.pdf")
    started = time.time()
    pipeline.run_task(task, document, config)
    elapsed = time.time() - started

    print(f"     状态={task.status} 阶段={task.stage} 耗时={elapsed:.1f}s")
    for line in task.logs:
        print(f"       | {line}")
    if task.status != "done":
        print(f"     错误：{task.error}")

    check(task.status == "done", "流水线执行成功")
    check(task.stats.get("translated", 0) > 0, f"翻译了 {task.stats.get('translated', 0)} 段")
    check(task.stats.get("failed", 0) == 0, "没有翻译失败的段落")

    # ------------------------------------------------------------------
    print("5) 校验输出文件")
    for entry in task.files:
        path = ROOT / "work" / "outputs" / task.task_id / entry["name"]
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        check(exists and size > 300, f"{entry['name']}（{entry['label']}，{size} 字节）")

    # 校验 PDF 里确实写入了中文
    import fitz

    for kind in ("dual", "mono"):
        path = ROOT / "work" / "outputs" / task.task_id / f"sample_multicolumn_{kind}.pdf"
        if not path.exists():
            check(False, f"{kind} PDF 存在")
            continue
        out = fitz.open(str(path))
        text = "\n".join(out[i].get_text() for i in range(out.page_count))
        out.close()
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        check(cjk > 100, f"{kind} PDF 含中文译文（{cjk} 个中文字符）")
        if kind == "dual":
            check("This section describes" in text, "双语 PDF 保留英文原文")
        else:
            check("Left column paragraph" not in text, "仅译文 PDF 已移除英文原文")

    # ------------------------------------------------------------------
    print("6) 校验 Markdown 导出")
    md = ROOT / "work" / "outputs" / task.task_id / "sample_multicolumn_bilingual.md"
    if md.exists():
        content = md.read_text(encoding="utf-8")
        check("**原文**" in content and "**译文**" in content, "Markdown 含原文/译文对照")
        check("## 第 1 页" in content, "Markdown 含按页分节")
    else:
        check(False, "Markdown 文件存在")

    # ------------------------------------------------------------------
    print("7) 渲染预览图（供 Web 界面使用）")
    preview_png = ROOT / "work" / "tmp" / "preview_dual_p1.png"
    out_pdf = ROOT / "work" / "outputs" / task.task_id / "sample_multicolumn_dual.pdf"
    if out_pdf.exists():
        doc = fitz.open(str(out_pdf))
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.3, 1.3))
        pix.save(str(preview_png))
        doc.close()
        check(preview_png.exists() and preview_png.stat().st_size > 5000,
              f"预览图渲染成功（{preview_png.stat().st_size} 字节）")
    else:
        check(False, "双语 PDF 存在以便预览")

    # ------------------------------------------------------------------
    print("=" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过 ✓")
    print(f"产物目录：{ROOT / 'work' / 'outputs' / task.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
