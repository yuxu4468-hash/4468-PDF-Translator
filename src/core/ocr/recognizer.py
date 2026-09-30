"""文本识别：CRNN + CTC（PP-OCR rec 模型）。

**一个很容易踩的坑**：PaddleOCR 的字符表不是"字典文件里的字符"那么简单。
训练时 `use_space_char=True`，字符表是：

    ['blank'] + 字典文件每一行 + [' ']

也就是**空格被追加在字典末尾**（字典文件本身通常不含空格）。
模型输出维度正好等于这个长度（PP-OCRv4/v5 中文为 6625 = 1 + 6623 + 1）。
如果按"字典行数 + 1"去索引，最后一个类别（空格）就会越界被丢掉，
表现为**识别结果里所有空格凭空消失**（`The quick brown fox` → `Thequickbrownfox`）。
"""

from __future__ import annotations

import logging
import math

import numpy as np
from PIL import Image

logger = logging.getLogger("OCRRecognizer")

# 识别输入高度（PP-OCR 固定 48）
REC_HEIGHT = 48
# 单条文本最大时间步，防止超长行爆显存/内存
REC_MAX_WIDTH = 1600


class Charset:
    """字符表：索引 0 是 CTC blank，末尾追加一个空格。"""

    def __init__(self, dict_path, extra_space: bool = True):
        from pathlib import Path

        path = Path(dict_path)
        if not path.exists():
            raise FileNotFoundError(f"字符表不存在：{path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.characters = ["blank"] + lines + ([" "] if extra_space else [])
        self.dict_path = str(path)

    def __len__(self) -> int:
        return len(self.characters)

    def decode(self, indices, scores=None) -> tuple[str, float]:
        """CTC 贪心解码：去掉 blank（0）与连续重复，再查表。"""
        out: list[str] = []
        confidences: list[float] = []
        previous = 0
        for step, index in enumerate(indices):
            index = int(index)
            if index != 0 and index != previous and 0 <= index < len(self.characters):
                out.append(self.characters[index])
                if scores is not None:
                    confidences.append(float(scores[step]))
            previous = index
        text = "".join(out)
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return text, confidence

    def match_length(self, output_dim: int) -> bool:
        """检查字符表长度是否与模型输出维度一致。"""
        return output_dim == len(self.characters)


def resize_for_recognition(crop: np.ndarray, height: int = REC_HEIGHT) -> np.ndarray:
    """把文本行裁剪图缩放到固定高度，保持长宽比。"""
    h, w = crop.shape[:2]
    if h <= 0 or w <= 0:
        return np.zeros((height, 8, 3), dtype=np.uint8)
    target_w = max(8, int(round(height * w / h)))
    target_w = min(target_w, REC_MAX_WIDTH)
    resized = Image.fromarray(crop).resize((target_w, height), Image.BILINEAR)
    return np.asarray(resized, dtype=np.uint8)


def normalize(crop: np.ndarray) -> np.ndarray:
    """PP-OCR rec 归一化：(x/255 - 0.5) / 0.5，输出 NCHW。"""
    array = crop.astype(np.float32) / 255.0
    array = (array - 0.5) / 0.5
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])


class Recognizer:
    """单个识别模型的封装。"""

    def __init__(self, session, charset: Charset, input_name: str = ""):
        self.session = session
        self.charset = charset
        self.input_name = input_name or session.get_inputs()[0].name
        output_shape = session.get_outputs()[0].shape
        self.output_dim = output_shape[-1] if isinstance(output_shape[-1], int) else 0
        if self.output_dim and not charset.match_length(self.output_dim):
            logger.warning(
                "字符表长度(%d)与模型输出维度(%d)不一致，识别结果可能有误",
                len(charset), self.output_dim,
            )

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        """批量识别若干文本行裁剪图。"""
        if not crops:
            return []
        results: list[tuple[str, float]] = []

        # 同一批里宽度差异过大时会浪费算力，这里按宽度分桶，桶内补齐到同宽
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1])
        bucket: list[int] = []
        bucket_width = 0

        def flush() -> None:
            nonlocal bucket, bucket_width
            if not bucket:
                return
            batch = np.zeros((len(bucket), 3, REC_HEIGHT, bucket_width), dtype=np.float32)
            for row, item in enumerate(bucket):
                image = resize_for_recognition(crops[item])
                tensor = normalize(image)[0]
                batch[row, :, :, : tensor.shape[2]] = tensor
            outputs = self.session.run(None, {self.input_name: batch})[0]
            for row, item in enumerate(bucket):
                prediction = outputs[row]
                indices = prediction.argmax(axis=1)
                scores = prediction.max(axis=1)
                results.append((item, self.charset.decode(indices, scores)))
            bucket, bucket_width = [], 0

        for item in order:
            width = resize_for_recognition(crops[item]).shape[1]
            if bucket and (len(bucket) >= 8 or abs(width - bucket_width) > 0.25 * bucket_width):
                flush()
            bucket.append(item)
            bucket_width = max(bucket_width, width)
        flush()

        # 还原成输入顺序
        ordered = [("", 0.0)] * len(crops)
        for item, value in results:
            ordered[item] = value
        return ordered
