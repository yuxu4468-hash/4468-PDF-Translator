"""OpenAI 兼容接口服务商。

涵盖 DeepSeek / SiliconFlow / OpenAI / Kimi / 智谱 / DashScope / 各类中转站、
vLLM、LM Studio 等所有提供 `/v1/chat/completions` 的服务。
"""

from __future__ import annotations

from .base_provider import BaseProvider, ProviderError


class OpenAICompatibleProvider(BaseProvider):
    """通过 OpenAI Chat Completions 协议调用。"""

    needs_key = True

    @property
    def name(self) -> str:
        return self.config.get("_label") or "OpenAI 兼容接口"

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _build_request(self, system: str, user: str) -> tuple[str, dict, dict]:
        if not self.base_url:
            raise ProviderError("未填写接口地址（Base URL）")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        return self._endpoint(), headers, payload

    def _extract_text(self, payload: dict) -> str:
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"服务端返回结构异常：{str(payload)[:200]}") from exc
        message = choice.get("message") or {}
        content = message.get("content")
        if content is None:
            # 部分推理模型把内容放在 reasoning_content，或返回空
            content = message.get("reasoning_content") or ""
        if isinstance(content, list):
            # 少数服务返回分段内容数组
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return content
