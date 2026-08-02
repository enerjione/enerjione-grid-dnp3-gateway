"""Cihazin kendi olay zaman damgasi (B2) — ve ona GUVENMEME.

NEDEN GEREKLI
-------------
Gateway `source_timestamp` olarak KENDI saatini basiyor ve bir cihazin tum
sinyallerine ayni degeri veriyor. 4G link 10 dakika koparsa outstation
olaylari KENDI damgalariyla event buffer'inda biriktirir; link gelince hepsi
tek fragment'ta bosalir ve gateway hepsini ayni saniyeye damgalar. Ariza
SURESI ve olay SIRASI kalici olarak kaybolur.

NEDEN GUVENMIYORUZ
------------------
Saha cihazinin RTC pili biter ve cihaz 2000-01-01'den saymaya baslar. Boyle
bir damgayi oldugu gibi kabul etmek olay verisini 26 yil geriye tasimak
demektir. Damga DOGRULANIR; gecmezse OLCUM ATILMAZ — yalnizca damga dusulur
ve `timestamp_quality` neden duruldugunu soyler.

Testlerin agirligi "kabul etme" tarafinda: yanlis bir damgayi kabul etmek,
hic damga almamaktan daha zararlidir (yanlis veriye guvenilir).
"""

from __future__ import annotations

from datetime import datetime, timezone

from dnp3_gateway.adapters.base import SignalReading
from dnp3_gateway.adapters.dnp3_yadnp3_master import (
    _extract_device_time,
    dogrula_cihaz_zamani,
)
from dnp3_gateway.poller import build_telemetry_payload

from .conftest import make_device

_SIMDI = 1_780_000_000.0  # sabit referans; testler saate bagli olmasin


# --------------------------------------------------------------------------
# Dogrulama — asil deger burada
# --------------------------------------------------------------------------


def test_makul_damga_kabul_edilir() -> None:
    epoch, kalite = dogrula_cihaz_zamani(_SIMDI - 30.0, "synchronized", _SIMDI)
    assert epoch == _SIMDI - 30.0
    assert kalite == "synchronized"


def test_rtc_pili_bitmis_cihazin_2000_damgasi_reddedilir() -> None:
    """Klasik saha vakasi: pil bitmis, cihaz 2000-01-01'den sayiyor.

    Kabul edilseydi olay 26 yil geriye damgalanir ve ariza suresi analizi
    tamamen bozulurdu.
    """
    y2k = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
    epoch, kalite = dogrula_cihaz_zamani(y2k, "synchronized", _SIMDI)
    assert epoch is None, "2000 damgasi kabul edildi"
    assert kalite == "invalid"


def test_gelecege_damgali_olay_reddedilir() -> None:
    """Saati ileri kaymis cihaz. Kabul edilse SOE siralamasi bozulurdu."""
    epoch, kalite = dogrula_cihaz_zamani(_SIMDI + 3600.0, "synchronized", _SIMDI)
    assert epoch is None
    assert kalite == "invalid"


def test_kucuk_saat_farki_tolere_edilir() -> None:
    """Cihaz ve gateway saatleri birkac saniye ayrisabilir; bu normaldir.

    Toleransi sifir yapmak, saglikli cihazlarin damgalarini de atardi.
    """
    epoch, _ = dogrula_cihaz_zamani(_SIMDI + 30.0, "synchronized", _SIMDI)
    assert epoch is not None


def test_link_kopmasindan_sonra_biriken_olay_kabul_edilir() -> None:
    """Bu ozelligin VAR OLMA sebebi: 10 dakika once olmus bir olay.

    Gecmis penceresi bunu elerse ozellik hicbir ise yaramaz."""
    epoch, kalite = dogrula_cihaz_zamani(_SIMDI - 600.0, "synchronized", _SIMDI)
    assert epoch == _SIMDI - 600.0
    assert kalite == "synchronized"


def test_cok_eski_damga_reddedilir() -> None:
    epoch, kalite = dogrula_cihaz_zamani(_SIMDI - 30 * 24 * 3600.0, "synchronized", _SIMDI)
    assert epoch is None
    assert kalite == "invalid"


def test_damga_yoksa_dogrulama_bir_sey_uydurmaz() -> None:
    """Statik/Class 0 okumasinda damga YOKTUR. Eksiklik degil, o okumanin
    dogasi — gateway saati uydurup damga gibi gostermemeli."""
    epoch, kalite = dogrula_cihaz_zamani(None, None, _SIMDI)
    assert epoch is None
    assert kalite is None


def test_cihaz_senkronsuz_dediyse_kalite_korunur() -> None:
    """Cihaz "saatim senkron degil" diyorsa, damga makul olsa BILE bu bilgi
    kaybolmamali — backend/analiz buna gore karar verir."""
    epoch, kalite = dogrula_cihaz_zamani(_SIMDI - 5.0, "unsynchronized", _SIMDI)
    assert epoch == _SIMDI - 5.0
    assert kalite == "unsynchronized"


# --------------------------------------------------------------------------
# Govdeye tasinma
# --------------------------------------------------------------------------


def _okuma(**kw) -> SignalReading:
    temel = dict(
        signal_key="master.fault_passage",
        source="master",
        data_type="binary",
        raw_value=1.0,
        scaled_value=1.0,
        quality="good",
    )
    temel.update(kw)
    return SignalReading(**temel)


def test_damga_govdeye_iso_olarak_giriyor() -> None:
    p = build_telemetry_payload(
        gateway_code="GW-001",
        device=make_device("DEV-1"),
        reading=_okuma(device_event_at=_SIMDI - 600.0, timestamp_quality="synchronized"),
        correlation_id="c",
    )
    assert p["timestamp_quality"] == "synchronized"
    cozulmus = datetime.fromisoformat(p["device_event_at"])
    assert cozulmus.tzinfo is not None, "damga UTC-aware olmali"
    assert abs(cozulmus.timestamp() - (_SIMDI - 600.0)) < 1.0


def test_source_timestamp_hala_gateway_saati() -> None:
    """`source_timestamp`in anlami DEGISMEMELI.

    O alan backend'de historian'in birincil anahtarinin parcasi, TimescaleDB
    partition kolonu ve retention silme kriteri. Cihaz zamanini oraya yazmak
    gecmise damgali satirlarla INSERT'i patlatabilir, ileriye damgali
    satirlari ise retention'a gorunmez kilardi.
    """
    p = build_telemetry_payload(
        gateway_code="GW-001",
        device=make_device("DEV-1"),
        reading=_okuma(device_event_at=_SIMDI - 600.0, timestamp_quality="synchronized"),
        correlation_id="c",
        now_iso="2026-08-02T21:00:00+00:00",
    )
    assert p["source_timestamp"] == "2026-08-02T21:00:00+00:00"
    assert p["device_event_at"] != p["source_timestamp"]


def test_damga_yoksa_alanlar_none_ama_olcum_yayinlanir() -> None:
    p = build_telemetry_payload(
        gateway_code="GW-001",
        device=make_device("DEV-1"),
        reading=_okuma(),
        correlation_id="c",
    )
    assert p["device_event_at"] is None
    assert p["timestamp_quality"] is None
    # ASIL MESELE: olcum yine de gidiyor.
    assert p["value"] == 1.0
    assert p["quality"] == "good"


# --------------------------------------------------------------------------
# Cihazin KENDI beyani (`_extract_device_time`)
# --------------------------------------------------------------------------


class _SahteDamga:
    """opendnp3 DNPTime benzeri: ms degeri + kalite beyani."""

    def __init__(self, ms: int, quality_name: str | None = None) -> None:
        self.value = ms
        if quality_name is not None:
            self.quality = type("K", (), {"name": quality_name})()


class _SahteOlcum:
    def __init__(self, time_obj) -> None:
        self.time = time_obj


def test_cihaz_senkron_degilim_derse_bu_bilgi_kaybolmaz() -> None:
    """Damga makul araliktaysa BILE cihazin beyani korunmali.

    Yutulursa: senkronsuz bir saatten gelen damga "synchronized" olarak
    kaydedilir ve SOE analizi ona tam guvenir. Bu, damgayi hic almamaktan
    daha zararli — yanlis veriye guven uretir.
    """
    olcum = _SahteOlcum(_SahteDamga(int(_SIMDI * 1000), "UNSYNCHRONIZED"))
    _epoch, kalite = _extract_device_time(olcum)
    assert kalite == "unsynchronized"


def test_cihaz_damgam_gecersiz_derse_invalid_olur() -> None:
    olcum = _SahteOlcum(_SahteDamga(int(_SIMDI * 1000), "INVALID"))
    _epoch, kalite = _extract_device_time(olcum)
    assert kalite == "invalid"


def test_beyan_yoksa_senkron_sayilir() -> None:
    """Kalite alani tasimayan binding'lerde damga kullanilabilir kalmali."""
    olcum = _SahteOlcum(_SahteDamga(int(_SIMDI * 1000)))
    epoch, kalite = _extract_device_time(olcum)
    assert epoch is not None
    assert kalite == "synchronized"


def test_epoch_sifir_damgasi_gecersiz_sayilir() -> None:
    """ "Saat hic kurulmamis" hali. Damga YOK gibi davranmak yanlis olurdu:
    cihaz bir sey SOYLEDI ve soyledigi sey gecersiz."""
    olcum = _SahteOlcum(_SahteDamga(0))
    epoch, kalite = _extract_device_time(olcum)
    assert epoch is None
    assert kalite == "invalid"


def test_damga_alani_hic_yoksa_sessizce_none() -> None:
    epoch, kalite = _extract_device_time(_SahteOlcum(None))
    assert epoch is None and kalite is None
