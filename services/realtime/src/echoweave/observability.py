"""Small, dependency-free observability primitives for EchoWeave services.

The module intentionally does not expose a process-global registry.  Gateways and
workers can own their lifecycle explicitly, which keeps tests isolated and makes
multi-process aggregation an operations concern instead of hidden shared state.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

_METRIC_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_LABEL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_NAME_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
_MAX_LABELS = 8
_MAX_LABEL_VALUE_LENGTH = 128
_MAX_HEALTH_METADATA_FIELDS = 16
_MAX_HEALTH_TEXT_LENGTH = 512


class MetricCardinalityError(RuntimeError):
    """Raised before a new time series would exceed the configured bound."""


class HealthStatus(str, Enum):
    """Status values used by component and aggregate health snapshots."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


def _finite_nonnegative(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def _validate_name(value: str, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_sensitive_name(value: str) -> bool:
    lowered = value.lower()
    return any(part in lowered for part in _SENSITIVE_NAME_PARTS)


def _normalise_labels(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if labels is None:
        return ()
    if not isinstance(labels, Mapping):
        raise TypeError("labels must be a mapping")
    if len(labels) > _MAX_LABELS:
        raise ValueError(f"metrics may have at most {_MAX_LABELS} labels")

    normalised: list[tuple[str, str]] = []
    for raw_name, raw_value in labels.items():
        name = _validate_name(raw_name, _LABEL_NAME, "label name")
        if _is_sensitive_name(name):
            raise ValueError(f"sensitive label names are not allowed: {name}")
        if not isinstance(raw_value, str):
            raise TypeError(f"label {name!r} must be a string")
        if len(raw_value) > _MAX_LABEL_VALUE_LENGTH or _contains_control_characters(
            raw_value
        ):
            raise ValueError(f"invalid value for label {name!r}")
        normalised.append((name, raw_value))
    return tuple(sorted(normalised))


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    rank = (len(sorted_values) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    fraction = rank - lower
    return (
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


class LatencyWindow:
    """Thread-safe latency summary with bounded samples and lifetime totals.

    Percentiles are calculated from the most recent ``max_samples`` values using
    linear interpolation.  ``count`` and ``sum_ms`` remain lifetime totals, while
    ``sample_count`` documents the population represented by the percentiles.
    """

    def __init__(self, max_samples: int = 2_048) -> None:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int):
            raise TypeError("max_samples must be an integer")
        if not 1 <= max_samples <= 1_000_000:
            raise ValueError("max_samples must be between 1 and 1000000")
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._count = 0
        self._sum_ms = 0.0
        self._minimum_ms: float | None = None
        self._maximum_ms: float | None = None
        self._lock = threading.Lock()

    @property
    def max_samples(self) -> int:
        return self._samples.maxlen or 0

    def observe(self, duration_ms: float) -> None:
        value = _finite_nonnegative(duration_ms, "duration_ms")
        with self._lock:
            self._samples.append(value)
            self._count += 1
            self._sum_ms += value
            self._minimum_ms = (
                value if self._minimum_ms is None else min(self._minimum_ms, value)
            )
            self._maximum_ms = (
                value if self._maximum_ms is None else max(self._maximum_ms, value)
            )

    def snapshot(self) -> dict[str, int | float | None]:
        with self._lock:
            values = sorted(self._samples)
            count = self._count
            total = self._sum_ms
            minimum = self._minimum_ms
            maximum = self._maximum_ms
        sample_count = len(values)
        return {
            "count": count,
            "sample_count": sample_count,
            "dropped_samples": max(0, count - sample_count),
            "sum_ms": total,
            "mean_ms": total / count if count else None,
            "min_ms": minimum,
            "max_ms": maximum,
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "p99_ms": _percentile(values, 0.99),
        }


@dataclass(frozen=True, slots=True)
class _SeriesKey:
    name: str
    labels: tuple[tuple[str, str], ...]


class _MetricTimer(
    AbstractContextManager["_MetricTimer"], AbstractAsyncContextManager["_MetricTimer"]
):
    def __init__(
        self,
        recorder: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self._recorder = recorder
        self._clock = clock
        self._started_at: float | None = None

    def __enter__(self) -> _MetricTimer:  # noqa: PYI034 -- Python 3.10 support
        if self._started_at is not None:
            raise RuntimeError("timer cannot be entered more than once")
        self._started_at = self._clock()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._finish()

    async def __aenter__(self) -> _MetricTimer:  # noqa: PYI034 -- Python 3.10 support
        return self.__enter__()

    async def __aexit__(self, *_exc: object) -> None:
        self._finish()

    def _finish(self) -> None:
        if self._started_at is None:
            raise RuntimeError("timer was not started")
        elapsed_ms = max(0.0, (self._clock() - self._started_at) * 1_000)
        self._started_at = None
        self._recorder(elapsed_ms)


class MetricRegistry:
    """Bounded, thread-safe counters, gauges and latency distributions."""

    def __init__(
        self,
        *,
        max_series: int = 512,
        latency_samples_per_series: int = 2_048,
        clock: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_series, bool) or not isinstance(max_series, int):
            raise TypeError("max_series must be an integer")
        if not 1 <= max_series <= 100_000:
            raise ValueError("max_series must be between 1 and 100000")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._max_series = max_series
        self._latency_samples_per_series = latency_samples_per_series
        # Validate eagerly so bad configuration fails before the first request.
        LatencyWindow(latency_samples_per_series)
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._started_at = clock()
        self._counters: dict[_SeriesKey, float] = {}
        self._gauges: dict[_SeriesKey, float] = {}
        self._latencies: dict[_SeriesKey, LatencyWindow] = {}
        self._lock = threading.RLock()

    def increment(
        self,
        name: str,
        value: float = 1,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> float:
        """Atomically increment a monotonically increasing counter."""

        amount = _finite_nonnegative(value, "counter increment")
        key = self._key(name, labels)
        with self._lock:
            self._reserve(key, self._counters)
            updated = self._counters.get(key, 0.0) + amount
            self._counters[key] = updated
            return updated

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Set a gauge to a finite numeric value."""

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("gauge value must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("gauge value must be finite")
        key = self._key(name, labels)
        with self._lock:
            self._reserve(key, self._gauges)
            self._gauges[key] = number

    def observe_latency(
        self,
        name: str,
        duration_ms: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Record one latency value in milliseconds."""

        value = _finite_nonnegative(duration_ms, "duration_ms")
        key = self._key(name, labels)
        with self._lock:
            self._reserve(key, self._latencies)
            window = self._latencies.get(key)
            if window is None:
                window = LatencyWindow(self._latency_samples_per_series)
                self._latencies[key] = window
            window.observe(value)

    def timer(
        self,
        name: str,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> _MetricTimer:
        """Return a sync/async context manager that records elapsed milliseconds."""

        # Validate before the timed operation starts.
        key = self._key(name, labels)
        return _MetricTimer(
            lambda elapsed: self.observe_latency(
                key.name,
                elapsed,
                labels=dict(key.labels),
            ),
            self._clock,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a deterministic JSON-compatible metrics snapshot."""

        with self._lock:
            counters = list(self._counters.items())
            gauges = list(self._gauges.items())
            latencies = list(self._latencies.items())
            uptime_seconds = max(0.0, self._clock() - self._started_at)

        return {
            "schema_version": 1,
            "generated_at": _iso_utc(self._wall_clock()),
            "uptime_seconds": uptime_seconds,
            "series_count": len(counters) + len(gauges) + len(latencies),
            "series_limit": self._max_series,
            "counters": [
                _series_payload(key, value)
                for key, value in sorted(counters, key=_series_sort_key)
            ],
            "gauges": [
                _series_payload(key, value)
                for key, value in sorted(gauges, key=_series_sort_key)
            ],
            "latencies_ms": [
                _series_payload(key, window.snapshot())
                for key, window in sorted(latencies, key=_series_sort_key)
            ],
        }

    def _key(
        self,
        name: str,
        labels: Mapping[str, str] | None,
    ) -> _SeriesKey:
        return _SeriesKey(
            _validate_name(name, _METRIC_NAME, "metric name"),
            _normalise_labels(labels),
        )

    def _reserve(self, key: _SeriesKey, target: Mapping[_SeriesKey, object]) -> None:
        if key in target:
            return
        series_count = len(self._counters) + len(self._gauges) + len(self._latencies)
        if series_count >= self._max_series:
            raise MetricCardinalityError(
                f"metric series limit ({self._max_series}) would be exceeded"
            )


def _series_sort_key(item: tuple[_SeriesKey, object]) -> tuple[object, ...]:
    key = item[0]
    return key.name, key.labels


def _series_payload(key: _SeriesKey, value: object) -> dict[str, object]:
    return {"name": key.name, "labels": dict(key.labels), "value": value}


@dataclass(slots=True)
class _HealthComponent:
    required: bool
    stale_after_seconds: float
    status: HealthStatus = HealthStatus.UNKNOWN
    message: str | None = None
    metadata: dict[str, bool | float | int | str | None] | None = None
    checked_monotonic: float | None = None
    checked_at: str | None = None


class HealthRegistry:
    """Track bounded dependency health without retaining credentials."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        max_components: int = 64,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(max_components, bool) or not isinstance(max_components, int):
            raise TypeError("max_components must be an integer")
        if not 1 <= max_components <= 1_024:
            raise ValueError("max_components must be between 1 and 1024")
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._max_components = max_components
        self._components: dict[str, _HealthComponent] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        *,
        required: bool = True,
        stale_after_seconds: float = 60,
    ) -> None:
        """Register a dependency before recording its checks."""

        component_name = _validate_name(name, _COMPONENT_NAME, "component name")
        if type(required) is not bool:
            raise TypeError("required must be a boolean")
        stale_after = _finite_nonnegative(stale_after_seconds, "stale_after_seconds")
        if stale_after == 0:
            raise ValueError("stale_after_seconds must be greater than zero")
        with self._lock:
            existing = self._components.get(component_name)
            if existing is not None:
                if (
                    existing.required != required
                    or existing.stale_after_seconds != stale_after
                ):
                    raise ValueError(
                        f"component {component_name!r} is already registered differently"
                    )
                return
            if len(self._components) >= self._max_components:
                raise MetricCardinalityError(
                    f"health component limit ({self._max_components}) would be exceeded"
                )
            self._components[component_name] = _HealthComponent(
                required=required,
                stale_after_seconds=stale_after,
            )

    def record(
        self,
        name: str,
        status: HealthStatus | str,
        *,
        message: str | None = None,
        metadata: Mapping[str, bool | float | int | str | None] | None = None,
    ) -> None:
        """Record the latest result for a registered dependency."""

        component_name = _validate_name(name, _COMPONENT_NAME, "component name")
        try:
            health_status = (
                status if isinstance(status, HealthStatus) else HealthStatus(status)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid health status: {status!r}") from exc
        clean_message = _health_text(message, "health message")
        clean_metadata = _health_metadata(metadata)
        checked_monotonic = self._clock()
        checked_at = _iso_utc(self._wall_clock())
        with self._lock:
            component = self._components.get(component_name)
            if component is None:
                raise KeyError(f"health component is not registered: {component_name}")
            component.status = health_status
            component.message = clean_message
            component.metadata = clean_metadata
            component.checked_monotonic = checked_monotonic
            component.checked_at = checked_at

    def snapshot(self) -> dict[str, Any]:
        """Return aggregate readiness plus per-component freshness."""

        now = self._clock()
        with self._lock:
            components = [
                (name, _copy_component(component))
                for name, component in self._components.items()
            ]

        payloads: list[dict[str, Any]] = []
        required_unhealthy = False
        required_not_ready = False
        any_degraded = False
        for name, component in sorted(components, key=lambda item: item[0]):
            age = (
                None
                if component.checked_monotonic is None
                else max(0.0, now - component.checked_monotonic)
            )
            stale = age is not None and age > component.stale_after_seconds
            effective_status = HealthStatus.UNKNOWN if stale else component.status

            if component.required:
                required_unhealthy |= effective_status is HealthStatus.UNHEALTHY
                required_not_ready |= effective_status in {
                    HealthStatus.UNKNOWN,
                    HealthStatus.UNHEALTHY,
                }
                any_degraded |= effective_status is not HealthStatus.HEALTHY
            else:
                any_degraded |= effective_status in {
                    HealthStatus.DEGRADED,
                    HealthStatus.UNHEALTHY,
                }

            payload: dict[str, Any] = {
                "name": name,
                "required": component.required,
                "status": effective_status.value,
                "reported_status": component.status.value,
                "stale": stale,
                "stale_after_seconds": component.stale_after_seconds,
                "age_seconds": age,
                "checked_at": component.checked_at,
            }
            if component.message is not None:
                payload["message"] = component.message
            if component.metadata:
                payload["metadata"] = component.metadata
            payloads.append(payload)

        if not components:
            aggregate = HealthStatus.UNKNOWN
        elif required_unhealthy:
            aggregate = HealthStatus.UNHEALTHY
        elif any_degraded:
            aggregate = HealthStatus.DEGRADED
        else:
            aggregate = HealthStatus.HEALTHY
        return {
            "schema_version": 1,
            "generated_at": _iso_utc(self._wall_clock()),
            "status": aggregate.value,
            "ready": bool(components) and not required_not_ready,
            "components": payloads,
        }


def _copy_component(component: _HealthComponent) -> _HealthComponent:
    return _HealthComponent(
        required=component.required,
        stale_after_seconds=component.stale_after_seconds,
        status=component.status,
        message=component.message,
        metadata=dict(component.metadata) if component.metadata else None,
        checked_monotonic=component.checked_monotonic,
        checked_at=component.checked_at,
    )


def _health_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if len(value) > _MAX_HEALTH_TEXT_LENGTH or _contains_control_characters(value):
        raise ValueError(f"invalid {field}")
    return value


def _health_metadata(
    metadata: Mapping[str, bool | float | int | str | None] | None,
) -> dict[str, bool | float | int | str | None] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise TypeError("health metadata must be a mapping")
    if len(metadata) > _MAX_HEALTH_METADATA_FIELDS:
        raise ValueError(
            f"health metadata may have at most {_MAX_HEALTH_METADATA_FIELDS} fields"
        )
    result: dict[str, bool | float | int | str | None] = {}
    for raw_name, raw_value in metadata.items():
        name = _validate_name(raw_name, _LABEL_NAME, "health metadata name")
        if _is_sensitive_name(name):
            raise ValueError(f"sensitive health metadata names are not allowed: {name}")
        if isinstance(raw_value, str):
            clean_value: bool | float | int | str | None = _health_text(
                raw_value, f"health metadata {name!r}"
            )
        elif (
            raw_value is None
            or isinstance(raw_value, (bool, int))
            or (isinstance(raw_value, float) and math.isfinite(raw_value))
        ):
            clean_value = raw_value
        else:
            raise TypeError(f"health metadata {name!r} must be a JSON scalar")
        result[name] = clean_value
    return dict(sorted(result.items()))


class Observability:
    """Convenience owner for metrics and health registries."""

    def __init__(
        self,
        *,
        metrics: MetricRegistry | None = None,
        health: HealthRegistry | None = None,
    ) -> None:
        self.metrics = metrics or MetricRegistry()
        self.health = health or HealthRegistry()

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "health": self.health.snapshot(),
            "metrics": self.metrics.snapshot(),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.snapshot(),
            ensure_ascii=False,
            allow_nan=False,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


def _iso_utc(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("wall_clock must return a datetime")
    if value.tzinfo is None:
        raise ValueError("wall_clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "HealthRegistry",
    "HealthStatus",
    "LatencyWindow",
    "MetricCardinalityError",
    "MetricRegistry",
    "Observability",
]
