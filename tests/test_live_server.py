"""对「正在运行的真实服务」做端到端验证（走真实 HTTP，不是测试客户端）。

先启动服务：
    python -m src.main --no-browser --quiet

再运行：
    python tests/test_live_server.py
    python tests/test_live_server.py --base http://127.0.0.1:9000
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import requests  # noqa: E402

FAILURES: list[str] = []


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def build_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    width = page.rect.width
    page.insert_textbox(fitz.Rect(72, 60, width - 72, 92),
                        "A Lightweight PDF Translation Pipeline", fontsize=15,
                        fontname="hebo", align=1)
    page.insert_textbox(fitz.Rect(72, 110, width - 72, 260),
                        "Large language models can translate documents with high quality. "
                        "This paper describes a lightweight pipeline that extracts text "
                        "blocks from a PDF, translates them in batches, and writes the "
                        "result back into the original layout without any neural network "
                        "running on the local machine.",
                        fontsize=10.5, fontname="helv")
    page.insert_textbox(fitz.Rect(72, 290, width - 72, 420),
                        "The approach keeps images and vector graphics untouched, which "
                        "makes it suitable for academic papers and technical reports that "
                        "contain figures and tables.",
                        fontsize=10.5, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def build_scanned_pdf() -> bytes:
    """生成一份**只有图片、没有文本层**的 PDF，用来验证 OCR 链路。"""
    source = fitz.open()
    page = source.new_page(width=595, height=420)
    page.insert_textbox(fitz.Rect(60, 40, 535, 75),
                        "Scanned Document OCR Test", fontsize=20,
                        fontname="hebo", align=1)
    lines = [
        "Optical character recognition converts scanned pages into text.",
        "The pipeline renders each page and recognises every text line.",
        "Contact: gs@dght.de https://www.dght.de",
    ]
    y = 110
    for line in lines:
        page.insert_textbox(fitz.Rect(60, y, 535, y + 40), line,
                            fontsize=12.5, fontname="helv")
        y += 52

    scanned = fitz.open()
    target = scanned.new_page(width=page.rect.width, height=page.rect.height)
    pixmap = source[0].get_pixmap(matrix=fitz.Matrix(2.6, 2.6), alpha=False)
    target.insert_image(target.rect, stream=pixmap.tobytes("png"))
    data = scanned.tobytes()
    scanned.close()
    source.close()
    return data


def main() -> int:
    base = "http://127.0.0.1:8765"
    if "--base" in sys.argv:
        base = sys.argv[sys.argv.index("--base") + 1]
    base = base.rstrip("/")

    print("=" * 72)
    print(f"目标服务：{base}")

    # 0) 服务是否在线
    try:
        info = requests.get(f"{base}/api/system/info", timeout=10).json()
    except Exception as exc:
        print(f"  无法连接服务：{exc}")
        print("  请先运行：  python -m src.main --no-browser --quiet")
        return 1
    check(info.get("ok") is True, "服务在线")
    data = info["data"]
    print(f"     {data['app_name']} v{data['app_version']} | "
          f"PyMuPDF {data['pymupdf']} | 字体 {data['cjk_font']}")

    # 应用元信息：名字必须与 constants.APP_NAME 一致（改名后这里是最先该发现的地方）
    from src.shared import constants

    check(data["app_name"] == constants.APP_NAME,
          f"接口返回的应用名与代码一致（{data['app_name']}）")

    # 中文必须是正确的 UTF-8（而不是乱码）。
    # 注意：应用名现在是纯 ASCII，**不能**拿它当编码样本了 ——
    # 这句原本断言的就是"中文能正确往返"，所以改用一个真正含中文的字段。
    slogan = data.get("app_slogan") or ""
    check(slogan and slogan == constants.APP_SLOGAN,
          f"接口返回的中文编码正确（UTF-8，样本：{slogan!r}）")
    check(data["cjk_ok"] is True, "中文字体可用")

    # 1) 静态资源
    page = requests.get(f"{base}/", timeout=10)
    check(page.status_code == 200 and "PDF" in page.text, "主页面可访问")
    for asset in ("/static/css/style.css", "/static/js/api.js", "/static/js/main.js"):
        response = requests.get(base + asset, timeout=10)
        check(response.status_code == 200 and len(response.content) > 1000,
              f"静态资源 {asset}（{len(response.content)} 字节）")

    # 2) 配置与服务商
    providers = requests.get(f"{base}/api/providers", timeout=10).json()["data"]
    check(len(providers["providers"]) >= 8, f"服务商 {len(providers['providers'])} 项")
    check(len(providers["languages"]) >= 10, f"语言 {len(providers['languages'])} 项")
    config = requests.get(f"{base}/api/config", timeout=10).json()["data"]["config"]
    check("provider" in config, "配置可读取")

    # 3) 上传
    #    加 --ocr 时改用"只有图片、没有文本层"的扫描件，验证 OCR 链路
    use_ocr = "--ocr" in sys.argv
    if use_ocr:
        file_bytes, upload_name = build_scanned_pdf(), "live_scanned.pdf"
    else:
        file_bytes, upload_name = build_pdf(), "live.pdf"
    response = requests.post(
        f"{base}/api/upload",
        files={"file": (upload_name, file_bytes, "application/pdf")},
        timeout=180,
    )
    payload = response.json()
    check(response.status_code == 200 and payload["ok"], "上传 PDF 成功")
    if not payload["ok"]:
        print("     错误：", payload.get("error"))
        return 1
    uploaded = payload["data"]
    file_id = uploaded["file_id"]
    print(f"     file_id={file_id} 页数={uploaded['pages']} 段落={uploaded['paragraphs']}")
    if use_ocr:
        check(uploaded["paragraphs"] == 0, "扫描件在上传时确实提取不到文本（符合预期）")

    # 4) 预览
    preview = requests.get(f"{base}/api/preview/{file_id}/1?zoom=1.0", timeout=30)
    check(preview.status_code == 200 and preview.content[:4] == b"\x89PNG",
          f"原文预览（{len(preview.content)} 字节 PNG）")

    # 4b) OCR 相关接口（--ocr 时）
    if use_ocr:
        ocr_status = requests.get(f"{base}/api/ocr/status", timeout=30).json()
        check(ocr_status.get("ok") is True, "GET /api/ocr/status 正常")
        ocr_data = ocr_status.get("data", {})
        check(ocr_data.get("engine_available") is True,
              f"OCR 引擎可用（{ocr_data.get('engine_reason') or '依赖齐全'}）")
        check(ocr_data.get("ready") is True,
              f"OCR 模型就绪（{len(ocr_data.get('present', []))} 个文件）")
        # 单页试识别
        probe = requests.post(f"{base}/api/ocr/test", json={
            "file_id": file_id, "page": 1, "lang": "ch", "dpi": 300,
        }, timeout=180).json()
        check(probe.get("ok") is True, "POST /api/ocr/test 正常")
        if probe.get("ok"):
            info = probe["data"]
            print(f"     试识别：{info['reason']}，识别 {len(info['lines'])} 行，"
                  f"耗时 {info['elapsed']}s")
            for line in info["lines"][:4]:
                print(f"       [{line['confidence']:.2f}] {line['text']}")
            check(info.get("needed") is True, "接口判定该页为扫描页")
            check(len(info["lines"]) >= 4, f"识别出 {len(info['lines'])} 行文字")
            joined = " ".join(line["text"] for line in info["lines"])
            check("Scanned" in joined or "scanned" in joined.lower(),
                  "识别出标题关键词 “Scanned”")

    # 5) 启动翻译
    #    默认用「模拟翻译」（不联网）；加 --real 时改用真实服务商
    use_real = "--real" in sys.argv
    if use_real:
        api_key = os.environ.get("PDFT_REAL_API_KEY", "").strip()
        if not api_key:
            print("  用了 --real 但没有设置 PDFT_REAL_API_KEY，改为模拟翻译")
            use_real = False
    if use_real:
        provider = os.environ.get("PDFT_REAL_PROVIDER", "deepseek")
        model = os.environ.get("PDFT_REAL_MODEL", "deepseek-chat")
        base_url = os.environ.get("PDFT_REAL_BASE_URL", "https://api.deepseek.com")
        start_payload = {
            "file_id": file_id, "provider": provider, "model": model,
            "api_key": api_key, "base_url": base_url,
            "target_lang": "zh", "mode": "dual",
            "batch_size": 8, "concurrency": 2, "temperature": 0.2,
            "export_pdf": True, "export_markdown": True,
        }
        print(f"     使用真实模型：{provider}/{model}")
        # 先测连通性
        probe = requests.post(f"{base}/api/test-connection", json={
            "provider": provider, "api_key": api_key,
            "base_url": base_url, "model": model,
        }, timeout=90).json()
        check(probe.get("ok") is True,
              f"测试连接接口（{probe.get('data', {}).get('latency_ms', '?')}ms）"
              if probe.get("ok") else f"测试连接失败：{probe.get('error')}")
    else:
        start_payload = {
            "file_id": file_id, "provider": "mock", "model": "mock",
            "target_lang": "zh", "mode": "dual",
            "export_pdf": True, "export_markdown": True,
        }
    if use_ocr:
        start_payload.update({"ocr_mode": "auto", "ocr_lang": "ch", "ocr_dpi": 300,
                              "ocr_min_confidence": 0.5})

    response = requests.post(f"{base}/api/translate/start", json=start_payload, timeout=30)
    payload = response.json()
    check(response.status_code == 200 and payload["ok"], "启动翻译任务")
    if not payload["ok"]:
        print("     错误：", payload.get("error"))
        return 1
    task_id = payload["data"]["task_id"]
    print(f"     task_id={task_id}")

    # 6) 轮询
    progress = None
    deadline = time.time() + 120
    while time.time() < deadline:
        progress = requests.get(f"{base}/api/translate/progress/{task_id}", timeout=15).json()["data"]
        if progress["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    check(progress and progress["status"] == "done",
          f"任务完成（状态={progress['status'] if progress else '?'}）")
    if progress:
        print(f"     {progress['percent']}%  {progress['elapsed']}s  {progress['message']}")
    if not progress or progress["status"] != "done":
        return 1

    # 7) 结果与下载
    result = requests.get(f"{base}/api/translate/result/{task_id}", timeout=15).json()["data"]
    check(len(result["files"]) >= 3, f"产出 {len(result['files'])} 个文件")
    for entry in result["files"]:
        downloaded = requests.get(base + entry["url"], timeout=30)
        check(downloaded.status_code == 200 and len(downloaded.content) == entry["size"],
              f"下载 {entry['name']}（{entry['size']} 字节）")

    # 8) 译文预览
    out = requests.get(f"{base}/api/outpreview/{file_id}/1?kind=dual&zoom=1.0", timeout=30)
    check(out.status_code == 200 and out.content[:4] == b"\x89PNG",
          f"译文预览（{len(out.content)} 字节 PNG）")

    # 9) 译文内容
    dual = next((f for f in result["files"] if f["kind"] == "dual"), None)
    if dual:
        data = requests.get(base + dual["url"], timeout=30).content
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        doc.close()
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        check(cjk > 30, f"译文 PDF 含中文（{cjk} 字）")
        # 双语模式应当把原文一并重绘；扫描件重绘的是 OCR 出来的文字
        expect_source = "Scanned Document" if use_ocr else "Large language models"
        check(expect_source in text, f"双语 PDF 保留原文（{expect_source}）")
    else:
        check(False, "结果里有双语 PDF")

    # 10) 清理
    cleanup = requests.delete(f"{base}/api/translate/task/{task_id}", timeout=15)
    check(cleanup.status_code == 200, "删除任务接口正常")

    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("真实服务端到端验证全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
