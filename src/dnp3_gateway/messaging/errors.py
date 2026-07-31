"""Yayin hatalarinin GECICI mi KALICI mi oldugunu belirleyen tek otorite.

NEDEN AYRI BIR MODUL
--------------------
Retrier eskiden siniflandirmayi METIN ESLESMESIYLE yapiyordu:

    is_not_ready = (
        type(exc).__name__ == "JetStreamNotReadyError"
        or "not ready" in err_str.lower()
    )

Bu iki yonlu hataliydi:

1. **Kalici hatayi gecici sayiyordu (head-of-line blocking).**
   HttpTelemetryPublisher HTTP 500'lerin TAMAMINI "backend ingest not ready:
   HTTP 500 ..." metniyle firlatiyordu. Payload'a OZGU kalici bir 500 (orn.
   NaN deger, silinmis device_code, sema ihlali) "gecici baglanti yok" olarak
   siniflaniyor; retry_count ARTMIYOR, dead-letter'a TASINMIYOR ve `ORDER BY
   id ASC` yuzunden ayni satir sonsuza kadar kuyrugun basinda kaliyordu.
   Arkasindaki TUM telemetri teslim edilemiyor, outbox saatler icinde
   500.000 limitine dayaniyor ve poller tamamen duruyordu.

2. **Kalici hata metninde 'not ready' gecerse.** Backend'in
   `{"detail": "signal not ready for ingest"}` gibi bir 4xx govdesi bile
   substring'e eslesip ayni kilitlenmeyi yaratabiliyordu.

Artik siniflandirma EXCEPTION TIPI + HTTP STATUS uzerinden yapilir; metin
hicbir karara girmez.
"""

from __future__ import annotations

# Gecici kabul edilen HTTP durum kodlari: baglanti/kapasite sorunlari.
#   408 Request Timeout, 425 Too Early, 429 Too Many Requests,
#   500 gecici olabilir ama payload'a ozgu de olabilir -> KALICI sayilir
#       (aksi halde bir zehirli mesaj kuyrugu sonsuza kadar tikar),
#   502/503/504 ag/kapasite -> GECICI.
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 502, 503, 504})


class PublishError(Exception):
    """Yayin hatalarinin ortak taban sinifi.

    `transient=True` -> baglanti/kapasite sorunu; mesajin ICERIGIYLE ilgisi
    yok. Retry sayaci ARTMAZ, mesaj dead-letter'a tasinmaz.
    `transient=False` -> mesaja ozgu kalici hata. Retry sayaci artar ve
    max_retries sonrasi dead-letter'a tasinir; kuyruk ilerlemeye devam eder.
    """

    transient: bool = False
    http_status: int | None = None

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class TransientPublishError(PublishError):
    """Gecici: broker/backend su an erisilemez. Mesaj yeniden denenmeli."""

    transient = True


class PermanentPublishError(PublishError):
    """Kalici: mesaj bu haliyle asla kabul edilmeyecek (sema/dogrulama hatasi)."""

    transient = False


def is_transient(exc: BaseException) -> bool:
    """Bu hata GECICI mi? (retry_count artirilmamali, dead-letter'a gitmemeli)

    Karar sirasi:
      1. `transient` ozniteligi olan hatalar (PublishError ailesi) — otoriter.
      2. HTTP status tasiyorsa TRANSIENT_HTTP_STATUSES'a bak.
      3. Ag seviyesi hatalar (ConnectionError / TimeoutError / OSError).
      4. Digerleri KALICI sayilir — boylece zehirli bir mesaj kuyrugu
         sonsuza kadar tikayamaz; max_retries sonrasi dead-letter'a duser.

    Metin eslesmesi BILINCLI OLARAK YOK; eski `"not ready" in str(exc)`
    sezgisi hem kalici 5xx'leri hem de govdesinde bu ifade gecen 4xx'leri
    yanlis siniflandiriyordu.
    """
    transient_attr = getattr(exc, "transient", None)
    if isinstance(transient_attr, bool):
        return transient_attr

    status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        return status in TRANSIENT_HTTP_STATUSES

    # requests / stdlib ag hatalari: baglanti kurulamadi veya zaman asimi.
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    try:  # requests opsiyonel degil ama import hatasi akisi bozmasin
        import requests

        if isinstance(exc, requests.ConnectionError | requests.Timeout):
            return True
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, OSError):
        return True

    return False
