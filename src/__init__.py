"""PDF-translater —— 基于 AI 大模型的 PDF 文档翻译器。

分层结构：
    src/shared/      通用能力（配置、路径、日志、文本工具）
    src/core/        文档处理（解析、版面分析、翻译、排版、导出、流水线）
    src/core/ocr/    扫描件 OCR（PP-OCR ONNX 推理）
    src/interfaces/  翻译服务商适配（OpenAI 兼容 / Ollama / 模拟）
    src/app/         Flask Web 应用（页面 + REST 接口）
    src/plugins/     预留的扩展点
"""

__version__ = "1.1.0"
