"""本地 Ollama 服务商（完全离线）。

使用 Ollama 原生 `/api/chat` 接口，不需要 API Key。
"""

from __future__ import annotations

from .base_provider import BaseProvider, ProviderError


class OllamaProvider(BaseProvider):
    """本地 Ollama 推理。"""

    needs_key = False
    DEFAULT_HOST = "http://localhost:11434"

    @property
    def name(self) -> str:
        return "本地 Ollama"

    def _endpoint(self) -> str:
        base = (self.base_url or self.DEFAULT_HOST).rstrip("/")
        if base.endswith("/api/chat"):
            return base
        if base.endswith("/v1"):
            # 用户误填了 OpenAI 兼容地址，退回原生接口
            base = base[: -len("/v1")]
        return base + "/api/chat"

    def _build_request(self, system: str, user: str) -> tuple[str, dict, dict]:
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                # 文档翻译不需要天马行空，限制输出长度避免跑飞
                "num_predict": 4096,
            },
        }
        return self._endpoint(), headers, payload

    def validate(self) -> None:
        if not self.model:
            raise ProviderError("未指定 Ollama 模型名称（例如 qwen2.5:7b）")

    def _extract_text(self, payload: dict) -> str:
        message = payload.get("message") or {}
        content = message.get("content")
        if content is None:
            error = payload.get("error")
            raise ProviderError(f"Ollama 返回异常：{error or str(payload)[:200]}")
        return content

    def _extract_usage(self, payload: dict) -> tuple[int, int]:
        return (
            int(payload.get("prompt_eval_count", 0) or 0),
            int(payload.get("eval_count", 0) or 0),
        )
