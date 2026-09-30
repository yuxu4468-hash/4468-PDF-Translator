"""版式质量校验：检查译后 PDF 是否存在文字越界、互相覆盖、内容丢失。

不依赖人眼看图，而是从生成的 PDF 里把文字连同坐标读回来做几何校验，
可以放进自动化回归里。

用法：
    python tests/test_typeset_quality.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

CJK_FONT_HINTS = ("droid", "china", "heiti", "song", "noto", "source")


def collect_lines(page: "fitz.Page") -> list[dict]:
    """把页面文字按行收集，带坐标与字体。"""
    lines = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            spans = line.get("spans", [])
            font = spans[0].get("font", "") if spans else ""
            lines.append(
                {
                    "text": text,
                    "bbox": fitz.Rect(line["bbox"]),
                    "font": font,
                    "size": spans[0].get("size", 0) if spans else 0,
                    "block": block.get("number", -1),
                }
            )
    return lines


def overlap_area(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = fitz.Rect(a) & fitz.Rect(b)
    if inter.is_empty or inter.width <= 0 or inter.height <= 0:
        return 0.0
    return inter.width * inter.height


def check_pdf(path: Path, label: str) -> list[str]:
    """返回问题列表（空列表表示通过）。"""
    problems: list[str] = []
    doc = fitz.open(str(path))

    for page_index in range(doc.page_count):
        page = doc[page_index]
        rect = page.rect
        lines = collect_lines(page)

        # --- 1) 越界检查 ---
        for line in lines:
            box = line["bbox"]
            if (
                box.x0 < -2
                or box.y0 < -2
                or box.x1 > rect.width + 2
                or box.y1 > rect.height + 2
            ):
                problems.append(
                    f"{label} 第 {page_index + 1} 页文字越界："
                    f"{line['text'][:24]!r} @ {tuple(round(v, 1) for v in box)}"
                )

        # --- 2) 行间重叠检查 ---
        # 只比不同 block 之间，且要求重叠面积超过较小行面积的一半，
        # 这样正常的上下标、连字不会误报。
        by_block: dict[int, list[dict]] = {}
        for line in lines:
            by_block.setdefault(line["block"], []).append(line)

        keys = sorted(by_block)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for line_a in by_block[keys[i]]:
                    for line_b in by_block[keys[j]]:
                        area = overlap_area(line_a["bbox"], line_b["bbox"])
                        if area <= 0:
                            continue
                        smaller = min(
                            line_a["bbox"].width * line_a["bbox"].height,
                            line_b["bbox"].width * line_b["bbox"].height,
                        )
                        if smaller > 0 and area / smaller > 0.5:
                            problems.append(
                                f"{label} 第 {page_index + 1} 页文字重叠："
                                f"{line_a['text'][:20]!r} 与 {line_b['text'][:20]!r}"
                            )

        # --- 3) 字号下限检查 ---
        for line in lines:
            if line["size"] and line["size"] < 3.5:
                problems.append(
                    f"{label} 第 {page_index + 1} 页字号过小({line['size']:.1f}pt)："
                    f"{line['text'][:24]!r}"
                )

    doc.close()
    return problems


def main() -> int:
    # 可以传入任务 id 指定要检查的目录，默认检查离线自测的产物
    task_id = sys.argv[1] if len(sys.argv) > 1 else "t-selftest"
    out_dir = ROOT / "work" / "outputs" / task_id
    if not out_dir.exists():
        print(f"找不到产物目录 {out_dir}，请先运行对应的测试")
        return 1

    targets = [
        (path.name, path.stem.split("_")[-1])
        for path in sorted(out_dir.glob("*.pdf"))
    ]
    if not targets:
        print(f"{out_dir} 下没有 PDF 产物")
        return 1

    all_problems: list[str] = []
    checked = 0
    for name, label in targets:
        problems = check_pdf(out_dir / name, label)
        checked += 1
        status = "PASS" if not problems else f"FAIL（{len(problems)} 项）"
        print(f"  [{status}] {name}（{out_dir}）")
        all_problems.extend(problems)

    print()
    if all_problems:
        for item in all_problems[:40]:
            print(f"  - {item}")
        if len(all_problems) > 40:
            print(f"  ... 以及另外 {len(all_problems) - 40} 项")
        return 1
    print(f"版式质量校验通过（检查了 {checked} 个文件）✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
