"""G110 (Octet String) okuma bloklari cihaz basina turetilir.

SAHADA GOZLENEN
---------------
Horstmann Pole Master Kit'ten string sinyalleri HIC okunmuyordu; ayni kod
SN2.0'da calisiyordu.

Iki ayri hata vardi:

1. ARALIK SN2'YE GORE ELLE SABITLENMISTI.
   `_G110_RANGES = ((3, 23), (65000, 65020))` bir SINIF SABITIYDI ve SN2.0'in
   index haritasiydi. Pole Master Kit'in string'leri 0-3, 5-50, 65000-65003
   araliklarinda; sabit aralik disinda kalan 30 nokta (SIM CCID, sebeke
   bilgileri, sat04-sat09'un tamami) hic ISTENMIYORDU. Ayni gateway'de iki
   model birlikte bulunabildigi icin bu bir sinif sabiti OLAMAZ.

2. ILK BAGLANTIDA HIC TETIKLENMIYORDU.
   `scan_g110_once` yalnizca `request_integrity_poll` icinden cagriliyordu;
   o da lost-probe / stale / relink / manuel refresh yollarindan geliyordu.
   Duzgun baglanip HIC KOPMAYAN bir cihazda string okumasi hicbir zaman
   tetiklenmiyordu — kapsam icindeki noktalar bile gelmiyordu. Sahadaki
   SN2'ler surekli kopup baglandigi icin bu eksik maskelenmisti.

BU DOSYA NEYI KILITLER
  * SN2.0 icin turetilen bloklar eski sabitle BIREBIR ayni (regresyon yok),
  * Pole Master Kit icin dogru bloklar cikiyor,
  * genis tek aralik (0-65535 gibi) asla uretilmiyor — Mayis 2026'da cihazi
    bozdu (revert 1302b83),
  * G110 sinyali olmayan cihaza hic istek gitmiyor,
  * master ilk kuruldugunda string okumasi tetikleniyor.
"""

from __future__ import annotations

from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.adapters.dnp3_yadnp3_master import _g110_bloklari
from tests.conftest import make_device, make_signal


def _g110(indexler: list[int]) -> list:
    return [
        make_signal(key=f"master.str{i}", data_type="string", object_group=110, index=i) for i in indexler
    ]


# SN2.0 gercek string index haritasi (Device Profile).
SN2_INDEXLER = [3, *range(5, 18), *range(19, 24), *range(65000, 65004), *range(65009, 65015), 65020]

# Pole Master Kit gercek string index haritasi.
PMK_INDEXLER = [*range(0, 4), *range(5, 51), *range(65000, 65004)]


# ------------------------------------------------------------- blok turetme
def test_sn2_bloklari_eski_sabitle_birebir_ayni() -> None:
    """REGRESYON KILIDI: SN2 davranisi degismemeli.

    Eski sinif sabiti ((3, 23), (65000, 65020)) idi; turetilen bloklar
    bununla ayni cikmazsa sahadaki SN2'lerin string okumasi bozulur.
    """
    assert _g110_bloklari(_g110(SN2_INDEXLER)) == ((3, 23), (65000, 65020))


def test_pole_master_kit_bloklari() -> None:
    """Asil hata: bu cihazin 54 string'inin 30'u hic istenmiyordu."""
    assert _g110_bloklari(_g110(PMK_INDEXLER)) == ((0, 50), (65000, 65003))


def test_sn2_ve_pmk_ayni_gatewayde_farkli_bloklar_alir() -> None:
    """Bloklar cihaz basina — sinif sabiti olsaydi biri digerini ezerdi."""
    assert _g110_bloklari(_g110(SN2_INDEXLER)) != _g110_bloklari(_g110(PMK_INDEXLER))


def test_g110_olmayan_cihaz_bos_doner() -> None:
    signals = [make_signal(object_group=30, index=0), make_signal(object_group=1, index=5)]
    assert _g110_bloklari(signals) == ()


def test_sadece_g110_sinyalleri_dikkate_alinir() -> None:
    """Analog/binary index'leri araliklara SIZMAMALI."""
    signals = [
        make_signal(key="a", object_group=30, index=900),
        *_g110([3, 4, 5]),
        make_signal(key="b", object_group=1, index=0),
    ]
    assert _g110_bloklari(signals) == ((3, 5),)


def test_bosluk_esigi_ayirir() -> None:
    """8'den buyuk mesafe yeni blok acar; kucuk/esit olan birlestirir."""
    assert _g110_bloklari(_g110([0, 8])) == ((0, 8),)  # mesafe 8 -> birlesir
    assert _g110_bloklari(_g110([0, 9])) == ((0, 0), (9, 9))  # mesafe 9 -> ayrilir


def test_tekrarli_ve_sirasiz_indexler_normalize_edilir() -> None:
    assert _g110_bloklari(_g110([7, 3, 5, 3, 7])) == ((3, 7),)


def test_genis_tek_aralik_asla_uretilmez(caplog: pytest.LogCaptureFixture) -> None:
    """0-65535 gibi genis aralik cihazi bozmustu (Mayis 2026, revert 1302b83).

    Yogun ve genis bir index dagilimi bile blok genislik tavanina bolunur.
    """
    bloklar = _g110_bloklari(_g110(list(range(0, 2000))))
    assert bloklar, "noktalar kaybolmamali"
    for bas, son in bloklar:
        assert son - bas + 1 <= mod._G110_BLOK_MAX_GENISLIK
    # Kirpilmis/parcalanmis olmasi sessiz kalmamali.
    assert any("g110_blok" in r.getMessage() for r in caplog.records)


def test_cok_parcali_dagilimda_blok_sayisi_kapakli(caplog: pytest.LogCaptureFixture) -> None:
    """Her blok ayri bir DNP3 read task'i; sinirsiz olamaz."""
    indexler = [i * 100 for i in range(40)]
    bloklar = _g110_bloklari(_g110(indexler), device_code="d1")
    assert len(bloklar) <= mod._G110_MAX_BLOK
    assert any("g110_blok_sayisi" in r.getMessage() for r in caplog.records), "kirpma sessiz olmamali"


# --------------------------------------------- ilk baglantida tetikleme
class _SahteMaster:
    def __init__(self) -> None:
        self.g110_ranges: tuple[tuple[int, int], ...] = ()
        self.connection_fingerprint: tuple = ()
        self.scan_sayisi = 0
        self.cache = object()
        self.device = None

    def scan_g110_once(self) -> bool:
        self.scan_sayisi += 1
        return True


@pytest.fixture
def okuyucu(monkeypatch: pytest.MonkeyPatch):
    import threading

    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._lock = threading.Lock()
    r._masters = {}
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    return r


def _master_yakala(okuyucu, monkeypatch: pytest.MonkeyPatch) -> list[_SahteMaster]:
    uretilen: list[_SahteMaster] = []

    def _sahte(*a: Any, **kw: Any) -> _SahteMaster:
        m = _SahteMaster()
        uretilen.append(m)
        return m

    monkeypatch.setattr(mod, "_ManagedMaster", _sahte)
    return uretilen


def test_ilk_kurulumda_string_okumasi_tetiklenir(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """ASIL DUZELTME 2: hic kopmayan cihazda string'ler hic okunmuyordu."""
    uretilen = _master_yakala(okuyucu, monkeypatch)
    device = make_device(code="PMK-1")

    mm = okuyucu._ensure_master(device, _g110(PMK_INDEXLER))

    assert uretilen and mm is uretilen[0]
    assert mm.scan_sayisi == 1, "Enable() sonrasi bir kez string okunmali"
    assert mm.g110_ranges == ((0, 50), (65000, 65003))


def test_g110_olmayan_cihazda_istek_gonderilmez(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bos G110 setinde ScanRange cagrilmamali."""
    _master_yakala(okuyucu, monkeypatch)
    device = make_device(code="d1")

    mm = okuyucu._ensure_master(device, [make_signal(object_group=30, index=0)])

    assert mm.g110_ranges == ()
    # scan_g110_once cagrilir ama bos aralikta hicbir ScanRange uretmez;
    # gercek davranis `scan_g110_once` icinde erken donus ile kilitli.
    assert mod._g110_bloklari([make_signal(object_group=30, index=0)]) == ()


def test_ikinci_cagride_tekrar_taranmaz(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """One-shot: her poll cycle'inda string taramasi YAPILMAZ."""
    _master_yakala(okuyucu, monkeypatch)
    device = make_device(code="PMK-1")
    signals = _g110(PMK_INDEXLER)

    mm = okuyucu._ensure_master(device, signals)
    for _ in range(5):
        okuyucu._ensure_master(device, signals)

    assert mm.scan_sayisi == 1


def test_komut_yolu_bloklari_silmez(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """`signals=None` (komut gonderme) okuma yolunun bloklarini bozmamali."""
    _master_yakala(okuyucu, monkeypatch)
    device = make_device(code="PMK-1")
    mm = okuyucu._ensure_master(device, _g110(PMK_INDEXLER))

    okuyucu._ensure_master(device)  # komut yolu

    assert mm.g110_ranges == ((0, 50), (65000, 65003))
    assert mm.scan_sayisi == 1


def test_sonradan_string_eklenirse_bir_kez_taranir(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ilk temas komutla olmus ya da config refresh string eklemis olabilir."""
    _master_yakala(okuyucu, monkeypatch)
    device = make_device(code="d1")

    mm = okuyucu._ensure_master(device, [make_signal(object_group=30, index=0)])
    assert mm.scan_sayisi == 1  # kurulum aninda (bos aralikla, istek uretmez)

    okuyucu._ensure_master(device, _g110([3, 4, 5]))

    assert mm.g110_ranges == ((3, 5),)
    assert mm.scan_sayisi == 2, "bos -> dolu gecisinde bir kez taranmali"
