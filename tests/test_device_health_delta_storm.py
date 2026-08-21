"""Cihaz saglik DELTA STORM regresyonu + cihaz RTC gozlemlenebilirligi.

SAHA BULGUSU (1.15.0)
---------------------
`device_health_published` logu ~2 SANIYEDE BIR goruluyordu.

KOK NEDEN: delta karari TAM KAYIT ESITLIGI ile veriliyordu
(`self._gonderilen.get(kod) != kayit`). Kayitta her poll'da degisen alanlar
oldugu icin yayinci neredeyse HER debounce penceresinde cihazi "changed"
sayiyordu:

    report_overdue_sec          LATE iken 0.1sn cozunurlukle surekli artar
    last_frame_epoch            HER frame'de degisir
    last_valid_contact_epoch    HER gecerli temasta degisir

Ikincisi sahada bildirilenden DAHA GENIS etkiliydi: telemetri alan HER cihaz
(ozellikle `continuous` filosu) POST uretiyordu — LATE durumu hic olmasa
bile. 200 cihazda bu, 2 saniyede bir 4 partilik trafik demekti.

COZUM: payload SOZLESMESI DEGISMEDI, alan KALDIRILMADI. Yalnizca DELTA
KARARI semantik imza uzerinden veriliyor (bkz. `SEMANTIC_FIELDS`).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from dnp3_gateway.backend.device_health_publisher import DeviceHealthPublisher
from dnp3_gateway.backend.device_health_wire import (
    OBSERVATIONAL_FIELDS,
    SEMANTIC_FIELDS,
    build_device_record,
    semantic_signature,
    siniflandirilmamis_alanlar,
)

_TIMEOUT = 5.0


def _saglik(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "reachable": True,
        "configured_session_policy": "smart",
        "effective_session_policy": "smart",
        "operation_mode": "smart",
        "dial_in_interval_min": 60,
        "next_expected_report_epoch": 1_755_643_200.0,
        "report_overdue_sec": 0.0,
        "report_late": False,
        "last_valid_contact_epoch": 1_755_600_000.0,
        "last_frame_epoch": 1_755_600_000.0,
        "ip_probe_status": "unknown",
        "tcp_probe_status": "open",
        "last_probe_epoch": None,
        "ip_endpoint_type": "listening",
        "device_clock_status": "ok",
        "device_clock_offset_sec": 0.5,
        "need_time_iin": False,
        "last_device_time_epoch": 1_755_600_000.0,
        "session_started_epoch": 1_755_599_000.0,
    }
    d.update(kw)
    return d


class SahteGonderici:
    def __init__(self) -> None:
        self.govdeler: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.govdeler.append(payload)

    def sayi(self) -> int:
        with self._lock:
            return len(self.govdeler)


def _yayinci(kaynak, gonderici, **kw: Any) -> DeviceHealthPublisher:
    kw.setdefault("change_debounce_sec", 0.0)
    kw.setdefault("snapshot_interval_sec", 3600.0)
    return DeviceHealthPublisher(
        health_source=kaynak,
        send=gonderici,
        gateway_code="GW-001",
        gateway_instance_id="i",
        boot_id=1,
        **kw,
    )


def _bekle(kosul, timeout: float = _TIMEOUT) -> bool:
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        if kosul():
            return True
        time.sleep(0.02)
    return False


# ==========================================================================
# Alan siniflandirmasi — ileride eklenen alan SESSIZCE storm uretmesin
# ==========================================================================


def test_her_alan_bilincli_olarak_siniflandirilmis() -> None:
    kayit = build_device_record("D1", _saglik())
    eksik = siniflandirilmamis_alanlar(kayit)
    assert not eksik, (
        f"su alanlar ne SEMANTIC ne OBSERVATIONAL: {sorted(eksik)} — "
        "sinifini BILINCLI secin (delta tetikler mi, tetiklemez mi?)"
    )
    assert not (SEMANTIC_FIELDS & OBSERVATIONAL_FIELDS), "alan iki kumede birden"


@pytest.mark.parametrize(
    "alan",
    [
        "report_overdue_sec",
        "last_frame_epoch",
        "last_valid_contact_epoch",
        "last_probe_epoch",
        "device_clock_offset_sec",
        "last_device_time_epoch",
        "session_started_epoch",
        "next_expected_report_epoch",
    ],
)
def test_volatil_alanlar_imzayi_degistirmez(alan: str) -> None:
    a = build_device_record("D1", _saglik())
    b = build_device_record("D1", _saglik(**{alan: 1_900_000_000.0}))
    assert a != b, f"{alan} kayda hic girmiyor — test anlamsiz"
    assert semantic_signature(a) == semantic_signature(b), (
        f"{alan} TEK BASINA delta tetikliyor — storm kaynagi"
    )


@pytest.mark.parametrize(
    ("alan", "deger"),
    [
        ("state", "smart_idle"),
        ("connected", False),
        ("reachable", False),
        ("report_late", True),
        ("operation_mode", "boost"),
        ("effective_session_policy", "continuous"),
        ("dial_in_interval_min", 720),
        ("device_clock_status", "invalid"),
        ("need_time_iin", True),
        ("tcp_probe_status", "connecting"),
    ],
)
def test_semantik_alanlar_imzayi_degistirir(alan: str, deger: Any) -> None:
    a = build_device_record("D1", _saglik())
    b = build_device_record("D1", _saglik(**{alan: deger}))
    assert semantic_signature(a) != semantic_signature(b), (
        f"{alan} degisimi delta TETIKLEMIYOR — gercek durum degisikligi kaybolur"
    )


# ==========================================================================
# A / B / C — STORM YOK
# ==========================================================================


def test_a_report_overdue_artarken_post_storm_yok() -> None:
    """LATE cihazda `report_overdue_sec` 1,3,5,7... artiyor."""
    durum = {"gecikme": 1.0}
    kaynak = lambda: {  # noqa: E731
        "D1": _saglik(report_late=True, report_overdue_sec=durum["gecikme"])
    }
    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        assert _bekle(lambda: g.sayi() >= 1), "ilk snapshot gelmedi"
        ilk = g.sayi()
        for _ in range(15):
            durum["gecikme"] += 2.0
            y.mark_dirty()
            time.sleep(0.05)
        time.sleep(0.4)
        assert g.sayi() == ilk, f"{g.sayi() - ilk} gereksiz POST uretildi (storm)"
    finally:
        y.stop()


def test_b_last_frame_her_cycle_degisirken_storm_yok() -> None:
    """`continuous` cihaz: her saniye taze frame geliyor.

    Sahada bildirilenden DAHA GENIS etki: LATE olmayan, sadece telemetri
    alan cihazlar da storm uretiyordu.
    """
    t = {"v": 1_755_600_000.0}
    kaynak = lambda: {  # noqa: E731
        "D1": _saglik(
            configured_session_policy="continuous",
            effective_session_policy="continuous",
            last_frame_epoch=t["v"],
            last_valid_contact_epoch=t["v"],
        )
    }
    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        assert _bekle(lambda: g.sayi() >= 1)
        ilk = g.sayi()
        for _ in range(15):
            t["v"] += 1.0
            y.mark_dirty()
            time.sleep(0.05)
        time.sleep(0.4)
        assert g.sayi() == ilk, f"{g.sayi() - ilk} gereksiz POST (continuous storm)"
    finally:
        y.stop()


def test_c_clock_offset_degisirken_storm_yok() -> None:
    o = {"v": 0.1}
    kaynak = lambda: {"D1": _saglik(device_clock_offset_sec=o["v"])}  # noqa: E731
    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        assert _bekle(lambda: g.sayi() >= 1)
        ilk = g.sayi()
        for _ in range(12):
            o["v"] += 0.7
            y.mark_dirty()
            time.sleep(0.05)
        time.sleep(0.4)
        assert g.sayi() == ilk, f"{g.sayi() - ilk} gereksiz POST (clock offset storm)"
    finally:
        y.stop()


# ==========================================================================
# D / E — GERCEK DEGISIM YAYINLANIR
# ==========================================================================


def test_d_clock_invalid_to_ok_delta_uretir() -> None:
    durum = {"clock": "invalid"}
    kaynak = lambda: {"D1": _saglik(device_clock_status=durum["clock"])}  # noqa: E731
    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        assert _bekle(lambda: g.sayi() >= 1)
        ilk = g.sayi()
        durum["clock"] = "ok"
        y.mark_dirty()
        assert _bekle(lambda: g.sayi() > ilk), "clock durumu degisti ama delta YOK"
        assert g.govdeler[-1]["devices"][0]["device_clock_status"] == "ok"
    finally:
        y.stop()


def test_e_baglanti_gecisleri_zamaninda_yayinlanir() -> None:
    """`smart_idle -> recovering -> online -> smart_idle` HER adimda."""
    durum = {"state": "smart_idle"}
    kaynak = lambda: {"D1": _saglik(state=durum["state"])}  # noqa: E731
    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        assert _bekle(lambda: g.sayi() >= 1)
        for hedef in ("recovering", "online", "smart_idle"):
            onceki = g.sayi()
            durum["state"] = hedef
            y.mark_dirty()
            assert _bekle(lambda n=onceki: g.sayi() > n), f"{hedef} gecisi yayinlanmadi"
            assert g.govdeler[-1]["devices"][0]["connection_state"] == hedef
    finally:
        y.stop()


# ==========================================================================
# F — PERIYODIK SNAPSHOT tum GUNCEL gozlem alanlarini tasir
# ==========================================================================


def test_f_snapshot_guncel_observational_alanlari_tasir() -> None:
    durum = {"gecikme": 5.0, "frame": 1_755_600_000.0}
    kaynak = lambda: {  # noqa: E731
        "D1": _saglik(
            report_late=True,
            report_overdue_sec=durum["gecikme"],
            last_frame_epoch=durum["frame"],
        )
    }
    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        assert _bekle(lambda: g.sayi() >= 1)
        # Yalnizca gozlem alanlari degisiyor -> delta YOK.
        durum["gecikme"] = 999.0
        durum["frame"] = 1_755_699_999.0
        y.mark_dirty()
        time.sleep(0.3)
        onceki = g.sayi()

        # Elle uzlastirma: snapshot GUNCEL degerleri tasimali.
        y.request_snapshot()
        assert _bekle(lambda: g.sayi() > onceki), "snapshot gonderilmedi"
        z = g.govdeler[-1]
        assert z["snapshot"] is True
        kayit = z["devices"][0]
        assert kayit["report_overdue_sec"] == 999.0, "snapshot BAYAT gozlem degeri tasidi"
        assert kayit["last_frame_epoch"] == 1_755_699_999.0
        # Sozlesme alanlari DEGISMEDI.
        for alan in ("boot_id", "sequence", "snapshot_id", "snapshot_batch_index"):
            assert alan in z, f"snapshot sozlesmesi bozuldu: {alan} yok"
    finally:
        y.stop()
