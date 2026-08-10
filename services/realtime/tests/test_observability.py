import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from echoweave.observability import (
    HealthRegistry,
    HealthStatus,
    LatencyWindow,
    MetricCardinalityError,
    MetricRegistry,
    Observability,
)

FIXED_WALL_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_latency_window_is_bounded_and_reports_lifetime_totals():
    window = LatencyWindow(max_samples=3)
    for value in (10, 20, 30, 40):
        window.observe(value)

    snapshot = window.snapshot()
    assert snapshot == {
        "count": 4,
        "sample_count": 3,
        "dropped_samples": 1,
        "sum_ms": 100.0,
        "mean_ms": 25.0,
        "min_ms": 10.0,
        "max_ms": 40.0,
        "p50_ms": 30.0,
        "p95_ms": 39.0,
        "p99_ms": 39.8,
    }


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "1"])
def test_latency_window_rejects_invalid_values(value):
    with pytest.raises((TypeError, ValueError)):
        LatencyWindow().observe(value)


def test_metric_registry_structures_series_and_times_sync_and_async_blocks():
    clock = FakeClock(100.0)
    registry = MetricRegistry(
        clock=clock,
        wall_clock=lambda: FIXED_WALL_TIME,
        latency_samples_per_series=4,
    )
    labels = {"backend": "deepseek", "outcome": "ok"}
    assert registry.increment("turns.total", labels=labels) == 1
    assert registry.increment("turns.total", 2, labels=labels) == 3
    registry.set_gauge("sessions.active", 4)

    with registry.timer("turn.latency", labels={"stage": "llm"}):
        clock.advance(0.125)

    snapshot = registry.snapshot()
    assert snapshot["generated_at"] == "2026-08-01T12:00:00Z"
    assert snapshot["uptime_seconds"] == pytest.approx(0.125)
    assert snapshot["series_count"] == 3
    assert snapshot["counters"] == [
        {
            "name": "turns.total",
            "labels": {"backend": "deepseek", "outcome": "ok"},
            "value": 3.0,
        }
    ]
    assert snapshot["gauges"][0]["value"] == 4.0
    latency = snapshot["latencies_ms"][0]
    assert latency["name"] == "turn.latency"
    assert latency["labels"] == {"stage": "llm"}
    assert latency["value"]["p99_ms"] == pytest.approx(125.0)


@pytest.mark.asyncio
async def test_metric_timer_supports_async_context_manager():
    clock = FakeClock()
    registry = MetricRegistry(clock=clock, wall_clock=lambda: FIXED_WALL_TIME)
    async with registry.timer("worker.latency"):
        clock.advance(0.01)
    assert registry.snapshot()["latencies_ms"][0]["value"]["mean_ms"] == 10


def test_metric_registry_is_thread_safe_and_bounds_cardinality():
    registry = MetricRegistry(
        max_series=2,
        clock=FakeClock(),
        wall_clock=lambda: FIXED_WALL_TIME,
    )

    def increment_many():
        for _ in range(1_000):
            registry.increment("requests.total", labels={"route": "ws"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: increment_many(), range(8)))

    assert registry.snapshot()["counters"][0]["value"] == 8_000
    registry.set_gauge("sessions.active", 1)
    with pytest.raises(MetricCardinalityError):
        registry.increment("errors.total")


@pytest.mark.parametrize(
    ("name", "labels"),
    [
        ("BadName", None),
        ("valid.name", {"access_token": "do-not-store"}),
        ("valid.name", {"route": "line\nbreak"}),
        ("valid.name", {"route": 7}),
    ],
)
def test_metric_names_and_labels_are_safe(name, labels):
    registry = MetricRegistry()
    with pytest.raises((TypeError, ValueError)):
        registry.increment(name, labels=labels)


def test_health_snapshot_tracks_readiness_degradation_and_staleness():
    clock = FakeClock()
    health = HealthRegistry(
        clock=clock,
        wall_clock=lambda: FIXED_WALL_TIME,
    )
    health.register("deepseek", required=True, stale_after_seconds=5)
    health.register("avatar-worker", required=False, stale_after_seconds=10)

    initial = health.snapshot()
    assert initial["status"] == "degraded"
    assert initial["ready"] is False

    health.record(
        "deepseek",
        HealthStatus.HEALTHY,
        message="reachable",
        metadata={"latency_ms": 23.5, "model": "deepseek-chat"},
    )
    healthy = health.snapshot()
    assert healthy["status"] == "healthy"
    assert healthy["ready"] is True
    assert healthy["components"][1]["metadata"]["latency_ms"] == 23.5

    health.record("avatar-worker", "unhealthy", message="worker timeout")
    degraded = health.snapshot()
    assert degraded["status"] == "degraded"
    assert degraded["ready"] is True

    clock.advance(6)
    stale = health.snapshot()
    deepseek = next(
        component
        for component in stale["components"]
        if component["name"] == "deepseek"
    )
    assert deepseek["reported_status"] == "healthy"
    assert deepseek["status"] == "unknown"
    assert deepseek["stale"] is True
    assert stale["ready"] is False


def test_health_required_failure_is_unhealthy_and_sensitive_metadata_is_rejected():
    health = HealthRegistry(wall_clock=lambda: FIXED_WALL_TIME)
    health.register("asr")
    with pytest.raises(ValueError, match="sensitive"):
        health.record("asr", "healthy", metadata={"api_key": "secret"})
    health.record("asr", "unhealthy", message="timeout")
    snapshot = health.snapshot()
    assert snapshot["status"] == "unhealthy"
    assert snapshot["ready"] is False
    assert "secret" not in json.dumps(snapshot)


def test_observability_snapshot_is_strict_json_without_non_finite_values():
    metrics = MetricRegistry(wall_clock=lambda: FIXED_WALL_TIME)
    health = HealthRegistry(wall_clock=lambda: FIXED_WALL_TIME)
    health.register("gateway")
    health.record("gateway", "healthy")
    metrics.increment("sessions.total")
    observability = Observability(metrics=metrics, health=health)

    encoded = observability.to_json()
    decoded = json.loads(encoded)
    assert decoded["schema_version"] == 1
    assert decoded["health"]["ready"] is True
    assert decoded["metrics"]["counters"][0]["name"] == "sessions.total"
