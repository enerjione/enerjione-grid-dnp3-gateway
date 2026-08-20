"""G-SMART-02 — `session_policy=auto`: MASTER Operation Mode -> etkin politika.

TASARIMIN IKI KRITIK NOKTASI
----------------------------
1. DEGER MI, BAYRAK MI. Dokumantasyon `0x01 = Boost`, `0x81 = Smart` der.
   Bunlar noktanin DEGERI DEGIL tam DNP3 bayrak oktetidir; 0x80 biti STATE
   (yani degerin kendisi). Kutuphane ile dogrulandi. Dolayisiyla:
       deger 1 -> SMART,  deger 0 -> BOOST
   Naif "1 = Boost" varsayimi Smart cihazi surekli taratir ve modemini
   hicbir zaman kapattirmazdi.

2. BASLANGIC DURUMU. `auto` mod gozlenene kadar SESSIZ baslar (tarama yok,
   acilis integrity yok). Siniflandirma ugruna tarama kurmak, cihaz gercekten
   Smart ise onun 15 saniyelik idle sayacini surekli sifirlardi.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend import DeviceConfig, GatewayConfigError
from dnp3_gateway.backend.config_client import _parse_gateway_config
from dnp3_gateway.operation_mode import (
    BOOST_RAW_VALUE,
    DNP3_BINARY_STATE_BIT,
    HORSTMANN_FLAGS_BOOST,
    HORSTMANN_FLAGS_SMART,
    MODE_BOOST,
    MODE_SMART,
    MODE_UNKNOWN,
    SMART_RAW_VALUE,
    normalize_operation_mode,
    reset_warning_state,
    resolve_master_operation_mode_signal,
)

from .conftest import make_device, make_signal

OP_IDX = 15
SAT_IDX = 16
BOOST_ENABLED_IDX = 63
AKIM_IDX = 2

SINYALLER = [
    make_signal("master.operation_mode", source="master", data_type="binary", object_group=1, index=OP_IDX),
    make_signal("sat01.operation_mode", source="sat01", data_type="binary", object_group=1, index=SAT_IDX),
    make_signal(
        "master.boost_mode_enabled",
        source="master",
        data_type="binary",
        object_group=1,
        index=BOOST_ENABLED_IDX,
    ),
    make_signal("master.boost_mode", source="master", data_type="binary_output", object_group=10, index=26),
    make_signal(
        "master.actual_current", source="master", data_type="analog", object_group=30, index=AKIM_IDX
    ),
]


@pytest.fixture(autouse=True)
def _uyari_sifirla():
    reset_warning_state()


# ==========================================================================
# 2C — HAM DEGER YORUMU (bayraktan TURETILIR)
# ==========================================================================


def test_2c_deger_bayrak_oktetinden_turetilir() -> None:
    """Dokumandaki 0x01/0x81 DEGER DEGIL BAYRAK oktetidir.

    Turetme adim adim pinlenir ki biri "1 = Boost" sanip tersine cevirmesin.
    """
    assert DNP3_BINARY_STATE_BIT == 0x80
    assert HORSTMANN_FLAGS_BOOST == 0x01
    assert HORSTMANN_FLAGS_SMART == 0x81
    # 0x01 -> STATE biti YOK -> deger 0 ; 0x81 -> STATE biti VAR -> deger 1
    assert BOOST_RAW_VALUE == 0
    assert SMART_RAW_VALUE == 1


def test_2c_kutuphane_state_bitini_dogruluyor() -> None:
    """yadnp3 bayragi degerden URETIYOR — varsayim degil, olculmus gercek."""
    opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    assert opendnp3.Binary(False, opendnp3.Flags(0x01)).flags.value == HORSTMANN_FLAGS_BOOST
    assert opendnp3.Binary(True, opendnp3.Flags(0x01)).flags.value == HORSTMANN_FLAGS_SMART


def test_2c_esleme() -> None:
    assert normalize_operation_mode(1) == MODE_SMART
    assert normalize_operation_mode(1.0) == MODE_SMART
    assert normalize_operation_mode(0) == MODE_BOOST
    assert normalize_operation_mode(0.0) == MODE_BOOST


def test_2c_esleme_ters_cevrilebilir() -> None:
    """Saha dogrulamasi tersini gosterirse kod degisikligi GEREKMEZ."""
    assert normalize_operation_mode(0, smart_raw_value=0) == MODE_SMART
    assert normalize_operation_mode(1, smart_raw_value=0) == MODE_BOOST


@pytest.mark.parametrize("ham", [None, 2.0, -1, float("nan"), "x", object()])
def test_2c_beklenmeyen_deger_unknown(ham: Any) -> None:
    assert normalize_operation_mode(ham) == MODE_UNKNOWN


# --- SINIR DEGERLERI: YALNIZCA TAM 0/1 ------------------------------------


@pytest.mark.parametrize("ham", [0, 0.0, -0.0, False, "0"])
def test_2c_tam_sifir_boost(ham: Any) -> None:
    assert normalize_operation_mode(ham) == MODE_BOOST


@pytest.mark.parametrize("ham", [1, 1.0, True, "1"])
def test_2c_tam_bir_smart(ham: Any) -> None:
    assert normalize_operation_mode(ham) == MODE_SMART


@pytest.mark.parametrize(
    "ham",
    [0.1, 0.49, 0.5, 0.51, 0.9, 0.999999, 1.000001, 1.1, 1.49, 1.5, 1.9, 2.0, -0.4, -1.0],
)
def test_2c_kesirli_degerler_yuvarlanmaz(ham: float) -> None:
    """YUVARLAMA FAIL-SAFE DEGILDIR.

    Onceki hali `int(round(float(raw)))` idi: `0.51`/`0.9`/`1.1`/`1.49`
    sessizce SMART'a, `0.1`/`0.49`/`0.5`/`-0.4` sessizce BOOST'a dusuyordu.
    Binary bir noktadan gelen 0/1 disi deger yorumlanacak bir sey degil,
    ANLASILAMAYAN bir sinyaldir; ona mod atamak cihazi yanlislikla susturmak
    (modem acik kalir) ya da gereksiz taramak (Smart mod bozulur) demektir.
    """
    assert normalize_operation_mode(ham) == MODE_UNKNOWN, f"{ham!r} bir moda YUVARLANDI — fail-safe degil"


@pytest.mark.parametrize("ham", [float("nan"), float("inf"), float("-inf")])
def test_2c_nan_ve_inf_unknown_ve_istisna_atmaz(ham: float) -> None:
    """`inf` eskiden `OverflowError` FIRLATIYORDU (`int(inf)`).

    O istisna `_auto_politikayi_coz` uzerinden `read_device` yoluna
    sizabilirdi; artik sessizce `unknown`a duser.
    """
    assert normalize_operation_mode(ham) == MODE_UNKNOWN


def test_2c_ters_esleme_de_tam_deger_ister() -> None:
    """`smart_raw_value` ters cevrildiginde de yuvarlama YOK."""
    assert normalize_operation_mode(0, smart_raw_value=0) == MODE_SMART
    assert normalize_operation_mode(1, smart_raw_value=0) == MODE_BOOST
    assert normalize_operation_mode(0.51, smart_raw_value=0) == MODE_UNKNOWN
    assert normalize_operation_mode(0.9, smart_raw_value=0) == MODE_UNKNOWN


# ==========================================================================
# 27-30 — SINYAL SECIMI (semantik, index sabitlenmez)
# ==========================================================================


def test_29_master_operation_mode_secilir() -> None:
    s = resolve_master_operation_mode_signal(make_device(), SINYALLER)
    assert s is not None and s.key == "master.operation_mode" and s.dnp3_index == OP_IDX


def test_27_satellite_operation_mode_kullanilmaz() -> None:
    """Satellite politikaya GIRMEZ — hucresel oturum Master'a aittir."""
    yalniz_sat = [x for x in SINYALLER if x.source == "sat01"]
    assert resolve_master_operation_mode_signal(make_device(), yalniz_sat) is None


def test_28_boost_mode_enabled_kullanilmaz() -> None:
    """`Boost Mode Enabled` KONFIGURASYONDUR, calisma anindaki durum DEGIL."""
    yalniz = [x for x in SINYALLER if x.key == "master.boost_mode_enabled"]
    assert resolve_master_operation_mode_signal(make_device(), yalniz) is None


def test_28b_boost_mode_komut_noktasi_kullanilmaz() -> None:
    yalniz = [x for x in SINYALLER if x.key == "master.boost_mode"]
    assert resolve_master_operation_mode_signal(make_device(), yalniz) is None


def test_30_index_sabitlenmemis_pole_master_da_bulunur() -> None:
    """Pole Master farkli index kullanir; kodda `index == 15` dali OLMAMALI."""
    pm = [
        make_signal("master.operation_mode", source="master", data_type="binary", object_group=1, index=31),
        make_signal("sat09.operation_mode", source="sat09", data_type="binary", object_group=1, index=40),
    ]
    s = resolve_master_operation_mode_signal(make_device(), pm)
    assert s is not None and s.dnp3_index == 31


def test_30b_kaynak_kodunda_sabit_index_dali_yok() -> None:
    """Sozlesme kilidi: genel adapter'da `index == 15` karar dali BULUNMAMALI."""
    import re

    for yol in ("src/dnp3_gateway/adapters/dnp3_yadnp3_master.py", "src/dnp3_gateway/operation_mode.py"):
        metin = (Path(__file__).resolve().parents[1] / yol).read_text(encoding="utf-8")
        # `index == 15` / `dnp3_index == 15` gibi karar dallari
        assert not re.search(r"index\s*==\s*15\b", metin), f"{yol}: sabit index dali var"


def test_belirsiz_aday_unknown_uretir() -> None:
    iki = [
        make_signal("master.operation_mode", source="master", data_type="binary", object_group=1, index=15),
        make_signal(
            "polemaster.operation_mode", source="polemaster", data_type="binary", object_group=1, index=31
        ),
    ]
    assert resolve_master_operation_mode_signal(make_device(), iki) is None


# ==========================================================================
# 22-26 — CONFIG SOZLESMESI
# ==========================================================================


def _parse_dev(**kw: Any):
    ham: dict[str, Any] = {"code": "D1", "ip_address": "10.0.0.10", "dnp3_address": 1}
    ham.update(kw)
    return _parse_gateway_config(
        {"config_version": "v1", "devices": [ham], "signals": []}, default_gateway_code="GW-001"
    ).devices[0]


def test_22_session_policy_eksik_continuous() -> None:
    assert _parse_dev().session_policy == "continuous"


def test_23_continuous_kabul() -> None:
    assert _parse_dev(session_policy="continuous").session_policy == "continuous"


def test_24_smart_kabul() -> None:
    d = _parse_dev(session_policy="smart", ip_endpoint_type="initiating", master_ip_port=20100)
    assert d.session_policy == "smart"


def test_25_auto_kabul() -> None:
    d = _parse_dev(session_policy="auto", ip_endpoint_type="initiating", master_ip_port=20100)
    assert d.session_policy == "auto"


@pytest.mark.parametrize("deger", ["otomatik", "AUTO_X", "smrt", "boost", "1"])
def test_26_gecersiz_session_policy_reddedilir(deger: str) -> None:
    with pytest.raises(GatewayConfigError):
        _parse_dev(session_policy=deger)


@pytest.mark.parametrize("politika", ["smart", "auto"])
def test_26b_listening_ile_smart_ve_auto_kabul_edilir(politika: str) -> None:
    """1.14.0: uc tipi ile mod BAGIMSIZ iki kavramdir.

    1.13.0'da `smart`/`auto` + `listening` config seviyesinde REDDEDILIYORDU.
    Bu kisit KALDIRILDI: uc tipi baglantiyi KIMIN actigini soyler, Operation
    Mode ise cihazin modemini kapatip kapatmadigini. Sabit IP'li bir
    Horstmann'in Smart modda calismasi gercek ve yaygin bir kurulumdur.

    Reddetmek YANLIS SONUC uretiyordu: ya kurulum hic yapilamiyordu ya da
    cihaz `continuous` kosturulup modemi hicbir zaman kapanmiyordu.
    """
    d = _parse_dev(session_policy=politika, ip_endpoint_type="listening")
    assert d.session_policy == politika
    assert (d.ip_endpoint_type or "listening") == "listening"


# ==========================================================================
# ADAPTER DAVRANISI — taklit master ile
# ==========================================================================


class SahteMaster(mod._OturumDurumu):
    """`_ManagedMaster` taklidi; GERCEK `_DeviceCache` tasir.

    Politika/mod alanlari `_OturumDurumu`dan gelir — gercek sinifla AYNI
    kaynak. Elle kopyalandiklarinda taklit sessizce sapiyordu.
    """

    def __init__(self, device: DeviceConfig, *, session_policy: str, known_mode: str | None = None) -> None:
        self.device = device
        self.cache = mod._DeviceCache()
        self._oturum_durumunu_kur(session_policy=session_policy, known_mode=known_mode)
        self.cache.set_session_policy(self.session_policy)
        self.connection_fingerprint: tuple = ()
        self.g110_ranges: tuple = ()
        self.last_command_at = 0.0
        self.listen_port = device.master_ip_port
        self.integrity_sayisi = 0
        self.g110_scan_sayisi = 0
        self.shutdown_sayisi = 0
        self.scan_eklendi = 0

    def request_integrity_poll(self) -> bool:
        self.integrity_sayisi += 1
        return True

    def scan_g110_once(self) -> bool:
        self.g110_scan_sayisi += 1
        return True

    @property
    def gateway_trafigi(self) -> int:
        return self.integrity_sayisi + self.g110_scan_sayisi

    def shutdown(self) -> None:
        self.shutdown_sayisi += 1

    def enable_periodic_scans(self) -> bool:
        if self.session_policy != "smart":
            return False
        self.session_policy = "continuous"
        self.cache.set_session_policy("continuous")
        self.scan_eklendi += 1
        return True

    def kanit_esigi_sn(self) -> float:
        return mod._bayatlik_esigi_sn(5, 30)

    def kanit_yasi(self) -> float:
        k = self.cache.last_evidence_at()
        return -1.0 if k == 0.0 else (mod.time.monotonic() - k)

    def ulasilabilir(self) -> bool:
        return self.cache.is_connected() and 0 <= self.kanit_yasi() <= self.kanit_esigi_sn()


class Saha:
    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.masterlar: dict[str, SahteMaster] = {}
        self.politikalar: dict[str, str] = {}

    def cihaz(self, code: str, *, policy: str = "auto") -> DeviceConfig:
        d = replace(
            make_device(code),
            session_policy=policy,
            ip_endpoint_type="initiating" if policy in ("smart", "auto") else "listening",
            master_ip_port=20100 + len(self.politikalar) if policy in ("smart", "auto") else None,
        )
        self.politikalar[code] = policy
        self._ensure(d)
        return d

    def _ensure(self, device: DeviceConfig) -> SahteMaster:
        mevcut = self.reader._masters.get(device.code)
        if mevcut is not None:
            return mevcut
        politika = self.reader._session_policy(device)
        mm = SahteMaster(
            device,
            session_policy=politika,
            known_mode=self.reader._bilinen_auto_mod.get(device.code),
        )
        self.reader._durumu_geri_yukle(mm, device)
        self.masterlar[device.code] = mm
        self.reader._masters[device.code] = mm
        return mm

    def master(self, code: str) -> SahteMaster:
        return self.masterlar[code]

    def oku(self, d: DeviceConfig) -> list:
        return self.reader.read_device(device=d, signals=SINYALLER)

    def baglan(self, code: str) -> None:
        mm = self.masterlar[code]
        mm.cache.set_connected(True)
        mm.cache.begin_recovery()

    def mod_bildir(self, code: str, deger: float) -> None:
        self.masterlar[code].cache.set(1, OP_IDX, deger)

    def kapat(self, code: str) -> None:
        self.masterlar[code].cache.set_connected(False)


def _reader(tmp_path: Path, **kw: Any) -> Any:
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(session_store_path=str(tmp_path / "s.json"), **kw)
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    r._publish_dnp3_quality = False
    return r


@pytest.fixture
def saha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Saha:
    r = _reader(tmp_path)
    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    return s


# ==========================================================================
# 31-34 — BASLANGIC DURUMU VE SINIFLANDIRMA
# ==========================================================================


def test_31_32_auto_siniflandirmadan_once_sessiz(saha: Saha) -> None:
    """KRITIK: mod bilinmeden periyodik tarama KURULMAZ.

    Kurulsaydi, cihaz gercekten Smart ise 15 saniyelik idle sayaci surekli
    sifirlanir ve modem hicbir zaman kapanmazdi.
    """
    d = saha.cihaz("SN2-1", policy="auto")
    mm = saha.master("SN2-1")
    assert mm.configured_session_policy == "auto"
    assert mm.session_policy == "smart", "auto sessiz baslamali (tarama yok)"
    assert mm.auto_pending is True
    assert mm.operation_mode == MODE_UNKNOWN

    saha.baglan("SN2-1")
    trafik = mm.gateway_trafigi
    for _ in range(30):
        saha.oku(d)
    assert mm.scan_eklendi == 0, "siniflandirmadan once periyodik tarama kurulmus"
    assert mm.gateway_trafigi == trafik, "siniflandirmadan once yoklama gonderilmis"

    saglik = saha.reader.device_health()["SN2-1"]
    assert saglik["effective_session_policy"] == "unknown", "belirsizlik health'te gorunmeli"
    assert saglik["operation_mode"] == "unknown"


def test_33_gozlenen_smart_etkin_smart(saha: Saha) -> None:
    d = saha.cihaz("SN2-1", policy="auto")
    saha.baglan("SN2-1")
    saha.mod_bildir("SN2-1", SMART_RAW_VALUE)
    saha.oku(d)

    mm = saha.master("SN2-1")
    assert mm.operation_mode == MODE_SMART
    assert mm.session_policy == "smart"
    assert mm.auto_pending is False
    assert mm.shutdown_sayisi == 0, "zaten sessizdi — REBUILD GEREKMEZ"
    assert saha.reader.device_health()["SN2-1"]["effective_session_policy"] == "smart"


def test_34_gozlenen_boost_etkin_continuous(saha: Saha) -> None:
    d = saha.cihaz("SN2-1", policy="auto")
    saha.baglan("SN2-1")
    saha.mod_bildir("SN2-1", BOOST_RAW_VALUE)
    saha.oku(d)

    mm = saha.master("SN2-1")
    assert mm.operation_mode == MODE_BOOST
    assert mm.session_policy == "continuous"
    assert mm.scan_eklendi == 1, "Boost'ta periyodik tarama devreye girmeli"
    assert mm.shutdown_sayisi == 0, "acik oturum YIKILMAMALI"


def test_31b_auto_fallback_baglantili_surede_olculur(saha: Saha) -> None:
    """Cihaz hic baglanmadiysa sayac ILERLEMEZ.

    Ilerleseydi, gunde bir baglanan bir cihaz icin taramalar cihaz yokken
    kurulur ve baglandigi anda idle sayacini bozardi.
    """
    d = saha.cihaz("SN2-1", policy="auto")
    mm = saha.master("SN2-1")
    gercek = mod.time.monotonic
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.time, "monotonic", lambda: gercek() + 10_000)
        saha.oku(d)  # BAGLI DEGIL
    assert mm.auto_fallback is False
    assert mm.session_policy == "smart", "baglanti yokken continuous'a dusulmus"


def test_31c_auto_fallback_sure_dolunca_continuous(saha: Saha, caplog: pytest.LogCaptureFixture) -> None:
    """Mod hic gelmezse GUVENLI TARAFA dusulur ve bu SESSIZ OLMAZ."""
    d = saha.cihaz("SN2-1", policy="auto")
    saha.baglan("SN2-1")
    saha.oku(d)  # sayac baslar
    mm = saha.master("SN2-1")

    gercek = mod.time.monotonic
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.time, "monotonic", lambda: gercek() + mod._AUTO_KARAR_TIMEOUT_SEC + 5)
        with caplog.at_level(logging.WARNING):
            saha.oku(d)

    assert mm.session_policy == "continuous"
    assert mm.auto_fallback is True
    assert any("auto_policy_fallback" in r.getMessage() for r in caplog.records)
    assert saha.reader.device_health()["SN2-1"]["auto_fallback"] is True


# ==========================================================================
# 27 (runtime) — Satellite politikayi ELE GECIREMEZ
# ==========================================================================


def test_27_runtime_satellite_modu_politikayi_degistirmez(saha: Saha) -> None:
    """Master SMART iken Satellite BOOST -> politika SMART kalir."""
    d = saha.cihaz("SN2-1", policy="auto")
    saha.baglan("SN2-1")
    mm = saha.master("SN2-1")
    mm.cache.set(1, OP_IDX, SMART_RAW_VALUE)  # Master = Smart
    mm.cache.set(1, SAT_IDX, BOOST_RAW_VALUE)  # Satellite = Boost
    mm.cache.set(1, BOOST_ENABLED_IDX, 1.0)  # Boost Mode Enabled = true
    saha.oku(d)

    assert mm.operation_mode == MODE_SMART
    assert mm.session_policy == "smart", "Satellite/BoostEnabled politikayi ele gecirmis"


# ==========================================================================
# 35-38 — GECISLER ve IZOLASYON
# ==========================================================================


def test_35_ayni_mod_tekrar_rebuild_uretmez(saha: Saha) -> None:
    d = saha.cihaz("SN2-1", policy="auto")
    saha.baglan("SN2-1")
    saha.mod_bildir("SN2-1", BOOST_RAW_VALUE)
    saha.oku(d)
    mm = saha.master("SN2-1")
    scan_once, shutdown_once = mm.scan_eklendi, mm.shutdown_sayisi

    for _ in range(20):
        saha.mod_bildir("SN2-1", BOOST_RAW_VALUE)
        saha.oku(d)
    assert mm.scan_eklendi == scan_once, "ayni mod tekrar tarama eklemis"
    assert mm.shutdown_sayisi == shutdown_once, "ayni mod rebuild tetiklemis (flap)"


def test_36_37_smart_boost_gecisleri(saha: Saha) -> None:
    """SMART <-> BOOST gecisleri; yalnizca ILGILI cihaz etkilenir."""
    a = saha.cihaz("A", policy="auto")
    b = saha.cihaz("B", policy="auto")
    for kod in ("A", "B"):
        saha.baglan(kod)
        saha.mod_bildir(kod, SMART_RAW_VALUE)
    saha.oku(a)
    saha.oku(b)
    a_mm, b_mm = saha.master("A"), saha.master("B")
    assert (a_mm.session_policy, b_mm.session_policy) == ("smart", "smart")

    # --- SMART -> BOOST (B): tarama calisma aninda eklenir, REBUILD YOK ---
    b_durum_once = (a_mm.session_policy, a_mm.shutdown_sayisi, a_mm.scan_eklendi)
    saha.mod_bildir("B", BOOST_RAW_VALUE)
    saha.oku(b)
    assert b_mm.session_policy == "continuous"
    assert b_mm.scan_eklendi == 1
    assert b_mm.shutdown_sayisi == 0, "acik oturum gereksiz yere yikilmis"
    assert (a_mm.session_policy, a_mm.shutdown_sayisi, a_mm.scan_eklendi) == b_durum_once, (
        "A cihazi B'nin gecisinden etkilendi"
    )

    # --- BOOST -> SMART (B): tarama kaldirilamaz -> YALNIZCA B rebuild ---
    saha.mod_bildir("B", SMART_RAW_VALUE)
    saha.oku(b)
    assert b_mm.shutdown_sayisi == 1, "Boost->Smart icin master yeniden kurulmali"
    assert "B" not in saha.reader._masters
    assert saha.reader._bilinen_auto_mod["B"] == MODE_SMART
    assert a_mm.shutdown_sayisi == 0, "A cihazinin master'i da yikilmis"
    assert "A" in saha.reader._masters


def test_38_smart_ve_boost_cihazlar_bagimsiz(saha: Saha) -> None:
    s = saha.cihaz("SMART-1", policy="auto")
    b = saha.cihaz("BOOST-1", policy="auto")
    saha.baglan("SMART-1")
    saha.baglan("BOOST-1")
    saha.mod_bildir("SMART-1", SMART_RAW_VALUE)
    saha.mod_bildir("BOOST-1", BOOST_RAW_VALUE)
    saha.oku(s)
    saha.oku(b)

    s_mm, b_mm = saha.master("SMART-1"), saha.master("BOOST-1")
    assert s_mm.session_policy == "smart"
    assert b_mm.session_policy == "continuous"

    # Smart cihaz uykuya girer; Boost cihaz etkilenmez.
    saha.kapat("SMART-1")
    saha.oku(s)
    assert s_mm.cache.state() == "smart_idle"
    assert b_mm.cache.state() == "online", "Boost komsu Smart uykusundan etkilendi"

    trafik = s_mm.gateway_trafigi
    for _ in range(40):
        saha.oku(s)
        saha.oku(b)
    assert s_mm.gateway_trafigi == trafik, "uyuyan cihaza trafik uretildi"


# ==========================================================================
# 39-40 — KALICILIK
# ==========================================================================


def test_39_restart_gozlenen_modu_korur(tmp_path: Path, monkeypatch) -> None:
    """Restart sonrasi `auto` yeniden sifirdan siniflandirmaya baslamamali.

    Baslasaydi Boost bir cihaz siniflandirma penceresi boyunca YOKLANMADAN
    kalirdi (telemetri bosluk).
    """
    yol = str(tmp_path / "s.json")

    def _yeni():
        r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
        r._init_runtime_state(session_store_path=yol)
        r._scan_interval_sec, r._baseline_interval_sec = 5, 30
        r._local_address, r._default_dnp3_tcp_port = 1, 20000
        r._time_sync, r._manager, r._publish_dnp3_quality = "lan", None, False
        return r

    r1 = _yeni()
    s1 = Saha(r1)
    monkeypatch.setattr(r1, "_ensure_master", lambda d, sig=None: s1._ensure(d))
    d = s1.cihaz("SN2-1", policy="auto")
    s1.baglan("SN2-1")
    s1.mod_bildir("SN2-1", BOOST_RAW_VALUE)
    s1.oku(d)
    assert s1.master("SN2-1").operation_mode == MODE_BOOST
    r1._session_store.flush(force=True)

    # --- restart ---
    r2 = _yeni()
    s2 = Saha(r2)
    monkeypatch.setattr(r2, "_ensure_master", lambda dd, sig=None: s2._ensure(dd))
    s2.cihaz("SN2-1", policy="auto")
    mm = s2.master("SN2-1")
    assert mm.operation_mode == MODE_BOOST, "gozlenen mod restart'ta kayboldu"
    assert mm.session_policy == "continuous", "Boost cihaz yeniden sessiz baslamis"
    assert mm.auto_pending is False


def test_39b_restart_smart_idle_ve_modu_birlikte_korur(tmp_path: Path, monkeypatch) -> None:
    yol = str(tmp_path / "s.json")

    def _yeni():
        r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
        r._init_runtime_state(session_store_path=yol)
        r._scan_interval_sec, r._baseline_interval_sec = 5, 30
        r._local_address, r._default_dnp3_tcp_port = 1, 20000
        r._time_sync, r._manager, r._publish_dnp3_quality = "lan", None, False
        return r

    r1 = _yeni()
    s1 = Saha(r1)
    monkeypatch.setattr(r1, "_ensure_master", lambda d, sig=None: s1._ensure(d))
    d = s1.cihaz("SN2-1", policy="auto")
    s1.baglan("SN2-1")
    s1.mod_bildir("SN2-1", SMART_RAW_VALUE)
    s1.oku(d)
    s1.kapat("SN2-1")
    s1.oku(d)
    assert s1.master("SN2-1").cache.state() == "smart_idle"
    r1._session_store.flush(force=True)

    r2 = _yeni()
    s2 = Saha(r2)
    monkeypatch.setattr(r2, "_ensure_master", lambda dd, sig=None: s2._ensure(dd))
    d2 = s2.cihaz("SN2-1", policy="auto")
    mm = s2.master("SN2-1")
    assert mm.operation_mode == MODE_SMART
    assert mm.cache.state() == "smart_idle", "restart sonrasi idle geri yuklenmedi"
    assert all(x.quality != "comm_lost" for x in s2.oku(d2)), "sahte comm_lost uretildi"


def test_40_bozuk_kalici_kayit_guvenli(tmp_path: Path, monkeypatch) -> None:
    """Bozuk kayit -> gateway YINE ACILIR, uydurma durum YOK."""
    yol = tmp_path / "s.json"
    yol.write_text("{bozuk", encoding="utf-8")
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(session_store_path=str(yol))
    r._scan_interval_sec, r._baseline_interval_sec = 5, 30
    r._local_address, r._default_dnp3_tcp_port = 1, 20000
    r._time_sync, r._manager, r._publish_dnp3_quality = "lan", None, False

    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    d = s.cihaz("SN2-1", policy="auto")
    mm = s.master("SN2-1")
    assert mm.operation_mode == MODE_UNKNOWN
    assert mm.auto_pending is True
    assert mm.cache.is_smart_idle() is False
    s.oku(d)  # crash olmamali


def test_40b_eski_surum_kayit_yok_sayilir(tmp_path: Path) -> None:
    """Store surumu degisti (v1 -> v2); eski dosya GUVENLI sekilde atlanir."""
    import json

    from dnp3_gateway.session_state_store import STORE_VERSION, SessionStateStore

    assert STORE_VERSION == 2
    yol = tmp_path / "s.json"
    yol.write_text(json.dumps({"version": 1, "devices": {"A": {"state": "smart_idle"}}}), encoding="utf-8")
    store = SessionStateStore(yol)
    assert store.load() == 0
    assert store.get("A") is None
