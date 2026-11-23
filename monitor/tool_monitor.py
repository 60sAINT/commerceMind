"""有界进程内聚合；不保留请求 ID、用户身份、参数或结果。"""
from __future__ import annotations

import json
import math
from collections import Counter, deque
from typing import Any

LATENCY_WINDOW = 256


def latency_snapshot(samples: deque[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "latency_samples": len(ordered),
        "p95_latency_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3) if ordered else 0.0,
    }


class ToolMonitor:
    def __init__(self) -> None:
        self._tools: dict[str, deque[float]] = {}
        self._errors: dict[str, Counter[str]] = {}
        self._in_flight: Counter[str] = Counter()
        self._loop = Counter()
        self._loop_latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._loop_latency_ms = 0.0

    def register(self, name: str) -> None:
        self._tools[name] = deque(maxlen=LATENCY_WINDOW)
        self._errors[name] = Counter()
        self._in_flight[name] = 0

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._errors.pop(name, None)
        self._in_flight.pop(name, None)

    def start_tool(self, name: str) -> None:
        if name in self._tools:
            self._in_flight[name] += 1

    def start_loop(self) -> None:
        self._loop["in_flight"] += 1

    def record_tool(self, name: str, latency_ms: float, error_code: str | None) -> None:
        if name not in self._tools:
            return
        self._in_flight[name] = max(0, self._in_flight[name] - 1)
        self._tools[name].append(latency_ms)
        if error_code:
            self._errors[name][error_code] += 1

    def tool_snapshot(self, name: str) -> dict[str, Any]:
        return {
            **latency_snapshot(self._tools[name]),
            "errors": dict(sorted(self._errors[name].items())),
            "in_flight": self._in_flight[name],
        }

    def record_loop(self, outcome: str, latency_ms: float, summary: dict[str, Any]) -> None:
        self._loop["total"] += 1
        self._loop["in_flight"] = max(0, self._loop["in_flight"] - 1)
        self._loop[outcome] += 1
        self._loop_latencies.append(latency_ms)
        self._loop_latency_ms += latency_ms
        for key in ("calls", "rounds", "repairs", "input_tokens", "output_tokens"):
            self._loop[key] += summary.get(key, 0)
        self._loop["protocol_fallbacks"] += int(summary.get("protocol_fallback", False))
        for trace in summary.get("tool_traces", []):
            code = trace.get("error_code")
            if code == "duplicate_tool_call":
                self._loop["duplicate_rejections"] += 1
            elif code == "too_many_tool_calls":
                self._loop["limit_rejections"] += 1

    def loop_snapshot(self) -> dict[str, Any]:
        keys = (
            "total", "success", "failed", "cancelled", "aborted", "in_flight", "calls", "rounds", "repairs",
            "protocol_fallbacks", "duplicate_rejections", "limit_rejections", "input_tokens", "output_tokens",
        )
        return {
            **{key: self._loop[key] for key in keys},
            "avg_latency_ms": round(self._loop_latency_ms / self._loop["total"], 3) if self._loop["total"] else 0.0,
            **latency_snapshot(self._loop_latencies),
        }


def prometheus_metrics(snapshot: dict[str, Any]) -> str:
    """标准 Prometheus 文本格式，标签仅使用注册工具名和有限的状态/错误码。"""
    lines: list[str] = []

    def family(name, kind, help_text, rows):
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])
        for value, labels in rows:
            label_text = ",".join(f"{key}={json.dumps(val)}" for key, val in labels.items())
            lines.append(f"{name}" + ("{" + label_text + "}" if labels else "") + f" {value}")

    tools = snapshot["tools"]
    family("commercemind_tools_available", "gauge", "Tool registry initialized.",
           [(int(snapshot["status"] in {"ok", "degraded"}), {})])
    family("commercemind_tool_calls_total", "counter", "Logical calls excluding invalid parameters.",
           [(s["total"], {"tool": name}) for name, s in tools.items()])
    family("commercemind_tool_in_flight", "gauge", "Currently executing logical tool calls.",
           [(s["in_flight"], {"tool": name}) for name, s in tools.items()])
    for field in (
        "attempts", "success", "failed", "invalid", "cancelled", "timeout", "cache_hits", "retries",
        "circuit_rejections", "degraded",
    ):
        family(f"commercemind_tool_{field}_total", "counter", f"Tool {field} count.",
               [(s[field], {"tool": name}) for name, s in tools.items()])
    family("commercemind_tool_errors_total", "counter", "Logical outcomes by error code.",
           [(count, {"tool": name, "code": code}) for name, s in tools.items() for code, count in s["errors"].items()])
    family("commercemind_tool_latency_p95_seconds", "gauge", "P95 of the last 256 observed calls.",
           [(s["p95_latency_ms"] / 1000, {"tool": name}) for name, s in tools.items()])
    family("commercemind_tool_circuit_state", "gauge", "One-hot circuit breaker state without probing.",
           [(int(s["circuit_state"] == state), {"tool": name, "state": state})
            for name, s in tools.items() for state in ("closed", "open", "half_open")])
    family("commercemind_tool_cache_entries", "gauge", "Entries including lazily expired cache entries.",
           [(snapshot["cache_size"], {})])
    loop = snapshot["function_calling"]
    family("commercemind_function_calling_total", "counter", "Completed or interrupted tool-loop requests.",
           [(loop[state], {"outcome": state}) for state in ("success", "failed", "cancelled", "aborted")])
    family("commercemind_function_calling_in_flight", "gauge", "Currently active tool-loop requests.",
           [(loop["in_flight"], {})])
    for field in ("calls", "rounds", "repairs", "protocol_fallbacks", "duplicate_rejections", "limit_rejections"):
        family(f"commercemind_function_calling_{field}_total", "counter", f"Function calling {field}.",
               [(loop[field], {})])
    for field in ("input_tokens", "output_tokens"):
        family(f"commercemind_tool_phase_{field}_total", "counter", "Non-streaming tool-phase tokens only.",
               [(loop[field], {})])
    family("commercemind_function_calling_latency_p95_seconds", "gauge", "P95 of the last 256 tool-loop requests.",
           [(loop["p95_latency_ms"] / 1000, {})])
    return "\n".join(lines) + "\n"
