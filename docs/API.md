# PDF-translater 本地服务 API 契约

服务默认监听 `http://127.0.0.1:8765`。
所有接口返回 JSON，统一结构：

```json
{ "ok": true, "data": { ... } }
{ "ok": false, "error": "错误信息" }
```

前端只需依赖本文件描述的接口。所有路径均为相对根路径。

---

## 1. 页面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 返回主界面 `index.html` |

---

## 2. 配置 `/api/config`

### GET `/api/config`
返回当前完整配置对象（已与默认值合并）。

```json
{
  "ok": true,
  "data": {
    "config": {
      "provider": "deepseek",
      "api_key": "sk-xxxx",
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-chat",
      "target_lang": "zh",
      "source_lang": "auto",
      "mode": "mono",
      "prompt": "……",
      "glossary": { "Transformer": "变换器" },
      "batch_size": 12,
      "concurrency": 4,
      "temperature": 0.3,
      "max_retries": 3,
      "timeout": 120,
      "merge_paragraphs": true,
      "skip_headers_footers": true,
      "skip_repeated": true,
      "font_family": "auto",
      "font_scale": 1.0,
      "min_font_size": 5.0,
      "line_spacing": 1.05,
      "keep_original_style": true,
      "translate_toc": true,
      "page_range": "all",

      "numeric_guard": true,
      "numeric_log_limit": 5,
      "symbol_fallback": true,
      "table_cell_clamp": true,
      "table_row_clamp": true,

      "ocr_mode": "auto",
      "ocr_lang": "ch",
      "ocr_dpi": 200,
      "ocr_min_confidence": 0.5,
      "ocr_det_version": "v5",
      "ocr_use_cls": false,
      "ocr_cover_original": true
    },
    "defaults": { "...": "内置默认值" }
  }
}
```

OCR 字段说明：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `ocr_mode` | `off` / `auto` / `always` | `auto` 只对没有文本层的扫描页做 OCR |
| `ocr_lang` | `ch` / `en` / `japan` / … | 识别用语言模型，见 `/api/ocr/status` |
| `ocr_dpi` | 120~400 | 页面渲染精度，越高越准也越慢 |
| `ocr_min_confidence` | 0~1 | 置信度下限，低于此值的识别结果丢弃 |
| `ocr_det_version` | `v5` / `v4` | 文本检测模型版本 |
| `ocr_use_cls` | true / false | 是否启用的方向分类（判断 0°/180°） |
| `ocr_cover_original` | true / false | 排版时是否遮盖扫描件里的原文 |

### POST `/api/config`
请求体：任意配置字段的子集（与现有配置深合并后落盘）。
返回同 GET 的结构，其中 `config` 为合并后的结果。

---

## 3. 服务商 `/api/providers`

### GET `/api/providers`

```json
{
  "ok": true,
  "data": {
    "providers": [
      {
        "id": "deepseek",
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "needs_key": true,
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "推荐，性价比高"
      }
    ],
    "languages": [
      { "id": "zh", "label": "简体中文" }
    ]
  }
}
```

### POST `/api/test-connection`
请求体：`{ "provider": "...", "api_key": "...", "base_url": "...", "model": "...", "timeout": 30 }`

```json
{ "ok": true, "data": { "latency_ms": 812, "reply": "连接正常", "model": "deepseek-chat" } }
```
失败时 `ok=false`，`error` 为可读原因（如“API Key 无效”“网络不可达”）。

---

## 4. 文档上传与预览

### POST `/api/upload`
`multipart/form-data`，字段名 **`file`**（.pdf）。

```json
{
  "ok": true,
  "data": {
    "file_id": "a1b2c3d4e5f6",
    "filename": "paper.pdf",
    "pages": 12,
    "size": 348812,
    "encrypted": false,
    "text_pages": 11,
    "message": "已解析 12 页"
  }
}
```

### GET `/api/preview/<file_id>/<page>`
返回 PNG（原始页面渲染图），`page` 从 **1** 开始。
查询参数 `zoom`（默认 1.4，范围 0.5~3.0）。

### GET `/api/outpreview/<file_id>/<page>`
返回**译后**页面的 PNG。仅在翻译完成后可用；未就绪时返回 404 + JSON 错误。
查询参数 `kind`（`mono` | `dual`，默认取任务的主模式）、`zoom`。

### GET `/api/document/<file_id>/info`
返回已解析文档的结构化信息（供界面显示原文/译文对照）。

```json
{
  "ok": true,
  "data": {
    "file_id": "a1b2c3d4e5f6",
    "filename": "paper.pdf",
    "pages": [
      { "page": 1, "width": 595.3, "height": 841.9, "paragraphs": 14, "columns": 1 }
    ],
    "paragraphs": [
      {
        "id": 0, "page": 1, "kind": "text",
        "source": "Chapter 1 ...", "target": "第一章 ……",
        "bbox": [72.0, 72.0, 500.6, 119.6]
      }
    ]
  }
}
```

---

## 5. 翻译任务

### POST `/api/translate/start`
请求体：

```json
{
  "file_id": "a1b2c3d4e5f6",
  "page_range": "all",
  "mode": "mono",
  "provider": "deepseek",
  "api_key": "sk-xxxx",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "target_lang": "zh",
  "prompt": "……",
  "glossary": {},
  "batch_size": 12,
  "concurrency": 4,
  "export_pdf": true,
  "export_markdown": true
}
```
除 `file_id` 外均可省略，省略时取服务器保存的配置。

```json
{ "ok": true, "data": { "task_id": "t-8f3c1a", "paragraphs": 168, "batches": 14 } }
```

### GET `/api/translate/progress/<task_id>`

```json
{
  "ok": true,
  "data": {
    "task_id": "t-8f3c1a",
    "status": "running",
    "stage": "translating",
    "message": "正在翻译第 6/14 批",
    "page_current": 5,
    "page_total": 12,
    "paragraph_done": 72,
    "paragraph_total": 168,
    "percent": 42.8,
    "elapsed": 31.4,
    "eta": 41.0,
    "logs": ["...", "..."],
    "error": null
  }
}
```

`status` 取值：`pending` | `running` | `done` | `error` | `cancelled`。
`stage` 取值：`queued` | `parsing` | `translating` | `typesetting` | `exporting` | `finished`。
`logs` 为最近 200 条日志（字符串数组）。

### POST `/api/translate/cancel/<task_id>`
```json
{ "ok": true, "data": { "status": "cancelling" } }
```

### DELETE `/api/translate/task/<task_id>`
删除任务与其产物。

### GET `/api/translate/result/<task_id>`

```json
{
  "ok": true,
  "data": {
    "task_id": "t-8f3c1a",
    "status": "done",
    "elapsed": 96.2,
    "files": [
      { "name": "paper_dual.pdf", "kind": "dual", "size": 402133, "url": "/api/download/t-8f3c1a/paper_dual.pdf" },
      { "name": "paper_mono.pdf", "kind": "mono", "size": 398001, "url": "/api/download/t-8f3c1a/paper_mono.pdf" },
      { "name": "paper_bilingual.md", "kind": "markdown", "size": 21044, "url": "/api/download/t-8f3c1a/paper_bilingual.md" }
    ],
    "stats": { "paragraphs": 168, "translated": 168, "failed": 0, "tokens": 0 }
  }
}
```

### GET `/api/download/<task_id>/<name>`
返回文件流（`Content-Disposition: attachment`）。

---

## 6. OCR（扫描版 PDF）

### GET `/api/ocr/status?lang=ch&det=v5`

```json
{
  "ok": true,
  "data": {
    "dir": "F:\\Translater\\PDF-translater\\models\\ocr",
    "lang": "ch",
    "det": "v5",
    "ready": true,
    "missing": [],
    "present": ["ch_PP-OCRv5_det_mobile.onnx", "ch_PP-OCRv5_rec_mobile.onnx", "ppocrv5_dict.txt"],
    "size": 21500000,
    "engine_available": true,
    "engine_reason": "",
    "download": { "running": false, "lang": "", "message": "", "error": "", "done": false },
    "modes": [
      { "id": "off", "label": "关闭" },
      { "id": "auto", "label": "自动（仅扫描页，推荐）" },
      { "id": "always", "label": "始终（所有页面都走 OCR）" }
    ],
    "languages": [
      { "id": "ch", "label": "中英混排（推荐）", "file": "ch_PP-OCRv5_rec_mobile.onnx", "ready": true }
    ]
  }
}
```

`engine_available` 为 false 时表示缺少运行依赖（未装 onnxruntime / numpy），
`engine_reason` 给出可读原因。

### POST `/api/ocr/download`
请求体 `{ "lang": "ch", "det": "v5" }`。下载在后台线程进行，立即返回：

```json
{ "ok": true, "data": { "message": "已开始下载", "lang": "ch" } }
```

轮询 `/api/ocr/status` 查看 `download.running` / `download.message` /
`download.error` / `download.done`。

### POST `/api/ocr/test`
对**已上传**文档的某一页做一次试识别（不翻译、不入库）：
请求体 `{ "file_id": "...", "page": 1, "lang": "ch", "dpi": 200 }`

```json
{
  "ok": true,
  "data": {
    "page": 1,
    "needed": true,
    "reason": "文本层仅 0 字符、图片占页面 100%，判定为扫描页",
    "lang": "ch",
    "elapsed": 0.62,
    "image": { "width": 1240, "height": 900 },
    "lines": [
      { "text": "Amphibien Deutschlands", "confidence": 0.99,
        "box": [166.7, 45.7, 429.0, 66.9], "angle": 0 }
    ],
    "text": "Amphibien Deutschlands\n..."
  }
}
```

---

## 7. 嵌字字体 `/api/fonts`

嵌字用哪款中文字体由后端按「原文风格 → 本机字体」自动决定，所以这里把探测
结果和匹配预览直接暴露出来，界面上不用猜"为什么我的加粗标题没粗"。

### GET `/api/fonts/status`

可选查询参数（都会影响匹配预览，不影响字体探测本身）：

| 参数 | 说明 |
| --- | --- |
| `refresh=1` | 清空缓存并重新探测本机字体 |
| `target_lang` | 目标语言，默认取配置里的值 |
| `family` | `auto` / `sans` / `serif` |
| `use_system` | `0` 表示禁用本机字体（退回内置字体） |
| `regular` / `bold` | 指定的字体文件路径 |

```json
{
  "ok": true,
  "data": {
    "count": 37,
    "usable": 27,
    "faces": [
      {
        "name": "DengXian",
        "label": "DengXian（无衬线/常规）",
        "path": "C:\\Windows\\Fonts\\Deng.ttf",
        "bold": false, "serif": false,
        "source": "系统",
        "subset_safe": true,
        "variable": false,
        "outline": "glyf",
        "space_ok": true
      }
    ],
    "preview": [
      { "style": "正文（无衬线）", "font": "DengXian", "source": "系统",
        "faux_bold": false, "subset_safe": true },
      { "style": "标题（无衬线粗体）", "font": "DengXian Bold", "source": "系统",
        "faux_bold": false, "subset_safe": true }
    ],
    "warnings": ["10 个字体无法被正确子集化（CFF/可变字体），已降权"]
  }
}
```

字段含义：

- `subset_safe` —— 能否被 `subset_fonts()` 正确压缩。`false` 的字库（CFF/OTF）
  会让产出 PDF 从几十 KB 膨胀到数 MB，因此自动降权。
- `variable` —— 可变字体（VF）。MuPDF 只取第一个命名实例，实测会把
  `NotoSansSC-VF` 解析成 `Thin`（极细），因此直接排除。
- `space_ok` —— 空格能否被正确提取。`false` 表示该字库把 `U+0020` 与
  `U+00A0` 映射到同一字形，产出里搜索多词短语会失败（实测微软雅黑、Noto Sans SC）。

### POST `/api/fonts/reload`

清空缓存并重新探测（刚装了新字体时用），返回结构与 `GET /api/fonts/status` 相同。

---

## 8. 系统与历史

### GET `/api/system/info`

```json
{
  "ok": true,
  "data": {
    "app_name": "PDF 文档翻译器",
    "app_version": "1.2.0",
    "python": "3.11.8",
    "pymupdf": "1.27.2.3",
    "platform": "Windows-10",
    "cjk_font": "Droid Sans Fallback Regular",
    "work_dir": "F:\\Translater\\PDF-translater\\work",
    "network": true,
    "ocr": { "ready": true, "lang": "ch", "dir": "..." }
  }
}
```

### GET `/api/history?limit=20`

```json
{
  "ok": true,
  "data": {
    "items": [
      { "task_id": "t-8f3c1a", "filename": "paper.pdf", "status": "done",
        "created": "2025-01-01 12:00:00", "paragraphs": 168, "elapsed": 96.2 }
    ]
  }
}
```

### GET `/api/logs?lines=200`
返回服务器日志文本 `{ "ok": true, "data": { "text": "..." } }`。
