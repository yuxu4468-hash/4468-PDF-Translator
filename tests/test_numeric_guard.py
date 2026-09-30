"""数值保护回归测试。

锁住三件事：

  1. **正确译文不被误杀** —— 这是本功能最大的风险。误报的代价是把一段正确译文
     退回原文，比漏检更糟。
  2. **真实漏译必须抓到** —— 数字被模型丢弃或改写时不能静默进入成品。
  3. **回退链完整** —— 失配段回退原文、坏译文不留在缓存里。

用法：
    python tests/test_numeric_guard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.translator import (  # noqa: E402
    Item,
    TranslateStats,
    TranslationCache,
    Translator,
)
from src.shared.constants import DEFAULT_CONFIG  # noqa: E402
from src.shared.text_utils import (  # noqa: E402
    chinese_numerals,
    extract_numbers,
    missing_numbers,
)

FAILURES: list[str] = []


def check(condition, label):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1) 归一化：写法差异不算失配
# ---------------------------------------------------------------------------

def test_normalization():
    print("\n[1] 归一化：写法差异不算失配")
    cases = [
        ("1,5 m", "1.5 米", "德语小数逗号 1,5 ↔ 1.5"),
        ("5 x 1 m", "5×1 米", "乘号写法 5 x 1 ↔ 5×1"),
        ("22-28 °C", "22–28 ℃", "区间连字符 - ↔ –"),
        ("1.000 m", "1000 米", "千分位读法 1.000 → 1000"),
        ("1.000 m", "1.0 米", "小数读法 1.000 → 1.0（数值相同）"),
        ("  50  cm  ", "50厘米", "空白差异"),
    ]
    for source, target, label in cases:
        got = missing_numbers(source, target)
        check(got == [], f"{label}（得到 {got}）")


# ---------------------------------------------------------------------------
# 2) 单向包含：译文多出的数字不算失配
# ---------------------------------------------------------------------------

def test_asymmetric():
    print("\n[2] 单向包含：译文多出的数字不算失配")
    got = missing_numbers("Essen, 1. Juni 2018", "埃森，2018年6月1日")
    check(got == [], f"月份名词转数字（Juni → 6）不得误杀（得到 {got}）")

    got = missing_numbers("Temperaturen 22 bis 28 Grad", "温度 22 至 28 摄氏度")
    check(got == [], f"正常译文无失配（得到 {got}）")

    got = missing_numbers("Kategorie 2 stark gefaehrdet", "第 2 类“严重受威胁”")
    check(got == [], f"条款类目译文无失配（得到 {got}）")


# ---------------------------------------------------------------------------
# 3) 真实漏译必须抓到
# ---------------------------------------------------------------------------

def test_real_miss():
    print("\n[3] 真实漏译必须抓到")
    miss = missing_numbers("Essen, 1. Juni 2018", "埃森，2018 年")
    check(bool(miss), f"丢失的日被检出（{miss}）")
    check("1" in miss, f"缺的正是 1（得到 {miss}）")

    miss = missing_numbers("Gesamtlaenge 50-60 cm", "全长 50 厘米")
    check("60" in miss, f"区间上端丢失被检出（{miss}）")

    miss = missing_numbers("Kategorie 2 stark gefaehrdet", "严重受威胁")
    check("2" in miss, f"类别号丢失被检出（{miss}）")


# ---------------------------------------------------------------------------
# 4) 中文数字参与比对
# ---------------------------------------------------------------------------

def test_chinese_numerals():
    print("\n[4] 中文数字参与比对")
    pairs = [
        ("5 Stueck", "第五条", "第五条 ↔ 5"),
        ("23 Arten", "二十三种", "二十三种 ↔ 23"),
        ("2020", "二〇二〇年", "二〇二〇年 ↔ 2020"),
        ("15 Tiere", "十五只", "十五 ↔ 15"),
    ]
    for source, target, label in pairs:
        got = missing_numbers(source, target)
        check(got == [], f"{label}（得到 {got}）")

    # 反向：词组内的数词不得被当成数字，否则会凭空造出原文没有的数
    for source, target, label in [
        ("1 Teil", "一般", "「一般」不得被当成 1"),
        ("10 Grad", "十分", "「十分」不得被当成 10"),
    ]:
        got = missing_numbers(source, target)
        check(bool(got), f"{label}（得到 {got}）")

    values = chinese_numerals("第五条 二十三种 一般 十分 二〇二〇年")
    check(5 in values and 23 in values and 2020 in values,
          f"独立成词的数词被正确解析（{values}）")
    check(1 not in values and 10 not in values,
          f"词组内部的数词未被误解析（{values}）")


# ---------------------------------------------------------------------------
# 5) 数值集合抽取
# ---------------------------------------------------------------------------

def test_fingerprint():
    print("\n[5] 数值集合抽取")
    got = extract_numbers("要 50-60 cm，湿度 60 %，温度 22 °C")
    check({"50", "60", "22"} <= got, f"阿拉伯数字被抽出（{sorted(got)}）")
    got = extract_numbers("二十三种")
    check("23" in got, f"中文数词被抽出（{sorted(got)}）")


# ---------------------------------------------------------------------------
# 6) 统计字段与开关
# ---------------------------------------------------------------------------

def test_stats_and_config():
    print("\n[6] 统计字段与开关")
    data = TranslateStats().to_dict()
    check("numeric_rejected" in data and "numeric_retries" in data,
          "TranslateStats 暴露数值校验统计")
    check(data["numeric_rejected"] == 0 and data["numeric_retries"] == 0,
          "统计初值为 0")
    check(DEFAULT_CONFIG.get("numeric_guard") is True, "numeric_guard 默认开启")
    check(DEFAULT_CONFIG.get("numeric_log_limit") == 5, "明细上限默认 5 条")


# ---------------------------------------------------------------------------
# 7) 集成：校验 / 定向重试 / 回退 / 缓存清理
# ---------------------------------------------------------------------------

class _FakeParagraph:
    def __init__(self):
        self.id = 0
        self.target = ""
        self.error = ""


def _make_translator(replies: list[str], cache=None) -> Translator:
    """构造一个 Translator，用固定回复队列替换真实大模型调用。"""
    translator = Translator(
        {"provider": "mock", "numeric_guard": True, "numeric_log_limit": 5},
        cache=cache,
    )
    queue = list(replies)
    seen: list[str] = []

    def fake_call(system_prompt: str, user_message: str):
        seen.append(system_prompt)
        if not queue:
            return None
        return type("R", (), {"text": queue.pop(0)})()

    translator._call = fake_call  # type: ignore[method-assign]
    translator._seen_prompts = seen  # type: ignore[attr-defined]
    return translator


def test_integration():
    print("\n[7] 集成：失配 → 定向重试 → 回退原文")

    # 场景 A：首次失配，重试后修好 → 采用重试结果，不回退
    paragraph = _FakeParagraph()
    item = Item(key=1, paragraph=paragraph, text="Laenge 50-60 cm")
    item.result = "全长 50 厘米"          # 丢了 60
    translator = _make_translator(['[{"i": 1, "t": "全长 50-60 厘米"}]'])
    translator._guard_numbers([item], "SYS")
    check(item.result == "全长 50-60 厘米", f"重试修好后采用（{item.result!r}）")
    check(translator.stats.numeric_rejected == 0, "未计入回退")
    check(translator.stats.numeric_retries == 1, "计入 1 次定向重试")
    check("数字保护" in translator._seen_prompts[0], "重试提示词带数字约束")

    # 场景 B：重试仍失配 → 回退原文
    paragraph = _FakeParagraph()
    item = Item(key=2, paragraph=paragraph, text="Laenge 50-60 cm")
    item.result = "全长 50 厘米"
    translator = _make_translator(['[{"i": 2, "t": "全长 50 厘米"}]'])
    translator._guard_numbers([item], "SYS")
    check(item.result == "", f"回退原文（result={item.result!r}）")
    check(bool(item.error), f"记录了原因（{item.error!r}）")
    check(translator.stats.numeric_rejected == 1, "计入 1 段回退")

    # 场景 C：坏译文不留在缓存里
    paragraph = _FakeParagraph()
    item = Item(key=3, paragraph=paragraph, text="Laenge 50-60 cm")
    item.result = "全长 50 厘米"
    with tempfile.TemporaryDirectory() as tmp:
        cache = TranslationCache(Path(tmp) / "c.json", enabled=True)
        cache.put(translator._cache_key(item.text), "全长 50 厘米")
        translator = _make_translator(['[{"i": 3, "t": "全长 50 厘米"}]'], cache=cache)
        translator._guard_numbers([item], "SYS")
        check(cache.get(translator._cache_key(item.text)) is None,
              "坏译文已从缓存中清除")

    # 场景 D：译文本来就正确 → 完全不动，且不发请求
    paragraph = _FakeParagraph()
    item = Item(key=4, paragraph=paragraph, text="Laenge 50-60 cm")
    item.result = "全长 50–60 厘米"
    translator = _make_translator([])
    translator._guard_numbers([item], "SYS")
    check(item.result == "全长 50–60 厘米", "正确译文保持不变")
    check(translator.stats.numeric_retries == 0, "未发起多余请求")
    check(not translator._seen_prompts, "一次请求都没发")


# ---------------------------------------------------------------------------
# 8) 被空格劈开的小数：严格失败后允许一次宽松判定
# ---------------------------------------------------------------------------

def test_split_decimal():
    print("\n[8] 被空格劈开的小数（实测 BMEL 语料里的误判）")
    source = "0,75 x 0, 5 x 0,75 über 1,5 m:"
    target = "0.75 × 0.5 × 0.75，超过 1.5 米："
    got = missing_numbers(source, target)
    check(got == [], f"劈开的小数不再误判失配（得到 {got}）")

    # 宽松判定只在严格失败后生效，不能掩盖真正的漏译
    got = missing_numbers("0,75 x 0, 5 x 0,75", "0.75 × 0.75")
    check(bool(got), f"真的丢了 0.5 仍然抓到（得到 {got}）")

    # 列表形态（逗号 + 空格）不能因为宽松判定而反过来误判
    got = missing_numbers("Arten 1, 2, 3", "物种 1、2、3")
    check(got == [], f"逗号分隔的列表正常通过（得到 {got}）")

    check(extract_numbers("1,5 m") == {"1.5"}, "严格抽取不受宽松规则影响")


def main() -> int:
    print("=" * 72)
    print("数值保护回归测试")
    print("=" * 72)
    test_normalization()
    test_asymmetric()
    test_real_miss()
    test_chinese_numerals()
    test_fingerprint()
    test_stats_and_config()
    test_integration()
    test_split_decimal()
    print()
    print("=" * 72)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("数值保护测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
