"""一键运行全部测试。

    python tests/run_all.py            # 离线测试（不需要联网/API Key）
    python tests/run_all.py --ocr      # 额外跑 OCR 测试（需要已下载 OCR 模型）
    python tests/run_all.py --real     # 额外跑真实大模型测试（需要 API Key）
    python tests/run_all.py --ocr --real

离线测试包含：
    test_frontend_contract.py  前端 DOM / 接口契约检查
    test_no_text_loss.py       译后 PDF 不丢内容（含 180° 倒排回归）
    test_lettering.py          嵌字：字体匹配 / 基线锚定 / 中文禁则 / 图片保留
    test_numeric_guard.py      数值保护：数字校验 / 定向重试 / 回退原文
    test_symbol_glyph.py       符号字形降级：缺字形符号不再画成空白方块
    test_cell_clamp.py         表格单元格内约束：译文不越格
    test_pipeline.py           解析 -> 翻译 -> 排版 -> 导出 全链路
    test_typeset_quality.py    译后 PDF 的版式几何校验（越界/重叠/字号）
    test_web_api.py            Flask 接口集成测试

可选测试：
    test_ocr.py                扫描版 PDF 的 OCR 识别与翻译（需 OCR 模型）
    test_real_api.py           真实大模型端到端

另有 test_live_server.py 用于验证「正在运行的真实服务」：
    先  python -m src.main --no-browser --quiet
    再  python tests/test_live_server.py [--real]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

STEPS = [
    ("前端契约检查", "test_frontend_contract.py"),
    ("无丢字回归", "test_no_text_loss.py"),
    ("嵌字专项", "test_lettering.py"),
    ("数值保护", "test_numeric_guard.py"),
    ("符号字形降级", "test_symbol_glyph.py"),
    ("单元格内约束", "test_cell_clamp.py"),
    ("全链路流水线", "test_pipeline.py"),
    ("版式质量校验", "test_typeset_quality.py"),
    ("Web 接口集成", "test_web_api.py"),
]

OPTIONAL = {
    "--ocr": ("OCR 扫描件识别", "test_ocr.py"),
    "--real": ("真实大模型端到端", "test_real_api.py"),
}


def run(script: str) -> int:
    path = TESTS / script
    if not path.exists():
        print(f"  找不到测试文件：{path}")
        return 1
    started = time.time()
    result = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    elapsed = time.time() - started
    print(f"  → 退出码 {result.returncode}，耗时 {elapsed:.1f}s\n")
    return result.returncode


def main() -> int:
    steps = list(STEPS)
    for flag, step in OPTIONAL.items():
        if flag in sys.argv:
            steps.append(step)

    results: list[tuple[str, int]] = []
    for label, script in steps:
        print("=" * 72)
        print(f"运行：{label}（{script}）")
        print("=" * 72)
        results.append((label, run(script)))

    print("=" * 72)
    print("汇总")
    print("=" * 72)
    failed = 0
    for label, code in results:
        print(f"  [{'PASS' if code == 0 else 'FAIL'}] {label}")
        if code != 0:
            failed += 1

    print()
    if failed:
        print(f"{failed} 个测试套件未通过")
        return 1
    print("全部测试套件通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
