"""REST 接口蓝图集合。"""

from .config_api import bp as config_bp
from .fonts_api import bp as fonts_bp
from .ocr_api import bp as ocr_bp
from .system_api import bp as system_bp
from .translate_api import bp as translate_bp

__all__ = ["config_bp", "fonts_bp", "ocr_bp", "system_bp", "translate_bp"]
