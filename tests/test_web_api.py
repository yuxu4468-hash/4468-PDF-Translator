"""Web 层集成测试：走真实 HTTP 接口跑完整流程。

使用 Flask 测试客户端，不启动真实服务器、不联网：
    上传 -> 文档信息 -> 预览 -> 启动翻译 -> 轮询进度 -> 结果 -> 下载 -> 译文预览

用法：
    python tests/test_web_api.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from src.app import create_app  # noqa: E402
from src.shared.path_helpers import ensure_dirs  # noqa: E402

FAILURES: list[str] = []


def check(condition, label):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        FAILURES.append(label)


def build_sample_pdf() -> bytes:
    """生成一份两页的英文测试 PDF（含页眉页脚与标题）。"""
    doc = fitz.open()
    body = (
        "This paper presents a lightweight approach for translating PDF documents "
        "with large language models. The pipeline extracts text blocks, analyses the "
        "layout, and writes the translation back while keeping the original formatting."
    )
    for index in range(2):
        page = doc.new_page()
        width, height = page.rect.width, page.rect.height
        page.insert_textbox(fitz.Rect(72, 30, width - 72, 50),
                            "International Conference on Document Engineering",
                            fontsize=8, fontname="helv")
        page.insert_textbox(fitz.Rect(72, height - 45, width - 72, height - 25),
                            str(index + 1), fontsize=9, fontname="helv", align=1)
        # 注意：insert_textbox 放不下时返回负数且不写入任何内容，
        # 所以标题框高度必须留够（15pt 粗体约需 25.3pt）。
        page.insert_textbox(fitz.Rect(72, 66, width - 72, 100),
                            f"{index + 1}. Introduction", fontsize=15, fontname="hebo")
        page.insert_textbox(fitz.Rect(72, 110, width - 72, 300),
                            f"Paragraph {index + 1}. {body}", fontsize=10.5, fontname="helv")
        page.insert_textbox(fitz.Rect(72, 320, width - 72, 480),
                            f"Second paragraph {index + 1}. {body}", fontsize=10.5, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def main() -> int:
    ensure_dirs()
    app = create_app(testing=True)
    client = app.test_client()

    print("=" * 72)
    print("1) 页面与系统信息")
    page = client.get("/")
    check(page.status_code == 200 and b"PDF" in page.data, "GET / 返回主界面 HTML")

    info = client.get("/api/system/info")
    payload = info.get_json()
    check(info.status_code == 200 and payload["ok"], "GET /api/system/info 正常")
    data = payload.get("data", {})
    print(f"     python={data.get('python')} pymupdf={data.get('pymupdf')} "
          f"字体={data.get('cjk_font')} 网络={data.get('network')}")
    check(data.get("cjk_ok") is True, "内置中文字体可用")

    providers = client.get("/api/providers").get_json()["data"]
    ids = [item["id"] for item in providers["providers"]]
    check("deepseek" in ids and "mock" in ids and "ollama" in ids,
          f"服务商列表包含 {len(ids)} 项")
    check(len(providers["languages"]) >= 10, f"语言列表包含 {len(providers['languages'])} 项")

    print("2) 配置读写")
    config = client.get("/api/config").get_json()["data"]["config"]
    check("provider" in config and "batch_size" in config, "GET /api/config 返回完整配置")
    response = client.post("/api/config", json={"batch_size": 7, "concurrency": 2, "__evil__": 1})
    saved = response.get_json()["data"]["config"]
    check(saved["batch_size"] == 7, "POST /api/config 保存生效")
    check("__evil__" not in saved, "非白名单字段被拒绝")
    client.post("/api/config", json={"batch_size": 12, "concurrency": 4})

    print("3) 上传与解析")
    pdf_bytes = build_sample_pdf()
    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(pdf_bytes), "sample.pdf")},
        content_type="multipart/form-data",
    )
    payload = response.get_json()
    check(response.status_code == 200 and payload["ok"], "POST /api/upload 成功")
    if not payload["ok"]:
        print("     错误：", payload.get("error"))
        return 1
    upload = payload["data"]
    file_id = upload["file_id"]
    print(f"     file_id={file_id} 页数={upload['pages']} 段落={upload['paragraphs']} "
          f"大小={upload['size']}")
    check(upload["pages"] == 2, "解析出 2 页")
    check(upload["paragraphs"] >= 4, f"识别出 {upload['paragraphs']} 个可翻译段落")

    print("4) 文档信息与页面预览")
    doc_info = client.get(f"/api/document/{file_id}/info").get_json()["data"]
    check(len(doc_info["paragraphs"]) > 0, f"返回 {len(doc_info['paragraphs'])} 个段落")
    kinds = doc_info["statistics"]["kinds"]
    print(f"     段落类型={kinds}")
    check(kinds.get("header", 0) > 0, "识别出页眉")

    preview = client.get(f"/api/preview/{file_id}/1?zoom=1.0")
    check(preview.status_code == 200 and preview.data[:8] == b"\x89PNG\r\n\x1a\n",
          f"预览图返回 PNG（{len(preview.data)} 字节）")
    check(client.get(f"/api/preview/{file_id}/99").status_code == 404, "越界页码返回 404")

    print("5) 启动翻译（模拟服务商，不联网）")
    response = client.post("/api/translate/start", json={
        "file_id": file_id,
        "provider": "mock",
        "model": "mock",
        "api_key": "",
        "base_url": "",
        "target_lang": "zh",
        "mode": "dual",
        "batch_size": 4,
        "concurrency": 2,
        "export_pdf": True,
        "export_markdown": True,
        "page_range": "all",
    })
    payload = response.get_json()
    check(response.status_code == 200 and payload["ok"], "POST /api/translate/start 成功")
    if not payload["ok"]:
        print("     错误：", payload.get("error"))
        return 1
    task_id = payload["data"]["task_id"]
    print(f"     task_id={task_id} 预计段落={payload['data']['paragraphs']}")

    print("6) 轮询进度")
    deadline = time.time() + 90
    last = None
    status = "pending"
    while time.time() < deadline:
        progress = client.get(f"/api/translate/progress/{task_id}").get_json()["data"]
        last = progress
        status = progress["status"]
        if status in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)

    check(last is not None, "进度接口有返回")
    if last:
        print(f"     最终状态={status} 阶段={last['stage']} "
              f"百分比={last['percent']}% 耗时={last['elapsed']}s")
        for line in last["logs"][:6]:
            print(f"       | {line}")
    check(status == "done", f"任务成功完成（状态={status}）")
    if status != "done":
        print("     错误：", (last or {}).get("error"))
        return 1

    print("7) 结果与下载")
    result = client.get(f"/api/translate/result/{task_id}").get_json()["data"]
    print(f"     统计={result['stats']}")
    check(len(result["files"]) >= 3, f"产出 {len(result['files'])} 个文件")
    for entry in result["files"]:
        print(f"     - {entry['name']}（{entry['label']}，{entry['size']} 字节）")

    for entry in result["files"]:
        downloaded = client.get(entry["url"])
        check(downloaded.status_code == 200 and len(downloaded.data) == entry["size"],
              f"下载 {entry['name']} 成功")
        check("attachment" in downloaded.headers.get("Content-Disposition", ""),
              f"{entry['name']} 带下载头")

    # 目录穿越防护
    evil = client.get(f"/api/download/{task_id}/..%2F..%2Fconfig%2Fconfig.json")
    check(evil.status_code in (403, 404), f"目录穿越被拦截（{evil.status_code}）")

    print("8) 译文预览")
    out = client.get(f"/api/outpreview/{file_id}/1?kind=dual&zoom=1.0")
    check(out.status_code == 200 and out.data[:8] == b"\x89PNG\r\n\x1a\n",
          f"译文预览返回 PNG（{len(out.data)} 字节）")

    print("9) 任务管理与历史")
    cancel = client.post(f"/api/translate/cancel/{task_id}")
    check(cancel.status_code == 200, "已完成任务取消接口幂等返回")
    history = client.get("/api/history").get_json()["data"]["items"]
    check(len(history) >= 1, f"历史记录 {len(history)} 条")
    check(client.get("/api/logs?lines=20").status_code == 200, "日志接口正常")
    check(client.get("/api/translate/progress/not-exist").status_code == 404, "未知任务返回 404")

    print("10) 校验产物内容")
    dual = next((f for f in result["files"] if f["kind"] == "dual"), None)
    if dual:
        data = client.get(dual["url"]).data
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        doc.close()
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        check(cjk > 50, f"双语 PDF 含中文译文（{cjk} 字）")
        check("Introduction" in text, "双语 PDF 保留原文标题")

    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("Web 层集成测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
