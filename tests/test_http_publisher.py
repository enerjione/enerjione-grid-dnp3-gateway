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


# --------------------------------------------------------------------------
# devre kesici: backend erisilemezken hizli hata
# --------------------------------------------------------------------------


class _SayanSession(_Session):
    """POST cagrilarini sayar — devre acikken ag'a gidilmedigini dogrular."""

    def __init__(self, response: _Response | Exception) -> None:
        super().__init__(response)
        self.post_sayisi = 0

    def post(self, url: str, **kwargs: Any) -> _Response:  # type: ignore[override]
        self.post_sayisi += 1
        return super().post(url, **kwargs)


def _yayinci(session: Any) -> HttpTelemetryPublisher:
    return HttpTelemetryPublisher(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=session,
    )


def _payload() -> dict[str, Any]:
    return {"message_id": "m", "device_code": "D1", "signal_key": "s", "value": 1.0}


def test_devre_acikken_aga_gidilmez() -> None:
    """REGRESYON: `_ready` tutuluyordu ama HIC OKUNMUYORDU.

    Backend erisilemezken her cihaz icin yeni bir POST deneniyor ve TAM
    timeout kadar bekleniyordu. Kara delik olmus bir agda (4G kopmasi,
    firewall drop) SYN cevapsiz kalir; 300 cihazli bir cycle timeout duvarina
    toslar, worker havuzu dolar ve kuyruktaki cihazlar HIC yoklanamaz.
    """
    session = _SayanSession(requests.ConnectionError("baglanti yok"))
    publisher = _yayinci(session)

    # Ilk deneme gercekten ag'a gider ve basarisiz olur -> devre acilir
    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish(_payload(), message_id="m1")
    assert session.post_sayisi == 1
    assert publisher.is_ready is False

    # Sonraki denemeler ag'a HIC gitmemeli (hizli hata)
    for _ in range(20):
        with pytest.raises(HttpTelemetryNotReadyError):
            publisher.publish(_payload(), message_id="m2")
    assert session.post_sayisi == 1, (
        f"devre acikken {session.post_sayisi - 1} gereksiz POST denendi"
    )

    # Hata GECICI kalmali: mesaj outbox'a yazilir, retry_count artmaz,
    # dead-letter'a dusmez. Veri kaybi yok.
    from dnp3_gateway.messaging.errors import is_transient

    try:
        publisher.publish(_payload(), message_id="m3")
    except HttpTelemetryNotReadyError as exc:
        assert is_transient(exc) is True


def test_bekleme_dolunca_tek_probe_gecer() -> None:
    """Yarim-acik: bekleme dolunca SADECE bir istek denenir."""
    session = _SayanSession(requests.ConnectionError("baglanti yok"))
    publisher = _yayinci(session)

    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish(_payload(), message_id="m1")
    assert session.post_sayisi == 1

    # Beklemeyi elle bitir
    publisher._ready_retry_at = 0.0

    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish(_payload(), message_id="m2")
    assert session.post_sayisi == 2, "bekleme dolunca probe gecmeli"

    # Probe da basarisiz oldu -> devre yeniden acik
    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish(_payload(), message_id="m3")
    assert session.post_sayisi == 2


def test_backend_geri_gelince_devre_kapanir() -> None:
    session = _SayanSession(requests.ConnectionError("baglanti yok"))
    publisher = _yayinci(session)

    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish(_payload(), message_id="m1")
    assert publisher.is_ready is False

    # Backend ayaga kalkti
    session.response = _Response(202, "{}")
    publisher._ready_retry_at = 0.0

    publisher.publish(_payload(), message_id="m2")
    assert publisher.is_ready is True

    # Devre kapali: sonraki her istek normal sekilde ag'a gider
    onceki = session.post_sayisi
    publisher.publish(_payload(), message_id="m3")
    assert session.post_sayisi == onceki + 1


def test_kalici_hata_devreyi_acmaz() -> None:
    """400/422 backend'in ERISILEBILIR oldugunu gosterir; sadece bu mesaj kotu.

    Devreyi acmak, tek bir bozuk payload yuzunden TUM telemetriyi
    geciktirirdi. Ayrica yarim-acik probe bayragi temizlenmezse devre kalici
    olarak acik kalirdi.
    """
    session = _SayanSession(_Response(422, "sema hatasi"))
    publisher = _yayinci(session)

    with pytest.raises(HttpTelemetryPublishError):
        publisher.publish(_payload(), message_id="m1")
    assert publisher.is_ready is True

    # Ag'a gitmeye devam etmeli
    with pytest.raises(HttpTelemetryPublishError):
        publisher.publish(_payload(), message_id="m2")
    assert session.post_sayisi == 2


def test_devre_kesici_batch_yolunda_da_calisir() -> None:
    session = _SayanSession(requests.ConnectionError("baglanti yok"))
    publisher = _yayinci(session)

    ogeler = [{"payload": _payload(), "correlation_id": "c1"}]
    with pytest.raises(HttpTelemetryNotReadyError):
        publisher.publish_batch(ogeler)
    assert session.post_sayisi == 1

    for _ in range(10):
        with pytest.raises(HttpTelemetryNotReadyError):
            publisher.publish_batch(ogeler)
    assert session.post_sayisi == 1
