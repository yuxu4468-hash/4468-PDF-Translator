"""core 层：与界面无关的文档处理能力。

    models.py      中间表示（IR）
    layout.py      版面分析（阅读顺序、对齐、段落合并、页眉页脚）
    pdf_parser.py  PDF -> IR
    translator.py  调用大模型翻译 IR
    fonts.py       嵌字字体匹配（探测本机中文字体、按原文风格排序选择）
    typesetter.py  把译文写回 PDF（嵌字：基线锚定、行距网格、字距拉伸、中文禁则）
    pipeline.py    端到端流程编排与进度回调 + 产出回读校验
"""

from . import models  # noqa: F401
from . import layout  # noqa: F401
from . import pdf_parser  # noqa: F401

__all__ = ["models", "layout", "pdf_parser"]
