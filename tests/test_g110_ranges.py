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


# ============================================================================
# TETIKLEME ANI — v1.6.2'de EKSIK KALAN KISIM
# ----------------------------------------------------------------------------
# v1.6.2 taramayi `_ensure_master` icinden, `Enable()`in HEMEN ardindan
# cagiriyordu ve yorumu "task kuyruga alinir, oturum acilinca kosar" diyordu.
# Bu YANLISTI: `Enable()` asenkron, o noktada TCP oturumu ACIK DEGIL
# (listening modda cihaz kendi baglanana kadar dakikalar surebilir).
# opendnp3'te ad-hoc ScanRange master OFFLINE iken kuyruga ALINMAZ, aninda
# fail edilir — ama `ScanRange()` exception atmadigi icin kod "queued" yazip
# True donuyordu: SAHTE BASARI.
#
# Sahadaki SN2'ler surekli flap ettigi icin recovery/relink yolundan
# `request_integrity_poll` -> `scan_g110_once` tetikleniyor ve sorun
# maskeleniyordu. Kararli hattaki Pole Master Kit bu yola hic girmiyor ve
# string'leri hicbir zaman gelmiyordu.
# ============================================================================


def _cache() -> Any:
    return mod._DeviceCache()


def test_link_kapaliyken_tarama_istenmez() -> None:
    """Bayrak kurulmus olsa bile link kapaliyken istek gonderilmez."""
    c = _cache()
    c.g110_iste()
    assert c.g110_gerekli() is True  # bayrak duruyor
    # ...ama gercek gonderim `scan_g110_once` icinde link kontrolunden gecer:
    # link kapaliyken sahte basari uretmeden False doner (asagidaki test).


def test_scan_g110_once_link_kapaliyken_sahte_basari_uretmez() -> None:
    """ESKI HATA: link kapaliyken bile 'queued' yazip True donuyordu."""

    class _Master:
        def __init__(self) -> None:
            self.cache = _cache()
            self.g110_ranges = ((3, 5),)
            self.device = make_device(code="d1")
            self.istek = 0

        def _g110_gvid(self):
            return object()

    m = _Master()
    # cache baglanti kurmadi -> is_connected False
    assert mod._ManagedMaster.scan_g110_once(m) is False
    assert m.istek == 0


def test_baglanti_acilinca_bayrak_kurulur() -> None:
    c = _cache()
    assert c.g110_gerekli() is False, "baglanti yokken istenmez"
    c.set_connected(True)
    c.g110_iste()  # OnOpen bunu yapar
    assert c.g110_gerekli() is True


def test_deger_gelmezse_backoff_ile_tekrar_denenir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tek atisa guvenilmez: istek dusmus olabilir."""
    c = _cache()
    c.g110_iste()
    assert c.g110_gerekli() is True  # 1. deneme
    assert c.g110_gerekli() is False, "backoff dolmadan tekrar istenmez"

    # Backoff dolsun
    ileri = [0.0]
    gercek = mod.time.monotonic

    def _sahte():
        return gercek() + ileri[0]

    monkeypatch.setattr(mod.time, "monotonic", _sahte)
    ileri[0] = mod._G110_BACKOFF_BASE_SEC + 1.0
    assert c.g110_gerekli() is True, "backoff dolunca yeniden denenmeli"


def test_deger_gelince_bir_daha_denenmez(monkeypatch: pytest.MonkeyPatch) -> None:
    """PERIYODIK SCAN YOK: string'ler statik, basarili okumadan sonra durur."""
    c = _cache()
    c.g110_iste()
    assert c.g110_gerekli() is True

    # Cihazdan gercek bir G110 degeri geldi.
    c.set(110, 3, 0.0, value_string="SN-12345")

    gercek = mod.time.monotonic
    monkeypatch.setattr(mod.time, "monotonic", lambda: gercek() + 10_000.0)
    assert c.g110_gerekli() is False, "deger geldikten sonra tekrar istenmemeli"
    bekliyor, geldi, _ = c.g110_durum()
    assert geldi is True and bekliyor is False


def test_deneme_tavani_ve_uyari(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sonsuza kadar denemez; tavana ulasinca sessiz kalmaz."""
    c = _cache()
    c.g110_iste()
    gercek = mod.time.monotonic
    ileri = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: gercek() + ileri[0])

    deneme = 0
    for _ in range(mod._G110_MAX_DENEME + 5):
        ileri[0] += mod._G110_BACKOFF_MAX_SEC + 1.0
        if c.g110_gerekli():
            deneme += 1
    assert deneme == mod._G110_MAX_DENEME
    assert c.g110_tukendi_mi() is True, "tavan doldu, operator uyarilmali"


def test_link_kopup_acilinca_yeniden_tetiklenir() -> None:
    """Cihaz degistirilmis / konfigi guncellenmis olabilir."""
    c = _cache()
    c.set_connected(True)
    c.g110_iste()
    c.g110_gerekli()
    c.set(110, 3, 0.0, value_string="SN-1")
    assert c.g110_gerekli() is False

    c.set_connected(False)  # link koptu
    c.set_connected(True)
    c.g110_iste()  # sonraki OnOpen
    assert c.g110_gerekli() is True


# ------------------------------------------- adapter seviyesinde tetikleme
class _SahteMaster:
    def __init__(self, cache: Any, device: Any) -> None:
        self.cache = cache
        self.device = device
        self.g110_ranges: tuple[tuple[int, int], ...] = ()
        self.connection_fingerprint: tuple = ()
        self.scan_sayisi = 0
        self.last_command_at = 0.0
        # G110 senaryolari varsayilan `continuous` politikayla kosar;
        # Smart dallarina hic girilmez.
        self.session_policy = "continuous"

    def scan_g110_once(self) -> bool:
        self.scan_sayisi += 1
        return True

    def request_integrity_poll(self) -> bool:
        return True

    def shutdown(self) -> None:
        pass


@pytest.fixture
def okuyucu():
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    # Native `DNP3Manager` KURULMAZ; calisma-zamani alanlari gercek nesnenin
    # kullandigi TEK kaynaktan gelir (elle liste tutulsaydi yeni bir alan
    # eklendiginde test sessizce gercek nesneden ayrisirdi).
    r._init_runtime_state()
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    r._publish_dnp3_quality = False
    return r


def test_master_kurulurken_tarama_gonderilmez(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """KABUL KRITERI: link henuz acik degilken hicbir ScanRange gitmez."""
    uretilen: list[_SahteMaster] = []

    def _sahte(*a: Any, **kw: Any) -> _SahteMaster:
        m = _SahteMaster(_cache(), kw.get("device"))
        uretilen.append(m)
        return m

    monkeypatch.setattr(mod, "_ManagedMaster", _sahte)
    device = make_device(code="PMK-1")

    mm = okuyucu._ensure_master(device, _g110(PMK_INDEXLER))

    assert mm.scan_sayisi == 0, "Enable() sonrasi ANINDA tarama gonderilmemeli"
    assert mm.g110_ranges == ((0, 50), (65000, 65003)), "aralilar yine de kurulmali"


def test_link_acilinca_ilk_cycle_bir_kez_tarar(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """KABUL KRITERI: OnOpen sonrasi ilk read_device'da TAM BIR KEZ."""
    device = make_device(code="PMK-1")
    signals = _g110([3, 4, 5])
    mm = _SahteMaster(_cache(), device)
    mm.g110_ranges = ((3, 5),)
    okuyucu._masters[device.code] = mm
    monkeypatch.setattr(okuyucu, "_ensure_master", lambda d, s=None: mm)

    # Link acildi (OnOpen'in yaptigi)
    mm.cache.set_connected(True)
    mm.cache.g110_iste()

    okuyucu.read_device(device=device, signals=signals)
    assert mm.scan_sayisi == 1

    # Ayni cycle'lar tekrar etse de backoff dolmadan yeniden istenmez.
    for _ in range(5):
        okuyucu.read_device(device=device, signals=signals)
    assert mm.scan_sayisi == 1
