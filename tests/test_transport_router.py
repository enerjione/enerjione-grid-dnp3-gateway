"""Telemetri tasima yolu secimi: yerel = NATS zorunlu, uzak = HTTP yedegi.

NEDEN BU DOSYA VAR
------------------
Gateway'in iki kurulum senaryosunda DAVRANISI FARKLI olmali ve bu fark
sessizce bozulabilecek turden:

* YEREL kurulumda (backend ile ayni makine) NATS erisilemezse HTTP'ye
  dusmek ariza GIZLER. Sistem calisir gorunur, panelde veri akar, ama her
  olcum backend HTTP + Postgres zincirinden gecer ve 500 cihaz hedefi
  tutmaz. Bu, ancak yuk testinde — haftalar sonra — fark edilir.
* UZAK kurulumda ayni dususu YAPMAMAK veri kaybi demektir; sahada 4222
  kapali olabilir ve elimizde yalnizca backend'in HTTPS ucu vardir.

Testler bu iki davranisi, aradaki gecisleri ve "gecis = olay, deneme =
sessiz" kuralini kilitler.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from dnp3_gateway.messaging.transport_router import (
    TRANSPORT_HTTP,
    TRANSPORT_NATS,
    TelemetryTransportRouter,
)


class SahtePublisher:
    """Kontrollu basari/hata ureten publisher taklidi."""

    def __init__(self, *, ad: str, ready: bool = True) -> None:
        self.ad = ad
        self.ready = ready
        self.calisiyor = True
        self.yayinlar: list[str] = []
        self.batchler: list[int] = []
        self.kapandi = False
        self.publish_failures = 0
        self.publish_successes = 0

    @property
    def is_ready(self) -> bool:
        return self.ready

    def publish(self, payload: dict[str, Any], *, message_id: str, **_: Any) -> None:
        if not self.calisiyor:
            self.publish_failures += 1
            raise RuntimeError(f"{self.ad} erisilemez")
        self.publish_successes += 1
        self.yayinlar.append(message_id)

    def publish_batch(self, items: list[dict[str, Any]]) -> None:
        if not self.calisiyor:
            self.publish_failures += len(items)
            raise RuntimeError(f"{self.ad} erisilemez")
        self.publish_successes += len(items)
        self.batchler.append(len(items))
        self.yayinlar.extend(str(i["message_id"]) for i in items)

    def counters_snapshot(self) -> dict[str, int]:
        return {
            "publish_failures": self.publish_failures,
            "publish_successes": self.publish_successes,
        }

    def close(self) -> None:
        self.kapandi = True


def _mesaj(i: int) -> dict[str, Any]:
    return {"payload": {"deger": i}, "message_id": f"m{i}", "correlation_id": None, "headers": None}


def _router(*, mode: str, nats: SahtePublisher, http: SahtePublisher | None, **kw: Any):
    # probe_interval VARSAYILAN OLARAK YUKSEK: testlerin cogu "yedege
    # dustukten sonra ne oluyor" ile ilgilenir ve araya kendiliginden bir
    # geri-donus yoklamasi girmesini istemez. Geri donusu SINAYAN testler
    # `probe_interval_sec=0.0` gecerek yoklamayi hemen tetikler.
    varsayilan = {"fail_threshold": 2, "probe_interval_sec": 60.0, "min_dwell_sec": 0.0}
    varsayilan.update(kw)
    return TelemetryTransportRouter(
        nats_broker=nats,
        http_broker=http,
        install_mode=mode,
        **varsayilan,
    )


# ---------------------------------------------------------------- YEREL MOD
def test_yerel_modda_nats_hatasi_http_ye_dusmez():
    """Yerel kurulumda yedek yol yok: hata yukari cikmali (outbox'a gitsin)."""
    nats = SahtePublisher(ad="nats")
    r = _router(mode="local", nats=nats, http=None)
    nats.calisiyor = False

    with pytest.raises(RuntimeError, match="nats erisilemez"):
        r.publish({"x": 1}, message_id="m1")

    assert r.fallback_enabled is False
    assert r.active_transport == TRANSPORT_NATS  # yol ASLA degismez


def test_yerel_modda_nats_dususu_saglik_durumunda_gorunur():
    """`is_ready` yerel modda dogrudan NATS'in durumu olmali.

    Aksi halde /health `telemetry_backend_unreachable` uretemez ve panel
    "sorun yok" der.
    """
    nats = SahtePublisher(ad="nats", ready=False)
    r = _router(mode="local", nats=nats, http=None)
    assert r.is_ready is False

    nats.ready = True
    assert r.is_ready is True


def test_yerel_modda_batch_hatasi_da_yukari_cikar():
    nats = SahtePublisher(ad="nats")
    r = _router(mode="local", nats=nats, http=None)
    nats.calisiyor = False
    with pytest.raises(RuntimeError):
        r.publish_batch([_mesaj(1), _mesaj(2)])


# ----------------------------------------------------------------- UZAK MOD
def test_uzak_modda_nats_dusunce_veri_http_den_akmaya_devam_eder():
    """Fallback'te KAYIP OLMAMALI: ayni cagrida HTTP'ye aktarilir."""
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http)

    r.publish({"x": 0}, message_id="m0")
    assert nats.yayinlar == ["m0"]

    nats.calisiyor = False
    r.publish({"x": 1}, message_id="m1")  # istisna YOK
    r.publish({"x": 2}, message_id="m2")

    # Her iki mesaj da teslim edildi — hicbiri kaybolmadi.
    assert http.yayinlar == ["m1", "m2"]
    assert r.transport_status()["fallback_delivered_total"] == 2


def test_uzak_modda_esik_asilinca_aktif_yol_http_olur():
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=3)
    nats.calisiyor = False
    nats.ready = False  # gercekten erisilemez NATS (baglanti da yok)

    r.publish({"x": 1}, message_id="m1")
    assert r.active_transport == TRANSPORT_NATS  # 1 hata: henuz degil
    r.publish({"x": 2}, message_id="m2")
    assert r.active_transport == TRANSPORT_NATS  # 2 hata: henuz degil
    r.publish({"x": 3}, message_id="m3")
    assert r.active_transport == TRANSPORT_HTTP  # 3. hatada gecis

    # Gectikten sonra artik her mesajda NATS timeout'u odenmiyor.
    onceki = nats.publish_failures
    r.publish({"x": 4}, message_id="m4")
    assert nats.publish_failures == onceki
    assert http.yayinlar[-1] == "m4"


def test_uzak_modda_nats_gelince_geri_donulur():
    """HTTP'de KALICI TAKILI KALMAMALI."""
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1, probe_interval_sec=0.0)

    nats.calisiyor = False
    nats.ready = False
    r.publish({"x": 1}, message_id="m1")
    assert r.active_transport == TRANSPORT_HTTP

    # NATS geri geldi.
    nats.calisiyor = True
    nats.ready = True
    r.publish({"x": 2}, message_id="m2")

    assert r.active_transport == TRANSPORT_NATS
    assert nats.yayinlar[-1] == "m2"


def test_nats_hazir_degilse_geri_donulmez():
    """Yoklama NATS'in gercek baglanti durumuna bakmali, korlemesine degil."""
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1, probe_interval_sec=0.0)

    nats.calisiyor = False
    nats.ready = False
    r.publish({"x": 1}, message_id="m1")
    assert r.active_transport == TRANSPORT_HTTP

    # Baglanti hala yok -> HTTP'de kalmali.
    r.publish({"x": 2}, message_id="m2")
    assert r.active_transport == TRANSPORT_HTTP


def test_her_iki_yol_da_coktuyse_istisna_yukari_cikar():
    """Iki yol da olunce mesaj SESSIZCE DUSURULMEMELI.

    Router istisna firlatmali ki ResilientPublisher outbox'a yazsin.
    """
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1)
    nats.calisiyor = False
    http.calisiyor = False

    with pytest.raises(RuntimeError, match="http erisilemez"):
        r.publish({"x": 1}, message_id="m1")


def test_batch_fallback_tum_mesajlari_tasir():
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1)
    nats.calisiyor = False

    r.publish_batch([_mesaj(i) for i in range(5)])

    assert http.batchler == [5]
    assert http.yayinlar == ["m0", "m1", "m2", "m3", "m4"]


# ------------------------------------------------------------- GOZLEMLENEBILIRLIK
def test_yol_degisimi_olay_uretir_denemeler_sessizdir(caplog):
    """500 cihazda her hatayi loglamak gercek olayi bogar.

    Yalnizca DURUM DEGISIMI log'a yazilmali; aradaki denemeler sessiz.
    """
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1, probe_interval_sec=0.0)
    nats.calisiyor = False
    nats.ready = False

    with caplog.at_level(logging.INFO, logger="dnp3_gateway.messaging.transport_router"):
        for i in range(20):
            r.publish({"x": i}, message_id=f"m{i}")
        dususler = [x for x in caplog.records if "telemetry_transport_switched" in x.getMessage()]
        assert len(dususler) == 1, "20 hatali denemede TEK gecis olayi olmali"
        assert dususler[0].levelno == logging.WARNING

        caplog.clear()
        nats.calisiyor = True
        nats.ready = True
        for i in range(20, 40):
            r.publish({"x": i}, message_id=f"m{i}")
        donusler = [x for x in caplog.records if "telemetry_transport_switched" in x.getMessage()]
        assert len(donusler) == 1, "geri donus de TEK olay olmali"
        assert donusler[0].levelno == logging.INFO

    assert r.transport_status()["transport_switches"] == 2


def test_transport_status_aktif_yolu_raporlar():
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1)

    s = r.transport_status()
    assert s["install_mode"] == "remote"
    assert s["active_transport"] == TRANSPORT_NATS
    assert s["fallback_enabled"] is True
    assert s["nats_ready"] is True
    assert s["http_ready"] is True

    nats.calisiyor = False
    nats.ready = False
    r.publish({"x": 1}, message_id="m1")
    s = r.transport_status()
    assert s["active_transport"] == TRANSPORT_HTTP
    assert s["nats_ready"] is False
    assert "nats_failed" in (s["last_switch_reason"] or "")


def test_yerel_modda_status_yedegin_kapali_oldugunu_soyler():
    nats = SahtePublisher(ad="nats")
    r = _router(mode="local", nats=nats, http=None)
    s = r.transport_status()
    assert s["install_mode"] == "local"
    assert s["fallback_enabled"] is False
    assert s["http_ready"] is None


def test_counters_iki_yolu_toplar():
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1)
    r.publish({"x": 1}, message_id="m1")
    nats.calisiyor = False
    r.publish({"x": 2}, message_id="m2")

    c = r.counters_snapshot()
    assert c["publish_successes"] == 2  # 1 NATS + 1 HTTP
    assert c["publish_failures"] == 1  # 1 NATS hatasi


def test_close_iki_yolu_da_kapatir():
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    _router(mode="remote", nats=nats, http=http).close()
    assert nats.kapandi and http.kapandi


def test_min_dwell_yol_salinimini_engeller():
    """NATS bir acilip bir kapanirken yol degistirme dongusune girilmemeli."""
    nats = SahtePublisher(ad="nats")
    http = SahtePublisher(ad="http")
    r = _router(mode="remote", nats=nats, http=http, fail_threshold=1, min_dwell_sec=60.0)

    nats.calisiyor = False
    nats.ready = False
    r.publish({"x": 1}, message_id="m1")
    # dwell dolmadigi icin ilk gecis bile bloke — NATS'ta kalir ama mesaj
    # yine de HTTP'den teslim edilir (kayip yok).
    assert r.active_transport == TRANSPORT_NATS
    assert http.yayinlar == ["m1"]
