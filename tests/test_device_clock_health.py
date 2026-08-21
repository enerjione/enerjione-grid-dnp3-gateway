"""CIHAZ RTC SAGLIGI — `device_clock_status` turetimi (G / H / I / J).

SAHA BULGUSU
------------
Bir Horstmann'in RTC'si **2066** yilina kaymisti. Bu bilgi telemetri
tarafinda (damga reddi) goruluyordu ama CALISMA-ZAMANI SAGLIGINDA HIC
gorunmuyordu: operator "cihaz online, olcum geliyor" diyordu ve cihazin
kendi olay damgasinin cope gittigini bilmiyordu.

BU DOSYANIN SINAVI
------------------
1. Saat durumu DOGRU turetiliyor mu (invalid / need_time / ok / unknown),
2. Bu durum `connection_state`i ETKILEMIYOR ve OLCUM DUSURMUYOR mu,
3. Log KENAR-TETIKLI mi (ayni durum tekrar tekrar loglanmiyor).
"""

from __future__ import annotations

import logging
import time

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

_G_ANALOG = mod._OBJECT_GROUP_ANALOG_INPUT
_G_BINARY = mod._OBJECT_GROUP_BINARY_INPUT

#: 2066 — sahada gorulen degerin ta kendisi.
_EPOCH_2066 = 3_029_000_000.0


def _cache() -> mod._DeviceCache:
    return mod._DeviceCache()


# ==========================================================================
# G — 2026 gateway + 2066 cihaz -> invalid
# ==========================================================================


def test_g_gelecekteki_cihaz_damgasi_invalid_uretir() -> None:
    c = _cache()
    c.set(_G_ANALOG, 0, 12.5, device_time=_EPOCH_2066, time_quality="synchronized")
    durum, offset, epoch = c.device_clock_snapshot()
    assert durum == "invalid", f"2066 damgasi icin durum {durum!r} (beklenen 'invalid')"
    assert epoch == pytest.approx(_EPOCH_2066)
    assert offset is not None and offset > 0, "ileri kayan saat icin offset POZITIF olmali"


def test_g_gecmiste_kalmis_rtc_de_invalid() -> None:
    """RTC pili bitmis cihaz 2000-01-01'den sayar — klasik vaka."""
    c = _cache()
    c.set(_G_ANALOG, 0, 12.5, device_time=946_684_800.0, time_quality="synchronized")
    durum, offset, _ = c.device_clock_snapshot()
    assert durum == "invalid"
    assert offset is not None and offset < 0, "geride kalan saat icin offset NEGATIF olmali"


def test_g_olcum_saat_bozukken_de_korunur() -> None:
    """EN ONEMLI DEGISMEZ: saat gecersiz diye OLCUM DUSMEZ.

    Saat durumu bir GOZLEMdir; degerin kendisi gecerliligini korur.
    """
    c = _cache()
    c.set(_G_ANALOG, 7, 42.5, device_time=_EPOCH_2066, time_quality="synchronized")
    okunan = c.get(_G_ANALOG, 7)
    assert okunan is not None, "saat gecersizken olcum CACHE'E YAZILMADI"
    assert okunan[0] == pytest.approx(42.5)


def test_g_saat_durumu_baglanti_durumunu_etkilemez() -> None:
    """`connection_state` saat durumundan BAGIMSIZ kalmali."""
    c = _cache()
    c.set_connected(True)
    onceki = c.state()
    c.set(_G_ANALOG, 0, 1.0, device_time=_EPOCH_2066, time_quality="synchronized")
    assert c.device_clock_snapshot()[0] == "invalid"
    assert c.state() == onceki, (
        f"saat gecersizken connection_state {onceki!r} -> {c.state()!r} degisti — "
        "saglikli cihaz SCADA'da arizali gorunur"
    )


# ==========================================================================
# H — normal damga -> ok
# ==========================================================================


def test_h_normal_damga_ok_uretir() -> None:
    c = _cache()
    simdi = time.time()
    c.set(_G_ANALOG, 0, 12.5, device_time=simdi - 1.0, time_quality="synchronized")
    durum, offset, epoch = c.device_clock_snapshot()
    assert durum == "ok", f"makul damga icin durum {durum!r}"
    assert epoch == pytest.approx(simdi - 1.0)
    assert offset is not None and abs(offset) < 30.0, f"offset makul degil: {offset}"


def test_h_hic_damga_yoksa_unknown() -> None:
    """Yalnizca statik/Class 0 okumasi yapan cihaz damga GONDERMEZ.

    Bunu `invalid` saymak, saglikli bir cihazi arizali gostermek olurdu.
    """
    c = _cache()
    c.set(_G_ANALOG, 0, 12.5)
    assert c.device_clock_snapshot() == ("unknown", None, None)


def test_h_cihaz_kendi_damgasina_guvenme_derse_invalid() -> None:
    """Damga makul araliktaysa BILE cihazin kendi beyani onceliklidir."""
    c = _cache()
    c.set(_G_ANALOG, 0, 1.0, device_time=time.time(), time_quality="invalid")
    assert c.device_clock_snapshot()[0] == "invalid"


# ==========================================================================
# I — IIN1.4 NEED_TIME
# ==========================================================================


def test_i_need_time_damga_yokken_de_gorunur() -> None:
    c = _cache()
    c.note_iin("need_time", True)
    durum, _, _ = c.device_clock_snapshot()
    assert durum == "need_time", f"IIN1.4 asserted iken durum {durum!r}"
    assert c.iin_snapshot()["need_time"] is True


def test_i_gecerli_damga_ama_need_time_ise_need_time_kazanir() -> None:
    """Cihaz saat ISTIYORSA bunu `ok` altinda saklamak yaniltici olurdu."""
    c = _cache()
    c.set(_G_ANALOG, 0, 1.0, device_time=time.time(), time_quality="synchronized")
    c.note_iin("need_time", True)
    assert c.device_clock_snapshot()[0] == "need_time"


def test_i_invalid_need_timeden_once_gelir() -> None:
    """ONCELIK: `invalid` > `need_time`. Bu bilincli bir siralamadir.

    Zaman senkronizasyonu — hangi prosedur secilirse secilsin — TALEP
    GUDUMLUDUR. Saat yanlis AMA cihaz NEED_TIME bildirmiyorsa durum
    KENDILIGINDEN DUZELMEZ; `need_time` ise senkronizasyonla duzelir.
    Yani `invalid` daha kotu haberdir ve ustte durmalidir.
    """
    c = _cache()
    c.set(_G_ANALOG, 0, 1.0, device_time=_EPOCH_2066, time_quality="synchronized")
    c.note_iin("need_time", True)
    assert c.device_clock_snapshot()[0] == "invalid", (
        "saat KANITLANABILIR sekilde yanlisken 'need_time' gosterilirse "
        "operator 'senkronizasyon halleder' sanir — oysa duzelmez"
    )


def test_i_need_time_temizlenince_ok_a_doner() -> None:
    c = _cache()
    c.set(_G_ANALOG, 0, 1.0, device_time=time.time(), time_quality="synchronized")
    c.note_iin("need_time", True)
    assert c.device_clock_snapshot()[0] == "need_time"
    c.note_iin("need_time", False)
    assert c.device_clock_snapshot()[0] == "ok"


# ==========================================================================
# J — LOG STORM YOK (kenar-tetikli)
# ==========================================================================


class _SahteOturum(mod._OturumDurumu):
    """`_saat_durumunu_bildir` yalnizca `saat_durumu` alanina dokunur.

    `_OturumDurumu`dan MIRAS ALIR: alan listesi tek yerde durur, taklit
    sessizce gercekten SAPAMAZ (bkz. o sinifin docstring'i).
    """

    def __init__(self) -> None:
        self._oturum_durumunu_kur(session_policy="smart", known_mode=None)


def _bildir(rd, mm, durum: str, need_time: bool = False) -> None:
    rd._saat_durumunu_bildir(
        device_code="D1",
        mm=mm,
        durum=durum,
        offset=-40_000_000.0,
        epoch=_EPOCH_2066,
        need_time=need_time,
    )


def test_j_ayni_saat_durumu_tekrar_loglanmaz(caplog: pytest.LogCaptureFixture) -> None:
    """IIN her yanitla gelir; her frame'de loglamak defteri bogar."""
    rd = object.__new__(_bildirici_sinifi())
    mm = _SahteOturum()

    with caplog.at_level(logging.INFO, logger=mod.logger.name):
        for _ in range(20):
            _bildir(rd, mm, "invalid")

    satirlar = [r for r in caplog.records if "dnp3_device_clock" in r.message]
    assert len(satirlar) == 1, f"ayni durum {len(satirlar)} kez loglandi — kenar-tetikli degil"
    assert "dnp3_device_clock_invalid" in satirlar[0].message


def test_j_durum_degisince_bir_kez_loglanir(caplog: pytest.LogCaptureFixture) -> None:
    rd = object.__new__(_bildirici_sinifi())
    mm = _SahteOturum()

    with caplog.at_level(logging.INFO, logger=mod.logger.name):
        for _ in range(5):
            _bildir(rd, mm, "invalid")
        for _ in range(5):
            _bildir(rd, mm, "ok")
        for _ in range(5):
            _bildir(rd, mm, "need_time", need_time=True)

    olaylar = [r.message.split()[0] for r in caplog.records if "dnp3_device_clock" in r.message]
    assert olaylar == [
        "dnp3_device_clock_invalid",
        "dnp3_device_clock_recovered",
        "dnp3_device_clock_need_time",
    ], f"kenar loglari beklenen sirada degil: {olaylar}"


def test_j_unknown_durumu_gurultu_uretmez(caplog: pytest.LogCaptureFixture) -> None:
    """Cihaz hic damga gondermiyorsa bu bir ARIZA DEGILDIR."""
    rd = object.__new__(_bildirici_sinifi())
    mm = _SahteOturum()
    with caplog.at_level(logging.INFO, logger=mod.logger.name):
        _bildir(rd, mm, "unknown")
    assert not [r for r in caplog.records if "dnp3_device_clock" in r.message], (
        "'unknown' icin log uretildi — statik okuma yapan her cihaz gurultu yapar"
    )


def _bildirici_sinifi():
    """`_saat_durumunu_bildir`i tasiyan sinifi bul (ad degisirse test kirilsin)."""
    for ad in dir(mod):
        obj = getattr(mod, ad)
        if isinstance(obj, type) and "_saat_durumunu_bildir" in vars(obj):
            return obj
    raise AssertionError("_saat_durumunu_bildir hicbir sinifta bulunamadi")
