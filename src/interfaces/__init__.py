"""翻译服务商工厂。

`create_provider(config)` 依据配置里的 `provider` 字段返回对应实现。
新增服务商只需在此登记，并在 constants.PROVIDERS 里补一条预设。
"""

from __future__ import annotations

from .base_provider import BaseProvider, ChatResponse, ProviderError, describe_status
from .mock_provider import MockProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAICompatibleProvider

__all__ = [
    "BaseProvider",
    "ChatResponse",
    "ProviderError",
    "describe_status",
    "MockProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "create_provider",
]

# provider id -> 实现类
_REGISTRY = {
    "mock": MockProvider,
    "ollama": OllamaProvider,
    # 其余均为 OpenAI 兼容协议
    "deepseek": OpenAICompatibleProvider,
    "siliconflow": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
    "moonshot": OpenAICompatibleProvider,
    "zhipu": OpenAICompatibleProvider,
    "dashscope": OpenAICompatibleProvider,
    "custom": OpenAICompatibleProvider,
}


def create_provider(config: dict) -> BaseProvider:
    """根据配置创建服务商实例。

    Args:
        config: 至少包含 provider / api_key / base_url / model 等字段。

    Raises:
        ProviderError: provider 未知，或缺少必填项。
    """
    from ..shared.constants import PROVIDER_MAP

    provider_id = (config.get("provider") or "deepseek").strip()
    impl = _REGISTRY.get(provider_id)
    if impl is None:
        known = "、".join(sorted(_REGISTRY))
        raise ProviderError(f"未知的翻译服务商“{provider_id}”，可选：{known}")

    preset = PROVIDER_MAP.get(provider_id, {})
    merged = dict(config)
    # 用户没填 base_url 时回落到预设
    if not merged.get("base_url") and preset.get("base_url"):
        merged["base_url"] = preset["base_url"]
    if not merged.get("model") and preset.get("default_model"):
        merged["model"] = preset["default_model"]
    merged["_label"] = preset.get("label") or provider_id

    instance = impl(merged)
    return instance


def provider_choices() -> list[dict]:
    """返回给前端的服务商列表（去掉内部字段）。"""
    from ..shared.constants import PROVIDERS

    return [dict(item) for item in PROVIDERS]
