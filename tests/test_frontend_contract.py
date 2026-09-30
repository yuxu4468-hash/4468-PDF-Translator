"""前端契约检查：确认 JS 引用的 DOM 元素与接口都在 HTML / 后端里真实存在。

这类"拼写不一致"是最常见也最难察觉的集成故障（JS 报 null 错、按钮没反应），
这里用静态分析把它们提前抓出来。

顺带守住两条**仓库卫生**约定（发布到 GitHub 前加的）：

  - `config/config.example.json` 必须与 `constants.DEFAULT_CONFIG` 完全一致 ——
    否则用户照示例改配置，就会改出与程序默认值不同的行为；
  - `.gitignore` 必须挡住 `config/config.json`（里面有 API Key）与运行产物目录。

用法：
    python tests/test_frontend_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "src" / "app"
TEMPLATE = APP_DIR / "templates" / "index.html"
JS_DIR = APP_DIR / "static" / "js"

FAILURES: list[str] = []


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    print("=" * 72)
    if not TEMPLATE.exists():
        print(f"找不到模板：{TEMPLATE}")
        return 1
    html = TEMPLATE.read_text(encoding="utf-8")
    scripts = sorted(JS_DIR.glob("*.js"))
    if not scripts:
        print(f"{JS_DIR} 下没有 JS 文件")
        return 1

    html_ids = set(re.findall(r'\bid\s*=\s*"([^"]+)"', html))
    print(f"  HTML 中定义了 {len(html_ids)} 个 id，JS 文件 {len(scripts)} 个")

    # ---- 1) JS 里 getElementById / querySelector('#x') 的 id 必须存在 ----
    referenced: dict[str, set[str]] = {}
    for script in scripts:
        source = script.read_text(encoding="utf-8")
        found = set(re.findall(r'getElementById\(\s*[\'"]([^\'"]+)[\'"]', source))
        found |= set(re.findall(r'querySelector(?:All)?\(\s*[\'"]#([A-Za-z0-9_\-]+)', source))
        # 项目里用 $('id') 作为 getElementById 的简写
        found |= set(re.findall(r'(?<![\w$.])\$\(\s*[\'"]([A-Za-z0-9_\-]+)[\'"]\s*\)', source))
        # 去掉 $(...) 中的选择器写法（以 . 或 # 开头的不是 id）
        found = {name for name in found if name and not name[0] in ".#["}
        for name in found:
            referenced.setdefault(name, set()).add(script.name)

    missing = {name: files for name, files in referenced.items() if name not in html_ids}
    check(not missing,
          f"JS 引用的 {len(referenced)} 个 DOM id 全部存在于 HTML"
          + (f"（缺失：{missing}）" if missing else ""))

    # ---- 2) HTML 里引用的静态资源必须存在 ----
    assets = re.findall(r'(?:src|href)\s*=\s*"(/static/[^"]+)"', html)
    check(bool(assets), f"HTML 引用了 {len(assets)} 个静态资源")
    for asset in assets:
        path = APP_DIR / asset.lstrip("/").replace("static/", "static/", 1)
        path = APP_DIR / asset[len("/"):]
        if not path.exists():
            check(False, f"静态资源存在：{asset}")
    if all((APP_DIR / a.lstrip("/")).exists() for a in assets):
        check(True, "所有静态资源文件都存在")

    # ---- 3) JS 调用的接口必须由后端注册 ----
    from src.app import create_app

    app = create_app(testing=True)
    server_routes: set[str] = set()
    for rule in app.url_map.iter_rules():
        path = str(rule)
        # 把 <converter:name> 统一替换成占位符，便于比对
        normalized = re.sub(r"<[^>]+>", "{}", path)
        server_routes.add(normalized)

    client_calls: set[str] = set()
    preprocess = {"encodeURIComponent", "fileId", "taskId", "name", "page", "zoom", "kind"}
    for script in scripts:
        source = script.read_text(encoding="utf-8")
        for raw in re.findall(r'[\'"`](/api/[^\'"`]*)', source):
            # 去掉查询串，替换 JS 拼接出来的变量段
            path = raw.split("?")[0]
            path = re.sub(r"/[\"']\s*\+.*$", "/{}", path)
            path = re.sub(r"\+.*$", "{}", path)
            path = re.sub(r"\{[^}]*\}", "{}", path)
            client_calls.add(path)

    unknown = set()
    for path in client_calls:
        normalized = re.sub(r"\{\}", "{}", path)
        if normalized in server_routes:
            continue
        # 前缀匹配：处理 JS 里拼接了多段的路径
        if any(route.startswith(normalized.rstrip("/")) and normalized.count("{}") <= route.count("{}")
               for route in server_routes):
            continue
        unknown.add(path)

    check(not unknown,
          f"JS 调用的 {len(client_calls)} 个接口在后端都已注册"
          + (f"（未知：{sorted(unknown)}）" if unknown else ""))

    # ---- 4) 离线可用性：不能有外链资源 ----
    external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html)
    check(not external, f"HTML 无外部资源引用（页面可完全离线加载）"
                        + (f"（发现：{external}）" if external else ""))

    combined_js = "\n".join(s.read_text(encoding="utf-8") for s in scripts)
    cdn = re.findall(r'(?:cdn\.|unpkg\.com|jsdelivr|googleapis\.com|@import\s+url\(https?)', combined_js)
    check(not cdn, "JS/CSS 无 CDN 依赖")

    # ---- 5) 仓库卫生：示例配置与内置默认值必须一致，且不含密钥 ----
    # 示例配置是给新用户看的"全部可调项"，内置默认值才是真正的 owner。
    # 两者一旦漂移，用户照示例改就会改出与默认值不一致的行为，所以这里守住。
    import json

    from src.shared import constants

    example_path = ROOT / "config" / "config.example.json"
    if example_path.exists():
        example = json.loads(example_path.read_text(encoding="utf-8"))
        defaults = constants.DEFAULT_CONFIG
        only_example = sorted(set(example) - set(defaults))
        only_default = sorted(set(defaults) - set(example))
        check(not only_example and not only_default,
              "config.example.json 与 DEFAULT_CONFIG 键集合一致"
              + (f"（示例多出 {only_example}，少了 {only_default}）"
                 if (only_example or only_default) else ""))
        mismatch = sorted(key for key in set(example) & set(defaults)
                          if example[key] != defaults[key])
        check(not mismatch,
              "config.example.json 的取值与 DEFAULT_CONFIG 一致"
              + (f"（不一致：{mismatch}）" if mismatch else ""))
        check(not str(example.get("api_key", "")).strip(),
              "示例配置里不含 API Key")
    else:
        check(False, "存在 config/config.example.json")

    # ---- 6) git 卫生：密钥文件必须被忽略 ----
    ignored = (ROOT / ".gitignore")
    if ignored.exists():
        rules = ignored.read_text(encoding="utf-8")
        check("config/config.json" in rules, "config.json 已在 .gitignore 中")
        check("work/" in rules and "logs/" in rules, "运行产物目录已在 .gitignore 中")
    else:
        check(False, "存在 .gitignore")

    # ---- 7) 语法检查（如果本机有 node） ----
    import shutil
    import subprocess

    if shutil.which("node"):
        for script in scripts:
            result = subprocess.run(
                ["node", "--check", str(script)],
                capture_output=True, text=True,
            )
            check(result.returncode == 0,
                  f"node --check {script.name}"
                  + ("" if result.returncode == 0 else f"：{result.stderr.strip()[:120]}"))
    else:
        print("  [SKIP] 未找到 node，跳过 JS 语法检查")

    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("前端契约检查全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
