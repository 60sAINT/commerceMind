"""进程内工具管理器：工具契约、注册表与统一执行入口。

T1 落地“契约 + 注册表”，T2 接入 JSON Schema 参数校验，T3 接入超时与重试，
T4 接入 TTL 缓存，T5 接入熔断与降级，T6 接入结果重排：

- `ToolContext` / `ToolSpec` / `ToolResult` / `ToolStats` / `CircuitState`；
- `ToolManager.register()` / `unregister()` / `definitions()` / `get_stats()` / `fork()`；
- `validate_params()` 在 handler 之前校验模型参数，`schema_hint()` 给模型精简 schema；
- `execute()` 完成注册表查找、参数校验、TTL 缓存、单次超时、read-only 重试与统计；
- `invalidate_cache()` 通过版本号同时实现主动失效与在途结果不回填。

缓存结构与 EchoMind 保持一致：进程内字典、`工具名 + MD5` 键、`(数据, 过期时间, 是否重排)` 值。
本模块不依赖 FastAPI，也不依赖具体 LLM 或业务工具，可被脚本、测试、评测直接复用。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jsonschema import ValidationError, validators
from jsonschema.exceptions import SchemaError
from monitor.tool_monitor import ToolMonitor

from tooling.errors import ToolError, ToolErrorCode, ToolValidationError
from tooling.reranker import Reranker

logger = logging.getLogger(__name__)

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CACHE_SCOPE_FIELDS = frozenset({"user_id", "session_id", "request_id", "intent"})
_DEPENDENCY_FAILURE_MESSAGES = {
    ToolErrorCode.TOOL_TIMEOUT.value: "工具执行超时",
    ToolErrorCode.TOOL_HANDLER_ERROR.value: "工具执行失败",
}


class CircuitState(str, Enum):
    """熔断器三态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """单次请求的工具上下文。

    只放执行工具需要的请求级信息，不放共享可变状态；
    `entities` 在构造时复制一份，调用方之后修改原字典不会影响这里。
    """

    user_id: str
    session_id: str
    request_id: str
    intent: str | None = None
    entities: Mapping[str, Any] = field(default_factory=dict)
    intent_group: str | None = None
    urgency: str | None = None
    intent_confidence: float = 0.0
    context_available: bool = False

    def __post_init__(self) -> None:
        for name in ("user_id", "session_id", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
            object.__setattr__(self, name, value.strip())

        if self.intent is not None:
            if not isinstance(self.intent, str) or not self.intent.strip():
                raise ValueError("intent 必须是非空字符串或 None")
            object.__setattr__(self, "intent", self.intent.strip())

        if not isinstance(self.entities, Mapping):
            raise TypeError("entities 必须是映射")
        object.__setattr__(self, "entities", copy.deepcopy(dict(self.entities)))
        for name in ("intent_group", "urgency"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} 必须是非空字符串或 None")
        if (
            isinstance(self.intent_confidence, bool)
            or not isinstance(self.intent_confidence, (int, float))
            or not 0 <= self.intent_confidence <= 1
        ):
            raise ValueError("intent_confidence 必须在 0 到 1 之间")
        if not isinstance(self.context_available, bool):
            raise TypeError("context_available 必须是布尔值")


@dataclass(frozen=True, slots=True)
class ToolFailure:
    """传给 fallback 的失败信息，不包含堆栈和内部路径。"""

    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具定义：不可变，运行时统计与熔断状态放在 `RegisteredTool`。

    `handler` 支持同步/异步函数，但永远不会直接暴露给模型；
    模型只看到 `ToolManager.definitions()` 输出的 name/description/input_schema。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Any]
    timeout_s: float = 3.0
    max_retries: int = 1
    retry_backoff_s: float = 0.1
    cache_ttl_s: float = 0.0  # 只读工具显式开启，0 = 不缓存
    cache_scope: tuple[str, ...] = ("user_id",)
    retry_safe: bool = True  # 只读工具 True；副作用工具 False
    supports_rerank: bool = False
    rerank_query_field: str = "query"
    fallback: Callable[[dict[str, Any], ToolContext, ToolFailure], Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("工具名必须是小写字母开头、仅含小写字母/数字/下划线的 1–64 字符字符串")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("description 必须是非空字符串")
        object.__setattr__(self, "description", self.description.strip())

        if not callable(self.handler):
            raise TypeError("handler 必须可调用")

        schema = self._normalized_schema(self.input_schema)
        object.__setattr__(self, "input_schema", schema)

        _require_positive_number("timeout_s", self.timeout_s)
        _require_non_negative_number("retry_backoff_s", self.retry_backoff_s)
        _require_non_negative_number("cache_ttl_s", self.cache_ttl_s)
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries 必须是非负整数")
        if not isinstance(self.retry_safe, bool):
            raise TypeError("retry_safe 必须是布尔值")
        if not isinstance(self.supports_rerank, bool):
            raise TypeError("supports_rerank 必须是布尔值")
        if not isinstance(self.rerank_query_field, str) or not self.rerank_query_field.strip():
            raise ValueError("rerank_query_field 必须是非空字符串")

        scope = self.cache_scope
        if isinstance(scope, str) or not isinstance(scope, (tuple, list)):
            raise TypeError("cache_scope 必须是字符串序列")
        scope = tuple(scope)
        for item in scope:
            if item not in _CACHE_SCOPE_FIELDS:
                raise ValueError(f"cache_scope 只支持 {sorted(_CACHE_SCOPE_FIELDS)}，收到 {item!r}")
        object.__setattr__(self, "cache_scope", scope)

        if self.fallback is not None and not callable(self.fallback):
            raise TypeError("fallback 必须可调用或为 None")

    @staticmethod
    def _normalized_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """校验并复制 JSON Schema，缺省时补上安全默认值。"""
        if not isinstance(schema, dict):
            raise TypeError("input_schema 必须是字典")
        normalized = copy.deepcopy(schema)
        if normalized.get("type") != "object":
            raise ValueError("input_schema 必须是 type=object 的 JSON Schema")

        properties = normalized.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("input_schema.properties 必须是字典")

        required = normalized.setdefault("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ValueError("input_schema.required 必须是字符串列表")
        missing = [item for item in required if item not in properties]
        if missing:
            raise ValueError(f"input_schema.required 包含未声明字段: {missing}")

        if normalized.setdefault("additionalProperties", False) is not False:
            raise ValueError("工具 schema 必须禁止额外字段（additionalProperties=false）")

        try:
            validators.validator_for(normalized).check_schema(normalized)
        except SchemaError as exc:
            raise ValueError(f"input_schema 不是合法的 JSON Schema: {exc.message}") from None
        return normalized


_HINT_FIELD_KEYS = (
    "type",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
)


def validate_params(spec: ToolSpec, params: Any) -> dict[str, Any]:
    """在进入 handler 前校验并规范化工具参数。

    只接受 JSON 对象；字符串值去掉首尾空白，不做隐式类型强转。
    校验失败统一抛 `ToolValidationError`，错误信息面向模型，可据此自我修复。
    """
    if not isinstance(params, Mapping):
        raise ToolValidationError(
            "工具参数必须是 JSON 对象",
            path="参数",
            validator="type",
        )

    arguments = _normalize_arguments(dict(params))
    validator = validators.validator_for(spec.input_schema)(spec.input_schema)
    try:
        validator.validate(arguments)
    except ValidationError as exc:
        raise _to_validation_error(exc) from None
    return arguments


def _normalize_arguments(value: Any) -> Any:
    """递归去空白：字典/列表保持结构，字符串只 strip，不转换类型。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return {key: _normalize_arguments(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_arguments(item) for item in value]
    return value


def _to_validation_error(exc: ValidationError) -> ToolValidationError:
    path = ".".join(str(part) for part in exc.absolute_path) or "参数"
    validator = exc.validator or "unknown"

    if validator == "required":
        required = exc.validator_value if isinstance(exc.validator_value, list) else []
        instance = exc.instance if isinstance(exc.instance, Mapping) else {}
        missing = [name for name in required if name not in instance]
        target = missing[0] if missing else None
        message = f"缺少必填字段: {', '.join(missing) or '未知'}"
        if target:
            path = f"{path}.{target}" if path != "参数" else target
    elif validator == "additionalProperties":
        properties = exc.schema.get("properties", {}) if isinstance(exc.schema, Mapping) else {}
        instance = exc.instance if isinstance(exc.instance, Mapping) else {}
        extra = [name for name in instance if name not in properties]
        target = extra[0] if extra else None
        message = f"不支持字段: {', '.join(extra) or '未知'}"
        if target:
            path = f"{path}.{target}" if path != "参数" else target
    elif validator == "type":
        expected = exc.validator_value
        if isinstance(expected, list):
            expected = "/".join(str(item) for item in expected)
        message = f"字段 {path} 类型应为 {expected}"
    elif validator == "enum":
        values = ", ".join(json.dumps(item, ensure_ascii=False) for item in exc.validator_value)
        message = f"字段 {path} 必须是以下之一: {values}"
    elif validator in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}:
        operator = {
            "minimum": ">=",
            "maximum": "<=",
            "exclusiveMinimum": ">",
            "exclusiveMaximum": "<",
        }[validator]
        message = f"字段 {path} 必须 {operator} {exc.validator_value}"
    elif validator in {"minLength", "maxLength"}:
        relation = "至少" if validator == "minLength" else "最多"
        message = f"字段 {path} 长度{relation} {exc.validator_value} 个字符"
    elif validator in {"minItems", "maxItems"}:
        relation = "至少" if validator == "minItems" else "最多"
        message = f"字段 {path} 数量{relation} {exc.validator_value} 个"
    elif validator == "pattern":
        message = f"字段 {path} 格式不匹配"
    else:
        message = f"字段 {path} 未通过校验（{validator}）"

    return ToolValidationError(message, path=path, validator=validator)


@dataclass(slots=True)
class ToolResult:
    """工具调用的统一返回。

    `success` 表示主路径成功；`usable` 表示有可用数据（含 fallback 降级）；
    `degraded` 表示主路径失败但拿到了降级数据，模型需要说明数据可能不完整。
    """

    tool_name: str
    success: bool
    data: Any = None
    usable: bool = False
    degraded: bool = False
    cached: bool = False
    reranked: bool = False
    truncated: bool = False
    attempts: int = 0
    latency_ms: float = 0.0
    error_code: str | None = None
    error: str | None = None
    retryable: bool = False


@dataclass(slots=True)
class ToolStats:
    """单个工具的运行时统计，供 `get_stats()` / monitor 读取。"""

    total: int = 0  # 逻辑调用数
    attempts: int = 0  # 物理尝试数（T3 接入重试后与 total 不同）
    success: int = 0
    failed: int = 0
    invalid: int = 0  # 参数校验失败（T2）
    timeout: int = 0  # 超时（T3）
    cache_hits: int = 0  # 缓存命中（T4）
    retries: int = 0  # 重试次数（T3）
    circuit_rejections: int = 0
    degraded: int = 0
    cancelled: int = 0
    total_latency_ms: float = 0.0
    consecutive_fails: int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0


class CircuitBreaker:
    """三态熔断器：CLOSED → OPEN → HALF_OPEN → CLOSED。

    使用 generation 隔离旧请求的迟到结果。
    使用注入的 `clock()`，测试不需要真的等待恢复时间。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(failure_threshold) is not int or failure_threshold <= 0:
            raise ValueError("failure_threshold 必须是正整数")
        _require_non_negative_number("recovery_s", recovery_s)
        self.threshold = failure_threshold
        self.recovery_s = float(recovery_s)
        self._clock = clock
        self.state = CircuitState.CLOSED
        self.generation = 0
        self.fail_count = 0
        self.opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def recovery_due(self) -> bool:
        """熔断是否已到恢复时间，不改变状态，也不占用探测名额。"""
        return (
            self.state is CircuitState.OPEN
            and self.opened_at is not None
            and self._clock() - self.opened_at >= self.recovery_s
        )

    def allow(self) -> bool:
        """是否允许本次调用；HALF_OPEN 时只放一个探测。"""
        if self.state is CircuitState.CLOSED:
            return True

        if self.state is CircuitState.OPEN:
            if not self.recovery_due:
                return False
            self.state = CircuitState.HALF_OPEN
            self.generation += 1
            self._probe_in_flight = False

        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self, generation: int | None = None) -> None:
        """记录一次成功；旧 generation 的迟到结果不能关闭新一轮熔断。"""
        if generation is not None and generation != self.generation:
            return
        self.fail_count = 0
        self._probe_in_flight = False
        self.state = CircuitState.CLOSED
        self.opened_at = None

    def abandon_probe(self, generation: int) -> None:
        """取消或无有效结果的探测可以立即让下一次调用接手。"""
        if self.state is CircuitState.HALF_OPEN and generation == self.generation:
            self.state = CircuitState.OPEN
            self._probe_in_flight = False

    def record_failure(self, generation: int | None = None) -> None:
        """记录一次依赖失败；达到阈值或半开探测失败时打开熔断。"""
        if generation is not None and generation != self.generation:
            return
        self._probe_in_flight = False
        self.fail_count += 1
        if self.state is CircuitState.HALF_OPEN or self.fail_count >= self.threshold:
            self.generation += 1
            self.state = CircuitState.OPEN
            self.opened_at = self._clock()
            logger.warning("工具熔断器打开（连续失败 %s 次）", self.fail_count)


@dataclass(slots=True)
class RegisteredTool:
    """注册表条目：不可变定义 + 可变的运行时状态。"""

    spec: ToolSpec
    stats: ToolStats = field(default_factory=ToolStats)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


class ToolManager:
    """工具注册表与统一执行入口。

    进程级单例，不保存任何请求级状态；每个请求只创建 `ToolContext`。
    `clock` / `sleep` 可注入，便于测试超时、退避和熔断。
    """

    def __init__(
        self,
        reranker: Reranker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_schema_hint_chars: int = 800,
        cache_max_entries: int = 5000,
        circuit_failure_threshold: int = 5,
        circuit_recovery_s: float = 60.0,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if not callable(sleep):
            raise TypeError("sleep 必须可调用")
        if type(max_schema_hint_chars) is not int or not 128 <= max_schema_hint_chars <= 4000:
            raise ValueError("max_schema_hint_chars 必须在 128 到 4000 之间")
        if type(cache_max_entries) is not int or not 1 <= cache_max_entries <= 1_000_000:
            raise ValueError("cache_max_entries 必须在 1 到 1000000 之间")
        self._tools: dict[str, RegisteredTool] = {}
        self._reranker = reranker
        self._clock = clock
        self._sleep = sleep
        self._max_schema_hint_chars = max_schema_hint_chars
        self._cache_max_entries = cache_max_entries
        self._cache: dict[str, tuple[Any, float, bool]] = {}
        self._cache_versions: dict[str, int] = {}
        self.monitor = ToolMonitor()
        # 复用状态机的参数校验，即使注册表暂时为空也能发现配置错误。
        breaker = CircuitBreaker(circuit_failure_threshold, circuit_recovery_s, clock)
        self._circuit_failure_threshold = breaker.threshold
        self._circuit_recovery_s = breaker.recovery_s

    # ── 注册 / 注销 ──────────────────────────────────────────────────────────

    def register(self, tool: ToolSpec) -> None:
        """注册工具；重名直接报错，注册前先失效同名缓存。"""
        if not isinstance(tool, ToolSpec):
            raise TypeError("只能注册 ToolSpec")
        if tool.name in self._tools:
            raise ValueError(f"工具已注册: {tool.name}")
        self.invalidate_cache(tool.name)
        self._tools[tool.name] = RegisteredTool(
            spec=tool,
            breaker=CircuitBreaker(self._circuit_failure_threshold, self._circuit_recovery_s, self._clock),
        )
        self.monitor.register(tool.name)
        logger.info("注册工具: %s", tool.name)

    def unregister(self, name: str) -> bool:
        """注销工具并丢弃其统计、熔断状态与缓存；返回是否真的删除了。"""
        removed = self._tools.pop(name, None)
        if removed is None:
            return False
        self.invalidate_cache(name)
        self.monitor.unregister(name)
        logger.info("注销工具: %s", name)
        return True

    def get_spec(self, name: str) -> ToolSpec | None:
        registered = self._tools.get(name)
        return registered.spec if registered else None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ── 模型可见定义 ─────────────────────────────────────────────────────────

    def definitions(self) -> list[dict[str, Any]]:
        """Anthropic 原生 tools 列表；不暴露 handler，schema 复制一份防止外部改坏。"""
        return [
            {
                "name": registered.spec.name,
                "description": registered.spec.description,
                "input_schema": copy.deepcopy(registered.spec.input_schema),
            }
            for _, registered in sorted(self._tools.items())
        ]

    def schema_hint(self, tool: ToolSpec | str) -> dict[str, Any] | None:
        """给模型看的精简 schema：required、字段类型/枚举/范围/pattern。

        预算按紧凑 JSON 字符串计；超出 `max_schema_hint_chars` 时逐步去掉
        约束细节，最后保证字段名和必填列表仍然可读。未知工具返回 None。
        """
        if isinstance(tool, str):
            spec = self.get_spec(tool)
        else:
            spec = tool
        if spec is None:
            return None

        full = _build_schema_hint(spec.input_schema, detail="full")
        if _hint_size(full) <= self._max_schema_hint_chars:
            return full

        compact = _build_schema_hint(spec.input_schema, detail="compact")
        if _hint_size(compact) <= self._max_schema_hint_chars:
            return compact

        minimal = _build_schema_hint(spec.input_schema, detail="minimal")
        if _hint_size(minimal) <= self._max_schema_hint_chars:
            return minimal

        # 极端情况：字段太多，逐个保留字段名直到预算用尽；被截断时显式标记。
        limited: dict[str, Any] = {
            "type": "object",
            "required": list(minimal["required"]),
            "additionalProperties": False,
            "properties": {},
            "truncated": True,
        }
        for name in minimal["properties"]:
            limited["properties"][name] = {}
            if _hint_size(limited) > self._max_schema_hint_chars:
                limited["properties"].pop(name)
                break
        limited["required"] = [name for name in limited["required"] if name in limited["properties"]]
        return limited

    # ── 缓存 ─────────────────────────────────────────────────────────────────

    @property
    def cache_size(self) -> int:
        """当前缓存条目数（含尚未清理的过期条目）。"""
        return len(self._cache)

    def invalidate_cache(self, name: str | None = None) -> int:
        """主动失效缓存，返回删除的条目数。

        `name=None` 失效全部工具。版本号在删除前 +1，因此在途请求即使随后
        成功也不会把旧结果写回（写缓存前会再比较一次版本号）。
        """
        if name is None:
            for tool_name in self._tools:
                self._cache_versions[tool_name] = self._cache_versions.get(tool_name, 0) + 1
            removed = len(self._cache)
            self._cache.clear()
            return removed

        if not isinstance(name, str) or not name:
            raise ValueError("name 必须是非空字符串或 None")
        self._cache_versions[name] = self._cache_versions.get(name, 0) + 1
        prefix = f"{name}:"
        keys = [key for key in self._cache if key.startswith(prefix)]
        for key in keys:
            del self._cache[key]
        return len(keys)

    def _cache_key(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        context: ToolContext,
        rerank_top_k: int,
    ) -> str:
        """与 EchoMind 一致：params + rerank_top_k + context + version 做 MD5。

        EchoMind 的 context 是调用方传入的字典；这里只取 `spec.cache_scope`
        里的字段（默认 `user_id`），对应 EchoMind 实际传入的 `{"user_id": ...}`。
        """
        payload = {
            "params": arguments,
            "rerank_top_k": rerank_top_k,
            "context": {field: getattr(context, field) for field in spec.cache_scope},
            "version": self._cache_versions.get(spec.name, 0),
        }
        digest = hashlib.md5(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        return f"{spec.name}:{digest}"

    def _cache_lookup(self, key: str) -> tuple[Any, bool] | None:
        """读缓存：过期就删除并返回 None；命中返回 (数据的深拷贝, 是否重排)。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        data, expire_at, reranked = entry
        if self._clock() < expire_at:
            return copy.deepcopy(data), reranked
        del self._cache[key]
        return None

    def _cache_store(self, key: str, data: Any, ttl: float, reranked: bool = False) -> None:
        """写缓存：达到上限时按插入顺序删掉最旧的 1/4。"""
        if key not in self._cache and len(self._cache) >= self._cache_max_entries:
            drop = max(1, self._cache_max_entries // 4)
            for old_key in list(self._cache)[:drop]:
                del self._cache[old_key]
        self._cache[key] = (copy.deepcopy(data), self._clock() + ttl, reranked)

    # ── 执行 ─────────────────────────────────────────────────────────────────

    async def execute(
        self,
        name: str,
        params: Any,
        context: ToolContext,
        *,
        deadline: float | None = None,
        use_cache: bool = True,
        rerank_top_k: int = 0,
    ) -> ToolResult:
        """统一观测一次逻辑调用；保留原执行结果、异常和取消语义。"""
        if not isinstance(context, ToolContext):
            raise TypeError("context 必须是 ToolContext")
        started = self._clock()
        registered = self._tools.get(name)
        result = None
        outcome = "error"
        if registered is not None:
            self.monitor.start_tool(name)
        try:
            result = await self._execute(
                name, params, context, deadline=deadline, use_cache=use_cache, rerank_top_k=rerank_top_k,
            )
            outcome = "success" if result.success else "failed"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            if registered is not None:
                registered.stats.cancelled += 1
            raise
        finally:
            latency_ms = max(0.0, (self._clock() - started) * 1000)
            code = result.error_code if result is not None else None
            if code not in {item.value for item in ToolErrorCode}:
                code = None
            if self._tools.get(name) is registered and registered is not None:
                self.monitor.record_tool(name, latency_ms, code)
            logger.info("tool_call %s", json.dumps({
                "request_id": context.request_id, "tool": registered.spec.name if registered else "_unknown",
                "outcome": outcome, "error_code": code, "latency_ms": round(latency_ms, 3),
                "attempts": result.attempts if result else None,
                "cached": result.cached if result else False,
                "reranked": result.reranked if result else False,
                "circuit_state": registered.breaker.state.value if registered else None,
                "usable": result.usable if result else False, "degraded": result.degraded if result else False,
            }, ensure_ascii=False))

    async def _execute(
        self,
        name: str,
        params: Any,
        context: ToolContext,
        *,
        deadline: float | None = None,
        use_cache: bool = True,
        rerank_top_k: int = 0,
    ) -> ToolResult:
        """执行一次工具调用。

        `deadline` 是 `time.monotonic()` 绝对时间，由 FunctionCallingLoop 传入。
        每次尝试取 `min(tool.timeout_s, deadline - now)`；只有 `retry_safe=True`
        的工具会按指数退避重试和使用缓存。熔断到恢复时间时跳过缓存读取，
        让一个请求执行半开探测。
        `rerank_top_k` 大于 0 时，支持重排的工具可以对成功返回的列表选取 Top-K。
        """
        if not isinstance(context, ToolContext):
            raise TypeError("context 必须是 ToolContext")

        registered = self._tools.get(name)
        if registered is None:
            return ToolResult(
                tool_name=name,
                success=False,
                usable=False,
                error_code=ToolErrorCode.TOOL_NOT_FOUND.value,
                error=f"工具未注册: {name}",
            )

        try:
            arguments = validate_params(registered.spec, params)
        except ToolValidationError as exc:
            # 参数错误是调用方/模型错误：不重试、不执行、不计熔断。
            registered.stats.invalid += 1
            logger.warning("工具参数校验失败: %s (%s)", name, exc.validator or "unknown")
            return ToolResult(
                tool_name=name,
                success=False,
                usable=False,
                attempts=0,
                error_code=ToolErrorCode.INVALID_ARGUMENTS.value,
                error=exc.message,
                retryable=False,
            )

        spec = registered.spec
        stats = registered.stats
        cache_key = None
        cache_version = self._cache_versions.get(name, 0)
        top_k = rerank_top_k if rerank_top_k > 0 and spec.supports_rerank else 0
        if use_cache and spec.retry_safe and spec.cache_ttl_s > 0:
            cache_key = self._cache_key(spec, arguments, context, top_k)

        # 缓存命中算一次成功调用。到达恢复时间时，给实际调用留出探测机会。
        if cache_key is not None and not registered.breaker.recovery_due:
            cached = self._cache_lookup(cache_key)
            if cached is not None:
                cached_data, cached_reranked = cached
                stats.total += 1
                stats.success += 1
                stats.cache_hits += 1
                return ToolResult(
                    tool_name=name,
                    success=True,
                    usable=True,
                    data=cached_data,
                    cached=True,
                    reranked=cached_reranked,
                )

        started = self._clock()
        breaker = registered.breaker
        stats.total += 1
        failure = None
        if deadline is not None and deadline <= started:
            result = ToolResult(
                tool_name=name, success=False,
                error_code=ToolErrorCode.TOOL_DEADLINE_EXCEEDED.value, error="工具阶段预算已耗尽",
            )
        elif not breaker.allow():
            stats.circuit_rejections += 1
            failure = ToolFailure(ToolErrorCode.TOOL_CIRCUIT_OPEN.value, "工具熔断中，请稍后重试")
            result = ToolResult(tool_name=name, success=False, error_code=failure.code, error=failure.message)
        else:
            generation = breaker.generation
            probe = breaker.state is CircuitState.HALF_OPEN
            try:
                result = await self._execute_attempts(registered, arguments, context, deadline, probe=probe)
            except asyncio.CancelledError:
                breaker.abandon_probe(generation)
                raise

            if result.success:
                breaker.record_success(generation)
                stats.consecutive_fails = breaker.fail_count
                cacheable = await self._apply_rerank(
                    result, arguments.get(spec.rerank_query_field), top_k, spec, deadline,
                )
                stats.success += 1
                if (
                    cacheable
                    and generation == breaker.generation
                    and cache_key is not None
                    and cache_version == self._cache_versions.get(name, 0)
                ):
                    self._cache_store(cache_key, result.data, spec.cache_ttl_s, result.reranked)
            elif result.error_code is not None and result.error_code in _DEPENDENCY_FAILURE_MESSAGES:
                breaker.record_failure(generation)
                failure = ToolFailure(
                    result.error_code, _DEPENDENCY_FAILURE_MESSAGES[result.error_code], result.retryable,
                )
            else:
                breaker.abandon_probe(generation)
            stats.consecutive_fails = breaker.fail_count

        if not result.success:
            stats.failed += 1
        if failure is not None:
            await self._apply_fallback(spec, arguments, context, deadline, failure, result)
            if result.degraded:
                stats.degraded += 1
        result.latency_ms = (self._clock() - started) * 1000
        stats.total_latency_ms += result.latency_ms
        return result

    async def _execute_attempts(
        self,
        registered: RegisteredTool,
        arguments: dict[str, Any],
        context: ToolContext,
        deadline: float | None,
        *,
        probe: bool,
    ) -> ToolResult:
        spec, stats = registered.spec, registered.stats
        name = spec.name
        attempts = 0
        max_attempts = 1 + (spec.max_retries if spec.retry_safe and not probe else 0)
        last_error_code: str | None = None
        last_error: str | None = None
        last_retryable = False

        while attempts < max_attempts:
            attempt_timeout = self._attempt_timeout(spec, deadline)
            if attempt_timeout <= 0:
                if attempts == 0:
                    last_error_code = ToolErrorCode.TOOL_DEADLINE_EXCEEDED.value
                    last_error = "工具阶段预算已耗尽"
                break

            if attempts > 0:
                stats.retries += 1
            attempts += 1
            stats.attempts += 1
            attempt_budget = asyncio.timeout(attempt_timeout)
            try:
                async with attempt_budget:
                    data = await self._invoke(spec.handler, copy.deepcopy(arguments), context)
            except TimeoutError:
                stats.timeout += 1
                last_error_code = ToolErrorCode.TOOL_TIMEOUT.value
                last_error = f"工具执行超时（{attempt_timeout:.3g}s）"
                last_retryable = True
                logger.warning(
                    "工具超时: %s (attempt %s/%s)", name, attempts, max_attempts,
                )
                if attempt_budget.expired() and attempt_timeout < spec.timeout_s:
                    # 阶段预算限制了这次等待，超时后无需用另一只时钟重新判断。
                    break
            except ToolError as exc:
                last_error_code = exc.code.value
                last_error = exc.message
                last_retryable = exc.retryable and last_error_code in _DEPENDENCY_FAILURE_MESSAGES
            except Exception as exc:
                last_error_code = ToolErrorCode.TOOL_HANDLER_ERROR.value
                last_error = str(exc) or type(exc).__name__
                last_retryable = True
                logger.error("工具执行失败: %s (%s)", name, type(exc).__name__)
            else:
                return ToolResult(
                    tool_name=name,
                    success=True,
                    usable=True,
                    data=data,
                    attempts=attempts,
                )

            if not last_retryable or not spec.retry_safe or attempts >= max_attempts:
                break

            delay = self._retry_delay(spec, attempts, deadline)
            if delay is None:
                break
            if delay > 0:
                if deadline is None:
                    await self._sleep(delay)
                else:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        break
                    try:
                        async with asyncio.timeout(remaining):
                            await self._sleep(delay)
                    except TimeoutError:
                        break

        if last_error_code is None:
            last_error_code = ToolErrorCode.TOOL_DEADLINE_EXCEEDED.value
            last_error = "工具阶段预算已耗尽"
        return ToolResult(
            tool_name=name,
            success=False,
            usable=False,
            attempts=attempts,
            error_code=last_error_code,
            error=last_error,
            retryable=last_retryable and spec.retry_safe,
        )

    async def _apply_rerank(
        self,
        result: ToolResult,
        query: Any,
        top_k: int,
        spec: ToolSpec,
        deadline: float | None,
    ) -> bool:
        """尝试重排，返回最终结果是否适合写缓存。"""
        items = result.data
        reranker = self._reranker
        if (
            reranker is None or top_k <= 0 or not isinstance(items, list) or len(items) <= top_k
            or not isinstance(query, str) or not query.strip()
        ):
            return True

        timeout = self._attempt_timeout(spec, deadline)
        if timeout > 0:
            try:
                async with asyncio.timeout(timeout):
                    order = await reranker.rerank(query, copy.deepcopy(items), top_k)
                if not isinstance(order, list) or not order or any(
                    type(index) is not int or not 0 <= index < len(items) for index in order
                ):
                    raise ValueError("重排索引必须是非空、有效的整数列表")
            except Exception as exc:
                logger.warning("工具重排失败: %s (%s)", spec.name, type(exc).__name__)
            else:
                # 重复索引只取一次，缺少的候选按原序补齐。
                selected = dict.fromkeys(order)
                selected.update(dict.fromkeys(range(len(items))))
                result.data = [items[index] for index in selected][:top_k]
                result.reranked = True
                return True

        result.data = items[:top_k]
        return False

    async def _apply_fallback(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        context: ToolContext,
        deadline: float | None,
        failure: ToolFailure,
        result: ToolResult,
    ) -> None:
        fallback = spec.fallback
        if fallback is None:
            return
        timeout = self._attempt_timeout(spec, deadline)
        if timeout <= 0:
            return
        try:
            async with asyncio.timeout(timeout):
                data = await self._invoke(fallback, copy.deepcopy(arguments), context, failure)
        except Exception as exc:
            logger.warning("工具降级失败: %s (%s)", spec.name, type(exc).__name__)
        else:
            if data is not None:
                result.data = data
                result.usable = True
                result.degraded = True

    def _attempt_timeout(self, spec: ToolSpec, deadline: float | None) -> float:
        """单次尝试的超时：不超过工具自身超时，也不超过剩余阶段预算。"""
        if deadline is None:
            return spec.timeout_s
        return min(spec.timeout_s, deadline - self._clock())

    def _retry_delay(self, spec: ToolSpec, attempts: int, deadline: float | None) -> float | None:
        """指数退避 + 抖动；预算不足时返回 None，表示不再重试。"""
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None

        base = spec.retry_backoff_s * (2 ** (attempts - 1))
        if base <= 0:
            return 0.0
        delay = base + random.uniform(0, base * 0.1)
        if remaining is not None:
            delay = min(delay, remaining)
        return max(delay, 0.0)

    async def _invoke(
        self,
        handler: Callable[..., Any],
        params: dict[str, Any],
        context: ToolContext,
        *args: Any,
    ) -> Any:
        """同步 handler 在线程池执行；超时/取消只停止等待，不能终止线程。"""
        if inspect.iscoroutinefunction(handler):
            return await handler(params, context, *args)
        result = await asyncio.to_thread(handler, params, context, *args)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── 统计 / 隔离 ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, dict[str, Any]]:
        """工具名 → 运行指标；不返回参数和结果。"""
        return {
            name: {
                "total": registered.stats.total,
                "attempts": registered.stats.attempts,
                "success": registered.stats.success,
                "failed": registered.stats.failed,
                "invalid": registered.stats.invalid,
                "timeout": registered.stats.timeout,
                "cache_hits": registered.stats.cache_hits,
                "retries": registered.stats.retries,
                "circuit_rejections": registered.stats.circuit_rejections,
                "degraded": registered.stats.degraded,
                "cancelled": registered.stats.cancelled,
                "cache_hit_rate": round(registered.stats.cache_hits / registered.stats.total, 3) if registered.stats.total else 0.0,
                "success_rate": round(registered.stats.success_rate, 3),
                "avg_latency_ms": round(registered.stats.avg_latency_ms, 1),
                "consecutive_fails": registered.stats.consecutive_fails,
                "circuit_state": registered.breaker.state.value,
                **self.monitor.tool_snapshot(name),
            }
            for name, registered in sorted(self._tools.items())
        }

    def get_health(self) -> dict[str, Any]:
        """只读快照：不尝试调用工具、清理缓存或占用半开探测名额。"""
        stats = self.get_stats()
        return {
            "status": "degraded" if any(s["circuit_state"] != "closed" for s in stats.values()) else "ok",
            "cache_size": self.cache_size, "tools": stats,
            "function_calling": self.monitor.loop_snapshot(),
        }

    def fork(self) -> "ToolManager":
        """复制注册表，隔离统计、熔断与缓存；供评测/测试使用。"""
        other = type(self)(
            self._reranker,
            clock=self._clock,
            sleep=self._sleep,
            max_schema_hint_chars=self._max_schema_hint_chars,
            cache_max_entries=self._cache_max_entries,
            circuit_failure_threshold=self._circuit_failure_threshold,
            circuit_recovery_s=self._circuit_recovery_s,
        )
        other._cache_versions = {}
        for name, registered in self._tools.items():
            other.monitor.register(name)
            other._tools[name] = RegisteredTool(
                spec=registered.spec,
                breaker=CircuitBreaker(
                    failure_threshold=registered.breaker.threshold,
                    recovery_s=registered.breaker.recovery_s,
                    clock=self._clock,
                ),
            )
        return other


def _build_schema_hint(
    schema: Mapping[str, Any],
    *,
    detail: str,
) -> dict[str, Any]:
    """构造精简 schema；detail=full/compact/minimal 控制字段细节层级。"""
    properties = schema.get("properties", {})
    hint_properties: dict[str, Any] = {}
    for name, field_schema in properties.items():
        if detail == "minimal":
            hint_properties[name] = {}
            continue
        if not isinstance(field_schema, Mapping):
            hint_properties[name] = {}
            continue

        item: dict[str, Any] = {}
        for key in _HINT_FIELD_KEYS:
            if key not in field_schema:
                continue
            if detail == "compact" and key in {"pattern", "minimum", "maximum", "minLength", "maxLength"}:
                continue
            item[key] = copy.deepcopy(field_schema[key])
        hint_properties[name] = item

    return {
        "type": "object",
        "required": list(schema.get("required", [])),
        "additionalProperties": False,
        "properties": hint_properties,
    }


def _hint_size(hint: Mapping[str, Any]) -> int:
    return len(json.dumps(hint, ensure_ascii=False, separators=(",", ":")))


def _require_positive_number(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是大于 0 的数字")


def _require_non_negative_number(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 必须是非负数字")
