"""4468-PDF-Translator 全局常量与内置默认值。

本模块不依赖任何项目内其他模块，供 shared / core / interfaces / app 各层引用。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 应用元信息
# ---------------------------------------------------------------------------

APP_NAME = "4468-PDF-Translator"
APP_VERSION = "1.2.0"
APP_SLOGAN = "基于 AI 大模型的 PDF 嵌字翻译"

# 本地服务监听地址（避免与 Saber-Translator 的 5000 端口冲突）
HOST = "127.0.0.1"
PORT = 8765

# ---------------------------------------------------------------------------
# 翻译模式
# ---------------------------------------------------------------------------

MODE_DUAL = "dual"   # 双语对照：原文与译文一起排版，译文插入原文下方
MODE_MONO = "mono"   # 嵌字：把原文擦掉，在原位置原基线上写入译文（默认）

VALID_MODES = (MODE_DUAL, MODE_MONO)

MODE_LABELS = {
    MODE_MONO: "嵌字版（保留原版式）",
    MODE_DUAL: "双语对照版",
}


# ---------------------------------------------------------------------------
# 目标语言
# ---------------------------------------------------------------------------

LANGUAGES = [
    {"id": "zh", "label": "简体中文", "prompt_name": "简体中文"},
    {"id": "zh-TW", "label": "繁體中文", "prompt_name": "繁体中文"},
    {"id": "en", "label": "English 英语", "prompt_name": "英语"},
    {"id": "ja", "label": "日本語 日语", "prompt_name": "日语"},
    {"id": "ko", "label": "한국어 韩语", "prompt_name": "韩语"},
    {"id": "fr", "label": "Français 法语", "prompt_name": "法语"},
    {"id": "de", "label": "Deutsch 德语", "prompt_name": "德语"},
    {"id": "es", "label": "Español 西班牙语", "prompt_name": "西班牙语"},
    {"id": "ru", "label": "Русский 俄语", "prompt_name": "俄语"},
    {"id": "pt", "label": "Português 葡萄牙语", "prompt_name": "葡萄牙语"},
    {"id": "it", "label": "Italiano 意大利语", "prompt_name": "意大利语"},
    {"id": "ar", "label": "العربية 阿拉伯语", "prompt_name": "阿拉伯语"},
    {"id": "vi", "label": "Tiếng Việt 越南语", "prompt_name": "越南语"},
    {"id": "th", "label": "ไทย 泰语", "prompt_name": "泰语"},
]

LANGUAGE_LABELS = {item["id"]: item["label"] for item in LANGUAGES}

# 需要 CJK 字体渲染译文的目标语言（其他语言用拉丁内嵌字体即可）
CJK_TARGET_LANGS = {"zh", "zh-TW", "ja", "ko"}

# ---------------------------------------------------------------------------
# 翻译服务商预设
# ---------------------------------------------------------------------------

PROVIDERS = [
    {
        "id": "deepseek",
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "needs_key": True,
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "推荐。中文语境好、价格低，文档翻译性价比最高。",
        "kind": "openai",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "needs_key": True,
        "default_model": "Qwen/Qwen3-8B",
        "models": [
            "Qwen/Qwen3-8B",
            "Qwen/Qwen3-14B",
            "Qwen/Qwen3-32B",
            "deepseek-ai/DeepSeek-V3",
            "THUDM/GLM-4-9B-0414",
        ],
        "hint": "国内可直连，模型选择多，部分模型免费。",
        "kind": "openai",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "needs_key": True,
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
        "hint": "需要可访问 OpenAI 的网络环境。",
        "kind": "openai",
    },
    {
        "id": "moonshot",
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "needs_key": True,
        "default_model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "hint": "长上下文，适合整段连续翻译。",
        "kind": "openai",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "needs_key": True,
        "default_model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus"],
        "hint": "glm-4-flash 有免费额度。",
        "kind": "openai",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "needs_key": True,
        "default_model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
        "hint": "通义千问系列，qwen-long 适合长文档。",
        "kind": "openai",
    },
    {
        "id": "ollama",
        "label": "本地 Ollama（离线）",
        "base_url": "http://localhost:11434",
        "needs_key": False,
        "default_model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b", "gemma2:9b"],
        "hint": "本地推理，完全离线、不花钱；需要先安装 Ollama 并 pull 模型。",
        "kind": "ollama",
    },
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容接口）",
        "base_url": "",
        "needs_key": True,
        "default_model": "",
        "models": [],
        "hint": "任何提供 /v1/chat/completions 的服务都可以，例如中转站、vLLM、LM Studio。",
        "kind": "openai",
    },
    {
        "id": "mock",
        "label": "模拟翻译（仅调试，不调用大模型）",
        "base_url": "",
        "needs_key": False,
        "default_model": "mock",
        "models": ["mock"],
        "hint": "不联网、秒出结果，用于验证解析与排版流程是否正常。",
        "kind": "mock",
    },
]

PROVIDER_MAP = {item["id"]: item for item in PROVIDERS}

# ---------------------------------------------------------------------------
# 默认提示词
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = (
    "你是专业的文档翻译专家。请把用户给出的文本准确、通顺地翻译成目标语言。\n"
    "要求：\n"
    "1. 忠实原文，不增删信息，不加入自己的解释或评论。\n"
    "2. 保持原文的段落结构、标点层级与语气；学术/技术文档请使用规范的书面语。\n"
    "3. 保留原文中的数字、公式、变量名、英文缩写、代码标识符与网址，不要翻译它们。\n"
    "4. 术语前后必须统一；若用户提供了术语表，术语表优先级最高。\n"
    "5. 文档标题、章节标题、图表标题都属于正文内容，必须一并翻译，\n"
    "   不要因为它是知名作品的标题就保留原文。\n"
    "6. 只有人名、机构名、产品名、算法名等专有名词才保留原文（例如作者署名列表原样返回）。\n"
    "7. 若某段文本本身已经是目标语言，或只是数字/符号，则原样返回。"
)

# 译文输出协议：要求模型返回 JSON，便于稳定解析
JSON_PROTOCOL = (
    "【输出格式要求】\n"
    "你会收到一个 JSON 数组，元素形如 {\"i\": 序号, \"t\": \"原文\"}。\n"
    "请只输出一个 JSON 数组，元素形如 {\"i\": 序号, \"t\": \"译文\"}，序号必须与输入一一对应且数量相同。\n"
    "不要输出 Markdown 代码块标记，不要输出任何解释、前言或后记。\n"
    "译文中的换行请写成 \\n 转义，双引号请写成 \\\" 转义。"
)

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # --- 大模型 ---
    "provider": "deepseek",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "timeout": 180,
    "max_retries": 3,
    "concurrency": 4,
    "batch_size": 12,
    # --- 翻译 ---
    "source_lang": "auto",
    "target_lang": "zh",
    # 默认输出"嵌字版"：保留原 PDF 的版式、图片与矢量图形，只把文字换成译文
    "mode": MODE_MONO,
    "prompt": DEFAULT_PROMPT,
    "glossary": {},
    # --- 版式 ---
    "merge_paragraphs": True,
    "skip_headers_footers": True,
    "skip_repeated": True,
    "skip_non_translatable": True,
    "keep_original_style": True,
    "font_scale": 1.0,
    "min_font_size": 5.0,
    "line_spacing": 1.05,
    "dual_gap": 1.6,
    # --- 嵌字（字体匹配）---
    # 译文用哪个中文字体由"原文风格（衬线/粗体）→ 本机字体"自动匹配；
    # 这两项留空即自动。填了就以指定文件为准（TrueType 字库体积小得多，
    # CFF/OTF 字库无法被正确子集化，输出会膨胀到几 MB，所以不推荐）。
    "lettering_font_regular": "",
    "lettering_font_bold": "",
    # auto: 跟随原文风格；sans: 一律无衬线；serif: 一律衬线
    "lettering_font_family": "auto",
    # 是否允许使用本机已安装的字体（关闭则退化为 MuPDF 内置 CJK 字体，无粗体）
    "lettering_use_system_fonts": True,
    # 字距拉伸上限（占字号比例）：译文比原文短时把字距拉开填满原行宽；0 = 关闭
    "lettering_tracking": 0.08,

    # --- 数值保护 ---
    # 翻译后校验译文里的数字是否与原文一致；失配则定向重试一次，
    # 仍失配则回退原文（该段保留外文原样）并告警。
    "numeric_guard": True,
    # 失配明细每个文档最多输出几条（0 = 不输出明细，只留统计数字）
    "numeric_log_limit": 5,

    # --- 符号字形降级 ---
    # 译文里的数学符号（≤ ≥ × ° √ 等）在渲染字体里没有字形时，先去本机字体里找
    # 一个有该字形的（保住原符号），确实找不到才退成 ASCII 等价写法。
    # 关闭后行为与改造前一致：缺字形的字符原样保留，画出来是空白方块。
    "symbol_fallback": True,

    # --- 表格单元格内约束 ---
    # 识别出表格单元格后，把段落的可用高度收紧到单元格边界内，避免译文压到
    # 相邻单元格。网格识别不到的表格（三线表）沿用原有机制，行为不变。
    "table_cell_clamp": True,

    # --- 表格行内约束 ---
    # 三线表（只有横线、没有竖线）识别不出单元格，改为按**横线**切出行带，
    # 把可用高度收紧到行边界内。实测全库 46 份：2679 段落在行带里，
    # 其中 801 段（29.9%）的行边界比原有机制更紧。
    "table_row_clamp": True,

    # --- OCR（扫描版 PDF）---
    # auto: 只在页面没有文本层时才 OCR；always: 所有页都 OCR；off: 关闭
    "ocr_mode": "auto",
    "ocr_lang": "ch",
    # 300 DPI 是实测出来的甜点：小字号（9pt）压在照片上的版面，200 DPI 时
    # 检测会整行漏掉，300 DPI 就能完整识别；而耗时并没有变长（识别耗时
    # 主要花在把碎框裁图缩放上，图像更清晰反而更省）。
    "ocr_dpi": 300,
    "ocr_min_confidence": 0.5,
    "ocr_det_version": "v5",
    "ocr_use_cls": False,
    "ocr_cover_original": True,
    # --- 导出 ---
    "export_pdf": True,
    "export_markdown": True,
    "page_range": "all",
    "theme": "light",
}

# OCR 工作模式
OCR_MODE_OFF = "off"
OCR_MODE_AUTO = "auto"
OCR_MODE_ALWAYS = "always"
OCR_MODES = (OCR_MODE_OFF, OCR_MODE_AUTO, OCR_MODE_ALWAYS)

OCR_MODE_LABELS = {
    OCR_MODE_OFF: "关闭",
    OCR_MODE_AUTO: "自动（仅扫描页，推荐）",
    OCR_MODE_ALWAYS: "始终（所有页面都走 OCR）",
}

# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------

# 支持的文件扩展名
ALLOWED_UPLOAD_EXT = (".pdf",)

# 单文件大小上限（字节），默认 200 MB
MAX_UPLOAD_SIZE = 200 * 1024 * 1024

# 单次上传保留的 task / 文件数量上限（超出时清理最旧的）
MAX_TASKS_KEPT = 50

# 进度日志保留条数
MAX_LOG_LINES = 200

# 页眉页脚识别区域（相对页面高度比例）
HEADER_ZONE = 0.08
FOOTER_ZONE = 0.90
