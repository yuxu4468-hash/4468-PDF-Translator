"""模拟服务商：不联网、不调用大模型，用于验证解析与排版链路。

译文规则（纯本地、确定性）：
  - 英文/其它拉丁文本：加前缀并做简单词形处理，长度接近原文，便于检验排版；
  - 已经是中文的文本：原样返回。

它让用户在没有 API Key 或没有网络时也能跑通完整流程、检查 PDF 版式效果。
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from .base_provider import BaseProvider


# 少量常见词，让模拟译文看起来更像中文，便于观察排版
_WORDS = {
    "the": "该", "a": "一个", "an": "一个", "and": "并且", "or": "或",
    "of": "的", "in": "在", "on": "在", "for": "用于", "with": "与",
    "is": "是", "are": "是", "was": "曾是", "were": "曾是", "be": "为",
    "this": "本", "that": "该", "these": "这些", "those": "那些",
    "chapter": "第章", "section": "小节", "figure": "图", "table": "表",
    "page": "页", "test": "测试", "translation": "翻译", "document": "文档",
    "example": "示例", "result": "结果", "method": "方法", "data": "数据",
    "text": "文本", "language": "语言", "model": "模型", "system": "系统",
}


class MockProvider(BaseProvider):
    """离线模拟翻译。"""

    needs_key = False

    @property
    def name(self) -> str:
        return "模拟翻译"

    def _build_request(self, system: str, user: str) -> tuple[str, dict, dict]:
        return "mock://local", {}, {"system": system, "user": user}

    def test_connection(self):
        from .base_provider import ChatResponse

        return ChatResponse(text="正常", model="mock", latency_ms=1)

    def chat(self, system: str, user: str):
        """直接本地生成译文，不发网络请求。"""
        from .base_provider import ChatResponse

        started = time.time()
        items = self._parse_items(user)
        if items:
            results = [{"i": item["i"], "t": self._fake_translate(item["t"])} for item in items]
        else:
            # 非批量协议（例如误把整段文本丢进来）时，退化处理
            results = [{"i": 1, "t": self._fake_translate(user)}]
        text = json.dumps(results, ensure_ascii=False)
        return ChatResponse(
            text=text,
            model="mock",
            prompt_tokens=len(user) // 3,
            completion_tokens=len(text) // 3,
            latency_ms=max(1, int((time.time() - started) * 1000)),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_items(user: str) -> list[dict]:
        try:
            data = json.loads(user)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict) and "t" in item]
        except (ValueError, TypeError):
            pass
        return []

    @staticmethod
    def _fake_translate(text: str) -> str:
        """生成“像中文”的确定性伪译文，长度与原文大致相当。"""
        if not text:
            return ""
        # 已经是中文则原样返回
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        if cjk and cjk >= sum(1 for ch in text if ch.isascii() and ch.isalpha()):
            return text

        # 用哈希决定填充字符，保证同样输入得到同样输出
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
        filler = "译文内容示例占位数据测试文档翻译结果方法系统模型语言文本"
        tokens = re.findall(r"[A-Za-z']+|\d+|[^\sA-Za-z\d]", text)

        out = []
        for index, token in enumerate(tokens):
            if token.isdigit():
                out.append(token)
                continue
            if not re.match(r"[A-Za-z']+$", token):
                # 标点原样保留，中文标点做一次替换
                out.append({"(": "（", ")": "）", ",": "，", ".": "。", ";": "；", ":": "："}.get(token, token))
                continue
            word = token.lower()
            if word in _WORDS:
                out.append(_WORDS[word])
            else:
                length = max(1, min(4, len(word) // 2 + 1))
                start = (seed + index * 7) % (len(filler) - length)
                out.append(filler[start:start + length])
        result = "".join(out)
        return result or text
