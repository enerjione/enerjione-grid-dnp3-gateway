"""G-SMART-RECOVERY — Smart oturum kurtarma durum makinesi (saha regresyonu).

SAHA OLAYI (2026-08-20, cihaz SN2_0, listening + smart)
--------------------------------------------------------
Grid v2.106.0 politikayi dogru sekilde `continuous` -> `smart` yapti ve ilk
Smart uykusu BASARILI oldu. Cihaz disaridan uyandirildiginda uc ayri hata
ortaya cikti:

    13:08:53  link_open (state=recovering)
    13:08:56  comm_lost_announced          <-- 3 SANIYE SONRA (BUG 3)
    13:09:12  g110_okundu                  <-- cihaz KONUSUYORDU
    13:09:16  device_recovered

    13:11:16  device_stale last_data_age=120s -> recovering -> comm_lost  (BUG 1)

    13:14:22  device_relink age=2
    13:14:37  recovery_timeout -> lost
    13:14:37  device_relink age=17         <-- AYNI eski kare (BUG 2)
    ... her ~15 saniyede bir tekrar

UC HATA
-------
BUG 1  Bayatlik esigi (`scan_interval`/`baseline_interval`den turetilir)
       "saglikli cihaz her tarama turunda cevap verir" varsayar. Bu varsayim
       SUREKLI politikaya aittir. Smart'ta tarama YOKTUR, dolayisiyla
       KANITLANMIS ve SAGLIKLI bir oturum 120 saniye sonra GARANTILI olarak
       bayat olur ve sahte comm_lost uretir.

BUG 2  `lost` -> `recovering` gecisi yalnizca "kanit bayat degil" kosuluna
       bagliydi. ESKI bir kare relink'i tekrar tekrar tetikliyor, recovery
       ise YENI veri istedigi icin hicbir zaman tamamlanamiyordu:
       deterministik 15 saniyelik salinim.

BUG 3  `recovering` durumunda comm_lost DERHAL yayinlaniyordu. Smart bir
       cihaz uyandiginda TCP el sikismasi ile ilk DNP3 fragmenti arasindaki
       kisa pazarlik suresi de `recovering`tir — orada alarm SAHTEDIR.

DEGISMEZ: `continuous` davranisi AYNEN korunur (bkz. §11 regresyon guvenligi).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

from .conftest import make_device
from .test_smart_session_policy import SINYALLER, SahteMaster

AKIM_IDX = 2
GRACE = mod._RECOVERY_GRACE_SEC


class Saha:
    """Gercek `_DeviceCache` + gercek `read_device`; yalnizca native master taklit."""

    def __init__(self, reader) -> None:
        self.reader = reader
        self.masterlar: dict[str, SahteMaster] = {}

    def cihaz(self, code: str, **kw):
        d = replace(make_device(code), **kw)
        self._ensure(d)
        return d

    def _ensure(self, device):
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

    def oku(self, device, signals=None) -> list:
        okumalar = self.reader.read_device(device=device, signals=signals or SINYALLER)
        self.reader.commit_published(device=device, readings=okumalar)
        return okumalar

    # --- cihaz davranisi ---
    def link_acildi(self, code: str) -> None:
        c = self.masterlar[code].cache
        c.set_connected(True)
        c.begin_recovery()

    def link_kapandi(self, code: str) -> None:
        self.masterlar[code].cache.set_connected(False)

    def veri_geldi(self, code: str, akim: float = 10.0) -> None:
        self.masterlar[code].cache.set(30, AKIM_IDX, akim)

    def kanit_geldi(self, code: str) -> None:
        """Olcum YOK ama gecerli IIN / basarili gorev — canlilik kaniti."""
        self.masterlar[code].cache.note_evidence()

    def yaslandir(self, code: str, saniye: float) -> None:
        """Kanit ve veri damgalarini GERIYE al (monotonic)."""
        c = self.masterlar[code].cache
        with c._lock:
            for alan in ("_last_evidence_at", "_last_update_at", "_recovery_started_at"):
                deger = getattr(c, alan, 0.0)
                if deger:
                    setattr(c, alan, deger - saniye)


def _reader(tmp_path, **kw):
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(session_store_path=str(tmp_path / "s.json"), diagnostics=False, **kw)
    # SAHADAKI tempo: 5sn event + 30sn baseline -> bayatlik esigi 120sn.
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    r._publish_dnp3_quality = False
    return r


@pytest.fixture
def saha(tmp_path, monkeypatch):
    r = _reader(tmp_path)
    s = Saha(r)
    monkeypatch.setattr(r, "_ensure_master", lambda d, sig=None: s._ensure(d))
    monkeypatch.setattr(r, "_sonda_calistir", lambda mm, device: None)
    return s


def _kaliteler(okumalar) -> set[str]:
    return {o.quality for o in okumalar}


def _smart_cihaz(saha, code="SN2_0", **kw):
    return saha.cihaz(
        code,
        ip_endpoint_type="listening",
        session_policy="smart",
        smart_max_silence_sec=86400,
        **kw,
    )


def _online_yap(saha, device) -> None:
    saha.link_acildi(device.code)
    saha.veri_geldi(device.code)
    saha.oku(device)
    assert saha.master(device.code).cache.state() == "online", "on kosul: online olunamadi"


# ==========================================================================
# 1 — Smart online + SESSIZ: 5+ dakika yeni olcum yok -> comm_lost YOK
# ==========================================================================


def test_1_smart_online_sessizken_comm_lost_uretmez(saha) -> None:
    """BUG 1'in tam regresyonu.

    Bayatlik esigi 120sn. Smart bir cihaz UYANIK ve baglidir ama olcumu
    degismiyor: tarama olmadigi icin kanit da gelmez. Eski kod 120. saniyede
    `device_stale` -> `recovering` -> `comm_lost` uretiyordu.
    """
    d = _smart_cihaz(saha)
    _online_yap(saha, d)

    # 5 dakikadan uzun sessizlik (esigin 2.5 kati).
    saha.yaslandir(d.code, 320)
    for _ in range(5):
        okumalar = saha.oku(d)
        assert "comm_lost" not in _kaliteler(okumalar), "sessiz ama SAGLIKLI Smart oturumu kopuk ilan edildi"
    mm = saha.master(d.code)
    assert mm.cache.state() == "online", f"durum bozuldu: {mm.cache.state()}"
    assert mm.cache.is_connected() is True


def test_1b_continuous_cihazda_bayatlik_aynen_calisir(saha) -> None:
    """§11 REGRESYON GUVENLIGI: duzeltme CIHAZ BAZINDADIR.

    `continuous` icin "her tarama turunda cevap gelir" varsayimi GECERLIDIR
    ve bayatlik -> recovering -> comm_lost davranisi KORUNMALIDIR.
    """
    d = saha.cihaz("BOOST-1", ip_endpoint_type="listening", session_policy="continuous")
    _online_yap(saha, d)

    saha.yaslandir(d.code, 320)
    okumalar = saha.oku(d)
    mm = saha.master(d.code)
    assert mm.cache.state() in ("recovering", "lost"), (
        "continuous cihazda bayatlik tespiti KAYBOLDU — regresyon"
    )
    assert "comm_lost" in _kaliteler(okumalar)


# ==========================================================================
# 2 — Smart online + beklenen kapanma -> smart_idle (comm_lost YOK)
# ==========================================================================


def test_2_beklenen_kapanma_smart_idle_uretir(saha) -> None:
    d = _smart_cihaz(saha)
    _online_yap(saha, d)

    saha.link_kapandi(d.code)
    okumalar = saha.oku(d)
    mm = saha.master(d.code)
    assert mm.cache.state() == "smart_idle", f"beklenen kapanma {mm.cache.state()} uretti"
    assert "comm_lost" not in _kaliteler(okumalar), "beklenen uyku comm_lost uretti"


# ==========================================================================
# 3 + 5 — ESKI kare sonsuz relink/timeout dongusu URETEMEZ
# ==========================================================================


def test_3_eski_kare_sonsuz_relink_dongusu_uretmez(saha) -> None:
    """BUG 2'nin tam regresyonu — sahada ~15sn'de bir tekrarliyordu.

    Sahne: cihaz `lost`, kanit hala "taze" (esigin altinda) ama YENI kanit
    GELMIYOR. Eski kod her grace bitiminde ayni eski kareyle relink ediyordu:

        lost -> relink -> recovering -> 15sn -> timeout -> lost -> relink ...
    """
    d = _smart_cihaz(saha)
    _online_yap(saha, d)

    # Cihaz susuyor ama TCP acik; recovery'yi zorla ve grace'i doldur.
    mm = saha.master(d.code)
    mm.cache.begin_recovery()
    saha.yaslandir(d.code, GRACE + 1)
    saha.oku(d)
    assert mm.cache.state() == "lost", "grace dolmasina ragmen lost'a gecilmedi"

    # ---- ASIL IDDIA: ayni eski kanitla YENIDEN recovery BASLAMAMALI ----
    gecisler = 0
    for _ in range(20):
        onceki = mm.cache.state()
        saha.oku(d)
        if onceki == "lost" and mm.cache.state() == "recovering":
            gecisler += 1
            # Salinimi taklit et: grace'i tekrar doldur.
            saha.yaslandir(d.code, GRACE + 1)
            saha.oku(d)
    assert gecisler == 0, (
        f"ESKI kanitla {gecisler} kez yeniden recovery baslatildi — 15sn salinimi geri geldi"
    )
    assert mm.cache.state() == "lost"


def test_5_recovery_timeout_sonrasi_ayni_kare_lost_birakir(saha) -> None:
    """Kanit sinirinin dogrudan birim testi."""
    d = _smart_cihaz(saha)
    _online_yap(saha, d)
    mm = saha.master(d.code)

    assert mm.cache.relink_izinli() is True, "basarisizlik yokken relink engellendi"

    mm.cache.begin_recovery()
    saha.yaslandir(d.code, GRACE + 1)
    saha.oku(d)
    assert mm.cache.state() == "lost"
    assert mm.cache.relink_izinli() is False, "basarisizliktan sonra ESKI kanit relink'e izin verdi"

    # GERCEKTEN yeni kanit gelirse izin yeniden acilir.
    saha.kanit_geldi(d.code)
    assert mm.cache.relink_izinli() is True, "YENI kanit geldigi halde relink engellendi"


# ==========================================================================
# 4 — lost -> YENI link -> YENI kare -> online (TAM BIR KEZ)
# ==========================================================================


def test_4_yeni_link_ve_yeni_kare_ile_online_olunur(saha) -> None:
    d = _smart_cihaz(saha)
    _online_yap(saha, d)
    mm = saha.master(d.code)

    # Kopus
    mm.cache.begin_recovery()
    saha.yaslandir(d.code, GRACE + 1)
    saha.oku(d)
    assert mm.cache.state() == "lost"

    # YENI baglanti + YENI veri
    saha.link_kapandi(d.code)
    saha.link_acildi(d.code)
    saha.veri_geldi(d.code, akim=33.0)
    saha.oku(d)
    assert mm.cache.state() == "online", "yeni link + yeni kare online yapmadi"

    # Ve bir daha salinmaz.
    for _ in range(5):
        saha.oku(d)
        assert mm.cache.state() == "online"


# ==========================================================================
# 6 — Smart uyanma pazarliginda comm_lost grace DOLMADAN yayinlanmaz
# ==========================================================================


def test_6_uyanma_pazarliginda_comm_lost_ertelenir(saha) -> None:
    """BUG 3'un regresyonu — sahada link_open'dan 3 saniye sonra alarm."""
    d = _smart_cihaz(saha)
    _online_yap(saha, d)

    # Cihaz uyudu -> smart_idle
    saha.link_kapandi(d.code)
    saha.oku(d)
    mm = saha.master(d.code)
    assert mm.cache.state() == "smart_idle"

    # Disaridan uyandirildi: TCP acildi, DNP3 henuz konusmadi.
    saha.link_acildi(d.code)
    assert mm.cache.state() == "recovering"

    okumalar = saha.oku(d)
    assert "comm_lost" not in _kaliteler(okumalar), "uyanma pazarligi sirasinda SAHTE comm_lost yayinlandi"


def test_6b_grace_gercekten_dolarsa_comm_lost_yayinlanir(saha) -> None:
    """Erteleme BASTIRMA DEGILDIR: gercek ariza GORUNUR kalmali."""
    d = _smart_cihaz(saha)
    _online_yap(saha, d)
    saha.link_kapandi(d.code)
    saha.oku(d)

    saha.link_acildi(d.code)
    saha.oku(d)  # erteleme sayaci burada baslar
    saha.yaslandir(d.code, GRACE + 2)
    mm = saha.master(d.code)
    with mm.cache._lock:  # erteleme damgasini da geriye al
        mm.cache._comm_lost_erteleme_at -= GRACE + 2

    okumalar = saha.oku(d)
    assert "comm_lost" in _kaliteler(okumalar), (
        "grace dolduktan sonra comm_lost YAYINLANMADI — gercek ariza gizlendi"
    )


def test_6c_link_flap_ederse_erteleme_sonsuz_uzamaz(saha) -> None:
    """EN TEHLIKELI SESSIZ BASARISIZLIK.

    Cihaz baglanip hemen kopuyorsa her `OnOpen` yeni bir `begin_recovery()`
    uretir ve `recovery_age()` sifirlanir. Erteleme ona baglansaydi bozuk
    bir cihaz SONSUZA KADAR saglikli gorunurdu. Erteleme damgasi yalnizca
    GERCEK `online` gecisinde sifirlanir.
    """
    d = _smart_cihaz(saha)
    _online_yap(saha, d)
    saha.link_kapandi(d.code)
    saha.oku(d)
    mm = saha.master(d.code)

    kalite: set[str] = set()
    for _ in range(12):
        saha.link_acildi(d.code)  # FLAP: her turda yeniden acilis
        kalite |= _kaliteler(saha.oku(d))
        saha.link_kapandi(d.code)
        saha.oku(d)
        # Erteleme damgasini geriye alarak zamani ilerlet.
        with mm.cache._lock:
            if mm.cache._comm_lost_erteleme_at:
                mm.cache._comm_lost_erteleme_at -= GRACE / 3

    assert "comm_lost" in kalite, "link flap ederken comm_lost SONSUZA KADAR ertelendi — bozuk cihaz gizlenir"


# ==========================================================================
# 9 — AUTO: etkin politikaya gore semantik
# ==========================================================================


def test_9_auto_smart_cozulunce_smart_semantigi_uygulanir(saha) -> None:
    d = saha.cihaz("AUTO-S", ip_endpoint_type="listening", session_policy="auto")
    mm = saha.master("AUTO-S")
    assert mm.configured_session_policy == "auto"
    assert mm.session_policy == "smart", "auto + mod bilinmiyor -> sessiz baslamali"

    _online_yap(saha, d)
    saha.yaslandir(d.code, 320)
    okumalar = saha.oku(d)
    assert "comm_lost" not in _kaliteler(okumalar), "etkin smart olan auto cihazda bayatlik comm_lost uretti"
    assert mm.cache.state() == "online"


def test_9b_auto_boost_cozulunce_continuous_semantigi_uygulanir(saha) -> None:
    d = saha.cihaz("AUTO-B", ip_endpoint_type="listening", session_policy="auto")
    mm = saha.master("AUTO-B")
    _online_yap(saha, d)

    # Boost'a cozuldu: etkin politika continuous.
    mm.session_policy = "continuous"
    mm.cache.set_session_policy("continuous")

    saha.yaslandir(d.code, 320)
    saha.oku(d)
    assert mm.cache.state() in ("recovering", "lost"), (
        "etkin continuous olan auto cihazda bayatlik tespiti calismadi"
    )
    assert mm.configured_session_policy == "auto", "yapilandirilan politika yeniden yazildi"


# ==========================================================================
# DURUM MODELI — istenen gecis zinciri ucdan uca
# ==========================================================================


def test_durum_modeli_ucdan_uca(saha) -> None:
    """smart_idle -> link open -> recovering -> taze kanit -> online
    -> beklenen kapanma -> smart_idle   (comm_lost HIC yok)
    """
    d = _smart_cihaz(saha)
    mm = saha.master(d.code)
    tum_kaliteler: set[str] = set()

    # Baslangic: cihaz uyuyor (listening + baglanti yok)
    tum_kaliteler |= _kaliteler(saha.oku(d))
    assert mm.cache.state() == "smart_idle"

    # Uyanma
    saha.link_acildi(d.code)
    assert mm.cache.state() == "recovering"
    tum_kaliteler |= _kaliteler(saha.oku(d))

    # Taze kanit -> online
    saha.veri_geldi(d.code, akim=12.5)
    tum_kaliteler |= _kaliteler(saha.oku(d))
    assert mm.cache.state() == "online"

    # Sessiz ama saglikli (esigin cok uzerinde)
    saha.yaslandir(d.code, 400)
    tum_kaliteler |= _kaliteler(saha.oku(d))
    assert mm.cache.state() == "online", "sessiz saglikli oturum bozuldu"

    # Cihaz kendi programina gore uyudu
    saha.link_kapandi(d.code)
    tum_kaliteler |= _kaliteler(saha.oku(d))
    assert mm.cache.state() == "smart_idle"

    assert "comm_lost" not in tum_kaliteler, (
        f"beklenen yasam dongusunde comm_lost yayinlandi: {tum_kaliteler}"
    )


def test_smart_online_sessizken_yoklama_da_gondermez(saha) -> None:
    """Sessiz oturumda gateway cihaza HICBIR istek gondermemeli.

    `_veri_sessizligini_yokla` ve `_kopuk_cihazi_yokla` smart'ta kapalidir;
    bu test tarafsiz bir sayacla dogrular.
    """
    d = _smart_cihaz(saha)
    _online_yap(saha, d)
    mm = saha.master(d.code)
    baslangic = mm.gateway_trafigi

    saha.yaslandir(d.code, 400)
    for _ in range(10):
        saha.oku(d)
    assert mm.gateway_trafigi == baslangic, (
        f"sessiz Smart oturumunda gateway {mm.gateway_trafigi - baslangic} istek gonderdi"
    )
