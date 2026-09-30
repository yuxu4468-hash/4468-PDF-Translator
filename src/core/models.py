"""文档中间表示（IR）。

解析阶段把 PDF 转换成这里的结构；翻译阶段只操作 `Paragraph`；
排版阶段再依据这些坐标把译文写回 PDF。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 段落类型
KIND_TEXT = "text"          # 正文
KIND_TITLE = "title"        # 标题
KIND_HEADER = "header"      # 页眉
KIND_FOOTER = "footer"      # 页脚
KIND_PAGE_NUM = "page_num"  # 页码
KIND_CODE = "code"          # 代码/公式等不翻译内容
KIND_EMPTY = "empty"        # 空白块

# 对齐方式
ALIGN_LEFT = "left"
ALIGN_CENTER = "center"
ALIGN_RIGHT = "right"
ALIGN_JUSTIFY = "justify"


@dataclass
class Span:
    """一段同字体的连续文本。"""

    text: str
    bbox: tuple[float, float, float, float]
    font: str = ""
    size: float = 10.0
    flags: int = 0
    color: int = 0
    origin: tuple[float, float] = (0.0, 0.0)

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def is_bold(self) -> bool:
        # PyMuPDF 的 flags 第 4 位（值 16）表示粗体
        return bool(self.flags & 16) or "bold" in (self.font or "").lower()


@dataclass
class Line:
    """一行文本（可能由多个 Span 组成）。"""

    bbox: tuple[float, float, float, float]
    spans: list[Span] = field(default_factory=list)
    wmode: int = 0
    direction: tuple[float, float] = (1.0, 0.0)

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return max(0.0, self.bbox[3] - self.bbox[1])

    @property
    def size(self) -> float:
        """该行的代表字号（按字符数加权）。"""
        if not self.spans:
            return 10.0
        total = sum(max(1, len(span.text)) for span in self.spans)
        return sum(span.size * max(1, len(span.text)) for span in self.spans) / total


@dataclass
class Paragraph:
    """可翻译的基本单位，通常对应 PDF 里的一个文本块。"""

    id: int
    page: int                     # 0 基页码
    bbox: tuple[float, float, float, float]
    lines: list[Line] = field(default_factory=list)
    text: str = ""
    target: str = ""              # 译文（翻译阶段填充）
    kind: str = KIND_TEXT
    column: int = 0
    align: str = ALIGN_LEFT
    rotation: float = 0.0
    size: float = 10.0            # 主导字号
    color: int = 0
    font: str = ""
    bold: bool = False
    translatable: bool = True
    skip_reason: str = ""
    cached: bool = False          # 译文是否命中缓存
    error: str = ""
    #: 该段落来自 OCR（扫描件），排版时需要先遮盖掉原图里的文字
    from_ocr: bool = False
    #: OCR 平均置信度（仅 from_ocr 的段落有值）
    ocr_confidence: float = 1.0
    #: 该段落是否落在表格里。表格的表头行会每页重复、也常在页面上方，
    #: 形态上与"页眉"无法区分，只能靠这个标记避免被当成页眉跳过。
    in_table: bool = False
    #: 该段落所属的**约束框**（x0, y0, x1, y1）：译文必须待在这个框里，
    #: 不许越出去压到相邻的格子/行。识别不到时为 None（沿用"按相邻段落
    #: 算可用空间"的老机制）。来源见 `constraint_kind`。
    constraint_bbox: tuple[float, float, float, float] | None = None
    #: 约束框的来源：`"cell"`（PyMuPDF 表格识别的单元格，更精确）
    #: / `"row"`（由表格横线切出的行带，三线表靠它）/ `""`（没有）
    constraint_kind: str = ""

    # --- 便捷属性 ---
    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return max(0.0, self.bbox[3] - self.bbox[1])

    @property
    def is_done(self) -> bool:
        return bool(self.target) and not self.error

    def to_dict(self, include_target: bool = True) -> dict:
        data = {
            "id": self.id,
            "page": self.page + 1,
            "kind": self.kind,
            "source": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "align": self.align,
            "font_size": round(self.size, 2),
            "translatable": self.translatable,
        }
        if self.from_ocr:
            data["from_ocr"] = True
            data["ocr_confidence"] = round(self.ocr_confidence, 3)
        if include_target:
            data["target"] = self.target
            data["error"] = self.error
        return data


@dataclass
class PageInfo:
    """单页的版面统计信息。"""

    page: int                     # 0 基页码
    width: float = 595.0
    height: float = 842.0
    rotation: int = 0
    columns: int = 1
    paragraphs: int = 0
    #: 该页识别出的表格区域（文字坐标空间），用于区分表头与页眉
    tables: list = field(default_factory=list)
    #: 该页识别出的表格单元格矩形（文字坐标空间），供排版阶段约束译文的边界
    cells: list = field(default_factory=list)
    #: 该页由表格**横线**切出的行带矩形（文字坐标空间）。三线表识别不出单元格，
    #: 只能靠行带约束"别往下压到下一行"。
    rows: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page": self.page + 1,
            "width": round(self.width, 1),
            "height": round(self.height, 1),
            "rotation": self.rotation,
            "columns": self.columns,
            "paragraphs": self.paragraphs,
        }


@dataclass
class ParsedDocument:
    """一份已解析的 PDF。"""

    file_id: str
    path: str
    filename: str
    doc: object = None            # fitz.Document，由解析器持有
    pages: list[PageInfo] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    encrypted: bool = False
    warnings: list[str] = field(default_factory=list)
    #: 最近一次翻译产出的文件 {"mono": path, "dual": path, ...}
    outputs: dict = field(default_factory=dict)
    #: 走 OCR 的页码（0 基）
    ocr_pages: list = field(default_factory=list)
    #: 每页 OCR 的明细信息
    ocr_info: list = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text_page_count(self) -> int:
        return sum(1 for page in self.pages if page.paragraphs > 0)

    def paragraphs_on(self, page_index: int) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.page == page_index]

    def translatable_paragraphs(self) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.translatable]

    def close(self) -> None:
        if self.doc is not None:
            try:
                self.doc.close()
            except Exception:
                pass
            self.doc = None
