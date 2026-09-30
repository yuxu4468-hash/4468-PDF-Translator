"""已上传文档的登记与访问。

上传时解析一次并把 `ParsedDocument` 缓存在内存里；后续"开始翻译"直接复用，
避免重复解析。文档按 `file_id` 索引，可被多个任务引用。
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid

import fitz  # PyMuPDF

from . import pdf_parser
from .models import ParsedDocument
from ..shared import constants
from ..shared.path_helpers import UPLOAD_DIR, safe_name

logger = logging.getLogger("DocumentStore")


class DocumentStore:
    """内存中的文档登记表 + 磁盘上的上传文件管理。"""

    def __init__(self, max_items: int = 30):
        self._documents: dict[str, ParsedDocument] = {}
        self._lock = threading.RLock()
        self.max_items = max_items

    # ------------------------------------------------------------------
    def new_file_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def save_upload(self, stream, filename: str) -> tuple[str, str]:
        """把上传流写到 work/uploads，返回 (file_id, 保存路径)。

        Args:
            stream: 任意类文件对象（werkzeug 的 `FileStorage.stream` 也可以）。
        """
        file_id = self.new_file_id()
        original = safe_name(filename, fallback="document.pdf")
        if not original.lower().endswith(constants.ALLOWED_UPLOAD_EXT):
            raise ValueError("目前只支持 PDF 文件（.pdf）")
        target = UPLOAD_DIR / f"{file_id}_{original}"
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        stream.seek(0)
        with open(target, "wb") as handle:
            shutil.copyfileobj(stream, handle, length=1024 * 256)
        return file_id, str(target)

    # ------------------------------------------------------------------
    def register(self, file_id: str, path: str, filename: str) -> ParsedDocument:
        """解析并登记一份文档。"""
        document = pdf_parser.parse_pdf(path, file_id, filename)
        with self._lock:
            self._documents[file_id] = document
            self._evict_locked()
        return document

    def get(self, file_id: str) -> ParsedDocument | None:
        with self._lock:
            return self._documents.get(file_id)

    def remove(self, file_id: str) -> None:
        with self._lock:
            self._documents.pop(file_id, None)

    def all(self) -> dict[str, ParsedDocument]:
        with self._lock:
            return dict(self._documents)

    def _evict_locked(self) -> None:
        if len(self._documents) <= self.max_items:
            return
        # 字典保持插入顺序，移除最早的
        for key in list(self._documents)[: len(self._documents) - self.max_items]:
            self._documents.pop(key, None)

    # ------------------------------------------------------------------
    @staticmethod
    def open_document(path: str) -> "fitz.Document":
        """按需重新打开原始 PDF（预览、排版都用它，保证线程安全）。"""
        doc = fitz.open(path)
        if doc.needs_pass:
            doc.close()
            raise RuntimeError("该 PDF 需要密码，无法打开")
        return doc


# 全局单例
document_store = DocumentStore()
