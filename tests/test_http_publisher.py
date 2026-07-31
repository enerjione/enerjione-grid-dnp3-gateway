from __future__ import annotations

from typing import Any

import pytest
import requests

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.messaging.http_publisher import (
    HttpTelemetryNotReadyError,
    HttpTelemetryPublisher,
    HttpTelemetryPublishError,
)


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Session:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_json: Any = None
        self.closed = False

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: Any,
        timeout: float,
    ) -> _Response:
        _ = timeout
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


def _identity() -> GatewayIdentity:
    return GatewayIdentity(
        gateway_code="GW-001",
        token="secret-token-123",
        instance_id="inst-1",
        app_version="0.0-test",
        app_environment="development",
    )


def test_http_publisher_posts_legacy_telemetry_list() -> None:
    session = _Session(_Response(202, '{"accepted":1}'))
    publisher = HttpTelemetryPublisher(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=session,  # type: ignore[arg-type]
    )
    payload = {
        "message_id": "m1",
        "device_code": "D1",
        "signal_key": "s1",
        "value": 1.0,
        "source_timestamp": "2026-07-17T00:00:00+00:00",
    }

    publisher.publish(payload, message_id="m1", correlation_id="c1")

    assert session.last_url == "http://backend/api/v1/telemetry/gateway/GW-001"
    assert session.last_json == [payload]
    assert session.last_headers is not None
    assert session.last_headers["X-Gateway-Token"] == "secret-token-123"
    assert session.last_headers["X-Gateway-Code"] == "GW-001"
    assert session.last_headers["X-Correlation-Id"] == "c1"
    assert publisher.counters_snapshot() == {"publish_failures": 0, "publish_successes": 1}
    assert publisher.is_ready is True


def test_http_publisher_batch_single_post() -> None:
    """publish_batch tum payload'lari TEK POST'ta (json=[p1,p2,...]) gonderir."""
    session = _Session(_Response(202, '{"accepted":3}'))
    publisher = HttpTelemetryPublisher(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=session,  # type: ignore[arg-type]
    )
    p1 = {"message_id": "m1", "device_code": "D1", "signal_key": "s1", "value": 1.0}
    p2 = {"message_id": "m2", "device_code": "D1", "signal_key": "s2", "value": 2.0}
    p3 = {"message_id": "m3", "device_code": "D1", "signal_key": "s3", "value": 3.0}
    items = [
        {"payload": p1, "message_id": "m1", "correlation_id": "c1", "headers": {}},
        {"payload": p2, "message_id": "m2", "correlation_id": "c1", "headers": {}},
        {"payload": p3, "message_id": "m3", "correlation_id": "c1", "headers": {}},
    ]

    publisher.publish_batch(items)

    assert session.last_json == [p1, p2, p3]  # tek POST, 3 payload
    assert session.last_headers["X-Correlation-Id"] == "c1"
    # 3 basari sayilir
    assert publisher.counters_snapshot() == {"publish_failures": 0, "publish_successes": 3}


def test_http_publisher_batch_transient_failure_raises() -> None:
    """Batch POST gecici hata verirse NotReady raise (caller outbox'a yazar)."""
    publisher = HttpTelemetryPublisher(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=_Session(requests.Timeout("boom")),  # type: ignore[arg-type]
    )
    items = [{"payload": {"message_id": "m1"}, "message_id": "m1", "correlation_id": None, "headers": {}}]
    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish_batch(items)
    assert publisher.is_ready is False


def test_http_publisher_raises_not_ready_for_transient_failure() -> None:
    publisher = HttpTelemetryPublisher(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=_Session(requests.Timeout("boom secret-token-123")),  # type: ignore[arg-type]
    )

    with pytest.raises(HttpTelemetryNotReadyError, match="REDACTED"):
        publisher.publish({"message_id": "m1"}, message_id="m1")

    assert publisher.counters_snapshot()["publish_failures"] == 1
    assert publisher.is_ready is False


def test_http_publisher_raises_publish_error_for_rejected_payload() -> None:
    publisher = HttpTelemetryPublisher(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=_Session(_Response(400, "bad payload")),  # type: ignore[arg-type]
    )

    with pytest.raises(HttpTelemetryPublishError, match="HTTP 400"):
        publisher.publish({"message_id": "m1"}, message_id="m1")

    assert publisher.counters_snapshot()["publish_failures"] == 1
    assert publisher.is_ready is True
