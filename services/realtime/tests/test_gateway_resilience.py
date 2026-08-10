import asyncio
import re
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient as _BaseTestClient

from echoweave.app import _OutboundPump, create_app
from echoweave.config import Settings
from echoweave.runtime import RuntimeAdapters, RuntimeFactory, RuntimeUnavailable

TEST_ACCESS_TOKEN = "gateway-test-access-token-with-at-least-32-bytes"
STRUCTURED_ID = re.compile(
    r"^(ews|evt|err)_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class TestClient(_BaseTestClient):
    """Exercise clear-text gateway behavior from a loopback connection."""

    def __init__(self, app, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        kwargs.setdefault("client", ("127.0.0.1", 50_000))
        super().__init__(app, **kwargs)

    def websocket_connect(self, url, *args, **kwargs):
        if url.startswith("/"):
            url = f"ws://127.0.0.1{url}"
        return super().websocket_connect(url, *args, **kwargs)


def _start_text_only(socket) -> list[dict]:
    socket.send_json(
        {
            "type": "start",
            "persona_id": "demo",
            "access_token": TEST_ACCESS_TOKEN,
            "ai_disclosure_ack": True,
            "protocol": {
                "version": 1,
                "capabilities": [
                    "input.text",
                    "output.text_stream",
                    "control.cancel",
                ],
            },
        }
    )
    return [socket.receive_json() for _ in range(3)]


def _metric_value(snapshot: dict, section: str, name: str):
    return next(
        series["value"] for series in snapshot[section] if series["name"] == name
    )


def test_hello_and_events_have_structured_correlation_and_negotiation(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "session.hello"
        assert STRUCTURED_ID.fullmatch(hello["session_id"])
        assert uuid.UUID(hello["session_id"].removeprefix("ews_")).version == 7
        assert STRUCTURED_ID.fullmatch(hello["event_id"])
        assert hello["sequence"] == 1
        assert hello["protocol"]["supported_versions"] == [1]
        assert "control.cancel" in hello["capabilities"]

        events = _start_text_only(socket)
        assert {event["type"] for event in events} == {
            "session.negotiated",
            "session.state",
            "session.ready",
        }
        assert all(event["session_id"] == hello["session_id"] for event in events)
        negotiated = next(
            event for event in events if event["type"] == "session.negotiated"
        )
        assert negotiated["protocol_version"] == 1
        assert negotiated["capabilities"] == [
            "control.cancel",
            "input.text",
            "output.text_stream",
        ]


def test_unsupported_protocol_error_has_consistent_envelope(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        hello = socket.receive_json()
        socket.send_json(
            {
                "type": "start",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
                "protocol": {"version": 99},
            }
        )
        error = socket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unsupported_protocol_version"
        assert error["category"] == "protocol"
        assert error["fatal"] is False
        assert error["retryable"] is False
        assert error["details"] == {"supported_versions": [1]}
        assert error["session_id"] == hello["session_id"]
        assert STRUCTURED_ID.fullmatch(error["error_id"])
        assert STRUCTURED_ID.fullmatch(error["event_id"])


def test_unstarted_and_unknown_controls_are_never_silent(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json({"type": "text", "text": "too early"})
        assert socket.receive_json()["code"] == "session_not_started"
        socket.send_json({"type": "future.unsupported"})
        assert socket.receive_json()["code"] == "session_not_started"
        socket.send_json({"type": "ping", "client_time_ms": 123})
        pong = socket.receive_json()
        assert pong["type"] == "session.pong"
        assert pong["client_time_ms"] == 123
        socket.send_json({"type": "ping", "client_time_ms": 10**400})
        oversized_time = socket.receive_json()
        assert oversized_time["type"] == "session.pong"
        assert "client_time_ms" not in oversized_time
        socket.send_text('{"type":"ping","client_time_ms":NaN}')
        assert socket.receive_json()["code"] == "invalid_control_json"


def test_control_message_token_bucket_closes_abusive_client(tmp_path):
    app = create_app(
        Settings(
            persona_root=tmp_path,
            access_token=TEST_ACCESS_TOKEN,
            control_rate_burst=1,
            control_rate_per_second=0.1,
        )
    )
    with TestClient(app).websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_text("not-json")
        assert socket.receive_json()["code"] == "invalid_control_json"
        socket.send_text("still-not-json")
        limited = socket.receive_json()
        assert limited["code"] == "control_rate_exceeded"
        assert limited["fatal"] is True
        close = socket.receive()
        assert close["type"] == "websocket.close"
        assert close["code"] == 1008


def test_absolute_start_deadline_is_configurable(tmp_path):
    app = create_app(
        Settings(
            persona_root=tmp_path,
            access_token=TEST_ACCESS_TOKEN,
            session_start_timeout_seconds=0.1,
        )
    )
    with TestClient(app).websocket_connect("/ws") as socket:
        socket.receive_json()
        time.sleep(0.15)
        timeout = socket.receive_json()
        assert timeout["code"] == "start_timeout"
        assert timeout["fatal"] is True
        assert timeout["retryable"] is True


def test_liveness_readiness_and_restricted_metrics_are_separate(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app) as client:
        live = client.get("/api/health/live")
        assert live.json()["ok"] is True
        assert live.headers["cache-control"] == "no-store"
        ready = client.get("/api/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.headers["cache-control"] == "no-store"
        denied_metrics = client.get("/api/metrics")
        assert denied_metrics.status_code == 403
        assert denied_metrics.headers["cache-control"] == "no-store"

        with client.websocket_connect("/ws") as socket:
            socket.receive_json()

        metrics = client.get(
            "/api/metrics",
            headers={"Authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
        )
        assert metrics.status_code == 200
        assert metrics.headers["cache-control"] == "no-store"
        snapshot = metrics.json()
        assert _metric_value(snapshot, "counters", "gateway.sessions.total") == 1
        assert _metric_value(snapshot, "gauges", "gateway.sessions.active") == 0
        assert TEST_ACCESS_TOKEN not in metrics.text


def test_pipeline_latency_metrics_flow_into_gateway_registry(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            _start_text_only(socket)
            socket.send_json({"type": "text", "text": "metrics integration"})
            saw_turn_metrics = False
            while True:
                event = socket.receive_json()
                saw_turn_metrics |= event["type"] == "turn.metrics"
                if (
                    event["type"] == "session.state"
                    and event["state"] == "listening"
                    and event["turn_id"] == 1
                ):
                    break
            assert saw_turn_metrics

        snapshot = client.get(
            "/api/metrics",
            headers={"Authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
        ).json()
        assert any(
            series["name"] == "echoweave.stage_latency"
            and series["labels"] == {"component": "turn", "outcome": "success"}
            for series in snapshot["latencies_ms"]
        )


class _SlowSocket:
    async def send_json(self, _payload):
        await asyncio.sleep(1)

    async def send_bytes(self, _payload):
        await asyncio.sleep(1)


class _BrokenSocket:
    async def send_json(self, _payload):
        raise ValueError("synthetic transport failure")

    async def send_bytes(self, _payload):
        raise ValueError("synthetic transport failure")


async def test_outbound_pump_times_out_slow_client():
    failures = []
    pump = _OutboundPump(
        _SlowSocket(),
        max_messages=8,
        max_bytes=1024,
        send_timeout_seconds=0.01,
        on_failure=failures.append,
    )
    pump.start()
    await pump.send_json({"type": "test"})
    await asyncio.wait_for(pump.wait_failed(), timeout=0.5)
    assert failures == ["send_timeout"]
    assert pump.failure is not None
    await pump.stop()


async def test_outbound_pump_rejects_queue_overflow_before_allocating_more():
    failures = []
    pump = _OutboundPump(
        _SlowSocket(),
        max_messages=1,
        max_bytes=1024,
        send_timeout_seconds=1,
        on_failure=failures.append,
    )
    await pump.send_json({"type": "first"})
    with pytest.raises(ConnectionError):
        await pump.send_json({"type": "second"})
    assert failures == ["slow_client"]
    await pump.stop()


async def test_outbound_pump_normalizes_unexpected_sender_failure():
    failures = []
    pump = _OutboundPump(
        _BrokenSocket(),
        max_messages=8,
        max_bytes=1024,
        send_timeout_seconds=1,
        on_failure=failures.append,
    )
    pump.start()
    await pump.send_json({"type": "test"})
    await asyncio.wait_for(pump.wait_failed(), timeout=0.5)
    assert failures == ["disconnect"]
    assert pump.failure is not None
    await pump.stop()


async def test_runtime_timeout_does_not_duplicate_or_cancel_thread_build(
    monkeypatch,
    tmp_path,
):
    builds = 0

    def slow_build(_settings):
        nonlocal builds
        builds += 1
        time.sleep(0.05)
        adapters = object()
        return RuntimeAdapters(adapters, adapters, adapters, adapters, adapters)

    monkeypatch.setattr("echoweave.runtime.build_runtime", slow_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    with pytest.raises(RuntimeUnavailable):
        await factory.create(0.01)
    assert factory.readiness()["ready"] is False
    await asyncio.sleep(0.08)
    assert factory.readiness()["ready"] is True
    assert builds == 1
    await factory.aclose()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
async def test_runtime_factory_rejects_invalid_timeout(timeout, tmp_path):
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await factory.create(timeout)


async def test_runtime_timeout_result_is_closed_during_factory_shutdown(
    monkeypatch,
    tmp_path,
):
    class CloseProbe:
        def __init__(self):
            self.close_count = 0

        async def aclose(self):
            self.close_count += 1

    probe = CloseProbe()

    def slow_build(_settings):
        time.sleep(0.03)
        return RuntimeAdapters(probe, probe, probe, probe, probe)

    monkeypatch.setattr("echoweave.runtime.build_runtime", slow_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    with pytest.raises(RuntimeUnavailable):
        await factory.create(0.005)
    await asyncio.sleep(0.05)

    await factory.aclose(timeout_seconds=1)

    assert probe.close_count == 1
    assert factory.readiness()["state"] == "closed"
    assert factory.readiness()["ready"] is False
    with pytest.raises(RuntimeUnavailable, match="rt_factory_closing"):
        await factory.acquire(1)


async def test_cancelled_runtime_request_retains_eventual_build_for_cleanup(
    monkeypatch,
    tmp_path,
):
    class CloseProbe:
        def __init__(self):
            self.close_count = 0

        async def aclose(self):
            self.close_count += 1

    probe = CloseProbe()

    def slow_build(_settings):
        time.sleep(0.03)
        return RuntimeAdapters(probe, probe, probe, probe, probe)

    monkeypatch.setattr("echoweave.runtime.build_runtime", slow_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    request = asyncio.create_task(factory.create(1))
    await asyncio.sleep(0.005)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.sleep(0.05)

    assert factory.readiness()["ready"] is True
    await factory.aclose(timeout_seconds=1)
    assert probe.close_count == 1


async def test_runtime_factory_single_flights_twelve_cancelled_waiters(
    monkeypatch,
    tmp_path,
):
    started = threading.Event()
    release_build = threading.Event()
    builds = 0
    closed = 0
    counter_lock = threading.Lock()

    class CloseProbe:
        async def aclose(self):
            nonlocal closed
            closed += 1

    def blocked_build(_settings):
        nonlocal builds
        with counter_lock:
            builds += 1
        started.set()
        if not release_build.wait(timeout=5):
            raise RuntimeError("test did not release runtime construction")
        probe = CloseProbe()
        return RuntimeAdapters(probe, probe, probe, probe, probe)

    monkeypatch.setattr("echoweave.runtime.build_runtime", blocked_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    requests = [
        asyncio.create_task(factory.create(2) if index % 2 == 0 else factory.acquire(2))
        for index in range(12)
    ]
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()
    await asyncio.sleep(0.02)
    assert all(not request.done() for request in requests)
    assert builds == 1
    inflight = factory._build_task
    assert inflight is not None

    for request in requests:
        request.cancel()
    results = await asyncio.gather(*requests, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert factory._build_task is inflight

    release_build.set()
    await asyncio.wait_for(asyncio.shield(inflight), timeout=1)
    assert builds == 1
    assert factory._build_task is None

    adapters = await factory.acquire(0.1)
    assert builds == 1
    assert await factory.release(adapters, timeout_seconds=1) is True
    await factory.aclose(timeout_seconds=1)
    assert closed == 1


async def test_runtime_factory_serializes_live_waiters_without_sharing_adapters(
    monkeypatch,
    tmp_path,
):
    builds = 0
    active_builds = 0
    maximum_active_builds = 0
    closed = 0
    counter_lock = threading.Lock()

    class CloseProbe:
        async def aclose(self):
            nonlocal closed
            closed += 1

    def isolated_build(_settings):
        nonlocal builds, active_builds, maximum_active_builds
        with counter_lock:
            builds += 1
            active_builds += 1
            maximum_active_builds = max(maximum_active_builds, active_builds)
        try:
            time.sleep(0.005)
            probe = CloseProbe()
            return RuntimeAdapters(probe, probe, probe, probe, probe)
        finally:
            with counter_lock:
                active_builds -= 1

    monkeypatch.setattr("echoweave.runtime.build_runtime", isolated_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    requests = [
        factory.create(2) if index % 2 == 0 else factory.acquire(2)
        for index in range(12)
    ]
    adapters = await asyncio.gather(*requests)

    assert builds == 12
    assert maximum_active_builds == 1
    assert len({id(runtime.vad) for runtime in adapters}) == 12
    released = await asyncio.gather(
        *(factory.release(runtime, timeout_seconds=1) for runtime in adapters)
    )
    assert all(released)
    assert closed == 12
    await factory.aclose(timeout_seconds=1)


async def test_runtime_factory_failed_single_flight_can_retry_safely(
    monkeypatch,
    tmp_path,
):
    first_started = threading.Event()
    release_failure = threading.Event()
    builds = 0
    closed = 0
    counter_lock = threading.Lock()

    class CloseProbe:
        async def aclose(self):
            nonlocal closed
            closed += 1

    def flaky_build(_settings):
        nonlocal builds
        with counter_lock:
            builds += 1
            attempt = builds
        if attempt == 1:
            first_started.set()
            if not release_failure.wait(timeout=5):
                raise RuntimeError("test did not release failed construction")
            raise RuntimeError("synthetic construction failure")
        probe = CloseProbe()
        return RuntimeAdapters(probe, probe, probe, probe, probe)

    monkeypatch.setattr("echoweave.runtime.build_runtime", flaky_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    requests = [asyncio.create_task(factory.create(2)) for _ in range(12)]
    for _ in range(100):
        if first_started.is_set():
            break
        await asyncio.sleep(0.001)
    assert first_started.is_set()
    await asyncio.sleep(0.02)
    assert all(not request.done() for request in requests)
    release_failure.set()

    results = await asyncio.gather(*requests, return_exceptions=True)
    assert builds == 1
    assert all(isinstance(result, RuntimeUnavailable) for result in results)
    assert factory._build_task is None
    assert factory.readiness()["state"] == "unavailable"

    adapters = await factory.acquire(1)
    assert builds == 2
    assert factory.readiness()["ready"] is True
    assert await factory.release(adapters, timeout_seconds=1) is True
    await factory.aclose(timeout_seconds=1)
    assert closed == 1


async def test_runtime_factory_close_race_owns_and_closes_shared_build(
    monkeypatch,
    tmp_path,
):
    started = threading.Event()
    release_build = threading.Event()
    builds = 0
    closed = 0
    counter_lock = threading.Lock()

    class CloseProbe:
        async def aclose(self):
            nonlocal closed
            await asyncio.sleep(0)
            closed += 1

    def blocked_build(_settings):
        nonlocal builds
        with counter_lock:
            builds += 1
        started.set()
        if not release_build.wait(timeout=5):
            raise RuntimeError("test did not release close-race construction")
        probe = CloseProbe()
        return RuntimeAdapters(probe, probe, probe, probe, probe)

    monkeypatch.setattr("echoweave.runtime.build_runtime", blocked_build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    requests = [asyncio.create_task(factory.acquire(2)) for _ in range(12)]
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()
    await asyncio.sleep(0.02)
    assert all(not request.done() for request in requests)

    close_task = asyncio.create_task(factory.aclose(timeout_seconds=1))
    await asyncio.sleep(0)
    assert factory.readiness()["state"] == "closed"
    release_build.set()
    await asyncio.wait_for(close_task, timeout=1)
    results = await asyncio.gather(*requests, return_exceptions=True)

    assert builds == 1
    assert closed == 1
    assert all(isinstance(result, RuntimeUnavailable) for result in results)
    assert factory._build_task is None
    assert factory._prepared is None
    with pytest.raises(RuntimeUnavailable, match="rt_factory_closing"):
        await factory.acquire(1)


async def test_runtime_factory_shutdown_cleanup_survives_caller_cancellation(
    monkeypatch,
    tmp_path,
):
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    closed = 0

    class CloseProbe:
        async def aclose(self):
            nonlocal closed
            close_started.set()
            await release_close.wait()
            closed += 1

    def build(_settings):
        probe = CloseProbe()
        return RuntimeAdapters(probe, probe, probe, probe, probe)

    monkeypatch.setattr("echoweave.runtime.build_runtime", build)
    factory = RuntimeFactory(Settings(persona_root=tmp_path))
    await factory.prepare(timeout_seconds=1)

    initiating_close = asyncio.create_task(factory.aclose(timeout_seconds=1))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    initiating_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initiating_close
    assert factory._adapter_cleanup_task is not None
    assert not factory._adapter_cleanup_task.done()

    release_close.set()
    await factory.aclose(timeout_seconds=1)
    assert closed == 1
    assert factory._adapter_cleanup_task is None
    assert factory._has_owned_resources() is False


def test_candidate_session_failure_closes_resources_and_injects_metrics(
    monkeypatch,
    tmp_path,
):
    instances = []

    class FailingSession:
        def __init__(self, session_id, _persona, _adapters, *_args, **kwargs):
            self.session_id = session_id
            self.observability = kwargs.get("observability")
            self.closed = 0
            instances.append(self)

        async def start(self):
            raise RuntimeError("synthetic start failure")

        async def close(self):
            self.closed += 1

    monkeypatch.setattr("echoweave.app.RealtimeSession", FailingSession)
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))

    with TestClient(app).websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
            }
        )
        assert socket.receive_json()["type"] == "session.negotiated"
        failure = socket.receive_json()
        assert failure["code"] == "internal_gateway_error"
        assert failure["fatal"] is True

    assert len(instances) == 1
    assert instances[0].closed == 1
    assert instances[0].observability is app.state.observability


def test_candidate_construction_failure_releases_acquired_runtime(
    monkeypatch,
    tmp_path,
):
    class CloseProbe:
        def __init__(self):
            self.close_count = 0

        async def aclose(self):
            self.close_count += 1

    class FailingSession:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("synthetic constructor failure")

    probe = CloseProbe()
    monkeypatch.setattr(
        "echoweave.runtime.build_runtime",
        lambda _settings: RuntimeAdapters(probe, probe, probe, probe, probe),
    )
    monkeypatch.setattr("echoweave.app.RealtimeSession", FailingSession)
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))

    with TestClient(app).websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
            }
        )
        failure = socket.receive_json()
        assert failure["code"] == "internal_gateway_error"
        assert failure["fatal"] is True

    assert probe.close_count == 1


def test_started_session_disconnect_closes_adapters_once(monkeypatch, tmp_path):
    class CloseProbe:
        def __init__(self):
            self.close_count = 0
            self.closed = threading.Event()

        async def aclose(self):
            self.close_count += 1
            self.closed.set()

    probe = CloseProbe()
    monkeypatch.setattr(
        "echoweave.runtime.build_runtime",
        lambda _settings: RuntimeAdapters(probe, probe, probe, probe, probe),
    )
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            _start_text_only(socket)
        assert probe.closed.wait(timeout=1)

    assert probe.close_count == 1
