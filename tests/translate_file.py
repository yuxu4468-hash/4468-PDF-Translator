"""用交付的翻译器翻译指定 PDF（通过真实 HTTP 接口，等价于界面操作）。

用法：
    python tests/translate_file.py "C:\\path\\to\\file.pdf" [目标语言] [模式]

例如：
    python tests/translate_file.py "C:\\x\\a.pdf" zh mono

说明：
    - 服务商/模型/密钥取自 config/config.json（也可用环境变量覆盖）。
    - 会打印进度、段落级原文/译文对照，并把产物留在 work/outputs/<task_id>/。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

BASE = os.environ.get("PDFT_BASE", "http://127.0.0.1:8765")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    source = Path(sys.argv[1])
    target_lang = sys.argv[2] if len(sys.argv) > 2 else "zh"
    mode = sys.argv[3] if len(sys.argv) > 3 else "mono"

    if not source.exists():
        print(f"找不到文件：{source}")
        return 1

    # 取服务端配置
    config = requests.get(f"{BASE}/api/config", timeout=15).json()["data"]["config"]
    provider = os.environ.get("PDFT_PROVIDER", config.get("provider"))
    model = os.environ.get("PDFT_MODEL", config.get("model"))
    api_key = os.environ.get("PDFT_API_KEY", config.get("api_key"))
    base_url = os.environ.get("PDFT_BASE_URL", config.get("base_url"))

    print("=" * 72)
    print(f"文件    : {source}（{source.stat().st_size / 1024:.0f} KB）")
    print(f"服务商  : {provider} / {model}")
    print(f"目标语言: {target_lang}    模式: {mode}")
    print("=" * 72)

    # 1) 上传
    with source.open("rb") as handle:
        response = requests.post(
            f"{BASE}/api/upload",
            files={"file": (source.name, handle, "application/pdf")},
            timeout=180,
        )
    payload = response.json()
    if not payload.get("ok"):
        print("上传失败：", payload.get("error"))
        return 1
    uploaded = payload["data"]
    file_id = uploaded["file_id"]
    print(f"[上传] {uploaded['message']}")
    print(f"       页数={uploaded['pages']} 大小={uploaded['size']} 字节")

    # 2) 启动翻译
    response = requests.post(f"{BASE}/api/translate/start", json={
        "file_id": file_id,
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "target_lang": target_lang,
        "mode": mode,
        "batch_size": 12,
        "concurrency": 4,
        "temperature": 0.2,
        "export_pdf": True,
        "export_markdown": True,
        "page_range": "all",
    }, timeout=60)
    payload = response.json()
    if not payload.get("ok"):
        print("启动失败：", payload.get("error"))
        return 1
    task_id = payload["data"]["task_id"]
    print(f"[任务] {task_id}  预计段落 {payload['data']['paragraphs']}")

    # 3) 轮询
    progress = None
    last_message = None
    deadline = time.time() + 3600
    while time.time() < deadline:
        progress = requests.get(f"{BASE}/api/translate/progress/{task_id}", timeout=30).json()["data"]
        if progress["message"] != last_message:
            last_message = progress["message"]
            print(f"       [{progress['percent']:5.1f}%] {progress['stage_label']} — {progress['message']}")
        if progress["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(1.0)

    if not progress or progress["status"] != "done":
        print("\n任务未成功完成：", (progress or {}).get("error"))
        for line in (progress or {}).get("logs", [])[-15:]:
            print("   |", line)
        return 1

    print(f"\n[完成] 耗时 {progress['elapsed']}s")
    for line in progress["logs"]:
        print("   |", line)

    # 4) 段落对照
    info = requests.get(f"{BASE}/api/document/{file_id}/info", timeout=60).json()["data"]
    print("\n" + "=" * 72)
    print("段落级对照（原文 -> 译文）")
    print("=" * 72)
    pairs = [
        p for p in info["paragraphs"]
        if p.get("source", "").strip() and p.get("translatable")
    ]
    for item in pairs:
        source_text = item["source"].replace("\n", " / ")
        target_text = (item.get("target") or "（未翻译）").replace("\n", " / ")
        print(f"[p{item['page']:>2}] {source_text}")
        print(f"      -> {target_text}")

    # 5) 结果文件
    result = requests.get(f"{BASE}/api/translate/result/{task_id}", timeout=30).json()["data"]
    out_dir = ROOT / "work" / "outputs" / task_id
    print("\n" + "=" * 72)
    print(f"产出文件（{out_dir}）")
    print("=" * 72)
    for entry in result["files"]:
        print(f"  {entry['label']:<18} {entry['name']:<34} {entry['size']:>9} 字节")
    print(f"\n统计: {result['stats']}")
    print(f"\n任务 ID: {task_id}")
    print(f"预览译文页面: {BASE}/api/outpreview/{file_id}/1?kind={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
