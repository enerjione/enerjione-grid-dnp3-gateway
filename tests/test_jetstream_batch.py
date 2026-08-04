"""JetStream toplu yayin (`publish_batch`) testleri.

NEDEN BU DOSYA VAR — SAHADA OLCULDU
-----------------------------------
`JetStreamPublisher.publish_batch` YOKKEN `ResilientPublisher.publish_batch`
"broker batch desteklemiyor" deyip mesajlari TEK TEK `publish()`e dusuruyordu.
Her cagri ayri bir `run_coroutine_threadsafe` + `future.result()`, yani
cagiran thread'i BLOKE eden bir JetStream ACK round-trip'i demekti.

300 cihazli sahada (2026-08-04): bir cycle'da 30.696 mesaj, NATS round-trip
0.064 ms -> yalnizca ack beklemesi ~2.0 sn; gozlenen cycle ortalamasi 4.02 sn
(hedef 1 sn), gateway CPU %95. Hedef 500 cihaz oldugu icin bu tavan
kabul edilemezdi.

Bu testler iki seyi kilitler:
  1. Toplu yayin gercekten PARALEL (tek loop turunda) yapiliyor,
  2. Hata semantigi bozulmuyor — kismi basarisizlik istisna firlatir ki
     `ResilientPublisher` tum batch'i outbox'a yazsin (at-least-once).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from dnp3_gateway.messaging.jetstream_publisher import (
    JetStreamNotReadyError,
    JetStreamPublisher,
    JetStreamPublishError,
)


class _SahteJs:
    """JetStream taklidi: her publish'te `gecikme` kadar bekler."""

    def __init__(self, *, gecikme: float = 0.0, patlayanlar: set[int] | None = None) -> None:
        self.gecikme = gecikme
        self.patlayanlar = patlayanlar or set()
        self.yayinlar: list[tuple[bytes, dict[str, str]]] = []
        self.anlik_es_zamanli = 0
        self.max_es_zamanli = 0
        self._sayac = 0
        self._lock = threading.Lock()

    async def publish(self, subject: str, body: bytes, headers: dict[str, str] | None = None) -> Any:  # noqa: ARG002
        with self._lock:
            self._sayac += 1
            sira = self._sayac
            self.anlik_es_zamanli += 1
            self.max_es_zamanli = max(self.max_es_zamanli, self.anlik_es_zamanli)
        try:
            if self.gecikme:
                await asyncio.sleep(self.gecikme)
            if sira in self.patlayanlar:
                raise RuntimeError(f"publish {sira} patladi")
            self.yayinlar.append((body, dict(headers or {})))
            return object()
        finally:
            with self._lock:
                self.anlik_es_zamanli -= 1


def _yayinci(js: _SahteJs, *, timeout: float = 5.0) -> JetStreamPublisher:
    """Gercek loop thread'i olan ama NATS'a baglanmayan publisher."""
    p = object.__new__(JetStreamPublisher)
    p.subject = "e1.telemetry.raw.GW-TEST"
    p._js = js
    p._publish_timeout = timeout
    p._ready = threading.Event()
    p._ready.set()
    p._counter_lock = threading.Lock()
    p._publish_failures = 0
    p._publish_successes = 0

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    p._loop = loop
    p._test_loop_thread = (loop, t)
    return p


def _kapat(p: JetStreamPublisher) -> None:
    loop, t = p._test_loop_thread
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def _ogeler(n: int) -> list[dict[str, Any]]:
    return [
        {
            "payload": {"message_id": f"m{i}", "value": float(i)},
            "message_id": f"m{i}",
            "correlation_id": f"c{i}",
            "headers": {"device_code": "DEV-1", "signal_key": f"s{i}", "bos": None},
        }
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# temel davranis
# --------------------------------------------------------------------------


def test_tum_mesajlar_yayinlanir() -> None:
    js = _SahteJs()
    p = _yayinci(js)
    try:
        p.publish_batch(_ogeler(50))
    finally:
        _kapat(p)
    assert len(js.yayinlar) == 50
    assert p.publish_successes == 50
    assert p.publish_failures == 0


def test_dedup_ve_korelasyon_basliklari_tasinir() -> None:
    """`Nats-Msg-Id` broker-side dedup'in tek dayanagi — kaybolmamali."""
    js = _SahteJs()
    p = _yayinci(js)
    try:
        p.publish_batch(_ogeler(3))
    finally:
        _kapat(p)
    _body, h = js.yayinlar[0]
    assert h["Nats-Msg-Id"] == "m0"
    assert h["X-Correlation-Id"] == "c0"
    assert h["device_code"] == "DEV-1"
    assert "bos" not in h, "None deger baslik olarak gonderilmemeli"


def test_bos_batch_hicbir_sey_yapmaz() -> None:
    js = _SahteJs()
    p = _yayinci(js)
    try:
        p.publish_batch([])
    finally:
        _kapat(p)
    assert js.yayinlar == []


# --------------------------------------------------------------------------
# ASIL KAZANC: paralellik
# --------------------------------------------------------------------------


def test_yayinlar_paralel_yapilir() -> None:
    """REGRESYON: eskiden mesajlar SIRAYLA yayinlaniyordu.

    Her mesaj icin ayri bir ack round-trip beklendigi icin cycle suresi
    mesaj sayisiyla dogru orantili buyuyordu (sahada 30.696 mesaj -> ~2 sn
    yalnizca ack beklemesi). Paralel yayinda toplam sure ~tek round-trip
    kadardir.
    """
    gecikme = 0.01  # her publish 10 ms
    adet = 100
    js = _SahteJs(gecikme=gecikme)
    p = _yayinci(js)
    try:
        t0 = time.perf_counter()
        p.publish_batch(_ogeler(adet))
        gecen = time.perf_counter() - t0
    finally:
        _kapat(p)

    sirali_sure = gecikme * adet  # 1.0 sn
    assert gecen < sirali_sure / 4, (
        f"toplu yayin SIRALI davraniyor: {gecen:.3f} sn "
        f"(sirali tahmin {sirali_sure:.3f} sn) — paralellik kaybolmus"
    )
    assert js.max_es_zamanli > 1, "hicbir an birden fazla publish ucusta degildi"


def test_paralellik_parca_boyuyla_sinirlanir() -> None:
    """Kontrolsuz paralellik NATS yazma tamponunu sisirir; parca boyu tavan koyar."""
    from dnp3_gateway.messaging.jetstream_publisher import _BATCH_CHUNK

    js = _SahteJs(gecikme=0.002)
    p = _yayinci(js, timeout=30.0)
    try:
        p.publish_batch(_ogeler(_BATCH_CHUNK * 2 + 10))
    finally:
        _kapat(p)
    assert js.max_es_zamanli <= _BATCH_CHUNK, (
        f"ayni anda {js.max_es_zamanli} publish ucusta (tavan {_BATCH_CHUNK})"
    )


# --------------------------------------------------------------------------
# hata semantigi — at-least-once bozulmamali
# --------------------------------------------------------------------------


def test_kismi_hata_istisna_firlatir() -> None:
    """ResilientPublisher TUM batch'i outboxa yazsin diye istisna SART.

    Sessizce yutulsaydi basarisiz mesajlar ne broker'a ne diske giderdi —
    yani sessiz veri kaybi. Duplicate riski `Nats-Msg-Id` dedup'i ile elenir.
    """
    js = _SahteJs(patlayanlar={3, 7})
    p = _yayinci(js)
    try:
        with pytest.raises(JetStreamPublishError) as ex:
            p.publish_batch(_ogeler(10))
        assert "2/10" in str(ex.value)
        assert p.publish_failures == 2
        assert p.publish_successes == 8
    finally:
        _kapat(p)


def test_hazir_degilken_gecici_hata_verir() -> None:
    """Baglanti yokken GECICI hata — retry_count artmamali (outbox mantigi)."""
    js = _SahteJs()
    p = _yayinci(js)
    p._ready.clear()
    try:
        with pytest.raises(JetStreamNotReadyError):
            p.publish_batch(_ogeler(5))
        assert p.publish_failures == 5
        assert js.yayinlar == []
    finally:
        _kapat(p)


def test_resilient_publisher_artik_batch_yolunu_kullanir() -> None:
    """Zincir dogrulamasi: ResilientPublisher tekli publish'e DUSMEMELI."""
    from dnp3_gateway.messaging.resilient_publisher import ResilientPublisher

    assert callable(getattr(JetStreamPublisher, "publish_batch", None)), (
        "JetStreamPublisher.publish_batch yok — ResilientPublisher tek tek "
        "publish'e duser ve sahadaki sirali-ack darbogazi geri gelir"
    )
    # ResilientPublisher broker'da `publish_batch` arar; sozlesme korunuyor mu?
    assert hasattr(ResilientPublisher, "publish_batch")
