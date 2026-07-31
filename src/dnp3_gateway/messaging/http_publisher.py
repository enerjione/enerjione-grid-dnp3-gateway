"""Backend HTTP ingest publisher.

Gateway NAT arkasinda oldugu icin outbound HTTP/HTTPS ile backend
`/telemetry/gateway/{code}` endpoint'ine telemetri yollar. ResilientPublisher
bunu broker gibi sarar; HTTP hata verirse mesaj SQLite outbox'a duser.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import requests

from dnp3_gateway.auth import GatewayIdentity, build_config_request_headers
from dnp3_gateway.backend.http_session import build_http_session
from dnp3_gateway.messaging.errors import (
    TRANSIENT_HTTP_STATUSES,
    PermanentPublishError,
    TransientPublishError,
)

logger = logging.getLogger(__name__)


class HttpTelemetryPublishError(PermanentPublishError):
    """HTTP ingest KALICI/semantik hata verdi (mesaja ozgu).

    Retry sayaci artar; max_retries sonrasi dead-letter'a tasinir ve kuyruk
    ilerler. Eskiden bu sinif `transient` bilgisi tasimiyordu ve retrier
    karari metin eslesmesiyle veriyordu.
    """


class HttpTelemetryNotReadyError(TransientPublishError):
    """Backend ingest su an hazir degil (ag/kapasite); retry_count artmamali.

    DIKKAT: Artik yalnizca GERCEKTEN gecici durumlar icin kullanilir —
    baglanti hatasi, timeout, 408/425/429/502/503/504. HTTP 500'ler KALICI
    kabul edilir; eskiden hepsi buraya dusuyordu ve payload'a ozgu tek bir
    500 tum kuyrugu sonsuza kadar tikayabiliyordu (head-of-line blocking).
    """


def _scrub_token(text: str, token: str | None) -> str:
    if not token or len(token) < 6:
        return text
    return text.replace(token, "***REDACTED***")


class HttpTelemetryPublisher:
    """Sync publisher: tek telemetri payload'unu backend HTTP ingest'e POST eder."""

    def __init__(
        self,
        *,
        base_url: str,
        identity: GatewayIdentity,
        timeout_sec: float = 5.0,
        session: requests.Session | None = None,
        verify: bool | str = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.timeout_sec = float(timeout_sec)
        self.url = f"{self.base_url}/telemetry/gateway/{identity.gateway_code}"
        # Connection-pooled session: poller paralel worker'lari + retrier ayni
        # host'a es zamanli POST atar. Default pool (maxsize=10) doyunca
        # baglantilar bloke olur; buyuk pool ile paralel akis serbest kalir.
        self._session = session or build_http_session(pool_maxsize=32, verify=verify)
        self._ready = True
        self._closed = False
        # Lock SADECE _closed/_ready gibi kucuk state'i korur; HTTP POST lock
        # DISINDA yapilir (requests.Session connection pool ile thread-safe).
        # Boylece bir yavas POST digerlerini kilitlemez.
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._publish_failures = 0
        self._publish_successes = 0

    def publish(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        _ = message_id, headers
        with self._lock:
            if self._closed:
                self._mark_failure()
                raise HttpTelemetryNotReadyError("backend ingest not ready: publisher closed")

        request_headers = build_config_request_headers(self.identity)
        request_headers["Content-Type"] = "application/json"
        if correlation_id:
            request_headers["X-Correlation-Id"] = correlation_id

        try:
            response = self._session.post(
                self.url,
                headers=request_headers,
                json=[payload],
                timeout=self.timeout_sec,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._set_ready(False)
            self._mark_failure()
            err = _scrub_token(str(exc), self.identity.token)
            raise HttpTelemetryNotReadyError(f"backend ingest not ready: {err}") from exc
        except requests.RequestException as exc:
            self._set_ready(False)
            self._mark_failure()
            err = _scrub_token(str(exc), self.identity.token)
            raise HttpTelemetryNotReadyError(f"backend ingest not ready: {err}") from exc

        if 200 <= response.status_code < 300:
            self._set_ready(True)
            self._mark_success()
            return

        preview = _scrub_token((response.text or "")[:500], self.identity.token)
        self._mark_failure()
        # GECICI mi KALICI mi? Karar TEK yerden (messaging.errors) gelir.
        # 500 artik KALICI sayilir: payload'a ozgu bir 500 (NaN deger, silinmis
        # device_code, sema ihlali) eskiden "gecici" siniflanip retry_count'u
        # artirmadigi icin ayni satir kuyrugun basinda sonsuza kadar kaliyor,
        # arkasindaki TUM telemetriyi bloke ediyordu.
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            self._set_ready(False)
            raise HttpTelemetryNotReadyError(
                f"backend ingest not ready: HTTP {response.status_code}: {preview}",
                http_status=response.status_code,
            )
        raise HttpTelemetryPublishError(
            f"backend ingest rejected: HTTP {response.status_code}: {preview}",
            http_status=response.status_code,
        )

    def publish_batch(self, items: list[dict[str, Any]]) -> None:
        """Birden fazla telemetri payload'unu TEK HTTP POST ile gonderir.

        Backend `/telemetry/gateway/{code}` endpoint'i `list[TelemetryIn]`
        kabul ettigi icin (mesaj-basina-POST yerine) tum dalgayi tek istekte
        yollariz — 300 cihaz recovery/refresh dalgalarinda throughput'u
        buyuk olcude artirir. Hata semantigi tekli publish ile ayni: gecici
        hata -> HttpTelemetryNotReadyError (caller/ResilientPublisher tum
        batch'i outbox'a tek tek yazar).

        `items`: her biri {"payload": {...}, "correlation_id": str|None} dict'i.
        """
        if not items:
            return
        with self._lock:
            if self._closed:
                self._mark_failure()
                raise HttpTelemetryNotReadyError("backend ingest not ready: publisher closed")

        payloads = [it["payload"] for it in items]
        # Correlation id: batch'in ilkini kullan (backend per-item id'yi
        # payload icinden de okuyabiliyor; header tek deger olabilir).
        first_corr = items[0].get("correlation_id")
        request_headers = build_config_request_headers(self.identity)
        request_headers["Content-Type"] = "application/json"
        if first_corr:
            request_headers["X-Correlation-Id"] = str(first_corr)

        try:
            response = self._session.post(
                self.url,
                headers=request_headers,
                json=payloads,
                timeout=self.timeout_sec,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._set_ready(False)
            self._mark_failure()
            err = _scrub_token(str(exc), self.identity.token)
            raise HttpTelemetryNotReadyError(f"backend ingest not ready: {err}") from exc
        except requests.RequestException as exc:
            self._set_ready(False)
            self._mark_failure()
            err = _scrub_token(str(exc), self.identity.token)
            raise HttpTelemetryNotReadyError(f"backend ingest not ready: {err}") from exc

        if 200 <= response.status_code < 300:
            self._set_ready(True)
            with self._counter_lock:
                self._publish_successes += len(payloads)
            return

        preview = _scrub_token((response.text or "")[:500], self.identity.token)
        self._mark_failure()
        # GECICI mi KALICI mi? Karar TEK yerden (messaging.errors) gelir.
        # 500 artik KALICI sayilir: payload'a ozgu bir 500 (NaN deger, silinmis
        # device_code, sema ihlali) eskiden "gecici" siniflanip retry_count'u
        # artirmadigi icin ayni satir kuyrugun basinda sonsuza kadar kaliyor,
        # arkasindaki TUM telemetriyi bloke ediyordu.
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            self._set_ready(False)
            raise HttpTelemetryNotReadyError(
                f"backend ingest not ready: HTTP {response.status_code}: {preview}",
                http_status=response.status_code,
            )
        raise HttpTelemetryPublishError(
            f"backend ingest rejected: HTTP {response.status_code}: {preview}",
            http_status=response.status_code,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            logger.debug("http_telemetry_session_close_error", exc_info=True)

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._ready and not self._closed

    @property
    def publish_failures(self) -> int:
        with self._counter_lock:
            return self._publish_failures

    @property
    def publish_successes(self) -> int:
        with self._counter_lock:
            return self._publish_successes

    def counters_snapshot(self) -> dict[str, int]:
        with self._counter_lock:
            return {
                "publish_failures": self._publish_failures,
                "publish_successes": self._publish_successes,
            }

    def _set_ready(self, ready: bool) -> None:
        with self._lock:
            self._ready = ready

    def _mark_failure(self) -> None:
        with self._counter_lock:
            self._publish_failures += 1

    def _mark_success(self) -> None:
        with self._counter_lock:
            self._publish_successes += 1
