"""真实大模型端到端测试（需要联网与可用的 API Key）。

默认跳过；设置环境变量后才执行：
    set PDFT_REAL_API_KEY=sk-xxxx
    python tests/test_real_api.py

也可以指定其它服务商：
    set PDFT_REAL_PROVIDER=siliconflow
    set PDFT_REAL_MODEL=Qwen/Qwen3-8B
    set PDFT_REAL_BASE_URL=https://api.siliconflow.cn/v1

该测试会真实调用大模型并产生少量费用（一次请求，几十个 token）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.core import pdf_parser, pipeline  # noqa: E402
from src.core.task import Task  # noqa: E402
from src.shared import constants  # noqa: E402
from src.shared.path_helpers import ensure_dirs  # noqa: E402

FAILURES: list[str] = []


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def build_doc(path: str) -> None:
    """一份带标题、正文与列表的小型英文文档。"""
    doc = fitz.open()
    page = doc.new_page()
    width = page.rect.width

    page.insert_textbox(fitz.Rect(72, 60, width - 72, 100),
                        "Attention Is All You Need", fontsize=17, fontname="hebo", align=1)
    page.insert_textbox(fitz.Rect(72, 115, width - 72, 135),
                        "Ashish Vaswani, Noam Shazeer, Niki Parmar", fontsize=10,
                        fontname="tiro", align=1)
    page.insert_textbox(
        fitz.Rect(72, 160, width - 72, 260),
        "The dominant sequence transduction models are based on complex recurrent or "
        "convolutional neural networks that include an encoder and a decoder. The best "
        "performing models also connect the encoder and decoder through an attention "
        "mechanism. We propose a new simple network architecture, the Transformer, based "
        "solely on attention mechanisms, dispensing with recurrence and convolutions entirely.",
        fontsize=10.5, fontname="helv",
    )
    page.insert_textbox(
        fitz.Rect(72, 285, width - 72, 380),
        "Experiments on two machine translation tasks show these models to be superior in "
        "quality while being more parallelizable and requiring significantly less time to "
        "train. Our model achieves 28.4 BLEU on the WMT 2014 English-to-German translation "
        "task, improving over the existing best results by over 2 BLEU.",
        fontsize=10.5, fontname="helv",
    )
    page.insert_textbox(
        fitz.Rect(72, 405, width - 72, 470),
        "1. The Transformer allows for significantly more parallelization.\n"
        "2. It reaches a new state of the art in translation quality.\n"
        "3. Training time is reduced by an order of magnitude.",
        fontsize=10.5, fontname="helv",
    )
    doc.save(path)
    doc.close()


def main() -> int:
    api_key = os.environ.get("PDFT_REAL_API_KEY", "").strip()
    if not api_key:
        print("未设置 PDFT_REAL_API_KEY，跳过真实 API 测试。")
        print("如需执行：set PDFT_REAL_API_KEY=sk-xxxx  &&  python tests/test_real_api.py")
        return 0

    provider = os.environ.get("PDFT_REAL_PROVIDER", "deepseek")
    model = os.environ.get("PDFT_REAL_MODEL", "deepseek-chat")
    base_url = os.environ.get("PDFT_REAL_BASE_URL", "https://api.deepseek.com")

    ensure_dirs()
    from src.shared.logger import setup_logging

    setup_logging(quiet=True)

    print("=" * 72)
    print(f"真实大模型测试：{provider} / {model} @ {base_url}")

    sample = str(ROOT / "work" / "tmp" / "sample_real.pdf")
    build_doc(sample)

    print("1) 解析")
    document = pdf_parser.parse_pdf(sample, "realtest", "sample_real.pdf")
    print(f"     {document.page_count} 页 / {len(document.paragraphs)} 段")
    check(document.page_count == 1, "解析出 1 页")

    print("2) 真实翻译")
    config = dict(constants.DEFAULT_CONFIG)
    config.update({
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "target_lang": "zh",
        "mode": "dual",
        "batch_size": 8,
        "concurrency": 2,
        "timeout": 120,
        "max_retries": 2,
        "temperature": 0.2,
        "export_pdf": True,
        "export_markdown": True,
        "page_range": "all",
        "_label": provider,
    })

    task = Task(task_id="t-realtest", file_id="realtest", filename="sample_real.pdf")
    started = time.time()
    pipeline.run_task(task, document, config)
    elapsed = time.time() - started

    print(f"     状态={task.status} 耗时={elapsed:.1f}s")
    for line in task.logs:
        print(f"       | {line}")
    if task.status != "done":
        print(f"     错误：{task.error}")
        check(False, "流水线执行成功")
        return 1

    check(task.status == "done", "流水线执行成功")
    check(task.stats.get("failed", 0) == 0, "没有翻译失败的段落")

    print("3) 译文质量检查")
    for paragraph in document.paragraphs:
        if not paragraph.target:
            continue
        source = paragraph.text.replace("\n", " ")[:70]
        target = paragraph.target.replace("\n", " ")[:70]
        print(f"     原文: {source}")
        print(f"     译文: {target}")
        print()

    translated = [p for p in document.paragraphs if p.target]
    check(len(translated) >= 4, f"共翻译 {len(translated)} 段")

    # 正文段落应当翻译成中文。
    # 注意：只有专有名词的段落（例如作者署名）保留原文是正确行为，
    # 所以这里只对"较长的正文段落"做要求，并另外统计整体中文字符占比。
    body = [p for p in translated if len(p.text.strip()) >= 60]
    good = 0
    for paragraph in body:
        target = paragraph.target
        cjk = sum(1 for ch in target if "\u4e00" <= ch <= "\u9fff")
        if cjk >= 5 and cjk > len(target) * 0.3:
            good += 1
    check(body and good == len(body), f"{good}/{len(body)} 个正文段落译成了中文")

    total_chars = sum(len(p.target.strip()) for p in translated)
    total_cjk = sum(
        1 for p in translated for ch in p.target if "\u4e00" <= ch <= "\u9fff"
    )
    ratio = total_cjk / total_chars if total_chars else 0
    check(ratio > 0.35, f"整体中文字符占比 {ratio:.0%}")

    # 专有名词应当被保留
    joined = " ".join(p.target for p in translated)
    check("Transformer" in joined or "变换器" in joined,
          "术语“Transformer”被合理处理（保留或按术语表翻译）")

    print("4) 产物检查")
    for entry in task.files:
        path = ROOT / "work" / "outputs" / task.task_id / entry["name"]
        print(f"     {entry['name']}（{entry['size']} 字节）")
        check(path.exists() and entry["size"] > 300, f"{entry['name']} 已生成")

    print("5) 译后 PDF 内容检查")
    out = ROOT / "work" / "outputs" / task.task_id / "sample_real_dual.pdf"
    if out.exists():
        doc = fitz.open(str(out))
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        doc.close()
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        check(cjk > 200, f"译后 PDF 含 {cjk} 个中文字符")
        check("attention" in text.lower(), "双语 PDF 保留了英文原文")

    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("真实大模型端到端测试全部通过 ✓")
    print(f"产物目录：{ROOT / 'work' / 'outputs' / task.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
