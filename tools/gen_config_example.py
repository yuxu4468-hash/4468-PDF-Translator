"""按内置默认值重新生成 `config/config.example.json`。

为什么要有这个脚本：示例配置是给用户看的"全部可调项"，它的**唯一来源**
必须是 `src/shared/constants.py` 的 `DEFAULT_CONFIG`。手工维护第二份拷贝
迟早会漂移（第一次跑 `tests/test_frontend_contract.py` 就抓到了：
示例里的 `ocr_dpi` 还是旧的 200，默认值早就按实测改成 300）。

用法（改了 DEFAULT_CONFIG 之后跑一次）：
    python tools/gen_config_example.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.shared import constants  # noqa: E402


def main() -> int:
    config = dict(constants.DEFAULT_CONFIG)
    # 示例配置永远不带密钥
    if "api_key" in config:
        config["api_key"] = ""

    target = ROOT / "config" / "config.example.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 显式写 LF：Windows 上 `write_text` 默认把 "\n" 翻成 CRLF，
    # 而仓库约定（.gitattributes）存 LF —— 不显式指定的话，每次跑完这个脚本
    # `git status` 都会显示"已修改"，但 diff 是空的，很误导人。
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(f"已写入 {target}（{len(config)} 个键）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
