"""G-SMART-01 — `session_policy` ve `smart_idle` oturum yasam dongusu.

KAPATILAN IKI URETIM HATASI
---------------------------
1. GATEWAY MODEMIN KAPANMASINI ENGELLIYORDU. Smart Navigator 2.0'in
   Initiating Endpoint hareketsizlik zaman asimi 15 SANIYE SABITTIR ve
   **her TCP/DNP trafigi bu sayaci sifirlar**. Gateway her cihaz icin
   periyodik Class 1/2/3 + Class 0 taramasi kurdugu (ve acilista integrity
   poll'u yaptigi) icin sayac hicbir zaman dolmuyordu.

2. BEKLENEN KAPANMA `comm_lost` OLUYORDU. `OnClose` kosulsuz `lost` yaziyordu.
   Smart Mode'da BASARILI bir oturumun ardindan gelen TCP kapanmasi cihazin
   TASARIM davranisidir, haberlesme arizasi DEGILDIR.

TASARIM KARARI — POLITIKA ACIKCA YAPILANDIRILIR
-----------------------------------------------
Rejim cihazin DNP3 noktalarindan CIKARILMAZ; `DeviceConfig.session_policy`
ile ACIKCA verilir ("continuous" varsayilan | "smart"). Horstmann
`Operation Mode` (G1/15) noktasindan otomatik tespit BILINCLI olarak AYRI
bir istir ve bu gorevin kapsaminda DEGILDIR.

Bu dosya GERCEK `_DeviceCache` ve GERCEK `read_device` durum makinesini
kosturur; yalnizca native DNP3 master taklit edilir (gercek protokol yolu
`test_smart_session_loopback.py`de).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend import DeviceConfig

from .conftest import make_device, make_signal

AKIM_IDX = 2
DURUM_IDX = 7

SINYALLER = [
    make_signal("master.actual_current", data_type="analog", object_group=30, index=AKIM_IDX),
    make_signal("master.overcurrent_tripped", data_type="binary", object_group=1, index=DURUM_IDX),
]


# --------------------------------------------------------------------------
# Taklit native master — GERCEK _DeviceCache tasir
# --------------------------------------------------------------------------
class SahteMaster:
    """`_ManagedMaster` taklidi. Cache GERCEK: durum makinesi gercekten kosar.

    Gateway KAYNAKLI DNP3 trafiginin tamami burada sayilir; "gateway susuyor"
    iddiasi ancak bu sayaclarla kanitlanabilir.
    """

    def __init__(self, device: DeviceConfig, *, session_policy: str = "continuous") -> None:
        self.device = device
        self.cache = mod._DeviceCache()
        # Gercek `_ManagedMaster` ile ayni yuzey: yapilandirilan politika ile
        # ETKIN politika ayri (bkz. `auto`).
        self.configured_session_policy = session_policy
        self.session_policy = "smart" if session_policy in ("smart", "auto") else "continuous"
        self.auto_pending = session_policy == "auto"
        self.auto_connected_since = 0.0
        self.auto_fallback = False
        self.operation_mode = "unknown"
        self.operation_mode_raw = None
        self.operation_mode_seen_at = None
        self.scan_etkinlestirme_sayisi = 0
        self.cache.set_session_policy(self.session_policy)
        self.connection_fingerprint: tuple = ()
        # Gercek `_ManagedMaster` ile ayni yuzey: initiating cihazlarda
        # dinlenen port, listening'de None (bkz. device_health).
        self.listen_port = device.master_ip_port if device.ip_endpoint_type == "initiating" else None
        self.g110_ranges: tuple = ()
        self.last_command_at = 0.0
        self.integrity_sayisi = 0
        self.g110_scan_sayisi = 0
        self.shutdown_sayisi = 0

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
        """Gercek `_ManagedMaster` ile ayni kural: quiet -> continuous."""
        if self.session_policy != "smart":
            return False
        self.session_policy = "continuous"
        self.cache.set_session_policy("continuous")
        self.scan_etkinlestirme_sayisi += 1
        return True

    def kanit_esigi_sn(self) -> float:
        return mod._bayatlik_esigi_sn(5, 30)

    def kanit_yasi(self) -> float:
        k = self.cache.last_evidence_at()
        return -1.0 if k == 0.0 else (mod.time.monotonic() - k)

    def ulasilabilir(self) -> bool:
        if not self.cache.is_connected():
            return False
        yas = self.kanit_yasi()
        return 0 <= yas <= self.kanit_esigi_sn()


class Saha:
    """Tek gateway prosesi + N bagimsiz outstation."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.masterlar: dict[str, SahteMaster] = {}

    def cihaz(self, code: str, **kw: Any) -> DeviceConfig:
        d = make_device(code)
        if kw:
            # SOZLESME: `smart` YALNIZCA `initiating` uc ile gecerli. Test
            # yardimcisi bunu otomatik saglar ki senaryolar sozlesmeye uygun
            # bir cihazla kossun; kisitin KENDISI ayri testlerde olculur
            # (GS04 / GS05).
            if kw.get("session_policy") == "smart" and "ip_endpoint_type" not in kw:
                kw = {**kw, "ip_endpoint_type": "initiating", "master_ip_port": 20100}
            d = replace(d, **kw)
        self._ensure(d)
        return d

    def _ensure(self, device: DeviceConfig) -> SahteMaster:
        """Gercek `_ensure_master` semantigi: yoksa politikayla yeniden kur."""
        kod = device.code
        mevcut = self.reader._masters.get(kod)
        if mevcut is not None:
            return mevcut
        mm = SahteMaster(device, session_policy=self.reader._session_policy(device))
        self.reader._durumu_geri_yukle(mm, device)
        self.masterlar[kod] = mm
        self.reader._masters[kod] = mm
        return mm

    def master(self, code: str) -> SahteMaster:
        return self.masterlar[code]

    def oku(self, device: DeviceConfig, signals: list | None = None) -> list:
        return self.reader.read_device(device=device, signals=signals or SINYALLER)

    def oku_ve_onayla(self, device: DeviceConfig, signals: list | None = None) -> list:
        """Poller'in GERCEK akisi: oku -> yayinla -> `commit_published`."""
        okumalar = self.oku(device, signals)
        self.reader.commit_published(device=device, readings=okumalar)
        return okumalar

    # --- cihaz davranisi taklidi ---
    def tcp_acildi(self, code: str) -> None:
        """opendnp3 `OnOpen`."""
        mm = self.masterlar[code]
        mm.cache.set_connected(True)
        mm.cache.begin_recovery()
        mm.cache.g110_iste()

    def dnp3_kaniti(self, code: str, *, akim: float = 10.0) -> None:
        """Cihaz GERCEK bir olcum gonderdi (kanitin en guclu hali)."""
        self.masterlar[code].cache.set(30, AKIM_IDX, akim)

    def sadece_iin(self, code: str) -> None:
        """Yalnizca IIN / basarili gorev — olcum yok ama GECERLI kanit."""
        self.masterlar[code].cache.note_evidence()

    def tcp_kapandi(self, code: str) -> None:
        """opendnp3 `OnClose`."""
        self.masterlar[code].cache.set_connected(False)

    def basarili_smart_oturum(self, code: str, **kw: Any) -> DeviceConfig:
        d = self.cihaz(code, session_policy="smart", **kw)
        self.tcp_acildi(code)
        self.dnp3_kaniti(code)
        self.oku_ve_onayla(d)
        return d


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
def saha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Saha:
    r = _reader(tmp_path)
    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    return s


def _kaliteler(okumalar: list) -> set[str]:
    return {o.quality for o in okumalar}


def _deger(okumalar: list, key: str) -> Any:
    return next(o.scaled_value for o in okumalar if o.signal_key == key)


# ==========================================================================
# A + B — MEVCUT continuous davranisi DEGISMEDI
# ==========================================================================


def test_a_continuous_listening_davranisi_degismedi(saha: Saha) -> None:
    """KABUL A: varsayilan politika + listening uc -> bugunku davranis."""
    d = saha.cihaz("RTU-1")
    assert d.session_policy == "continuous", "varsayilan continuous OLMALI"
    assert saha.master("RTU-1").session_policy == "continuous"

    saha.tcp_acildi("RTU-1")
    saha.dnp3_kaniti("RTU-1", akim=42.0)
    okumalar = saha.oku(d)
    assert saha.master("RTU-1").cache.state() == "online"
    assert _deger(okumalar, "master.actual_current") == 42.0

    # Kapanma -> lost -> comm_lost (DEGISMEDI)
    saha.tcp_kapandi("RTU-1")
    assert saha.master("RTU-1").cache.state() == "lost"
    assert _kaliteler(saha.oku(d)) == {"comm_lost"}


def test_b_continuous_initiating_davranisi_degismedi(saha: Saha) -> None:
    """KABUL B: initiating uc + continuous politika -> yine bugunku davranis.

    `smart_idle` YALNIZCA politikaya baglidir; uc tipinden CIKARILMAZ.
    """
    d = saha.cihaz("RTU-2", ip_endpoint_type="initiating", master_ip_port=20100)
    saha.tcp_acildi("RTU-2")
    saha.dnp3_kaniti("RTU-2")
    saha.oku_ve_onayla(d)

    saha.tcp_kapandi("RTU-2")
    assert saha.master("RTU-2").cache.state() == "lost", (
        "initiating uc TEK BASINA smart_idle uretmemeli — politika 'continuous'"
    )
    assert _kaliteler(saha.oku(d)) == {"comm_lost"}


# ==========================================================================
# F + G — TCP kapanmasinin anlami KANITA baglidir
# ==========================================================================


def test_f_kanitsiz_oturum_kapaninca_lost(saha: Saha) -> None:
    """KABUL F: TCP acildi ama DNP3 kaniti YOK, sonra kapandi -> lost.

    Salt TCP baglantisi kanit DEGILDIR. 4G'de soket kurulup DNP3 katmani hic
    konusmadan da dusebilir; bunu "basarili oturum" sayip `smart_idle`e
    gecmek GERCEK bir arizayi saglikli uyku gibi gosterirdi.
    """
    d = saha.cihaz("SN2-1", session_policy="smart")
    saha.tcp_acildi("SN2-1")  # yalnizca TCP — hicbir DNP3 kaniti yok
    saha.tcp_kapandi("SN2-1")

    mm = saha.master("SN2-1")
    assert mm.cache.state() == "lost", "kanitsiz kapanma smart_idle sayilmis"
    assert mm.cache.is_smart_idle() is False
    assert _kaliteler(saha.oku(d)) == {"comm_lost"}


def test_g_kanitli_oturum_kapaninca_smart_idle(saha: Saha) -> None:
    """KABUL G: TCP acildi + GECERLI DNP3 kaniti + kapandi -> smart_idle."""
    saha.basarili_smart_oturum("SN2-1")
    mm = saha.master("SN2-1")
    assert mm.cache.state() == "online"

    saha.tcp_kapandi("SN2-1")
    assert mm.cache.state() == "smart_idle"
    assert mm.cache.is_smart_idle() is True


def test_g2_iin_veya_basarili_gorev_de_kanittir(saha: Saha) -> None:
    """Sartname: kanit = olcum / gecerli IIN / basarili DNP3 gorevi.

    Durgun bir fiderde hicbir deger degismeyebilir; cihaz yine de IIN ile
    cevap verir. Bunu kanit saymamak, saglikli bir oturumu `lost` yapardi.
    """
    d = saha.cihaz("SN2-1", session_policy="smart")
    saha.tcp_acildi("SN2-1")
    saha.sadece_iin("SN2-1")  # olcum YOK, yalnizca IIN
    saha.tcp_kapandi("SN2-1")

    assert saha.master("SN2-1").cache.state() == "smart_idle"
    assert "comm_lost" not in _kaliteler(saha.oku(d))


# ==========================================================================
# H + I — smart_idle okuma davranisi
# ==========================================================================


def test_h_smart_idle_comm_lost_uretmez(saha: Saha) -> None:
    """KABUL H: `smart_idle` durumunda cihaz seviyesinde comm_lost YOK."""
    d = saha.basarili_smart_oturum("SN2-1")
    saha.tcp_kapandi("SN2-1")

    for _ in range(50):
        okumalar = saha.oku_ve_onayla(d)
        assert "comm_lost" not in _kaliteler(okumalar), (
            "SAGLIKLI smart_idle comm_lost olarak yayinlandi — duzeltilen hata geri gelmis"
        )


def test_i_son_iyi_degerler_korunur(saha: Saha) -> None:
    """KABUL I: idle sirasinda son bilinen degerler KAYBOLMAZ."""
    d = saha.cihaz("SN2-1", session_policy="smart")
    saha.tcp_acildi("SN2-1")
    saha.dnp3_kaniti("SN2-1", akim=123.0)
    saha.oku_ve_onayla(d)
    saha.tcp_kapandi("SN2-1")

    # Degismeyen sinyaller `no_change` doner — SCADA son iyi degeri korur.
    okumalar = saha.oku(d)
    assert _kaliteler(okumalar) == {"no_change"}
    # ...ve deger cache'te DURUYOR (comm_lost yolunun aksine 0.0'a cevrilmedi).
    mm = saha.master("SN2-1")
    assert mm.cache.get(30, AKIM_IDX)[0] == 123.0

    # Operator refresh-all tetiklerse son bilinen degerler yeniden yayinlanir;
    # bu YEREL bir istir, cihaza istek gitmez ve modem uyanmaz.
    trafik_once = mm.gateway_trafigi
    mm.cache.mark_all_dirty()
    yeniden = saha.oku(d)
    assert _deger(yeniden, "master.actual_current") == 123.0
    assert mm.gateway_trafigi == trafik_once, "yayin icin cihaza istek gonderilmis"


def test_h2_idle_sirasinda_gateway_kaynakli_trafik_yok(saha: Saha) -> None:
    """Modem kapanabilsin diye gateway HICBIR sey sormamali.

    Yoklama (`lost probe`), veri-sessizligi integrity poll'u ve zorla relink
    — hepsi hareketsizlik sayacini sifirlardi.
    """
    d = saha.basarili_smart_oturum("SN2-1")
    saha.tcp_kapandi("SN2-1")
    mm = saha.master("SN2-1")
    saha.oku_ve_onayla(d)
    trafik_once = mm.gateway_trafigi
    shutdown_once = mm.shutdown_sayisi

    for _ in range(200):  # sahada dakikalarca sure
        saha.oku(d)

    assert mm.gateway_trafigi == trafik_once, (
        f"idle sirasinda {mm.gateway_trafigi - trafik_once} gateway kaynakli DNP3 "
        "istegi gonderildi — modem hicbir zaman kapanamaz"
    )
    assert mm.shutdown_sayisi == shutdown_once, "idle sirasinda zorla relink denendi"
    st = saha.reader.recovery_stats()
    assert st["lost_probe_total"] == 0
    assert st["forced_relink_total"] == 0


# ==========================================================================
# J — sessizlik denetimi
# ==========================================================================


def test_j_sessizlik_asilinca_tek_bir_comm_lost(saha: Saha) -> None:
    """KABUL J: smart_idle -> lost ve TAM OLARAK BIR comm_lost kenari."""
    d = saha.basarili_smart_oturum("SN2-1", smart_max_silence_sec=3600)
    saha.tcp_kapandi("SN2-1")
    assert _kaliteler(saha.oku_ve_onayla(d)) == {"no_change"}, "on kosul: once saglikli idle"

    mm = saha.master("SN2-1")
    # Zaman ilerledi: son gecerli temas esikten eski (MONOTONIC olculur).
    gercek = mod.time.monotonic
    ileri = [0.0]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.time, "monotonic", lambda: gercek() + ileri[0])
        ileri[0] = 3600 + 120

        okumalar = saha.oku(d)
        assert _kaliteler(okumalar) == {"comm_lost"}, "sessizlik asildi, lost olmali"
        assert mm.cache.state() == "lost"
        assert mm.cache.is_smart_idle() is False

        # Yayin ONAYLANANA kadar tekrar comm_lost (mevcut yayin garantisi).
        tekrar = saha.oku(d)
        assert _kaliteler(tekrar) == {"comm_lost"}
        saha.reader.commit_published(device=d, readings=tekrar)

        # Onaydan sonra SUSAR — comm_lost her cycle tekrarlanmaz.
        for _ in range(20):
            assert _kaliteler(saha.oku(d)) == {"no_change"}


def test_j2_esik_yoksa_denetim_kapali(saha: Saha) -> None:
    """Adapter'da GOMULU bir "24 saat" YOKTUR.

    Dogru deger cihazin Dial-In rapor programina baglidir; uydurulmasi
    saglikli bir cihazi erken offline ilan etmek olurdu.
    """
    d = saha.basarili_smart_oturum("SN2-1")  # smart_max_silence_sec YOK
    saha.tcp_kapandi("SN2-1")
    assert saha.reader._smart_silence_limit(d) is None

    gercek = mod.time.monotonic
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.time, "monotonic", lambda: gercek() + 40 * 24 * 3600)
        assert "comm_lost" not in _kaliteler(saha.oku(d))
        assert saha.master("SN2-1").cache.state() == "smart_idle"


def test_j3_esik_cihaz_bazinda_kurulum_yedegini_ezer(tmp_path: Path, monkeypatch) -> None:
    """Ayni gateway'de farkli Dial-In programlari yan yana calisabilmeli."""
    r = _reader(tmp_path, smart_max_silence_sec=7200)
    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))

    yedek = s.cihaz("A", session_policy="smart")
    ozel = s.cihaz("B", session_policy="smart", smart_max_silence_sec=900)
    assert r._smart_silence_limit(yedek) == 7200, "kurulum geneli yedek kullanilmali"
    assert r._smart_silence_limit(ozel) == 900, "cihaz bazli deger yedegi EZMELI"


# ==========================================================================
# K + L — uyanma ve tekrar idle
# ==========================================================================


def test_k_idle_sonrasi_yeniden_baglanma(saha: Saha) -> None:
    """KABUL K: smart_idle -> recovering -> (taze kanit) -> online."""
    d = saha.basarili_smart_oturum("SN2-1")
    saha.tcp_kapandi("SN2-1")
    mm = saha.master("SN2-1")
    assert mm.cache.state() == "smart_idle"

    saha.tcp_acildi("SN2-1")
    assert mm.cache.state() == "recovering", "baglantida once recovering olmali"
    assert mm.cache.is_smart_idle() is False

    saha.dnp3_kaniti("SN2-1", akim=77.0)
    assert mm.cache.state() == "online", "taze kanit online'a yukseltmeli"
    okumalar = saha.oku(d)
    assert _deger(okumalar, "master.actual_current") == 77.0


def test_l_basarili_oturum_sonrasi_tekrar_idle(saha: Saha) -> None:
    """KABUL L: online -> (kapanma) -> smart_idle; dongu tekrarlanabilir."""
    d = saha.basarili_smart_oturum("SN2-1")
    mm = saha.master("SN2-1")

    for tur in range(3):
        saha.tcp_kapandi("SN2-1")
        assert mm.cache.state() == "smart_idle", f"{tur}. turda idle'a gecilmedi"
        assert "comm_lost" not in _kaliteler(saha.oku_ve_onayla(d))

        saha.tcp_acildi("SN2-1")
        saha.dnp3_kaniti("SN2-1", akim=float(tur))
        assert mm.cache.state() == "online", f"{tur}. turda online'a donulmedi"
        saha.oku_ve_onayla(d)


def test_k2_uyanma_kenar_tetikli_loglanir(saha: Saha, caplog: pytest.LogCaptureFixture) -> None:
    d = saha.basarili_smart_oturum("SN2-1")
    saha.tcp_kapandi("SN2-1")
    with caplog.at_level(logging.INFO, logger="dnp3_gateway.adapters.dnp3_yadnp3_master"):
        for _ in range(30):
            saha.oku(d)  # idle: HICBIR uyanma logu olmamali
        ara = [r for r in caplog.records if "smart_idle_wakeup" in r.getMessage()]
        saha.tcp_acildi("SN2-1")
        saha.dnp3_kaniti("SN2-1")
        saha.oku(d)
        sonra = [r for r in caplog.records if "smart_idle_wakeup" in r.getMessage()]

    assert ara == [], "idle sirasinda uyanma logu basilmis"
    assert len(sonra) == 1, "uyanma TEK satir olmali (kenar-tetikli)"
    assert not [r for r in caplog.records if "comm_lost" in r.getMessage()]


# ==========================================================================
# M — komut guvenligi
# ==========================================================================


def test_m_idle_cihaza_komut_gonderilmez(saha: Saha) -> None:
    """KABUL M: uyuyan cihaz ULASILAMAZ; DNP3 operate DENENMEZ.

    Bu gorevde komut kuyruklama YOK — beklenen davranis guvenli reddetmedir.
    """
    d = saha.basarili_smart_oturum("SN2-1")
    mm = saha.master("SN2-1")
    assert mm.ulasilabilir() is True, "on kosul: uyanikken ulasilabilir"

    saha.tcp_kapandi("SN2-1")
    saha.oku(d)
    assert mm.ulasilabilir() is False, (
        "uyuyan cihaz ulasilabilir gorunuyor — komut olmayan bir baglanti uzerinden denenirdi"
    )
    assert saha.reader.device_health()["SN2-1"]["reachable"] is False


def test_m2_operate_crob_idle_cihazda_offline_doner(saha: Saha) -> None:
    """`operate_crob` fail-safe kapisi DEGISMEDI: sahte basari URETILMEZ."""
    d = saha.basarili_smart_oturum("SN2-1")
    saha.tcp_kapandi("SN2-1")
    saha.oku(d)
    mm = saha.master("SN2-1")

    trafik_once = mm.gateway_trafigi
    sonuc = mod._ManagedMaster.operate_crob(mm, index=7, op_type="latch_on")
    assert sonuc["ok"] is False
    assert sonuc["status"] == "offline"
    assert mm.gateway_trafigi == trafik_once, "kapali oturum uzerinden DNP3 istegi denendi"


# ==========================================================================
# 7 — /health ciktisi
# ==========================================================================


def test_health_teshis_icin_yeterli_bilgi_tasiyor(saha: Saha) -> None:
    d = saha.basarili_smart_oturum("SN2-1", smart_max_silence_sec=3600)
    saha.tcp_kapandi("SN2-1")
    saha.oku(d)
    saglik = saha.reader.device_health()["SN2-1"]

    # Sartname madde 7
    for alan in ("state", "connected", "reachable", "last_frame_epoch", "evidence_age_sec", "session_policy"):
        assert alan in saglik, f"/health cihaz ozetinde `{alan}` yok"

    assert saglik["state"] == "smart_idle"
    assert saglik["connected"] is False
    assert saglik["reachable"] is False
    assert saglik["session_policy"] == "smart"
    assert saglik["smart_idle_age_sec"] is not None
    assert saglik["smart_max_silence_sec"] == 3600
    assert saglik["smart_silence_remaining_sec"] > 0


def test_health_continuous_cihazda_degismedi(saha: Saha) -> None:
    d = saha.cihaz("RTU-1")
    saha.tcp_acildi("RTU-1")
    saha.dnp3_kaniti("RTU-1")
    saha.oku(d)
    saglik = saha.reader.device_health()["RTU-1"]
    assert saglik["state"] == "online"
    assert saglik["session_policy"] == "continuous"
    assert saglik["smart_idle_age_sec"] is None
    assert saglik["smart_max_silence_sec"] is None


def test_smart_sayaclari_raporlanir(saha: Saha) -> None:
    d = saha.basarili_smart_oturum("SN2-1")
    saha.tcp_kapandi("SN2-1")
    saha.oku(d)
    saha.tcp_acildi("SN2-1")
    saha.dnp3_kaniti("SN2-1")
    saha.oku(d)

    st = saha.reader.recovery_stats()
    assert st["smart_idle_wakeup_total"] == 1
    # Mevcut sayaclar bozulmadi.
    assert st["lost_probe_total"] == 0
    assert "forced_relink_total" in st


# ==========================================================================
# CIHAZ IZOLASYONU — bir cihaz digerini etkilemez
# ==========================================================================


def test_politikalar_cihaz_basina_bagimsiz(saha: Saha) -> None:
    """Ayni proseste continuous ve smart cihazlar birbirini ETKILEMEZ."""
    plan = {"C-1": "continuous", "S-1": "smart", "S-2": "smart", "C-2": "continuous"}
    cihazlar = {}
    for kod, politika in plan.items():
        cihazlar[kod] = saha.cihaz(kod, session_policy=politika)  # smart -> initiating (helper)
        saha.tcp_acildi(kod)
        saha.dnp3_kaniti(kod)
        saha.oku_ve_onayla(cihazlar[kod])

    # Smart olanlar uykuya gecer; continuous olanlar bagli kalir.
    for kod in ("S-1", "S-2"):
        saha.tcp_kapandi(kod)
        saha.oku_ve_onayla(cihazlar[kod])

    saglik = saha.reader.device_health()
    assert saglik["C-1"]["state"] == "online"
    assert saglik["C-2"]["state"] == "online"
    assert saglik["S-1"]["state"] == "smart_idle"
    assert saglik["S-2"]["state"] == "smart_idle"

    # Smart cihazlar sessiz; continuous cihazlar etkilenmemis.
    trafik = {k: saha.master(k).gateway_trafigi for k in plan}
    for _ in range(50):
        for dev in cihazlar.values():
            saha.oku(dev)
    for kod in ("S-1", "S-2"):
        assert saha.master(kod).gateway_trafigi == trafik[kod], f"{kod} idle iken trafik uretti"
    for kod in ("C-1", "C-2"):
        assert saha.master(kod).cache.state() == "online", f"{kod} Smart komsusundan etkilendi"


def test_bir_cihazin_kopmasi_digerinin_idle_ini_bozmaz(saha: Saha) -> None:
    s1 = saha.basarili_smart_oturum("S-1")
    saha.tcp_kapandi("S-1")
    saha.oku_ve_onayla(s1)

    c1 = saha.cihaz("C-1")
    saha.tcp_acildi("C-1")
    saha.dnp3_kaniti("C-1")
    saha.oku_ve_onayla(c1)
    saha.tcp_kapandi("C-1")

    assert _kaliteler(saha.oku(c1)) == {"comm_lost"}, "continuous cihaz comm_lost URETMELI"
    assert saha.master("S-1").cache.state() == "smart_idle", "komsu kopmasi idle'i bozmus"
    assert "comm_lost" not in _kaliteler(saha.oku(s1))


# ==========================================================================
# KALICILIK — restart sonrasi sahte comm_lost firtinasi olmamali
# ==========================================================================


def test_restart_sonrasi_smart_idle_geri_yuklenir(tmp_path: Path, monkeypatch) -> None:
    yol = str(tmp_path / "session_state.json")

    def _yeni_reader() -> Any:
        r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
        r._init_runtime_state(session_store_path=yol)
        r._scan_interval_sec = 5
        r._baseline_interval_sec = 30
        r._local_address = 1
        r._default_dnp3_tcp_port = 20000
        r._time_sync = "lan"
        r._manager = None
        r._publish_dnp3_quality = False
        return r

    # --- 1. surec: cihaz saglikli sekilde idle'a girer ---
    r1 = _yeni_reader()
    s1 = Saha(r1)
    monkeypatch.setattr(r1, "_ensure_master", lambda d, sig=None: s1._ensure(d))
    d = s1.basarili_smart_oturum("SN2-1")
    s1.tcp_kapandi("SN2-1")
    s1.oku_ve_onayla(d)
    assert s1.master("SN2-1").cache.state() == "smart_idle"
    r1._session_store.flush(force=True)

    # --- 2. surec: gateway yeniden basladi ---
    r2 = _yeni_reader()
    s2 = Saha(r2)
    monkeypatch.setattr(r2, "_ensure_master", lambda dd, sig=None: s2._ensure(dd))
    d2 = s2.cihaz("SN2-1", session_policy="smart")

    assert s2.master("SN2-1").cache.state() == "smart_idle", "restart sonrasi idle geri yuklenmedi"
    assert "comm_lost" not in _kaliteler(s2.oku(d2)), (
        "restart sonrasi saglikli uyuyan cihaz comm_lost ilan edildi — "
        "uyuyan filoda yanlis alarm firtinasi demek"
    )


def test_restart_sonrasi_sessizlik_gecmisse_idle_geri_yuklenmez(tmp_path: Path, monkeypatch) -> None:
    """Kalici kayit cihazi SONSUZA KADAR saglikli gostermek icin kullanilamaz."""
    from dnp3_gateway.session_state_store import SessionStateRecord, SessionStateStore

    yol = str(tmp_path / "session_state.json")
    store = SessionStateStore(yol)
    eski = mod.time.time() - (10 * 3600)
    store.record(
        "SN2-1",
        SessionStateRecord(state="smart_idle", last_valid_contact_unix=eski, smart_idle_since_unix=eski),
    )
    store.flush(force=True)

    r = _reader(tmp_path)
    r._session_store = SessionStateStore(yol)
    r._session_store.load()
    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    d = s.cihaz("SN2-1", session_policy="smart", smart_max_silence_sec=3600)

    assert s.master("SN2-1").cache.state() == "lost", "sessizligi gecmis cihaz idle geri yuklenmis"
    assert _kaliteler(s.oku(d)) == {"comm_lost"}


def test_bozuk_kalici_kayit_guvenli(tmp_path: Path, monkeypatch) -> None:
    """Bozuk kayit -> CRASH YOK, uydurma karar YOK, mevcut davranis."""
    yol = tmp_path / "session_state.json"
    yol.write_text("{bu gecerli json degil", encoding="utf-8")

    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(session_store_path=str(yol))
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    r._publish_dnp3_quality = False
    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    d = s.cihaz("SN2-1", session_policy="smart")

    assert s.master("SN2-1").cache.state() == "lost"
    assert s.master("SN2-1").cache.is_smart_idle() is False
    assert _kaliteler(s.oku(d)) == {"comm_lost"}


# ==========================================================================
# connection_fingerprint — politika degisimi master'i yeniden kurar
# ==========================================================================


def test_politika_degisimi_imzayi_degistirir(tmp_path: Path) -> None:
    """Imzada olmasaydi operator politikayi degistirir ve HICBIR etki gormezdi."""
    r = _reader(tmp_path)
    surekli = replace(make_device("SN2-1"), ip_endpoint_type="initiating", master_ip_port=20100)
    akilli = replace(surekli, session_policy="smart")
    assert r._connection_fingerprint(surekli) != r._connection_fingerprint(akilli)
    # Ayni politika -> ayni imza (gereksiz yeniden kurulum YOK).
    assert r._connection_fingerprint(akilli) == r._connection_fingerprint(
        replace(akilli, smart_max_silence_sec=999)
    ), "sessizlik esigi master'i yeniden kurmayi GEREKTIRMEZ"


# ==========================================================================
# C — smart + initiating: gateway TCP SERVER olarak dinler
# ==========================================================================


def test_c_smart_initiating_tcp_server_kullanir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """KABUL C: `smart` + `initiating` -> `AddTCPServer` (gateway dinler).

    Smart Mode'da baglantiyi CIHAZ baslatir (dial-in). Gateway TCP client
    olarak cihaza baglanmaya calisirsa uyuyan bir modeme surekli SYN gonderir
    ve baglanti hicbir zaman kurulamaz.

    Ayrica KABUL D + E: `smart` politikada HICBIR tekrarlayan tarama gorevi
    kurulmaz ve acilis integrity poll'u ISTENMEZ.
    """
    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

    cagrilar: dict[str, Any] = {}

    class _SahteKanal:
        def AddMaster(self, ad, soe, app, cfg):  # noqa: N802, ARG002
            cagrilar["app"] = app
            return _SahteNativeMaster()

        def Shutdown(self):  # noqa: N802
            pass

    class _SahteNativeMaster:
        def AddClassScan(self, *a, **k):  # noqa: N802
            cagrilar.setdefault("class_scan", 0)
            cagrilar["class_scan"] += 1
            return object()

        def Enable(self):  # noqa: N802
            cagrilar["enabled"] = True

    class _SahteManager:
        def AddTCPServer(self, ad, lvl, kabul, endpoint, dinleyici):  # noqa: N802, ARG002
            cagrilar["server_port"] = endpoint.port
            return _SahteKanal()

        def AddTCPClient(self, *a, **k):  # noqa: N802
            cagrilar["client"] = True
            return _SahteKanal()

    device = replace(
        make_device("SN2-1"),
        ip_endpoint_type="initiating",
        master_ip_port=20100,
        session_policy="smart",
    )
    mm = mod._ManagedMaster(
        _SahteManager(),
        device=device,
        local_address=1,
        tcp_port=20000,
        scan_interval_sec=5,
        baseline_interval_sec=30,
        session_policy="smart",
    )

    # C: TCP SERVER, cihazin atanmis portunda.
    assert cagrilar.get("server_port") == 20100, "initiating uc icin AddTCPServer kullanilmadi"
    assert "client" not in cagrilar, "gateway TCP client olarak baglanmaya calisti"

    # D: tekrarlayan tarama gorevi YOK.
    assert cagrilar.get("class_scan", 0) == 0, "smart politikada AddClassScan cagrilmis"
    assert mm._scan_event is None and mm._scan_class0 is None

    # E: acilis integrity/Class 0 davranisi ISTENMEZ.
    assert cagrilar["app"].AssignClassDuringStartup() is False, (
        "smart politikada acilis integrity poll'u isteniyor — bu, cihazin 15 saniyelik "
        "hareketsizlik sayacini sifirlayan gereksiz trafiktir"
    )
    assert cagrilar.get("enabled") is True


def test_c2_continuous_politikada_tarama_ve_acilis_polli_kurulur(tmp_path: Path) -> None:
    """Kontrol testi: `continuous`ta bugunku davranis AYNEN duruyor."""
    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    cagrilar: dict[str, Any] = {"class_scan": 0}

    class _SahteNativeMaster:
        def AddClassScan(self, *a, **k):  # noqa: N802
            cagrilar["class_scan"] += 1
            return object()

        def Enable(self):  # noqa: N802
            pass

    class _SahteKanal:
        def AddMaster(self, ad, soe, app, cfg):  # noqa: N802, ARG002
            cagrilar["app"] = app
            return _SahteNativeMaster()

    class _SahteManager:
        def AddTCPClient(self, *a, **k):  # noqa: N802
            cagrilar["client"] = True
            return _SahteKanal()

    mm = mod._ManagedMaster(
        _SahteManager(),
        device=make_device("RTU-1"),
        local_address=1,
        tcp_port=20000,
        scan_interval_sec=5,
        baseline_interval_sec=30,
        session_policy="continuous",
    )
    assert cagrilar.get("client") is True, "listening uc icin AddTCPClient kullanilmali"
    assert cagrilar["class_scan"] == 2, "continuous'ta event + integrity taramasi KURULMALI"
    assert mm._scan_event is not None and mm._scan_class0 is not None
    assert cagrilar["app"].AssignClassDuringStartup() is True, (
        "continuous'ta acilis integrity poll'u KORUNMALI"
    )
