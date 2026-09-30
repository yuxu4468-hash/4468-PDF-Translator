"""OCR 子模块：基于 PP-OCR（PaddleOCR / RapidOCR）ONNX 模型的离线文字识别。

    registry.py    模型清单、下载与校验
    detector.py    DBNet 文本检测与后处理
    recognizer.py  CRNN + CTC 文本识别
    engine.py      推理引擎（检测 + 方向分类 + 识别）
    scanner.py     扫描页判定，以及「页面图像 -> 可翻译段落」的桥接

设计约束：
  - **不新增 Python 依赖**：只用本机已有的 onnxruntime / numpy / Pillow / scipy，
    刻意绕开 opencv（本机 cv2 安装损坏）与 pyclipper；
  - 模型按需下载到项目内 `models/ocr/`，带 SHA256 校验，可离线复用。
"""

from .engine import OCRLine, OCRResult, PPOCREngine  # noqa: F401
from .registry import (  # noqa: F401
    DEFAULT_DET,
    DEFAULT_LANG,
    REC_MODELS,
    ensure_models,
    language_choices,
    models_dir,
    status,
)

__all__ = [
    "DEFAULT_LANG",
    "DEFAULT_DET",
    "REC_MODELS",
    "PPOCREngine",
    "OCRLine",
    "OCRResult",
    "ensure_models",
    "language_choices",
    "models_dir",
    "status",
]
