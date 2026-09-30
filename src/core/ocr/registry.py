"""OCR 模型清单与下载器。

模型来自 GitHub 项目 **RapidOCR**（RapidAI/RapidOCR）整理并转换的
**PP-OCR**（PaddleOCR）ONNX 模型，托管在 ModelScope 上。

为什么这样做：
  - 本机已经装了 `onnxruntime`，直接用它推理即可，**不需要新增任何 Python 依赖**，
    也不需要 torch / paddle / opencv（本机的 cv2 恰好是坏的）；
  - 模型按需下载到项目内的 `models/ocr/`，扁平可搬运，不污染系统目录；
  - 每个模型都带 SHA256，下载后校验，避免半截文件导致诡异报错。

清单里的 URL / SHA256 取自 RapidOCR 官方 `default_models.yaml`。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("OCRRegistry")

# ---------------------------------------------------------------------------
# 模型清单
# ---------------------------------------------------------------------------

#: 文本检测模型（DBNet）。v5 为默认，v4 更小、更保守。
DET_MODELS = {
    "v5": {
        "file": "ch_PP-OCRv5_det_mobile.onnx",
        "size": 4_745_517,
        "sha256": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
        "url": ("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"
                "/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx"),
    },
    "v4": {
        "file": "ch_PP-OCRv4_det_mobile.onnx",
        "size": 4_745_517,
        "sha256": "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
        "url": ("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"
                "/onnx/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.onnx"),
    },
}

#: 文本方向分类模型（判断 0°/180°，可选）
CLS_MODEL = {
    "file": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "size": 585_532,
    "sha256": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    "url": ("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"
            "/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
}

_MS = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"

#: 识别模型（CRNN + CTC），按语言选择
REC_MODELS = {
    "ch": {
        "label": "中英混排（推荐）",
        "file": "ch_PP-OCRv5_rec_mobile.onnx",
        "size": 16_631_306,
        "sha256": "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/ch_PP-OCRv5_rec_server/ppocrv5_dict.txt",
    },
    "ch_v4": {
        "label": "中英混排（PP-OCRv4，体积更小）",
        "file": "ch_PP-OCRv4_rec_mobile.onnx",
        "size": 10_857_958,
        "sha256": "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
        "url": f"{_MS}/onnx/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile.onnx",
        "dict_file": "ppocr_keys_v1.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile/ppocr_keys_v1.txt",
    },
    "en": {
        "label": "英文",
        "file": "en_PP-OCRv5_rec_mobile.onnx",
        "size": 7_653_044,
        "sha256": "c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/en_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_en_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/en_PP-OCRv5_rec_mobile/ppocrv5_en_dict.txt",
    },
    "japan": {
        "label": "日文",
        "file": "japan_PP-OCRv4_rec_mobile.onnx",
        "size": 17_332_000,
        "sha256": "e1075a67dba758ecfc7ebc78a10ae61c95ac8fb66a9c86fab5541e33f085cb7a",
        "url": f"{_MS}/onnx/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile.onnx",
        "dict_file": "japan_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile/japan_dict.txt",
    },
    "korean": {
        "label": "韩文",
        "file": "korean_PP-OCRv5_rec_mobile.onnx",
        "size": 47_451_000,
        "sha256": "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_korean_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile/ppocrv5_korean_dict.txt",
    },
    "latin": {
        "label": "拉丁语系（法/德/西/葡…）",
        "file": "latin_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_latin_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile/ppocrv5_latin_dict.txt",
    },
    "cyrillic": {
        "label": "西里尔语系（俄/乌…）",
        "file": "cyrillic_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_cyrillic_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile/ppocrv5_cyrillic_dict.txt",
    },
    "arabic": {
        "label": "阿拉伯语",
        "file": "arabic_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "c1192e632d0baa9146ae5b756a0e635e3dc63c1733737ebfd1629e87144e9295",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/arabic_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_arabic_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/arabic_PP-OCRv5_rec_mobile/ppocrv5_arabic_dict.txt",
    },
    "eslav": {
        "label": "东斯拉夫语",
        "file": "eslav_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "08705d6721849b1347d26187f15a5e362c431963a2a62bfff4feac578c489aab",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/eslav_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_eslav_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/eslav_PP-OCRv5_rec_mobile/ppocrv5_eslav_dict.txt",
    },
    "th": {
        "label": "泰语",
        "file": "th_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "de541dd83161c241ff426f7ecfd602a0ba77d686cf3ab9a6c255ea82fd08006e",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/th_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_th_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/th_PP-OCRv5_rec_mobile/ppocrv5_th_dict.txt",
    },
    "el": {
        "label": "希腊语",
        "file": "el_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "b4368bccd557123c702b7549fee6cd1e94b581337d1c9b65310f109131542b7f",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/el_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_el_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/el_PP-OCRv5_rec_mobile/ppocrv5_el_dict.txt",
    },
    "devanagari": {
        "label": "天城文（印地语等）",
        "file": "devanagari_PP-OCRv5_rec_mobile.onnx",
        "size": 8_000_000,
        "sha256": "d6f0a906580e3fa6b324a318718f1f31f268b6ea8ef985f91c2012a37f52c91e",
        "url": f"{_MS}/onnx/PP-OCRv5/rec/devanagari_PP-OCRv5_rec_mobile.onnx",
        "dict_file": "ppocrv5_devanagari_dict.txt",
        "dict_url": f"{_MS}/paddle/PP-OCRv5/rec/devanagari_PP-OCRv5_rec_mobile/ppocrv5_devanagari_dict.txt",
    },
}

DEFAULT_LANG = "ch"
DEFAULT_DET = "v5"


# ---------------------------------------------------------------------------
# 目录与状态
# ---------------------------------------------------------------------------

def models_dir() -> Path:
    """OCR 模型存放目录（项目内，便于整体搬运）。"""
    from ...shared.path_helpers import PROJECT_ROOT

    path = PROJECT_ROOT / "models" / "ocr"
    path.mkdir(parents=True, exist_ok=True)
    return path


def language_choices() -> list[dict]:
    """给界面用的语言列表（含实际下载体积，避免前端瞎估）。"""
    out = []
    for key, cfg in REC_MODELS.items():
        directory = models_dir()
        model_ready = (directory / cfg["file"]).exists()
        dict_ready = (directory / cfg["dict_file"]).exists()
        out.append(
            {
                "id": key,
                "label": cfg["label"],
                "file": cfg["file"],
                "ready": model_ready and dict_ready,
                # 该语言还需要额外下载多少字节（检测模型是共享的，不计入）
                "download_size": 0 if (model_ready and dict_ready)
                                 else sum(
                                     c.get("size", 0)
                                     for name, c in (
                                         (cfg["file"], cfg),
                                         (cfg["dict_file"], {"size": cfg.get("dict_size", 80_000)}),
                                     )
                                     if not (directory / name).exists()
                                 ),
                "size": cfg.get("size", 0),
            }
        )
    return out


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expect_sha: str = "") -> bool:
    """校验文件是否存在且 SHA256 匹配（未提供 sha 时只检查非空）。"""
    if not path.exists() or path.stat().st_size == 0:
        return False
    if not expect_sha:
        return True
    return _sha256_file(path) == expect_sha


def required_files(lang: str = DEFAULT_LANG, det: str = DEFAULT_DET, need_cls: bool = False):
    """返回该语言所需的 (文件名, 配置) 列表。"""
    lang = lang if lang in REC_MODELS else DEFAULT_LANG
    det = det if det in DET_MODELS else DEFAULT_DET
    items = [
        (DET_MODELS[det]["file"], DET_MODELS[det]),
        (REC_MODELS[lang]["file"], REC_MODELS[lang]),
        (REC_MODELS[lang]["dict_file"], {"url": REC_MODELS[lang]["dict_url"]}),
    ]
    if need_cls:
        items.append((CLS_MODEL["file"], CLS_MODEL))
    return items


def status(lang: str = DEFAULT_LANG, det: str = DEFAULT_DET) -> dict:
    """检查模型是否齐备。"""
    directory = models_dir()
    missing, present = [], []
    for name, _cfg in required_files(lang, det):
        (present if (directory / name).exists() else missing).append(name)
    total = sum((directory / n).stat().st_size for n in present) if present else 0
    return {
        "dir": str(directory),
        "lang": lang,
        "det": det,
        "ready": not missing,
        "missing": missing,
        "present": present,
        "size": total,
        "languages": language_choices(),
    }


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def download_file(url: str, target: Path, expect_sha: str = "", log=None, timeout: int = 300) -> Path:
    """下载单个文件到目标路径，带 SHA256 校验与原子替换。"""
    import requests

    log = log or (lambda message: logger.info(message))
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and verify(target, expect_sha):
        log(f"已存在且校验通过：{target.name}")
        return target

    log(f"正在下载 {target.name} …")
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=str(target.parent))
    try:
        with requests.get(url, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            expected_size = int(response.headers.get("content-length") or 0)
            written = 0
            with open(tmp_fd, "wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
        if expected_size and written != expected_size:
            raise RuntimeError(f"下载不完整：收到 {written} 字节，应为 {expected_size} 字节")
        if expect_sha and _sha256_file(Path(tmp_name)) != expect_sha:
            raise RuntimeError("SHA256 校验失败，文件可能已损坏或被篡改")
        shutil.move(tmp_name, target)
        log(f"完成 {target.name}（{written:,} 字节）")
        return target
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_models(
    lang: str = DEFAULT_LANG,
    det: str = DEFAULT_DET,
    need_cls: bool = False,
    log=None,
    timeout: int = 300,
) -> dict:
    """确保所选语言的模型齐备；缺失则下载。

    Returns:
        status() 的结果。
    """
    log = log or (lambda message: logger.info(message))
    directory = models_dir()
    for name, cfg in required_files(lang, det, need_cls):
        target = directory / name
        if verify(target, cfg.get("sha256", "")):
            continue
        download_file(cfg["url"], target, cfg.get("sha256", ""), log=log, timeout=timeout)
    return status(lang, det)
