"""翻译服务商抽象接口。

设计要点：
  - 不依赖 openai SDK，只用 requests，保持轻量；
  - 统一 `chat()` 返回 `ChatResponse`，便于统计 token 与耗时；
  - HTTP 错误统一翻译成中文可读信息，界面上能直接看懂。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests

logger = logging.getLogger("Provider")


class ProviderError(RuntimeError):
    """翻译服务商调用失败（信息可直接展示给用户）。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class ChatResponse:
    """一次对话调用的结果。"""

    text: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# HTTP 状态码 -> 中文可读原因
_STATUS_MESSAGES = {
    400: "请求格式被拒绝（可能是提示词过长或参数不合法）",
    401: "API Key 无效或已过期，请检查密钥是否正确",
    403: "没有访问权限（密钥受限、模型未开通或地区受限）",
    404: "接口地址或模型名不存在，请检查 Base URL 与模型名称",
    408: "服务端超时，请稍后重试",
    413: "请求体过大，请调小“每批段落数”",
    422: "请求参数校验失败",
    429: "请求过于频繁或额度已用尽，请降低并发数或稍后重试",
    500: "服务端内部错误，请稍后重试",
    502: "网关错误，请稍后重试",
    503: "服务暂时不可用，请稍后重试",
    504: "网关超时，请稍后重试",
}


def describe_status(status: int, body: str = "") -> str:
    """把 HTTP 状态码 + 响应体描述成中文错误信息。"""
    base = _STATUS_MESSAGES.get(status, f"HTTP {status} 错误")
    snippet = (body or "").strip().replace("\n", " ")
    if snippet:
        # 服务端通常会在 body 里给出更具体的原因
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        return f"{base}｜服务端返回：{snippet}"
    return base


class BaseProvider(ABC):
    """所有翻译服务商的基类。"""

    #: 该类服务商是否需要 API Key
    needs_key = True

    def __init__(self, config: dict):
        self.config = dict(config or {})
        self.base_url = (self.config.get("base_url") or "").rstrip("/")
        self.api_key = self.config.get("api_key") or ""
        self.model = self.config.get("model") or ""
        self.timeout = int(self.config.get("timeout") or 180)
        self.max_retries = max(1, int(self.config.get("max_retries") or 3))
        self.temperature = float(self.config.get("temperature", 0.3))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "4468-PDF-Translator/1.0"})

    # ---------------------------------------------------------------- 抽象
    @property
    @abstractmethod
    def name(self) -> str:
        """服务商显示名。"""

    @abstractmethod
    def _build_request(self, system: str, user: str) -> tuple[str, dict, dict]:
        """返回 (url, headers, payload)。"""

    def _extract_text(self, payload: dict) -> str:
        """从响应 JSON 中取出文本（可被覆盖）。"""
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"服务端返回结构异常，无法解析译文：{exc}") from exc

    def _extract_usage(self, payload: dict) -> tuple[int, int]:
        usage = payload.get("usage") or {}
        return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)

    # ---------------------------------------------------------------- 通用
    def validate(self) -> None:
        """调用前的本地校验。"""
        if self.needs_key and not self.api_key:
            raise ProviderError("未填写 API Key")
        if not self.base_url:
            raise ProviderError("未填写接口地址（Base URL）")
        if not self.model:
            raise ProviderError("未指定模型名称")

    def chat(self, system: str, user: str) -> ChatResponse:
        """发起一次对话调用，内部自动重试。"""
        self.validate()
        url, headers, payload = self._build_request(system, user)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            started = time.time()
            try:
                response = self._session.post(
                    url, headers=headers, json=payload, timeout=self.timeout
                )
            except requests.exceptions.Timeout as exc:
                last_error = ProviderError(
                    f"请求超时（{self.timeout}s），可尝试调大超时时间或减小每批段落数",
                    retryable=True,
                )
                logger.warning("%s 请求超时（第 %d/%d 次）", self.name, attempt, self.max_retries)
            except requests.exceptions.SSLError as exc:
                raise ProviderError(f"TLS/SSL 握手失败：{exc}。若使用自建服务请检查证书。") from exc
            except requests.exceptions.ConnectionError as exc:
                last_error = ProviderError(
                    f"无法连接到 {self.base_url}，请检查网络、代理或本地服务是否已启动（{type(exc).__name__}）",
                    retryable=True,
                )
                logger.warning("%s 连接失败（第 %d/%d 次）", self.name, attempt, self.max_retries)
            except requests.exceptions.RequestException as exc:
                raise ProviderError(f"请求失败：{exc}") from exc
            else:
                latency = int((time.time() - started) * 1000)
                if response.status_code >= 400:
                    message = describe_status(response.status_code, response.text)
                    retryable = response.status_code in (408, 429, 500, 502, 503, 504)
                    last_error = ProviderError(
                        message, retryable=retryable, status=response.status_code
                    )
                    logger.warning(
                        "%s 返回 %d（第 %d/%d 次）：%s",
                        self.name, response.status_code, attempt, self.max_retries, message,
                    )
                else:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            f"服务端返回的不是 JSON（可能是网关/HTML 错误页）：{response.text[:200]}"
                        ) from exc
                    text = self._extract_text(data)
                    if not isinstance(text, str):
                        raise ProviderError("服务端返回的译文不是字符串")
                    prompt_tokens, completion_tokens = self._extract_usage(data)
                    return ChatResponse(
                        text=text,
                        model=data.get("model") or self.model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=latency,
                        raw=data,
                    )

            # 重试前的退避
            if attempt < self.max_retries and isinstance(last_error, ProviderError) and last_error.retryable:
                time.sleep(min(8.0, 1.5 * attempt))
            elif attempt < self.max_retries:
                break  # 不可重试的错误直接跳出

        raise last_error or ProviderError("调用失败（未知原因）")

    def test_connection(self) -> ChatResponse:
        """连通性测试：发一句极短的请求。"""
        return self.chat(
            "你是一个测试助手，只回复用户要求的内容。",
            "请只回复两个字：正常",
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
