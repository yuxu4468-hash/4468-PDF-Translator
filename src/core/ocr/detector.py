"""文本检测：DBNet（PP-OCR det 模型）+ 后处理。

后处理不依赖 opencv / pyclipper，改用 `scipy.ndimage` 做连通域分析：

    概率图 --阈值--> 二值图 --连通域--> 每个域的最小外接框 --外扩(unclip)--> 文本框

与 RapidOCR/PP-OCR 的差异：官方用 `cv2.findContours + minAreaRect + pyclipper`
得到带角度的四边形；这里用**轴对齐矩形 + 外扩**，对正常扫描件（基本水平）
效果一致，而且少两个重依赖。略微倾斜的行仍能被正确识别，因为识别网络本身
对小幅倾斜不敏感。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

logger = logging.getLogger("OCRDetector")

# 检测输入的最长边（PP-OCR 默认 960）
DET_LIMIT_SIDE = 960
# 概率图二值化阈值
DET_BIN_THRESHOLD = 0.3
# 文本框平均得分下限
DET_BOX_THRESHOLD = 0.6
# 外扩比例（对应官方 unclip_ratio 的思路）
UNCLIP_X = 0.30
UNCLIP_Y = 0.35

# ImageNet 归一化参数（PP-OCR det 使用）
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass
class TextBox:
    """一个检测到的文本框（原图坐标）。"""

    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 0.0

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def crop(self, image: np.ndarray) -> np.ndarray:
        h, w = image.shape[:2]
        x0 = max(0, int(math.floor(self.x0)))
        y0 = max(0, int(math.floor(self.y0)))
        x1 = min(w, int(math.ceil(self.x1)))
        y1 = min(h, int(math.ceil(self.y1)))
        return image[y0:y1, x0:x1]


def resize_for_detection(image: np.ndarray) -> tuple[np.ndarray, float, float]:
    """按 PP-OCR 的方式缩放：最长边不超过 960，且宽高都是 32 的倍数。

    Returns:
        (缩放后的 float32 数组 RGB, x 缩放比, y 缩放比)
    """
    height, width = image.shape[:2]
    ratio = min(DET_LIMIT_SIDE / max(height, width), 1.0)
    target_h = max(32, int(round(height * ratio / 32) * 32))
    target_w = max(32, int(round(width * ratio / 32) * 32))
    resized = Image.fromarray(image).resize((target_w, target_h), Image.BILINEAR)
    array = np.asarray(resized, dtype=np.float32) / 255.0
    return array, width / target_w, height / target_h


def normalize(array: np.ndarray) -> np.ndarray:
    """归一化并转成 NCHW。"""
    normalized = (array - _MEAN) / _STD
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None, ...])


def binarize(probability: np.ndarray, threshold: float = DET_BIN_THRESHOLD) -> np.ndarray:
    return (probability > threshold).astype(np.uint8)


def find_boxes(
    probability: np.ndarray,
    scale_x: float,
    scale_y: float,
    image_width: int,
    image_height: int,
    box_threshold: float = DET_BOX_THRESHOLD,
    min_size: int = 3,
) -> list[TextBox]:
    """从概率图提取文本框（坐标已换回原图尺度）。"""
    from scipy import ndimage

    binary = binarize(probability)
    if not binary.any():
        return []

    labels, count = ndimage.label(binary)
    if count == 0:
        return []

    # 用 find_objects 一次性拿到所有连通域的外接框，比逐个 np.where 快得多
    slices = ndimage.find_objects(labels)
    boxes: list[TextBox] = []

    for index, region in enumerate(slices, start=1):
        if region is None:
            continue
        ys, xs = region
        y0, y1 = ys.start, ys.stop - 1
        x0, x1 = xs.start, xs.stop - 1
        box_w = x1 - x0 + 1
        box_h = y1 - y0 + 1
        if box_w < min_size or box_h < min_size:
            continue

        # 得分：该连通域内概率均值（近似官方的 box_score_fast）
        mask = labels[ys, xs] == index
        if not mask.any():
            continue
        score = float(probability[ys, xs][mask].mean())
        if score < box_threshold:
            continue

        # 外扩：DBNet 预测的是"收缩后"的文本区域，需要放回去
        pad_x = UNCLIP_X * box_h + 4.0
        pad_y = UNCLIP_Y * box_h + 3.0
        boxes.append(
            TextBox(
                x0=max(0.0, (x0 - pad_x)) * scale_x,
                y0=max(0.0, (y0 - pad_y)) * scale_y,
                x1=min(image_width, (x1 + pad_x) * scale_x),
                y1=min(image_height, (y1 + pad_y) * scale_y),
                score=score,
            )
        )

    return boxes


def _ink_contrast(gray: np.ndarray) -> float:
    """区域内的"墨迹对比度"（灰度标准差）。

    空白背景的标准差接近 0；有文字的区域因为有笔画与底色的反差，标准差明显更大。
    """
    if gray.size == 0:
        return 0.0
    return float(gray.std())


def _gap_is_text(image, existing: "TextBox", box: "TextBox", max_gap_ratio: float) -> bool:
    """判断两个框之间的空隙里到底有没有字。

    检测模型在文字压在照片/底纹上时会漏掉中间一截，只剩首尾两个框。
    这时如果直接看"间隙有多大"会误判成两段独立文字；真正可靠的判据是
    **间隙里有没有墨迹**：把间隙区域的灰度标准差和框内的对比一下，
    接近就说明那里确实还有字（只是没检出来），应当合并。
    """
    if image is None:
        return False
    x0 = int(min(existing.x1, box.x1))
    x1 = int(max(existing.x0, box.x0))
    if x1 - x0 < 4:
        return True
    y0 = int(max(existing.y0, box.y0))
    y1 = int(min(existing.y1, box.y1))
    if y1 - y0 < 4:
        return False
    height, width = image.shape[:2]
    gap = image[max(0, y0):min(height, y1), max(0, x0):min(width, x1)]
    if gap.size == 0:
        return False
    if gap.ndim == 3:
        gap_gray = gap.mean(axis=2)
    else:
        gap_gray = gap.astype(np.float32)

    box_gray = image[max(0, y0):min(height, y1), max(0, int(existing.x0)):min(width, int(existing.x1))]
    if box_gray.ndim == 3:
        box_gray = box_gray.mean(axis=2)
    reference = _ink_contrast(box_gray.astype(np.float32))
    if reference < 6.0:
        # 框内本身就没什么对比度，无法作为参考，退回保守判断
        return False
    return _ink_contrast(gap_gray.astype(np.float32)) >= 0.45 * reference


def merge_boxes(boxes: list[TextBox], image=None, max_gap_ratio: float = 1.6,
                min_overlap: float = 0.75, ink_gap_ratio: float = 3.5) -> list[TextBox]:
    """合并属于同一文本行的碎片。

    检测模型在**文字压在照片/底纹上**时经常把一整行切成几段。判断"是不是同一行"
    最可靠的信号是**纵向重叠**：两段的垂直区间几乎重合，就在同一基线附近。

    横向间隙分两种情况：
      - 间隙较小（≤ `max_gap_ratio` 倍行高）→ 直接合并；
      - 间隙较大但**间隙里有墨迹**（说明中间那截只是没被检测到）→ 也合并，
        上限放宽到 `ink_gap_ratio` 倍行高。
    """
    if len(boxes) < 2:
        return boxes

    ordered = sorted(boxes, key=lambda b: (round(b.center_y / 12), b.x0))
    merged: list[TextBox] = []
    for box in ordered:
        attached = False
        for existing in merged:
            overlap = min(existing.y1, box.y1) - max(existing.y0, box.y0)
            shorter = max(1.0, min(existing.height, box.height))
            if overlap / shorter < min_overlap:
                continue

            gap = max(box.x0, existing.x0) - min(existing.x1, box.x1)
            if gap > 0:
                line_height = max(existing.height, box.height)
                if gap <= max_gap_ratio * line_height:
                    pass  # 挨得近，直接合并
                elif gap <= ink_gap_ratio * line_height and _gap_is_text(image, existing, box, max_gap_ratio):
                    pass  # 间隙里有墨迹，说明中间还有没检测到的字
                else:
                    continue

            existing.x0 = min(existing.x0, box.x0)
            existing.y0 = min(existing.y0, box.y0)
            existing.x1 = max(existing.x1, box.x1)
            existing.y1 = max(existing.y1, box.y1)
            existing.score = max(existing.score, box.score)
            attached = True
            break
        if not attached:
            merged.append(TextBox(box.x0, box.y0, box.x1, box.y1, box.score))

    return merged


def sort_boxes(boxes: list[TextBox]) -> list[TextBox]:
    """按阅读顺序（上到下、左到右）排序。"""
    if not boxes:
        return []
    heights = sorted(b.height for b in boxes)
    typical = heights[len(heights) // 2] or 10.0
    return sorted(boxes, key=lambda b: (round(b.center_y / max(6.0, typical * 0.7)), b.x0))
