"""程序入口：启动本地 Web 服务并自动打开浏览器。

用法：
    python -m src.main                # 默认监听 127.0.0.1:8765
    python -m src.main --port 9000    # 指定端口
    python -m src.main --no-browser   # 不自动打开浏览器
    python -m src.main --debug        # 开发模式（热重载 + 详细日志）
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 允许直接以脚本方式运行：python src/main.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared import constants  # noqa: E402
from src.shared.logger import setup_logging  # noqa: E402
from src.shared.path_helpers import PROJECT_ROOT, ensure_dirs  # noqa: E402

logger = logging.getLogger("Main")

BANNER = r"""
  ____  ____  _____   _____                    _       _
 |  _ \|  _ \|  ___| |_   _| __ __ _ _ __  ___| | __ _| |_ ___ _ __
 | |_) | | | | |_    | || '__/ _` | '_ \/ __| |/ _` | __/ _ \ '__|
 |  __/| |_| |  _|   | || | | (_| | | | \__ \ | (_| | ||  __/ |
 |_|   |____/|_|     |_||_|  \__,_|_| |_|___/_|\__,_|\__\___|_|
"""


def check_dependencies() -> list[str]:
    """检查运行依赖，返回缺失项说明列表。"""
    problems: list[str] = []

    try:
        import fitz  # noqa: F401
    except ImportError:
        problems.append("缺少 PyMuPDF（PDF 解析核心库）。安装命令：pip install pymupdf")

    try:
        import flask  # noqa: F401
    except ImportError:
        problems.append("缺少 Flask（Web 服务框架）。安装命令：pip install flask")

    try:
        import requests  # noqa: F401
    except ImportError:
        problems.append("缺少 requests（HTTP 客户端）。安装命令：pip install requests")

    return problems


def check_ocr_ready() -> str:
    """OCR 是可选的：没有 onnxruntime 时只是不能翻译扫描版 PDF。

    Pillow / scipy 与 onnxruntime 一起构成 OCR 的最小依赖集
    （Pillow 负责页面图读写与缩放，scipy.ndimage 负责检测框的连通域聚类，
    用来替代 opencv + pyclipper）—— 缺任何一个，OCR 都会在跑起来之后才炸，
    所以这里一起检查。
    """
    missing = []
    for module, name in (("onnxruntime", "onnxruntime"), ("numpy", "numpy"),
                         ("PIL", "Pillow"), ("scipy", "scipy")):
        try:
            __import__(module)
        except ImportError:
            missing.append(name)
    if missing:
        return ("未安装 %s，扫描版 PDF 的 OCR 功能不可用"
                "（pip install onnxruntime numpy Pillow scipy）" % "、".join(missing))
    return ""


def check_cjk_font() -> str:
    """确认内置中日韩字体可用（译文渲染依赖它）。"""
    try:
        import fitz

        return fitz.Font("china-s").name
    except Exception:
        return ""


def port_available(host: str, port: int) -> bool:
    """检查端口是否空闲。

    注意：这里**不能**设置 SO_REUSEADDR。在 Windows 上该选项允许重复绑定
    已经被占用的端口，会让检查误报"空闲"，于是两个进程都以为自己是 8765，
    请求随机落到旧进程上（实测踩过：旧服务进程没退干净，新服务照常启动，
    结果访问到的是旧代码）。所以这里先"连一下"确认没有人监听。
    """
    # 先确认没有人在监听
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        try:
            probe.connect((host, port))
            return False  # 连得上，说明已被占用
        except OSError:
            pass

    # 再确认自己绑得上（不设 SO_REUSEADDR，避免掩盖冲突）
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(host: str, start: int, attempts: int = 20) -> int | None:
    """从 start 开始找一个可用端口。"""
    for offset in range(attempts):
        candidate = start + offset
        if port_available(host, candidate):
            return candidate
    return None


def open_browser_later(url: str, delay: float = 1.2) -> None:
    """延迟打开浏览器（等服务器真正起来）。"""

    def worker():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            logger.debug("自动打开浏览器失败", exc_info=True)

    threading.Thread(target=worker, name="open-browser", daemon=True).start()


def print_banner(url: str, font_name: str, network: bool) -> None:
    """打印启动信息。"""
    line = "=" * 66
    print(line)
    print(f"  {constants.APP_NAME}  v{constants.APP_VERSION}")
    print(f"  {constants.APP_SLOGAN}")
    print(line)
    print(f"  访问地址   : {url}")
    print(f"  工作目录   : {PROJECT_ROOT}")
    print(f"  中文字体   : {font_name or '不可用（译文将无法正确显示）'}")
    print(f"  网络状态   : {'可访问外网（可使用云端大模型）' if network else '未检测到外网（可使用本地 Ollama 或模拟翻译）'}")
    print(line)
    print("  按 Ctrl+C 停止服务")
    print(line)
    print()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pdf-translater",
        description=f"{constants.APP_NAME} —— {constants.APP_SLOGAN}",
    )
    parser.add_argument("--host", default=constants.HOST, help=f"监听地址（默认 {constants.HOST}）")
    parser.add_argument("--port", type=int, default=constants.PORT, help=f"监听端口（默认 {constants.PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="开发模式（热重载）")
    parser.add_argument("--quiet", action="store_true", help="不输出控制台日志")
    parser.add_argument(
        "--download-ocr",
        nargs="?",
        const="ch",
        default=None,
        metavar="语言",
        help="下载 OCR 模型后退出（语言：ch/en/japan/korean/latin/cyrillic…，默认 ch；"
             "传 all 下载全部）",
    )
    parser.add_argument(
        "--ocr-status",
        action="store_true",
        help="查看 OCR 模型状态后退出",
    )
    return parser.parse_args(argv)


def print_ocr_status() -> int:
    """打印 OCR 模型状态。"""
    from src.core import ocr as ocr_pkg

    info = ocr_pkg.status()
    print("=" * 66)
    print("  OCR 模型状态")
    print("=" * 66)
    print(f"  目录    : {info['dir']}")
    print(f"  就绪    : {'是' if info['ready'] else '否'}")
    if info["missing"]:
        print(f"  缺失    : {', '.join(info['missing'])}")
    total = sum(
        (ocr_pkg.models_dir() / name).stat().st_size
        for name in info["present"]
    ) if info["present"] else 0
    print(f"  已占用  : {total / 1024 / 1024:.1f} MB")
    print()
    print("  可选语言：")
    for item in info["languages"]:
        mark = "✓" if item["ready"] else " "
        print(f"    [{mark}] {item['id']:<12} {item['label']}")
    print()
    print("  下载模型： python -m src.main --download-ocr ch")
    print("=" * 66)
    return 0


def download_ocr_models(lang: str) -> int:
    """下载 OCR 模型。"""
    from src.core import ocr as ocr_pkg

    langs = list(ocr_pkg.REC_MODELS) if lang == "all" else [lang]
    unknown = [item for item in langs if item not in ocr_pkg.REC_MODELS]
    if unknown:
        print(f"未知的 OCR 语言：{', '.join(unknown)}")
        print(f"可选：{', '.join(ocr_pkg.REC_MODELS)}")
        return 1

    print("=" * 66)
    print(f"  准备下载 OCR 模型（{', '.join(langs)}）")
    print(f"  存放目录：{ocr_pkg.models_dir()}")
    print("=" * 66)
    try:
        for index, item in enumerate(langs):
            # 检测模型只需要一份
            ocr_pkg.ensure_models(item, need_cls=(index == 0), log=lambda m: print(f"  {m}"))
    except Exception as exc:
        print(f"\n下载失败：{exc}")
        print("可以稍后重试，或手动把模型文件放进上面的目录。")
        return 1
    print()
    return print_ocr_status()


def main(argv=None) -> int:
    args = parse_args(argv)

    ensure_dirs()
    setup_logging(quiet=args.quiet)

    print(BANNER)

    problems = check_dependencies()
    if problems:
        print("启动失败，缺少必需的依赖：\n")
        for item in problems:
            print(f"  · {item}")
        print("\n提示：也可以在项目目录下执行  pip install -r requirements.txt")
        return 1

    # 这两个是纯命令行操作，不需要起服务
    if args.ocr_status:
        return print_ocr_status()
    if args.download_ocr:
        return download_ocr_models(args.download_ocr)

    font_name = check_cjk_font()
    if not font_name:
        print("警告：内置中文字体不可用，译文的 PDF 渲染可能异常。\n")

    ocr_note = check_ocr_ready()
    if ocr_note:
        print(f"提示：{ocr_note}\n")

    # 端口占用时自动顺延
    port = args.port
    if not port_available(args.host, port):
        fallback = find_free_port(args.host, port + 1)
        if fallback is None:
            print(f"启动失败：端口 {port} 已被占用，且未找到可用端口。")
            print(f"可以用 --port 指定其它端口，例如：python -m src.main --port 9000")
            return 1
        print(f"提示：端口 {port} 已被占用，自动改用 {fallback}\n")
        port = fallback

    url = f"http://{args.host}:{port}/"

    # 网络探测放在这里做一次，界面里也会再查一次
    try:
        from src.app.api.system_api import check_network

        network = check_network(timeout=2.0)
    except Exception:
        network = False

    print_banner(url, font_name, network)

    if not args.no_browser:
        open_browser_later(url)

    from src.app import create_app

    app = create_app()
    try:
        app.run(
            host=args.host,
            port=port,
            debug=args.debug,
            threaded=True,
            use_reloader=args.debug,
        )
    except KeyboardInterrupt:
        print("\n已停止。")
    except OSError as exc:
        print(f"启动失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
