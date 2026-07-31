"""OutboxRetrier: hata siniflandirmasi, head-of-line blocking, batch drenaj.

Kapatilan uretim riskleri:

1. **Head-of-line blocking.** Siniflandirma METIN ESLESMESIYLE yapiliyordu
   (`"not ready" in str(exc)`). HttpTelemetryPublisher TUM 5xx'leri
   "backend ingest not ready: HTTP 500 ..." metniyle firlattigi icin,
   payload'a OZGU kalici bir 500 (NaN deger, silinmis device_code, sema
   ihlali) "gecici" siniflaniyordu: retry_count artmiyor, dead-letter'a
   tasinmiyor ve `ORDER BY id ASC` yuzunden ayni satir sonsuza kadar
   kuyrugun basinda kaliyordu. Arkasindaki TUM telemetri teslim edilemiyor,
   outbox saatler icinde limite dayaniyor, poller tamamen duruyordu.

2. **Drenaj hizi tavani.** Her mesaj ayri POST + ayri DELETE/commit ile
   gonderiliyor, her batch sonrasi 2sn uyunuyordu (~32 mesaj/sn). Birikmis
   500K mesaj saatler suruyor, bu sirada uretim devam ettigi icin net
   drenaj sifira yaklasiyordu.

3. **Retrier thread'i sessizce oluyordu.** Tek bir SQLite hatasi (disk dolu)
   thread'i oldurup telemetri teslimini kalici durduruyordu; hicbir gosterge
   yoktu.
"""

from __future__ import annotations

import time
from pathlib import Path

from dnp3_gateway.messaging.errors import (
    PermanentPublishError,
    TransientPublishError,
    is_transient,
)
from dnp3_gateway.messaging.outbox import Outbox, OutboxRetrier


def _enq(ob: Outbox, mid: str) -> int:
    return ob.enqueue(message_id=mid, correlation_id=None, headers=None, payload={"m": mid})


# --------------------------------------------------------------------------
# siniflandirma
# --------------------------------------------------------------------------


def test_transient_ve_permanent_tipleri() -> None:
    assert is_transient(TransientPublishError("x")) is True
    assert is_transient(PermanentPublishError("x")) is False


class _StatusOnlyError(Exception):
    """`transient` bayragi TASIMAYAN, yalnizca http_status tasiyan hata.

    (Ucuncu parti kutuphanelerin firlattigi hatalari temsil eder; siniflandirma
    bu durumda status koduna duser.)
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.http_status = status


def test_http_status_siniflandirmasi() -> None:
    # Gecici: ag/kapasite
    for code in (408, 425, 429, 502, 503, 504):
        assert is_transient(_StatusOnlyError(code)) is True, code
    # Kalici: mesaja ozgu — 500 DAHIL
    for code in (400, 404, 409, 413, 422, 500, 501):
        assert is_transient(_StatusOnlyError(code)) is False, code


def test_acik_sinif_status_koduna_baskin_gelir() -> None:
    """Publisher zaten status'e gore dogru sinifi secer; sinif otoriterdir."""
    assert is_transient(TransientPublishError("x", http_status=422)) is True
    assert is_transient(PermanentPublishError("x", http_status=503)) is False


def test_ag_hatalari_gecicidir() -> None:
    assert is_transient(TimeoutError("zaman asimi")) is True
    assert is_transient(ConnectionError("baglanti yok")) is True
    assert is_transient(OSError("socket")) is True


def test_bilinmeyen_hata_kalici_sayilir() -> None:
    """Zehirli mesaj kuyrugu sonsuza kadar tikayamamali."""
    assert is_transient(ValueError("bilinmeyen")) is False


def test_metin_eslesmesi_artik_karar_vermiyor() -> None:
    """REGRESYON: govdesinde 'not ready' gecen KALICI bir 4xx gecici sayiliyordu."""
    exc = PermanentPublishError(
        'backend ingest rejected: HTTP 400: {"detail":"signal not ready for ingest"}',
        http_status=400,
    )
    assert is_transient(exc) is False


# --------------------------------------------------------------------------
# head-of-line blocking
# --------------------------------------------------------------------------


def test_kalici_hatali_satir_kuyrugu_tikamaz(tmp_path: Path) -> None:
    """REGRESYON: tek bozuk satir arkasindaki TUM telemetriyi blokluyordu."""
    ob = Outbox(tmp_path / "o.db")
    try:
        _enq(ob, "bozuk")
        for i in range(5):
            _enq(ob, f"saglam-{i}")

        sent: list[str] = []

        def publish(row: dict) -> None:
            if row["message_id"] == "bozuk":
                raise PermanentPublishError("sema hatasi", http_status=422)
            sent.append(row["message_id"])

        r = OutboxRetrier(ob, publish, max_retries=100, min_backoff_sec=30.0)
        drained, transient = r._drain_batch(ob.fetch_batch(10))

        assert transient is False
        assert drained == 5, "bozuk satirin arkasindaki mesajlar gonderilmeli"
        assert sent == [f"saglam-{i}" for i in range(5)]
        # Bozuk satir ertelendi: su an HAZIR degil
        assert ob.ready_count() == 0
        assert ob.pending_count() == 1
    finally:
        ob.close()


def test_gecici_hata_retry_count_artirmaz(tmp_path: Path) -> None:
    """Uzun kesintide 500K mesaj sessizce dead-letter'a dusmemeli."""
    ob = Outbox(tmp_path / "o.db")
    try:
        _enq(ob, "m1")

        def publish(row: dict) -> None:  # noqa: ARG001
            raise TransientPublishError("backend erisilemez", http_status=503)

        r = OutboxRetrier(ob, publish, max_retries=2)
        drained, transient = r._drain_batch(ob.fetch_batch(10))
        assert drained == 0
        assert transient is True
        rows = ob.fetch_batch(10)
        assert rows[0]["retry_count"] == 0, "gecici hatada retry_count artmamali"
    finally:
        ob.close()


def test_kalici_hata_max_retries_sonrasi_dead_letter(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        _enq(ob, "zehirli")

        def publish(row: dict) -> None:  # noqa: ARG001
            raise PermanentPublishError("kalici", http_status=422)

        r = OutboxRetrier(ob, publish, max_retries=3, min_backoff_sec=0.1, max_backoff_sec=0.1)
        for _ in range(5):
            r._drain_batch(ob.fetch_batch(10, now=time.time() + 3600))
        assert ob.pending_count() == 0
        assert ob.dead_letter_count() == 1
    finally:
        ob.close()


def test_ertelenen_satir_suresi_dolunca_yeniden_denenir(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        _enq(ob, "m1")

        def publish(row: dict) -> None:  # noqa: ARG001
            raise PermanentPublishError("kalici", http_status=422)

        r = OutboxRetrier(ob, publish, max_retries=100, min_backoff_sec=10.0, max_backoff_sec=10.0)
        r._drain_batch(ob.fetch_batch(10))
        assert ob.ready_count() == 0                       # ertelendi
        assert ob.ready_count(now=time.time() + 60) == 1   # sure dolunca hazir
    finally:
        ob.close()


# --------------------------------------------------------------------------
# batch drenaj
# --------------------------------------------------------------------------


def test_batch_yolu_tek_cagriyla_bosaltir(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        for i in range(50):
            _enq(ob, f"m{i}")

        calls: list[int] = []

        def publish_batch(rows: list[dict]) -> None:
            calls.append(len(rows))

        def publish_single(row: dict) -> None:  # noqa: ARG001
            raise AssertionError("batch yolu varken tekil publish cagrilmamali")

        r = OutboxRetrier(ob, publish_single, publish_batch_fn=publish_batch)
        drained, transient = r._drain_batch(ob.fetch_batch(50))
        assert drained == 50
        assert transient is False
        assert calls == [50], "50 mesaj TEK cagriyla gonderilmeli"
        assert ob.pending_count() == 0
    finally:
        ob.close()


def test_batch_basarisiz_olursa_tekil_yola_duser(tmp_path: Path) -> None:
    """Toplu gonderim hangi satirin bozuk oldugunu soylemez; ayikla."""
    ob = Outbox(tmp_path / "o.db")
    try:
        _enq(ob, "bozuk")
        _enq(ob, "saglam")

        def publish_batch(rows: list[dict]) -> None:  # noqa: ARG001
            raise PermanentPublishError("batch reddedildi", http_status=422)

        sent: list[str] = []

        def publish_single(row: dict) -> None:
            if row["message_id"] == "bozuk":
                raise PermanentPublishError("sema", http_status=422)
            sent.append(row["message_id"])

        r = OutboxRetrier(ob, publish_single, publish_batch_fn=publish_batch, min_backoff_sec=30.0)
        drained, _t = r._drain_batch(ob.fetch_batch(10))
        assert drained == 1
        assert sent == ["saglam"]
    finally:
        ob.close()


def test_delete_many_tek_transaction(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        ids = [_enq(ob, f"m{i}") for i in range(10)]
        assert ob.delete_many(ids) == 10
        assert ob.pending_count() == 0
        assert ob.delete_many([]) == 0
    finally:
        ob.close()


# --------------------------------------------------------------------------
# thread saglik gorunurlugu
# --------------------------------------------------------------------------


def test_retrier_saglik_snapshot(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        r = OutboxRetrier(ob, lambda row: None, poll_interval_sec=0.5)
        assert r.is_alive() is False
        r.start()
        try:
            snap = r.status_snapshot()
            assert snap["alive"] is True
            assert snap["last_error"] is None
        finally:
            r.stop(timeout_sec=3.0)
        assert r.is_alive() is False
    finally:
        ob.close()


def test_dongu_hatasi_thread_i_oldurmez(tmp_path: Path) -> None:
    """REGRESYON: tek SQLite hatasi retrier thread'ini sessizce olduruyordu."""
    ob = Outbox(tmp_path / "o.db")
    try:
        _enq(ob, "m1")
        calls = {"n": 0}

        def publish(row: dict) -> None:  # noqa: ARG001
            calls["n"] += 1
            raise RuntimeError("beklenmedik")

        r = OutboxRetrier(
            ob, publish, poll_interval_sec=0.5, min_backoff_sec=0.1, max_backoff_sec=0.1
        )
        r.start()
        try:
            deadline = time.time() + 5
            while calls["n"] < 2 and time.time() < deadline:
                time.sleep(0.05)
            assert r.is_alive() is True, "thread beklenmedik hatada olmemeli"
        finally:
            r.stop(timeout_sec=3.0)
    finally:
        ob.close()
