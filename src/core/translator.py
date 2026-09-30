"""翻译引擎：把中间表示里的段落交给大模型翻译。

核心设计：

1. **批量协议**：一个请求里送多段文本（JSON 数组 `[{"i":序号,"t":"原文"}]`），
   模型按同一序号返回译文。相比“一段一次请求”，可把请求数降低一个数量级。
2. **多层容错解析**：模型偶尔会加代码块、少返回几条、或返回非 JSON。
   这里用 JSON → 正则键值 → 编号行 → 顺序对齐 四级回退，最后再对缺失项
   单独重试、最终单条重试，尽最大努力不丢内容。
3. **并发**：批次之间可并行（ThreadPoolExecutor），并发数可配。
4. **缓存**：以 (服务商/模型/目标语言/提示词/术语表/原文) 为键缓存译文，
   重复翻译同一文档不再产生费用。
5. **长段落切分**：超长段落按句子切成子块分别翻译再拼回，避免超出上下文。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .models import Paragraph
from ..interfaces import ProviderError, create_provider
from ..shared import constants
from ..shared.text_utils import (
    clean_text,
    estimate_tokens,
    format_glossary_block,
    missing_numbers,
    normalize_for_key,
    parse_glossary,
    stable_hash,
)

logger = logging.getLogger("Translator")

# 单条文本超过该长度时按句子切分
MAX_ITEM_CHARS = 1600
# 单个批次的总字符上限（防止请求体过大被服务端拒绝）
MAX_BATCH_CHARS = 6000
# 送进系统提示词的上下文长度上限
CONTEXT_HINT_CHARS = 600


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """一个待翻译的最小单元（段落的一段）。"""

    key: int
    paragraph: Paragraph
    text: str
    chunk: int = 0
    chunk_total: int = 1
    result: str = ""
    error: str = ""
    #: 指向本文档中第一个内容相同的条目；非空表示"重复项"，直接复用其译文
    leader: "Item | None" = None


@dataclass
class TranslateStats:
    """翻译过程的统计信息。"""

    paragraphs: int = 0
    items: int = 0
    batches: int = 0
    requests: int = 0
    retries: int = 0
    failed_items: int = 0
    failed_paragraphs: int = 0
    cache_hits: int = 0
    deduplicated: int = 0
    #: 数值校验未通过、已回退原文的条目数
    numeric_rejected: int = 0
    #: 因数值失配而发起的定向重试次数
    numeric_retries: int = 0
    #: 初检就没通过数字校验的条目数（含后续被重试修好的）
    #: 这才是"失配率"的分子 —— 只看 numeric_rejected 会把 0 当成没问题
    numeric_mismatched: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "paragraphs": self.paragraphs,
            "items": self.items,
            "batches": self.batches,
            "requests": self.requests,
            "retries": self.retries,
            "failed_items": self.failed_items,
            "failed_paragraphs": self.failed_paragraphs,
            "cache_hits": self.cache_hits,
            "deduplicated": self.deduplicated,
            "numeric_rejected": self.numeric_rejected,
            "numeric_retries": self.numeric_retries,
            "numeric_mismatched": self.numeric_mismatched,
            "tokens": self.prompt_tokens + self.completion_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed": round(self.elapsed, 1),
        }


# ---------------------------------------------------------------------------
# 译文缓存
# ---------------------------------------------------------------------------

class TranslationCache:
    """基于 JSON 文件的译文缓存（线程安全）。"""

    def __init__(self, path, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._dirty = False
        if enabled:
            self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self._data = {str(k): str(v) for k, v in loaded.items()}
                logger.info("译文缓存已载入：%d 条", len(self._data))
        except Exception:
            logger.warning("译文缓存读取失败，将忽略缓存", exc_info=True)
            self._data = {}

    @staticmethod
    def make_key(provider: str, model: str, target: str, prompt: str, glossary: dict, text: str) -> str:
        return stable_hash(provider, model, target, prompt, glossary, text)[:32]

    def get(self, key: str) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        if not self.enabled or not value:
            return
        with self._lock:
            self._data[key] = value
            self._dirty = True

    def drop(self, key: str) -> None:
        """删除一条缓存。

        用于"这条译文已知有问题、不应被复用"的场景（例如数值校验未通过）。
        不删的话，下次重跑会命中这条坏译文，然后又被拒绝一次 —— 永远好不了。
        """
        if not self.enabled:
            return
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._dirty = True

    def flush(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty:
                return
            data = dict(self._data)
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            tmp.replace(self.path)
        except Exception:
            logger.warning("译文缓存写入失败", exc_info=True)


# ---------------------------------------------------------------------------
# 提示词构造
# ---------------------------------------------------------------------------

def target_language_name(target_lang: str) -> str:
    for item in constants.LANGUAGES:
        if item["id"] == target_lang:
            return item["prompt_name"]
    return target_lang or "简体中文"


def build_system_prompt(config: dict, context_hint: str = "") -> str:
    """组装系统提示词：角色 + 用户要求 + 术语表 + 上下文 + 输出协议。"""
    target = target_language_name(config.get("target_lang", "zh"))
    source = config.get("source_lang", "auto")
    user_prompt = (config.get("prompt") or constants.DEFAULT_PROMPT).strip()

    parts = [
        f"你是一名资深文档翻译专家，正在把一份 PDF 文档翻译成【{target}】。",
    ]
    if source and source != "auto":
        parts.append(f"原文语言为【{source}】，请据此判断。")
    parts.append("")
    parts.append("【翻译要求】")
    parts.append(user_prompt)

    glossary = parse_glossary(config.get("glossary"))
    if glossary:
        parts.append("")
        parts.append(format_glossary_block(glossary))

    if context_hint:
        parts.append("")
        parts.append("【文档背景（仅供理解上下文，不要翻译它）】")
        parts.append(context_hint[:CONTEXT_HINT_CHARS])

    parts.append("")
    parts.append(constants.JSON_PROTOCOL)
    return "\n".join(parts)


def build_user_message(items: list[Item]) -> str:
    """把待翻译条目序列化成 JSON 数组。"""
    payload = [{"i": item.key, "t": item.text} for item in items]
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 响应解析（四级容错）
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_KV_PAIR = re.compile(
    r'"i"\s*:\s*(\d+)\s*,\s*"t"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL
)
_KV_PAIR_REVERSED = re.compile(
    r'"t"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"i"\s*:\s*(\d+)', re.DOTALL
)
_NUMBERED_LINE = re.compile(r"^\s*\[?(\d+)\]?\s*[.、:：)）]\s*(.+?)\s*$")


def _strip_fences(text: str) -> str:
    match = _FENCE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _unescape(value: str) -> str:
    """还原 JSON 字符串里的转义序列。"""
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return (
            value.replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
            .replace("\\t", "\t")
        )


def parse_batch_response(text: str) -> dict[int, str]:
    """把模型返回解析成 {序号: 译文}。尽最大努力兼容各种格式。"""
    if not text or not text.strip():
        return {}
    content = _strip_fences(text)
    result: dict[int, str] = {}

    # ---- 第 1 级：标准 JSON ----
    for candidate in _json_candidates(content):
        parsed = _try_load_json(candidate)
        if parsed is None:
            continue
        extracted = _extract_from_json(parsed)
        if extracted:
            result.update(extracted)
            break

    # ---- 第 2 级：正则匹配 {"i":N,"t":"..."} ----
    if not result:
        for match in _KV_PAIR.finditer(content):
            result[int(match.group(1))] = _unescape(match.group(2))
        if not result:
            for match in _KV_PAIR_REVERSED.finditer(content):
                result[int(match.group(2))] = _unescape(match.group(1))

    # ---- 第 3 级：编号行 "1. 译文" ----
    if not result:
        for line in content.splitlines():
            match = _NUMBERED_LINE.match(line)
            if match:
                result[int(match.group(1))] = match.group(2).strip()

    return {key: value.strip() for key, value in result.items() if value and value.strip()}


def _json_candidates(content: str):
    """给出若干可能的 JSON 片段，从最可能到最不可能。"""
    yield content
    # 去掉可能的说明文字，取最外层数组
    start, end = content.find("["), content.rfind("]")
    if start != -1 and end > start:
        yield content[start:end + 1]
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        yield content[start:end + 1]


def _try_load_json(candidate: str):
    for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    return None


def _extract_from_json(parsed) -> dict[int, str]:
    """从已解析的 JSON 对象里提取 {序号: 译文}。"""
    result: dict[int, str] = {}

    if isinstance(parsed, list):
        for position, element in enumerate(parsed):
            if isinstance(element, dict):
                key = element.get("i", element.get("index", element.get("id", position + 1)))
                value = element.get("t", element.get("text", element.get("translation", "")))
                try:
                    result[int(key)] = str(value)
                except (TypeError, ValueError):
                    result[position + 1] = str(value)
            elif isinstance(element, str):
                result[position + 1] = element
        return result

    if isinstance(parsed, dict):
        # 形如 {"1": "译文", "2": "..."}
        numeric = True
        for key, value in parsed.items():
            try:
                result[int(key)] = str(value) if not isinstance(value, dict) else str(
                    value.get("t") or value.get("text") or ""
                )
            except (TypeError, ValueError):
                numeric = False
                break
        if numeric and result:
            return result
        # 形如 {"translations": [...]}
        for key in ("translations", "result", "results", "data", "items"):
            if key in parsed:
                nested = _extract_from_json(parsed[key])
                if nested:
                    return nested
    return {}


# ---------------------------------------------------------------------------
# 长文本切分
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？；;：:\n])\s*")


def split_long_text(text: str, max_chars: int = MAX_ITEM_CHARS) -> list[str]:
    """把超长文本按句子切成若干块，尽量不破坏句子。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for piece in _SENTENCE_SPLIT.split(text):
        if not piece:
            continue
        if len(buffer) + len(piece) <= max_chars:
            buffer += piece
            continue
        if buffer:
            chunks.append(buffer)
        # 单句本身就超长时硬切
        while len(piece) > max_chars:
            chunks.append(piece[:max_chars])
            piece = piece[max_chars:]
        buffer = piece
    if buffer:
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()] or [text[:max_chars]]


# ---------------------------------------------------------------------------
# 翻译器
# ---------------------------------------------------------------------------

class Translator:
    """把段落列表翻译成目标语言。"""

    def __init__(self, config: dict, cache: TranslationCache | None = None, log=None, progress=None):
        self.config = dict(config)
        self.provider = create_provider(self.config)
        self.cache = cache
        self.stats = TranslateStats()
        self._log = log or (lambda message: logger.info(message))
        self._progress = progress or (lambda done, total: None)
        self._lock = threading.Lock()
        #: 已输出过的数值失配明细（去重，避免批量跑时刷屏）
        self._numeric_reported: set = set()
        self._cache_key_base = (
            self.config.get("provider", ""),
            self.config.get("model", ""),
            self.config.get("target_lang", "zh"),
            self.config.get("prompt", ""),
            parse_glossary(self.config.get("glossary")),
        )

    # ------------------------------------------------------------------
    def _cache_key(self, text: str) -> str:
        return TranslationCache.make_key(*self._cache_key_base, text)

    # ------------------------------------------------------------------
    def build_items(self, paragraphs: list[Paragraph]) -> list[Item]:
        """把段落展开成待翻译条目（含超长段落切分）。"""
        items: list[Item] = []
        key = 1
        for paragraph in paragraphs:
            text = clean_text(paragraph.text.replace("\n", " "))
            if not text:
                continue
            chunks = split_long_text(text)
            for index, chunk in enumerate(chunks):
                items.append(
                    Item(
                        key=key,
                        paragraph=paragraph,
                        text=chunk,
                        chunk=index,
                        chunk_total=len(chunks),
                    )
                )
                key += 1
        return items

    def build_batches(self, items: list[Item]) -> list[list[Item]]:
        """按“每批条数”与“每批字符数”双上限切分批次。"""
        batch_size = max(1, int(self.config.get("batch_size") or 12))
        batches: list[list[Item]] = []
        current: list[Item] = []
        current_chars = 0

        for item in items:
            length = len(item.text)
            too_many = len(current) >= batch_size
            too_long = current and current_chars + length > MAX_BATCH_CHARS
            if current and (too_many or too_long):
                batches.append(current)
                current, current_chars = [], 0
            current.append(item)
            current_chars += length

        if current:
            batches.append(current)
        return batches

    # ------------------------------------------------------------------
    def translate_paragraphs(self, paragraphs: list[Paragraph], cancel_event=None) -> TranslateStats:
        """主入口：翻译给定段落（就地写入 `paragraph.target`）。"""
        started = time.time()
        paragraphs = [p for p in paragraphs if p.translatable]
        self.stats.paragraphs = len(paragraphs)
        if not paragraphs:
            self.stats.elapsed = time.time() - started
            return self.stats

        context_hint = self._build_context_hint(paragraphs)
        system_prompt = build_system_prompt(self.config, context_hint)

        items = self.build_items(paragraphs)
        self.stats.items = len(items)

        # --- 文档内去重 ---
        # 完全相同的文本只翻译一次：既省 token，更重要的是保证**同一份文档里
        # 同一个词译法一致**（否则相同的词分散在不同并发批次里，模型可能给出
        # 不同答案，例如 "INNENSEITE" 一处译成"内页"、另一处译成"内侧"）。
        first_by_signature: dict[str, Item] = {}
        unique_items: list[Item] = []
        for item in items:
            signature = normalize_for_key(item.text)
            leader = first_by_signature.get(signature)
            if leader is None:
                first_by_signature[signature] = item
                unique_items.append(item)
            else:
                item.leader = leader
        deduplicated = len(items) - len(unique_items)
        if deduplicated:
            self.stats.deduplicated = deduplicated
            self._log(f"文档内有 {deduplicated} 条重复文本，将复用同一译文以保持一致")

        # --- 先查缓存 ---
        pending: list[Item] = []
        for item in unique_items:
            cached = self.cache.get(self._cache_key(item.text)) if self.cache else None
            if cached is not None:
                item.result = cached
                self.stats.cache_hits += 1
            else:
                pending.append(item)

        if self.stats.cache_hits:
            self._log(f"缓存命中 {self.stats.cache_hits} 条，剩余 {len(pending)} 条需要调用大模型")

        # --- 缓存命中的条目同样要过数值校验 ---
        # 不能只校验新翻译的：缓存里的条目**不进批次**，`_guard_numbers` 不会跑到
        # 它们身上，于是"坏译文一旦进缓存就永远修不好" —— 下次重跑直接命中，
        # 连校验都不跑。作废并重新翻译才是正确行为。
        if bool(self.config.get("numeric_guard", True)) and self.cache:
            polluted = [
                item for item in unique_items
                if item.result and missing_numbers(item.text, item.result)
            ]
            for item in polluted:
                self.cache.drop(self._cache_key(item.text))
                item.result = ""
                self.stats.cache_hits = max(0, self.stats.cache_hits - 1)
                pending.append(item)
            if polluted:
                self.stats.numeric_mismatched += len(polluted)
                self._log(
                    f"缓存中有 {len(polluted)} 条译文的数字与原文不符，已作废并重新翻译"
                )

        batches = self.build_batches(pending)
        self.stats.batches = len(batches)
        self._log(
            f"共 {self.stats.paragraphs} 段 / {self.stats.items} 条"
            f"（去重后 {len(unique_items)} 条），拆分为 {len(batches)} 批"
            f"（并发 {self.config.get('concurrency', 4)}）"
        )

        if batches:
            self._run_batches(batches, system_prompt, cancel_event)

        # --- 把去重项的译文补齐 ---
        for item in items:
            if item.leader is not None and item.leader.result:
                item.result = item.leader.result

        # --- 回填段落 ---
        self._assemble(paragraphs, items)

        if self.cache:
            self.cache.flush()

        self.stats.elapsed = time.time() - started
        return self.stats

    # ------------------------------------------------------------------
    @staticmethod
    def _build_context_hint(paragraphs: list[Paragraph]) -> str:
        """用文档开头若干段作为背景提示，帮助模型统一术语与语气。"""
        pieces = []
        for paragraph in paragraphs[:3]:
            text = clean_text(paragraph.text.replace("\n", " "))
            if text:
                pieces.append(text)
            if sum(len(p) for p in pieces) > CONTEXT_HINT_CHARS:
                break
        return "\n".join(pieces)[:CONTEXT_HINT_CHARS]

    # ------------------------------------------------------------------
    def _run_batches(self, batches: list[list[Item]], system_prompt: str, cancel_event=None) -> None:
        """并发执行所有批次。"""
        try:
            concurrency = max(1, min(16, int(self.config.get("concurrency") or 4)))
        except (TypeError, ValueError):
            concurrency = 4

        if concurrency == 1 or len(batches) == 1:
            for index, batch in enumerate(batches):
                if cancel_event is not None and cancel_event.is_set():
                    break
                self._log(f"正在翻译第 {index + 1}/{len(batches)} 批（{len(batch)} 条）")
                self._translate_batch(batch, system_prompt)
                self._progress(index + 1, len(batches))
            return

        done = 0
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="translate") as pool:
            futures = {
                pool.submit(self._translate_batch, batch, system_prompt): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                done += 1
                if cancel_event is not None and cancel_event.is_set():
                    continue
                try:
                    future.result()
                except Exception as exc:  # _translate_batch 内部已兜底，这里只是保险
                    logger.error("批次 %d 异常：%s", index + 1, exc)
                self._progress(done, len(batches))
                self._log(f"已完成 {done}/{len(batches)} 批")

    # ------------------------------------------------------------------
    def _translate_batch(self, batch: list[Item], system_prompt: str) -> None:
        """翻译一个批次，含缺失项重试与单条兜底。"""
        if not batch:
            return

        response = self._call(system_prompt, build_user_message(batch))
        mapping = parse_batch_response(response.text) if response else {}

        filled = self._apply(batch, mapping)

        # 缺失项重试：整批缩小重发
        attempt = 0
        missing = [item for item in batch if not item.result and not item.error]
        while missing and attempt < 2:
            attempt += 1
            with self._lock:
                self.stats.retries += 1
            self._log(f"有 {len(missing)} 条未返回译文，正在重试（第 {attempt} 次）")
            time.sleep(0.6 * attempt)
            response = self._call(system_prompt, build_user_message(missing))
            if response:
                filled += self._apply(missing, parse_batch_response(response.text))
            missing = [item for item in missing if not item.result and not item.error]

        # ---- 数值校验：失配则定向重试一次，仍失配则回退原文 ----
        if bool(self.config.get("numeric_guard", True)):
            self._guard_numbers(batch, system_prompt)

        # 最后兜底：逐条单独请求
        if missing:
            self._log(f"仍有 {len(missing)} 条未成功，改为逐条请求")
            for item in missing:
                response = self._call(system_prompt, build_user_message([item]))
                if response:
                    mapping = parse_batch_response(response.text)
                    if not mapping and response.text.strip():
                        # 模型直接返回纯译文（没走 JSON 协议）
                        mapping = {item.key: response.text.strip()}
                    self._apply([item], mapping)

        for item in batch:
            if not item.result and not item.error:
                item.error = "大模型未返回该段译文"

        failed = [item for item in batch if item.error and not item.result]
        if failed:
            with self._lock:
                self.stats.failed_items += len(failed)

    # ------------------------------------------------------------------
    def _call(self, system_prompt: str, user_message: str):
        """调用服务商，捕获异常并记录。"""
        with self._lock:
            self.stats.requests += 1
        try:
            response = self.provider.chat(system_prompt, user_message)
        except ProviderError as exc:
            logger.error("翻译请求失败：%s", exc)
            self._log(f"调用失败：{exc}")
            # 把错误写到该批所有条目上，避免无限重试
            return None
        except Exception as exc:  # 未预期的异常
            logger.exception("翻译请求出现未预期异常")
            self._log(f"调用异常：{type(exc).__name__}: {exc}")
            return None

        with self._lock:
            self.stats.prompt_tokens += response.prompt_tokens
            self.stats.completion_tokens += response.completion_tokens
        return response

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _validate_numbers(self, items: list[Item]) -> dict[int, list[str]]:
        """校验条目译文里的数字，返回 `{item.key: 缺失的数值}`。

        只校验**没有 error 且已有译文**的条目。单向包含判定见
        `text_utils.missing_numbers`。

        为什么在 item 级校验而不是段落级：`Item` 才是真正的翻译单元 ——
        长段落会被 `split_long_text` 切成多个 chunk 分别翻译，段落级的
        源/译文本对不上，比出来的结果是错的。
        """
        problems: dict[int, list[str]] = {}
        for item in items:
            if item.error or not item.result:
                continue
            missing = missing_numbers(item.text, item.result)
            if missing:
                problems[item.key] = missing
        return problems

    def _report_numeric(self, source: str, missing: list[str]) -> None:
        """输出数值失配明细。

        按 `(原文片段, 缺失数值)` 去重，且每个文档不超过 `numeric_log_limit` 条。
        批量跑几十份资料时，同一个失配模式可能触发几百次；逐段刷屏会让日志
        失去可读性。统计数字 `numeric_rejected` 不受此限制，仍然完整。
        """
        try:
            limit = int(self.config.get("numeric_log_limit", 5))
        except (TypeError, ValueError):
            limit = 5
        if limit <= 0:
            return
        key = (source.strip()[:40], tuple(missing))
        with self._lock:
            if key in self._numeric_reported:
                return
            if len(self._numeric_reported) >= limit:
                return
            self._numeric_reported.add(key)
        self._log(
            "数值校验未通过：%r 缺 %s" % (source.strip()[:40], "、".join(missing[:6]))
        )

    def _guard_numbers(self, batch: list[Item], system_prompt: str) -> None:
        """数值校验：失配的条目定向重试一次，仍失配则回退原文。

        `_translate_batch` 的收尾步骤之一。回退方式是**清空 `item.result` 并记
        error** —— `_assemble()` 见到"没有译文"会把段落 `target` 置空，
        排版阶段对 target 为空的段落既不擦除也不绘制，于是该段保持外文原样。
        这与项目既有的"取不到译文就保留原文"行为完全一致。
        """
        problems = self._validate_numbers(batch)
        if not problems:
            return

        retry_items = [item for item in batch if item.key in problems]
        with self._lock:
            self.stats.numeric_retries += 1
            self.stats.numeric_mismatched += len(retry_items)

        # 只重发失配的这几条，不重发整批
        nums = sorted({num for values in problems.values() for num in values})
        hint = (
            "\n\n【数字保护】以下数值必须原样保留，一个字符都不能改："
            + "、".join(nums)[:300]
        )
        self._log(f"数值校验：{len(retry_items)} 条译文的数字与原文不符，正在定向重试")
        response = self._call(system_prompt + hint, build_user_message(retry_items))
        if response:
            self._apply(retry_items, parse_batch_response(response.text))

        for item in retry_items:
            still = missing_numbers(item.text, item.result) if item.result else []
            if not still:
                continue
            # 坏译文不能留在缓存里：否则下次重跑命中它、又被拒一次，永远好不了
            if self.cache and item.result:
                self.cache.drop(self._cache_key(item.text))
            item.result = ""
            item.error = "数值校验未通过，已保留原文"
            with self._lock:
                self.stats.numeric_rejected += 1
            self._report_numeric(item.text, still)

    def _apply(self, items: list[Item], mapping: dict[int, str]) -> int:
        """把解析出来的译文写回条目，返回成功条数。"""
        filled = 0
        for item in items:
            value = mapping.get(item.key)
            if not value:
                continue
            item.result = value
            filled += 1
            if self.cache:
                self.cache.put(self._cache_key(item.text), value)
        return filled

    # ------------------------------------------------------------------
    def _assemble(self, paragraphs: list[Paragraph], items: list[Item]) -> None:
        """把条目译文按段落拼装回去。"""
        grouped: dict[int, list[Item]] = {}
        for item in items:
            grouped.setdefault(id(item.paragraph), []).append(item)

        for paragraph in paragraphs:
            chunk_items = sorted(grouped.get(id(paragraph), []), key=lambda i: i.chunk)
            if not chunk_items:
                paragraph.error = "没有可翻译内容"
                continue
            pieces = [item.result for item in chunk_items if item.result]
            if pieces:
                paragraph.target = self._join_chunks(pieces)
            else:
                paragraph.target = ""
                errors = [item.error for item in chunk_items if item.error]
                paragraph.error = errors[0] if errors else "翻译失败"

    @staticmethod
    def _join_chunks(pieces: list[str]) -> str:
        """拼接子块译文：CJK 之间不加空格，拉丁文本之间加空格。"""
        if len(pieces) == 1:
            return pieces[0].strip()
        result = pieces[0].strip()
        for piece in pieces[1:]:
            piece = piece.strip()
            if not piece:
                continue
            if result and result[-1].isascii() and result[-1].isalnum() and piece[0].isascii():
                result += " " + piece
            else:
                result += piece
        return result

    # ------------------------------------------------------------------
    def count_failed(self, paragraphs: list[Paragraph]) -> int:
        return sum(1 for p in paragraphs if p.translatable and not p.target)

    def close(self) -> None:
        self.provider.close()
