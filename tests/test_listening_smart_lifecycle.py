"""G-SMART-LISTEN-01 — `listening` ucta Smart/Boost/Auto + Dial-In sagligi.

KAPATILAN URETIM HATASI
-----------------------
1.13.0'da `session_policy` YALNIZCA `initiating` uc ile gecerliydi;
`listening` + `smart` sessizce `continuous`a dusuruluyordu. Bu, UC TIPI ile
OPERATION MODE'u birbirine karistiran bir hataydi:

    uc tipi          -> TCP baglantisini KIM acar
    operation mode   -> cihaz modemini KAPATIR MI

Ikisi BAGIMSIZDIR ve alti kombinasyonun hepsi sahada gecerlidir. Sabit IP'li
(ya da APN icinden erisilebilen) bir Horstmann Smart modda calisir: modemini
kapatir, gateway ona baglanamaz. 1.13.0 davranisi bu cihazi `continuous`
kosturuyordu; gateway her tarama araliginda frame gonderiyor, cihazin 15
saniyelik hareketsizlik sayaci HIC dolmuyor ve modem HICBIR ZAMAN
kapanmiyordu — yani ozelligin tamami calismiyordu.

BU DOSYANIN OLCTUGU SOZLESME
----------------------------
* Basarisiz TCP denemeleri TEK BASINA `comm_lost` URETMEZ.
* Uc ayri pencere KARISTIRILMAZ:
      beklenen rapordan ONCE           -> `smart_idle`, SAGLIKLI
      rapor gecti, max_silence dolmadi -> LATE (degraded), kopuk DEGIL
      max_silence asildi               -> `lost`, comm_lost TAM BIR KEZ
* ICMP/TCP sonda sonuclari SALT TESHIStir; hicbiri `comm_lost` uretmez.

`test_smart_session_policy.py` `initiating` yolunu olcer; bu dosya onun
`listening` esidir ve o dosyadaki davranislari DEGISTIRMEZ.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dnp3_gateway import network_probe
from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend import DeviceConfig
from dnp3_gateway.operation_mode import MODE_BOOST, MODE_SMART

from .conftest import make_device, make_signal
from .test_smart_session_policy import SINYALLER, SahteMaster

AKIM_IDX = 2
MOD_IDX = 15

MOD_SINYALI = make_signal(
    "master.operation_mode", data_type="binary", object_group=1, index=MOD_IDX
)
SINYALLER_MODLU = [*SINYALLER, MOD_SINYALI]


# ==========================================================================
# Saha — `listening` ucta tek gateway + N outstation
# ==========================================================================
class ListeningSaha:
    """`Saha` ile ayni yuzey, ama cihazlar `listening` ucta.

    UYKU MODELI FARKLI: `listening`te baglantiyi gateway acar. Cihaz
    uyudugunda `OnClose` DEGIL, HICBIR SEY olmaz — TCP baglantisi kurulamaz.
    Bu yuzden `uyudu()` yalnizca `connected=False` birakir ve idle'a girisin
    okuma yolundan yapilmasi BEKLENIR.
    """

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.masterlar: dict[str, SahteMaster] = {}

    def cihaz(self, code: str, **kw: Any) -> DeviceConfig:
        # Varsayilan `listening`; test acikca `initiating` isteyebilir
        # (komsu izolasyonu senaryolari icin gerekli).
        kw.setdefault("ip_endpoint_type", "listening")
        d = replace(make_device(code), **kw)
        self._ensure(d)
        return d

    def _ensure(self, device: DeviceConfig) -> SahteMaster:
        mevcut = self.reader._masters.get(device.code)
        if mevcut is not None:
            return mevcut
        mm = SahteMaster(device, session_policy=self.reader._session_policy(device))
        self.reader._durumu_geri_yukle(mm, device)
        self.masterlar[device.code] = mm
        self.reader._masters[device.code] = mm
        return mm

    def master(self, code: str) -> SahteMaster:
        return self.masterlar[code]

    def oku(self, device: DeviceConfig, signals: list | None = None) -> list:
        return self.reader.read_device(device=device, signals=signals or SINYALLER)

    def oku_ve_onayla(self, device: DeviceConfig, signals: list | None = None) -> list:
        okumalar = self.oku(device, signals)
        self.reader.commit_published(device=device, readings=okumalar)
        return okumalar

    # --- cihaz davranisi ---
    def gateway_bagladi(self, code: str) -> None:
        """Gateway TCP client olarak baglandi (`OnOpen`)."""
        mm = self.masterlar[code]
        mm.cache.set_connected(True)
        mm.cache.begin_recovery()

    def dnp3_kaniti(self, code: str, *, akim: float = 10.0) -> None:
        self.masterlar[code].cache.set(30, AKIM_IDX, akim)

    def mod_bildir(self, code: str, ham: float) -> None:
        """Cihaz MASTER `Operation Mode` noktasini raporladi (1=Smart, 0=Boost)."""
        self.masterlar[code].cache.set(1, MOD_IDX, ham)

    def uyudu(self, code: str) -> None:
        """Cihaz modemini kapatti: gateway artik baglanamaz."""
        self.masterlar[code].cache.set_connected(False)

    def yaslandir(self, code: str, saniye: float) -> None:
        """Son gecerli temasi `saniye` kadar GERIYE al (monotonic + duvar)."""
        c = self.masterlar[code].cache
        with c._lock:
            if c._last_valid_contact:
                c._last_valid_contact -= saniye
            if c._last_valid_contact_wall:
                c._last_valid_contact_wall -= saniye
        self.masterlar[code].olusturuldu_wall -= saniye


def _reader(tmp_path: Path, **kw: Any) -> Any:
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(session_store_path=str(tmp_path / "session_state.json"), **kw)
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    r._publish_dnp3_quality = False
    return r


@pytest.fixture
def saha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ListeningSaha:
    r = _reader(tmp_path)
    s = ListeningSaha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    # SONDALAR VARSAYILAN OLARAK KAPALI: gercek `ping`/TCP cagrisi yapan bir
    # birim testi hem yavas hem ortama bagimli olurdu. Sonda davranisi
    # KENDI testlerinde acikca olculur.
    monkeypatch.setattr(r, "_sonda_calistir", lambda mm, device: None)
    return s


def _durumlar(saha: ListeningSaha) -> dict[str, str]:
    return {k: v["state"] for k, v in saha.reader.device_health().items()}


# ==========================================================================
# L1 — listening + boost: 1.13.0 davranisi AYNEN korunur
# ==========================================================================


def test_l1_listening_boost_degismedi(saha: ListeningSaha) -> None:
    """En kritik geriye donuk uyumluluk testi: uretimdeki cihazlarin cogu bu.

    `session_policy` verilmemis bir `listening` cihaz `continuous` kalir,
    `smart_idle` diye bir sey GORMEZ ve baglanti kopunca NORMAL comm_lost
    yoluna girer.
    """
    d = saha.cihaz("BOOST-1")
    assert saha.master("BOOST-1").session_policy == "continuous"

    saha.gateway_bagladi("BOOST-1")
    saha.dnp3_kaniti("BOOST-1")
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["BOOST-1"] == "online"

    # Baglanti koptu -> `smart_idle` DEGIL, `lost`.
    saha.uyudu("BOOST-1")
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["BOOST-1"] == "lost"


# ==========================================================================
# L2 — listening + manuel smart: uyku KABUL EDILIR
# ==========================================================================


def test_l2_listening_smart_politika_dusurulmez(saha: ListeningSaha) -> None:
    """1.13.0 burada `continuous`a dusuyordu. Artik DUSMUYOR."""
    d = saha.cihaz("SMART-L1", session_policy="smart")
    assert saha.reader._session_policy(d) == "smart"
    assert saha.master("SMART-L1").session_policy == "smart"


def test_l3_baglanamama_comm_lost_uretmez(saha: ListeningSaha) -> None:
    """SOZLESMENIN KALBI.

    Cihaz uykudayken gateway'in TCP denemeleri BASARISIZ olur. Bu tek basina
    ariza DEGILDIR — uykunun TANIMIDIR. Cihaz `smart_idle` olmali ve SCADA
    onu kopuk GORMEMELIDIR.
    """
    d = saha.cihaz("SMART-L2", session_policy="smart", smart_max_silence_sec=3600)
    # Hic baglanti kurulmadi (cihaz uyuyor).
    saha.oku_ve_onayla(d)

    saglik = saha.reader.device_health()["SMART-L2"]
    assert saglik["state"] == "smart_idle", "basarisiz TCP denemesi comm_lost uretti"
    # Uyuyan cihaz FIZIKSEL olarak ulasilamazdir — sahte "iyi" URETILMEZ.
    assert saglik["reachable"] is False
    assert saglik["connected"] is False


def test_l4_gecerli_oturum_online_yapar(saha: ListeningSaha) -> None:
    """Cihaz uyanip veri verince NORMAL online akisi isler."""
    d = saha.cihaz("SMART-L3", session_policy="smart", smart_max_silence_sec=3600)
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SMART-L3"] == "smart_idle"

    saha.gateway_bagladi("SMART-L3")
    saha.dnp3_kaniti("SMART-L3", akim=12.5)
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SMART-L3"] == "online"


def test_l5_oturum_sonrasi_tekrar_uykuya_doner(saha: ListeningSaha) -> None:
    """Uyan -> veri ver -> uyu dongusu TEKRARLANABILIR olmali.

    Tek seferlik calisip sonra takilan bir gecis, sahada ilk gunden sonra
    her cihazi kopuk gosterirdi.
    """
    d = saha.cihaz("SMART-L4", session_policy="smart", smart_max_silence_sec=3600)
    for _ in range(3):
        saha.gateway_bagladi("SMART-L4")
        saha.dnp3_kaniti("SMART-L4")
        saha.oku_ve_onayla(d)
        assert _durumlar(saha)["SMART-L4"] == "online"

        saha.uyudu("SMART-L4")
        saha.oku_ve_onayla(d)
        assert _durumlar(saha)["SMART-L4"] == "smart_idle"


def test_l6_idle_sirasinda_gateway_kaynakli_trafik_yok(saha: ListeningSaha) -> None:
    """Modem ancak gateway SUSARSA kapanabilir — olcunun kendisi budur."""
    d = saha.cihaz("SMART-L5", session_policy="smart", smart_max_silence_sec=3600)
    saha.oku_ve_onayla(d)
    mm = saha.master("SMART-L5")
    baslangic = mm.gateway_trafigi

    for _ in range(25):
        saha.oku_ve_onayla(d)

    assert mm.gateway_trafigi == baslangic, "idle sirasinda gateway cihaza istek gonderdi"


# ==========================================================================
# L7-L9 — Dial-In farkindali saglik (LATE)
# ==========================================================================


def test_l7_rapor_penceresi_icinde_late_yok(saha: ListeningSaha) -> None:
    d = saha.cihaz(
        "DIAL-1", session_policy="smart", smart_max_silence_sec=86400, dial_in_interval_min=60
    )
    saha.gateway_bagladi("DIAL-1")
    saha.dnp3_kaniti("DIAL-1")
    saha.oku_ve_onayla(d)
    saha.uyudu("DIAL-1")
    saha.oku_ve_onayla(d)

    saglik = saha.reader.device_health()["DIAL-1"]
    assert saglik["state"] == "smart_idle"
    assert saglik["report_late"] is False
    assert saglik["report_overdue_sec"] == 0.0
    assert saglik["next_expected_report_epoch"] is not None


def test_l8_rapor_gecince_late_ama_comm_lost_yok(
    saha: ListeningSaha, caplog: pytest.LogCaptureFixture
) -> None:
    """LATE ile LOST ayrimi. Bu ikisi karistirilirsa ya erken alarm
    (operator korkar) ya da gec alarm (gercek ariza gizlenir) olur."""
    d = saha.cihaz(
        "DIAL-2", session_policy="smart", smart_max_silence_sec=86400, dial_in_interval_min=60
    )
    saha.gateway_bagladi("DIAL-2")
    saha.dnp3_kaniti("DIAL-2")
    saha.oku_ve_onayla(d)
    saha.uyudu("DIAL-2")
    saha.oku_ve_onayla(d)

    # Beklenen rapor (60dk) gecti; max_silence (24s) DOLMADI.
    saha.yaslandir("DIAL-2", 5400)
    with caplog.at_level("WARNING"):
        saha.oku_ve_onayla(d)

    saglik = saha.reader.device_health()["DIAL-2"]
    assert saglik["state"] == "smart_idle", "LATE cihaz kopuk ilan edildi"
    assert saglik["report_late"] is True
    assert saglik["report_overdue_sec"] > 1700
    assert any("smart_report_overdue" in r.getMessage() for r in caplog.records)


def test_l9_late_taze_temasla_duzelir(saha: ListeningSaha) -> None:
    d = saha.cihaz(
        "DIAL-3", session_policy="smart", smart_max_silence_sec=86400, dial_in_interval_min=60
    )
    saha.gateway_bagladi("DIAL-3")
    saha.dnp3_kaniti("DIAL-3")
    saha.oku_ve_onayla(d)
    saha.uyudu("DIAL-3")
    saha.yaslandir("DIAL-3", 5400)
    saha.oku_ve_onayla(d)
    assert saha.reader.device_health()["DIAL-3"]["report_late"] is True

    # Cihaz gec de olsa haber verdi.
    saha.gateway_bagladi("DIAL-3")
    saha.dnp3_kaniti("DIAL-3", akim=9.0)
    saha.oku_ve_onayla(d)

    saglik = saha.reader.device_health()["DIAL-3"]
    assert saglik["report_late"] is False
    assert saglik["state"] == "online"


def test_l10_dial_in_yoksa_late_uretilmez(saha: ListeningSaha) -> None:
    """Alanin olmamasi ARIZA DEGILDIR; her kurulumda Dial-In tanimli olmaz."""
    d = saha.cihaz("DIAL-4", session_policy="smart", smart_max_silence_sec=86400)
    saha.oku_ve_onayla(d)
    saha.yaslandir("DIAL-4", 40000)
    saha.oku_ve_onayla(d)

    saglik = saha.reader.device_health()["DIAL-4"]
    assert saglik["report_late"] is False
    assert saglik["next_expected_report_epoch"] is None
    assert saglik["state"] == "smart_idle"


# ==========================================================================
# L11 — max_silence: TAM BIR KEZ comm_lost
# ==========================================================================


def test_l11_max_silence_asilinca_tek_comm_lost(saha: ListeningSaha) -> None:
    """`lost`a gecis KALICI olmali.

    Eski bir hata sinifinda cihaz her cycle'da yeniden `smart_idle`e girip
    comm_lost'u SUSTURUYORDU: gercekten kaybolmus bir cihaz SCADA'da son iyi
    degeriyle canli gorunmeye devam ederdi.
    """
    d = saha.cihaz("SIL-1", session_policy="smart", smart_max_silence_sec=3600)
    saha.gateway_bagladi("SIL-1")
    saha.dnp3_kaniti("SIL-1")
    saha.oku_ve_onayla(d)
    saha.uyudu("SIL-1")
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SIL-1"] == "smart_idle"

    saha.yaslandir("SIL-1", 7200)
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SIL-1"] == "lost"

    # SONRAKI CYCLE'LAR: `smart_idle`e GERI DONMEMELI.
    for _ in range(5):
        saha.oku_ve_onayla(d)
        assert _durumlar(saha)["SIL-1"] == "lost", "cihaz idle'a geri donup comm_lost'u sustur"


def test_l12_lost_cihaz_taze_temasla_geri_gelir(saha: ListeningSaha) -> None:
    """`lost` KALICI ama GERI DONULEMEZ DEGIL."""
    d = saha.cihaz("SIL-2", session_policy="smart", smart_max_silence_sec=3600)
    saha.gateway_bagladi("SIL-2")
    saha.dnp3_kaniti("SIL-2")
    saha.oku_ve_onayla(d)
    saha.uyudu("SIL-2")
    saha.yaslandir("SIL-2", 7200)
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SIL-2"] == "lost"

    saha.gateway_bagladi("SIL-2")
    saha.dnp3_kaniti("SIL-2", akim=3.0)
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SIL-2"] == "online"

    saha.uyudu("SIL-2")
    saha.oku_ve_onayla(d)
    assert _durumlar(saha)["SIL-2"] == "smart_idle", "kurtarma sonrasi uyku yeniden kabul edilmedi"


# ==========================================================================
# L13-L16 — listening + auto
# ==========================================================================


def test_l13_auto_mod_bilinmiyorken_sessiz_baslar(saha: ListeningSaha) -> None:
    """Siniflandirma ugruna tarama kurmak, cihaz Smart ise idle sayacini
    surekli sifirlardi. Bilinmiyorken SESSIZ baslamak dogru varsayilan."""
    d = saha.cihaz("AUTO-L1", session_policy="auto")
    mm = saha.master("AUTO-L1")
    assert mm.configured_session_policy == "auto"
    assert mm.session_policy == "smart"

    saglik = saha.reader.device_health()[d.code]
    assert saglik["effective_session_policy"] == "unknown"
    assert saglik["operation_mode"] == "unknown"


def test_l14_auto_smart_gozlenince_smart_kalir(saha: ListeningSaha) -> None:
    d = saha.cihaz("AUTO-L2", session_policy="auto", smart_max_silence_sec=3600)
    saha.gateway_bagladi("AUTO-L2")
    saha.mod_bildir("AUTO-L2", 1.0)  # 0x81'in STATE biti -> SMART
    saha.oku_ve_onayla(d, SINYALLER_MODLU)

    mm = saha.master("AUTO-L2")
    assert mm.operation_mode == MODE_SMART
    assert mm.session_policy == "smart"
    assert mm.scan_etkinlestirme_sayisi == 0, "Smart cihazda periyodik tarama acildi"

    # Uyku KABUL EDILIR.
    saha.uyudu("AUTO-L2")
    saha.oku_ve_onayla(d, SINYALLER_MODLU)
    assert _durumlar(saha)["AUTO-L2"] == "smart_idle"


def test_l15_auto_boost_gozlenince_continuous_olur(saha: ListeningSaha) -> None:
    """Boost'a gecis ACIK OTURUMU YIKMAZ — tarama calisma aninda eklenir."""
    d = saha.cihaz("AUTO-L3", session_policy="auto")
    saha.gateway_bagladi("AUTO-L3")
    saha.mod_bildir("AUTO-L3", 0.0)  # 0x01 -> BOOST
    saha.oku_ve_onayla(d, SINYALLER_MODLU)

    mm = saha.master("AUTO-L3")
    assert mm.operation_mode == MODE_BOOST
    assert mm.session_policy == "continuous"
    assert mm.scan_etkinlestirme_sayisi == 1
    assert mm.shutdown_sayisi == 0, "Boost'a gecerken calisan oturum yikildi"


def test_l16_auto_listening_siniflandirma_pollu_oturum_basina_bir(
    saha: ListeningSaha, caplog: pytest.LogCaptureFixture
) -> None:
    """`listening`te gateway hic sormazsa mod HIC ogrenilemez.

    Baglanti KURULMUS olmasi cihazin uyanik oldugunun kanitidir; tek bir
    integrity poll 15sn'lik sayaci BIR KEZ sifirlar, sonra gateway susar.
    Her cycle'da sormak Smart Mode'u imkansiz kilardi.
    """
    d = saha.cihaz("AUTO-L4", session_policy="auto")
    mm = saha.master("AUTO-L4")
    saha.gateway_bagladi("AUTO-L4")

    with caplog.at_level("INFO"):
        for _ in range(10):
            saha.oku_ve_onayla(d, SINYALLER_MODLU)

    assert mm.integrity_sayisi == 1, f"siniflandirma pollu {mm.integrity_sayisi} kez gonderildi"
    assert any("auto_classify_poll" in r.getMessage() for r in caplog.records)


def test_l17_auto_initiating_siniflandirma_pollu_gondermez(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.13.0'da dogrulanmis `initiating` yolu DEGISMEMELI.

    Orada cihaz kendi raporunu gonderiyorken araya soru sokmak, sahada
    kabul edilmis davranisi degistirirdi.
    """
    r = _reader(tmp_path)
    s = ListeningSaha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    monkeypatch.setattr(r, "_sonda_calistir", lambda mm, device: None)

    d = s.cihaz("AUTO-I1", session_policy="auto", ip_endpoint_type="initiating", master_ip_port=20100)
    mm = s.master("AUTO-I1")
    s.gateway_bagladi("AUTO-I1")
    for _ in range(10):
        s.oku_ve_onayla(d, SINYALLER_MODLU)

    assert mm.integrity_sayisi == 0, "initiating cihaza siniflandirma pollu gonderildi"


def test_l18_auto_uykuda_gecen_sure_fallback_penceresini_doldurmaz(
    saha: ListeningSaha,
) -> None:
    """`auto_connected_since` YALNIZCA baglantili sureyi saymali.

    Uykuda gecen zaman pencereyi doldursaydi, `auto` bir listening cihaz
    hicbir zaman siniflandirilamadan `continuous`a duser ve tam da
    kacinmak istedigimiz surekli trafigi uretirdi.
    """
    d = saha.cihaz("AUTO-L5", session_policy="auto")
    mm = saha.master("AUTO-L5")

    saha.gateway_bagladi("AUTO-L5")
    saha.oku_ve_onayla(d, SINYALLER_MODLU)
    assert mm.auto_connected_since > 0

    saha.uyudu("AUTO-L5")
    saha.oku_ve_onayla(d, SINYALLER_MODLU)
    assert mm.auto_connected_since == 0.0, "uykuda sayac ilerlemeye devam etti"
    assert mm.auto_fallback is False


# ==========================================================================
# L19-L21 — aktif tanilama sondalari: comm_lost URETMEZLER
# ==========================================================================


def test_l19_icmp_basarisizligi_tek_basina_comm_lost_uretmez(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En acik yanlis tasarim: "ping dusuyor -> cihaz oldu".

    ICMP saha aglarinda/APN'lerde sikca ENGELLIDIR ve Smart bir modem MESRU
    olarak uykudadir. Ping'e cevap vermemek BEKLENEN davranistir.
    """
    r = _reader(tmp_path)
    s = ListeningSaha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    monkeypatch.setattr(network_probe, "icmp_probe", lambda host, **kw: network_probe.IP_UNREACHABLE)
    monkeypatch.setattr(network_probe, "tcp_probe", lambda host, port, **kw: network_probe.TCP_TIMEOUT)

    d = s.cihaz(
        "PROBE-1", session_policy="smart", smart_max_silence_sec=86400, dial_in_interval_min=60
    )
    s.gateway_bagladi("PROBE-1")
    s.dnp3_kaniti("PROBE-1")
    s.oku_ve_onayla(d)
    s.uyudu("PROBE-1")
    s.yaslandir("PROBE-1", 5400)  # LATE -> sonda calisir
    s.oku_ve_onayla(d)

    saglik = r.device_health()["PROBE-1"]
    assert saglik["ip_probe_status"] == network_probe.IP_UNREACHABLE
    assert saglik["tcp_probe_status"] == network_probe.TCP_TIMEOUT
    assert saglik["state"] == "smart_idle", "sonda sonucu comm_lost uretti"
    assert saglik["last_probe_epoch"] is not None


def test_l20_sonda_yalnizca_gecikmisken_calisir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal uykuda ping/TCP denemesi gereksiz trafik ve gurultudur."""
    r = _reader(tmp_path)
    s = ListeningSaha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    sayac = {"n": 0}

    def _say(host: str, **kw: Any) -> str:
        sayac["n"] += 1
        return network_probe.IP_REACHABLE

    monkeypatch.setattr(network_probe, "icmp_probe", _say)
    monkeypatch.setattr(network_probe, "tcp_probe", lambda host, port, **kw: network_probe.TCP_OPEN)

    d = s.cihaz(
        "PROBE-2", session_policy="smart", smart_max_silence_sec=86400, dial_in_interval_min=60
    )
    s.gateway_bagladi("PROBE-2")
    s.dnp3_kaniti("PROBE-2")
    s.oku_ve_onayla(d)
    s.uyudu("PROBE-2")
    for _ in range(10):
        s.oku_ve_onayla(d)
    assert sayac["n"] == 0, "rapor penceresi icindeyken sonda calisti"

    s.yaslandir("PROBE-2", 5400)
    for _ in range(10):
        s.oku_ve_onayla(d)
    # Siklik siniri: 10 cycle'da yalnizca BIR sonda.
    assert sayac["n"] == 1, f"sonda siklik siniri calismadi ({sayac['n']} cagri)"


def test_l21_teshis_zinciri_dogru_yorumlar() -> None:
    """Sonda ucluesu -> operatore tek satirlik ANLAMLI yorum."""
    assert "protokol" in network_probe.diagnose(
        network_probe.IP_REACHABLE, network_probe.TCP_OPEN, network_probe.DNP3_FAILED
    )
    assert "dinleyici" in network_probe.diagnose(
        network_probe.IP_REACHABLE, network_probe.TCP_CLOSED, network_probe.DNP3_UNKNOWN
    )
    assert "modem" in network_probe.diagnose(
        network_probe.IP_UNREACHABLE, network_probe.TCP_TIMEOUT, network_probe.DNP3_UNKNOWN
    )
    # ICMP yoksa bu bir ARIZA DEGILDIR.
    assert "kullanilamiyor" in network_probe.diagnose(
        network_probe.IP_UNSUPPORTED, network_probe.TCP_UNKNOWN, network_probe.DNP3_UNKNOWN
    )


# ==========================================================================
# L22-L23 — filo izolasyonu ve kalicilik
# ==========================================================================


def test_l22_karisik_komsular_birbirini_etkilemez(saha: ListeningSaha) -> None:
    """Ayni gateway'de alti kombinasyon BIR ARADA calisabilmeli."""
    boost = saha.cihaz("MIX-BOOST", ip_address="10.0.0.11")
    smart = saha.cihaz(
        "MIX-SMART", ip_address="10.0.0.12", session_policy="smart", smart_max_silence_sec=3600
    )
    init = saha.cihaz(
        "MIX-INIT",
        ip_address="10.0.0.13",
        session_policy="smart",
        ip_endpoint_type="initiating",
        master_ip_port=20100,
        smart_max_silence_sec=3600,
    )

    for kod in ("MIX-BOOST", "MIX-SMART", "MIX-INIT"):
        saha.gateway_bagladi(kod)
        saha.dnp3_kaniti(kod)
    for d in (boost, smart, init):
        saha.oku_ve_onayla(d)

    # Smart olanlar uyusun; boost ayakta kalsin.
    saha.uyudu("MIX-SMART")
    saha.uyudu("MIX-INIT")
    for d in (boost, smart, init):
        saha.oku_ve_onayla(d)

    durum = _durumlar(saha)
    assert durum["MIX-BOOST"] == "online"
    assert durum["MIX-SMART"] == "smart_idle"
    assert durum["MIX-INIT"] == "smart_idle"

    # Bir Smart cihazin kopmasi digerlerini BOZMAZ.
    saha.yaslandir("MIX-SMART", 7200)
    for d in (boost, smart, init):
        saha.oku_ve_onayla(d)
    durum = _durumlar(saha)
    assert durum["MIX-SMART"] == "lost"
    assert durum["MIX-INIT"] == "smart_idle"
    assert durum["MIX-BOOST"] == "online"


def test_l23_restart_listening_smart_idle_i_korur(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart, uyuyan filoyu comm_lost firtinasina CEVIRMEMELI."""
    yol = str(tmp_path / "session_state.json")

    r1 = _reader(tmp_path)
    r1._session_store_path = yol
    s1 = ListeningSaha(r1)
    monkeypatch.setattr(r1, "_ensure_master", lambda d, sig=None: s1._ensure(d))
    monkeypatch.setattr(r1, "_sonda_calistir", lambda mm, device: None)
    d = s1.cihaz("RST-1", session_policy="smart", smart_max_silence_sec=86400)
    s1.gateway_bagladi("RST-1")
    s1.dnp3_kaniti("RST-1")
    s1.oku_ve_onayla(d)
    s1.uyudu("RST-1")
    s1.oku_ve_onayla(d)
    assert r1.device_health()["RST-1"]["state"] == "smart_idle"

    # --- YENIDEN BASLATMA ---
    r2 = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r2._init_runtime_state(session_store_path=yol)
    r2._scan_interval_sec = 5
    r2._baseline_interval_sec = 30
    r2._local_address = 1
    r2._default_dnp3_tcp_port = 20000
    r2._time_sync = "lan"
    r2._manager = None
    r2._publish_dnp3_quality = False
    s2 = ListeningSaha(r2)
    monkeypatch.setattr(r2, "_ensure_master", lambda dd, sig=None: s2._ensure(dd))
    monkeypatch.setattr(r2, "_sonda_calistir", lambda mm, device: None)

    d2 = s2.cihaz("RST-1", session_policy="smart", smart_max_silence_sec=86400)
    s2.oku_ve_onayla(d2)
    assert r2.device_health()["RST-1"]["state"] == "smart_idle", "restart sahte comm_lost uretti"


# ==========================================================================
# L24 — health ozeti: `late` ile `lost` AYRI sayilir
# ==========================================================================


def test_l24_health_ozeti_late_ve_lost_u_ayirir(saha: ListeningSaha) -> None:
    from dnp3_gateway.health_server import _device_health_snapshot

    gec = saha.cihaz(
        "SUM-LATE",
        ip_address="10.0.0.21",
        session_policy="smart",
        smart_max_silence_sec=86400,
        dial_in_interval_min=60,
    )
    kopuk = saha.cihaz(
        "SUM-LOST", ip_address="10.0.0.22", session_policy="smart", smart_max_silence_sec=3600
    )
    for kod in ("SUM-LATE", "SUM-LOST"):
        saha.gateway_bagladi(kod)
        saha.dnp3_kaniti(kod)
    for d in (gec, kopuk):
        saha.oku_ve_onayla(d)
        saha.uyudu(d.code)

    saha.yaslandir("SUM-LATE", 5400)  # LATE ama kopuk DEGIL
    saha.yaslandir("SUM-LOST", 7200)  # max_silence asildi
    for d in (gec, kopuk):
        saha.oku_ve_onayla(d)

    ozet = _device_health_snapshot(saha.reader, 2)
    assert ozet["late"] == 1, "gecikmis cihaz sayilmadi"
    assert ozet["smart_idle"] == 1
    assert ozet["lost"] == 1
    assert ozet["smart_lost"] == 1
    # `late` TOPLAMA GIRMEZ: cihaz ayni anda hem `smart_idle` hem `late`.
    assert ozet["total"] == 2
    assert ozet["unknown"] == 0


# ==========================================================================
# L25-L26 — DISK ONBELLEGI YOLU: parser ATLANIR, aralik yine dogrulanmali
# ==========================================================================


def test_l25_onbellekten_gelen_gecersiz_dial_in_yok_sayilir(saha: ListeningSaha) -> None:
    """`state.load_from_cache` alanlari dogrudan JSON'dan kurar ve backend
    parser'ini ATLAR. Onbellekten gelen 1 dakikalik bir deger her uyuyan
    cihazi kalici olarak `late` gosterir ve uyariyi anlamsizlastirirdi.
    """
    r = saha.reader
    gecerli = replace(make_device("RANGE-1"), dial_in_interval_min=60)
    assert r._dial_in_limit_sn(gecerli) == 3600

    for gecersiz in (0, -5, 1, 59, 1441, 100000, "x", 3.7):
        d = replace(make_device("RANGE-1"), dial_in_interval_min=gecersiz)
        assert r._dial_in_limit_sn(d) is None, f"{gecersiz!r} aralik disi ama kabul edildi"

    # Sinirlar DAHILDIR.
    assert r._dial_in_limit_sn(replace(make_device("X"), dial_in_interval_min=1440)) == 1440 * 60


def test_l26_onbellekten_gelen_gecersiz_probe_araligi_varsayilana_duser() -> None:
    """Aralik disi bir deger kutuphaneye gecerse yeniden baglanma davranisi
    TANIMSIZ olurdu; guvenli taraf kutuphane varsayilanidir."""
    opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

    # `TimeDuration` `__eq__` TANIMLAMIYOR (pybind11 nesnesi): dogrudan
    # karsilastirma KIMLIK karsilastirmasina duser ve esit degerler bile
    # esitsiz cikar. Milisaniye METNI uzerinden karsilastiriyoruz.
    def _ms(sure: Any) -> int:
        return int(str(sure))

    varsayilan_ms = _ms(opendnp3.ChannelRetry.Default().maxOpenRetry)
    assert varsayilan_ms == 60_000, "kutuphane varsayilani degismis"

    def _tavan(deger: Any) -> int:
        d = replace(make_device("PR-1"), smart_listen_probe_interval_sec=deger)
        return _ms(mod._ManagedMaster._listening_retry(d).maxOpenRetry)

    for gecersiz in (None, 0, -1, 4, 601, "x"):
        assert _tavan(gecersiz) == varsayilan_ms, f"{gecersiz!r} kutuphaneye gecti"

    # Gecerli deger TAVANI kisar, TABAN 1sn'de kalir (yeni uyanmis cihaza
    # hizli baglanmak istiyoruz).
    d = replace(make_device("PR-1"), smart_listen_probe_interval_sec=30)
    retry = mod._ManagedMaster._listening_retry(d)
    assert _ms(retry.maxOpenRetry) == 30_000
    assert _ms(retry.minOpenRetry) == 1_000
    # Sinirlar DAHILDIR.
    assert _tavan(5) == 5_000
    assert _tavan(600) == 600_000
