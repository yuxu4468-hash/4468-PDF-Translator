"""PP-OCR 推理引擎：把检测、方向分类、识别串起来。

只依赖 `onnxruntime` + `numpy` + `Pillow`（都已在环境中），不需要 torch / paddle / opencv。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import detector as det_mod
from . import registry
from .recognizer import Charset, Recognizer

logger = logging.getLogger("OCREngine")

# 方向分类输入尺寸
CLS_WIDTH, CLS_HEIGHT = 192, 48


@dataclass
class OCRLine:
    """一行识别结果。"""

    text: str
    box: det_mod.TextBox
    confidence: float = 0.0
    angle: int = 0

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "box": [round(self.box.x0, 1), round(self.box.y0, 1),
                    round(self.box.x1, 1), round(self.box.y1, 1)],
            "angle": self.angle,
        }


@dataclass
class OCRResult:
    """一整页的识别结果。"""

    lines: list[OCRLine] = field(default_factory=list)
    elapsed: float = 0.0
    width: int = 0
    height: int = 0
    used_lang: str = ""

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def to_dict(self) -> dict:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "elapsed": round(self.elapsed, 2),
            "width": self.width,
            "height": self.height,
            "lang": self.used_lang,
            "count": len(self.lines),
        }


class PPOCREngine:
    """PP-OCR（ONNX）推理引擎，线程安全、按需加载。"""

    def __init__(
        self,
        lang: str = registry.DEFAULT_LANG,
        det_version: str = registry.DEFAULT_DET,
        use_cls: bool = False,
        providers: list[str] | None = None,
    ):
        self.lang = lang if lang in registry.REC_MODELS else registry.DEFAULT_LANG
        self.det_version = det_version if det_version in registry.DET_MODELS else registry.DEFAULT_DET
        self.use_cls = use_cls
        self.providers = providers or ["CPUExecutionProvider"]
        self._lock = threading.RLock()
        self._det_session = None
        self._rec: Recognizer | None = None
        self._cls_session = None
        self._loaded_lang: str | None = None
        self.load_seconds = 0.0

    # ------------------------------------------------------------------
    def _load_session(self, path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        # 本地单机推理：限制线程数，避免和 Flask 线程抢 CPU
        options.intra_op_num_threads = 0
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(str(path), sess_options=options, providers=self.providers)

    def load(self, lang: str | None = None, log=None) -> None:
        """加载（或切换语言后重新加载）模型。"""
        target_lang = lang or self.lang
        with self._lock:
            if self._rec is not None and self._loaded_lang == target_lang:
                return
            log = log or (lambda message: logger.info(message))
            directory = registry.models_dir()
            started = time.time()

            if self._det_session is None:
                det_file = directory / registry.DET_MODELS[self.det_version]["file"]
                if not det_file.exists():
                    raise FileNotFoundError(
                        f"缺少检测模型 {det_file.name}，请先下载 OCR 模型"
                    )
                log(f"加载 OCR 检测模型 {det_file.name}")
                self._det_session = self._load_session(det_file)

            rec_cfg = registry.REC_MODELS.get(target_lang) or registry.REC_MODELS[registry.DEFAULT_LANG]
            rec_file = directory / rec_cfg["file"]
            dict_file = directory / rec_cfg["dict_file"]
            if not rec_file.exists() or not dict_file.exists():
                raise FileNotFoundError(
                    f"缺少 {target_lang} 的识别模型，请先下载 OCR 模型"
                )
            log(f"加载 OCR 识别模型 {rec_file.name}（{rec_cfg['label']}）")
            charset = Charset(dict_file)
            self._rec = Recognizer(self._load_session(rec_file), charset)
            self._loaded_lang = target_lang
            self.lang = target_lang

            if self.use_cls and self._cls_session is None:
                cls_file = directory / registry.CLS_MODEL["file"]
                if cls_file.exists():
                    log("加载 OCR 方向分类模型")
                    self._cls_session = self._load_session(cls_file)

            self.load_seconds = time.time() - started
            log(f"OCR 模型就绪（{self.load_seconds:.1f}s）")

    @property
    def ready(self) -> bool:
        return self._rec is not None

    # ------------------------------------------------------------------
    def _classify_angle(self, crop: np.ndarray) -> int:
        """判断文本行是否需要旋转 180°。"""
        if self._cls_session is None:
            return 0
        from PIL import Image

        resized = Image.fromarray(crop).resize((CLS_WIDTH, CLS_HEIGHT), Image.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        array = (array - 0.5) / 0.5
        tensor = np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])
        name = self._cls_session.get_inputs()[0].name
        output = self._cls_session.run(None, {name: tensor})[0][0]
        label = int(np.argmax(output))
        score = float(output[label])
        # 标签 1 表示 180°
        if label == 1 and score > 0.9:
            return 180
        return 0

    # ------------------------------------------------------------------
    def recognize_page(
        self,
        image: np.ndarray,
        min_confidence: float = 0.0,
        merge: bool = True,
        progress=None,
    ) -> OCRResult:
        """对整页图像做检测 + 识别。

        Args:
            image: RGB uint8 数组（H, W, 3）。
            min_confidence: 低于该置信度的行会被丢弃（0 表示不过滤）。
        """
        import time as _time

        started = _time.time()
        self.load()
        if image.ndim == 2:
            image = np.stack([image] * 3, axis=-1)
        if image.shape[2] == 4:
            image = image[:, :, :3]

        height, width = image.shape[:2]
        result = OCRResult(width=width, height=height, used_lang=self.lang)

        # ---- 1) 检测 ----
        resized, scale_x, scale_y = det_mod.resize_for_detection(image)
        tensor = det_mod.normalize(resized)
        det_input = self._det_session.get_inputs()[0].name
        probability = self._det_session.run(None, {det_input: tensor})[0][0, 0]
        boxes = det_mod.find_boxes(probability, scale_x, scale_y, width, height)
        if not boxes:
            result.elapsed = _time.time() - started
            return result
        if merge:
            # 传入原图，让合并逻辑能判断"两框之间到底有没有字"
            boxes = det_mod.merge_boxes(boxes, image=image)
        boxes = det_mod.sort_boxes(boxes)

        # ---- 2) 方向分类（可选）----
        crops = []
        for box in boxes:
            crop = box.crop(image)
            if crop.size == 0 or crop.shape[0] < 2 or crop.shape[1] < 2:
                crops.append(None)
                continue
            crops.append(crop)

        angles = [0] * len(crops)
        if self._cls_session is not None:
            for index, crop in enumerate(crops):
                if crop is None:
                    continue
                angle = self._classify_angle(crop)
                angles[index] = angle
                if angle == 180:
                    crops[index] = np.ascontiguousarray(crop[::-1, ::-1])

        # ---- 3) 识别 ----
        valid = [crop for crop in crops if crop is not None]
        valid_index = [i for i, crop in enumerate(crops) if crop is not None]
        predictions = self._rec.recognize(valid) if valid else []
        for position, index in enumerate(valid_index):
            text, confidence = predictions[position]
            if not text.strip():
                continue
            if min_confidence and confidence < min_confidence:
                continue
            result.lines.append(
                OCRLine(text=text, box=boxes[index], confidence=confidence, angle=angles[index])
            )

        if progress is not None:
            try:
                progress(len(result.lines), len(boxes))
            except Exception:
                logger.debug("OCR 进度回调异常", exc_info=True)

        result.elapsed = _time.time() - started
        return result
