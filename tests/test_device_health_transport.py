"""G-DEVICE-HEALTH-01 — cihaz basina calisma-zamani sagligi GIDEN kanali.

KAPATILAN URETIM SORUNU
-----------------------
Gateway v1.14 cihaz basina zengin saglik bilgisine SAHIPTI ama Grid onu
GUVENLI sekilde ALAMIYORDU. Tek tasiyici `X-E1-Gateway-Health` basligiydi ve
o baslik:

  * `/pending` isteklerine biner — yani FIZIKSEL KOMUT KANALININ tasiyicisi,
  * backend tavani ~2 KB (`health_header.MAX_HEADER_BYTES` = 1600),
  * 200+ cihaz oraya SIGMAZ.

Baslik buyutulerek cozulseydi bir proxy/backend baslik limitinde `/pending`
400 doner ve KESICI KOMUTLARI DURURDU. Bu yuzden cihaz sagligi AYRI, GOVDE
tabanli, komuttan BAGIMSIZ bir kanala tasindi.

BU DOSYANIN OLCTUGU SOZLESME
----------------------------
* Parti HTTP GOVDESINDE gider, BASLIKTA DEGIL.
* `/pending` basligi 200 cihazda bile SINIRLI kalir.
* `smart_idle` != offline; `report_late` != lost; sondalar durum BELIRLEMEZ.
* Backend erisilemezken bellek SINIRLI buyur (cihaz basina EN SON durum).
* Ag hatasi DNP3 okumasini/komutlari BLOKLAMAZ.
* Bayat bir yeniden gonderim daha yeni durumu EZEMEZ (`boot_id`+`sequence`).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from dnp3_gateway.backend import health_header
from dnp3_gateway.backend.device_health_publisher import (
    BACKOFF_MAX_SEC,
    DeviceHealthPublisher,
    next_boot_id,
)
from dnp3_gateway.backend.device_health_wire import (
    CONNECTION_STATES,
    SCHEMA_VERSION,
    build_device_record,
    build_envelope,
)

_OLAY_TIMEOUT = 5.0


# ==========================================================================
# Yardimcilar
# ==========================================================================


def _saglik(
    *,
    state: str = "online",
    connected: bool = True,
    reachable: bool = True,
    configured: str = "smart",
    effective: str = "smart",
    mode: str = "smart",
    late: bool = False,
    **kw: Any,
) -> dict[str, Any]:
    """`device_health()` ciktisinin gercekci bir satiri."""
    d: dict[str, Any] = {
        "state": state,
        "connected": connected,
        "reachable": reachable,
        "configured_session_policy": configured,
        "effective_session_policy": effective,
        "operation_mode": mode,
        "operation_mode_raw": 1.0 if mode == "smart" else 0.0,
        "dial_in_interval_min": 720,
        "next_expected_report_epoch": 1_755_643_200.0,
        "report_overdue_sec": 0.0,
        "report_late": late,
        "last_valid_contact_epoch": 1_755_600_000.0,
        "last_frame_epoch": 1_755_600_000.0,
        "ip_probe_status": "unknown",
        "tcp_probe_status": "connecting",
        "last_probe_epoch": None,
        "ip_endpoint_type": "listening",
    }
    d.update(kw)
    return d


class SahteGonderici:
    """`post_device_health` taklidi. Gonderilen govdeleri SAKLAR."""

    def __init__(self, *, hata: Exception | None = None) -> None:
        self.govdeler: list[dict[str, Any]] = []
        self.hata = hata
        self.cagri = 0
        self._lock = threading.Lock()
        self.gonderildi = threading.Event()

    def __call__(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.cagri += 1
            if self.hata is not None:
                raise self.hata
            # KOPYALA: yayinci ic durumunu sonradan degistirirse test
            # gonderilen ANI olcmeli.
            self.govdeler.append(json.loads(json.dumps(payload)))
        self.gonderildi.set()

    def cihaz_kodlari(self) -> list[str]:
        return [d["device_code"] for g in self.govdeler for d in g["devices"]]


def _yayinci(kaynak, gonderici, **kw: Any) -> DeviceHealthPublisher:
    kw.setdefault("change_debounce_sec", 0.0)
    kw.setdefault("snapshot_interval_sec", 30.0)
    return DeviceHealthPublisher(
        health_source=kaynak,
        send=gonderici,
        gateway_code="GW-001",
        gateway_instance_id="inst-abc",
        boot_id=7,
        **kw,
    )


def _govde_kaynagi(fn) -> str:
    """Fonksiyonun GOVDE kaynagi — docstring HARIC.

    Duz `inspect.getsource` docstring'i de dondurur; bu depoda docstring'ler
    KALDIRILAN davranisi acikca anlatiyor (orn. "X-E1-Gateway-Health neden
    kullanilmiyor"). Duz metin aramasi o aciklamalari ihlal sanip yanlis
    alarm verirdi.
    """
    import ast
    import inspect
    import textwrap

    kaynak = textwrap.dedent(inspect.getsource(fn))
    dugum = ast.parse(kaynak).body[0]
    govde = dugum.body
    if (
        govde
        and isinstance(govde[0], ast.Expr)
        and isinstance(govde[0].value, ast.Constant)
        and isinstance(govde[0].value.value, str)
    ):
        govde = govde[1:]  # docstring'i DISLA
    return "\n".join(ast.unparse(n) for n in govde)


def _bekle(kosul, timeout: float = _OLAY_TIMEOUT) -> bool:
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        if kosul():
            return True
        time.sleep(0.02)
    return False


# ==========================================================================
# D1-D3 — tel sozlesmesi
# ==========================================================================


def test_d1_zarf_sema_ve_siralama_alanlarini_tasir() -> None:
    zarf = build_envelope(
        gateway_code="GW-001",
        gateway_instance_id="inst-abc",
        boot_id=7,
        sequence=1,
        snapshot=True,
        devices=[build_device_record("D1", _saglik())],
        device_total=1,
    )
    assert zarf["schema"] == SCHEMA_VERSION == "device_health_v1"
    assert zarf["gateway_code"] == "GW-001"
    assert zarf["gateway_instance_id"] == "inst-abc"
    # SIRALAMA IKILISI: duvar saati YOK.
    assert zarf["boot_id"] == 7
    assert zarf["sequence"] == 1
    assert zarf["snapshot"] is True
    assert zarf["device_total"] == 1
    assert "timestamp" not in zarf and "sent_at" not in zarf, "siralama duvar saatine baglanmis olabilir"


def test_d2_kayit_yalnizca_sahip_olunan_alanlari_tasir() -> None:
    kayit = build_device_record("SN2-001", _saglik(state="smart_idle", connected=False, reachable=False))
    assert kayit["device_code"] == "SN2-001"
    assert kayit["connection_state"] == "smart_idle"
    assert kayit["connected"] is False
    assert kayit["reachable"] is False
    assert kayit["operation_mode"] == "smart"
    assert kayit["configured_session_policy"] == "smart"
    assert kayit["dial_in_interval_min"] == 720
    assert kayit["last_valid_contact_epoch"] == 1_755_600_000.0

    # UYDURMA YOK: tanimadigimiz token `unknown`a duser.
    bozuk = build_device_record("X", _saglik(state="uydurma_durum", mode="belirsiz"))
    assert bozuk["connection_state"] == "unknown"
    assert bozuk["operation_mode"] == "unknown"

    # `0`/negatif epoch "hic olmadi"dir; panelde 1970 gostermeyiz.
    sifir = build_device_record("X", _saglik(last_probe_epoch=0, last_frame_epoch=-5))
    assert sifir["last_probe_epoch"] is None
    assert sifir["last_frame_epoch"] is None


def test_d3_beklenen_baglanti_durumlari() -> None:
    for durum in ("online", "smart_idle", "recovering", "lost", "unknown"):
        assert durum in CONNECTION_STATES
        assert build_device_record("D", _saglik(state=durum))["connection_state"] == durum


# ==========================================================================
# D4-D6 — SEMANTIK (v1.14 korunur)
# ==========================================================================


def test_d4_smart_idle_offline_degildir() -> None:
    """`smart_idle` SAGLIKLI uykudur. `lost`a esitlenirse saglikli uyuyan
    filo SCADA'da arizali gorunur."""
    k = build_device_record("D", _saglik(state="smart_idle", connected=False, reachable=False))
    assert k["connection_state"] == "smart_idle"
    assert k["connection_state"] != "lost"


def test_d5_report_late_lost_degildir() -> None:
    """`late` DEGRADED uyaridir; `connection_state` `smart_idle` KALIR.

    Karistirilirsa Dial-In gecikmesi (cok sik iyi huylu) gunluk sahte alarm
    uretir.
    """
    k = build_device_record(
        "D", _saglik(state="smart_idle", connected=False, late=True, report_overdue_sec=1800.0)
    )
    assert k["report_late"] is True
    assert k["connection_state"] == "smart_idle", "late cihaz kopuk raporlandi"
    assert k["report_overdue_sec"] == 1800.0


def test_d6_sonda_sonuclari_durumu_belirlemez() -> None:
    """Sondalar SALT TESHIS. "ping dusuyor -> cihaz oldu" kurali filonun
    yarisini sahte kopuk gosterirdi."""
    k = build_device_record(
        "D",
        _saglik(state="smart_idle", ip_probe_status="unreachable", tcp_probe_status="unknown"),
    )
    assert k["ip_probe_status"] == "unreachable"
    assert k["connection_state"] == "smart_idle", "sonda sonucu durumu degistirdi"


def test_d7_operation_mode_smart_boost() -> None:
    """1 = Smart, 0 = Boost (bkz. operation_mode.py bayrak okteti turetmesi)."""
    assert build_device_record("D", _saglik(mode="smart"))["operation_mode"] == "smart"
    assert build_device_record("D", _saglik(mode="boost"))["operation_mode"] == "boost"


def test_d8_boost_mode_enabled_calisma_zamanina_giremez() -> None:
    """`Boost Mode Enabled` KONFIGURASYONDUR (yetenek), calisma anindaki
    durum DEGIL. Tel kaydinda YERI YOKTUR."""
    saglik = _saglik(mode="smart")
    saglik["boost_mode_enabled"] = True  # katalogda olsa bile
    k = build_device_record("D", saglik)
    assert k["operation_mode"] == "smart", "boost_mode_enabled modu ezdi"
    assert "boost_mode_enabled" not in k, "yetenek alani tel kaydina sizdi"


def test_d9_satellite_alanlari_tel_kaydina_girmez() -> None:
    saglik = _saglik()
    saglik["sat01_operation_mode"] = "boost"
    k = build_device_record("D", saglik)
    assert not any("sat" in a.lower() for a in k), f"satellite alani sizdi: {list(k)}"


# ==========================================================================
# D10-D13 — TASIMA: govde, parti, olcek
# ==========================================================================


def test_d10_tek_cihaz_ilk_parti_snapshot_olur() -> None:
    g = SahteGonderici()
    y = _yayinci(lambda: {"D1": _saglik()}, g)
    y.start()
    try:
        assert g.gonderildi.wait(_OLAY_TIMEOUT)
        assert _bekle(lambda: len(g.govdeler) >= 1)
        z = g.govdeler[0]
        assert z["snapshot"] is True, "ilk parti tam anlik goruntu olmali"
        assert z["device_total"] == 1
        assert [d["device_code"] for d in z["devices"]] == ["D1"]
        assert z["sequence"] == 1
    finally:
        y.stop()


def test_d11_200_cihaz_partilere_bolunur_ve_hepsi_gider() -> None:
    kaynak = {f"SN2-{i:04d}": _saglik(state="smart_idle" if i % 3 else "online") for i in range(200)}
    g = SahteGonderici()
    y = _yayinci(lambda: kaynak, g, batch_max=50)
    y.start()
    try:
        assert _bekle(lambda: len(g.cihaz_kodlari()) >= 200)
        assert len(g.govdeler) == 4, f"200/50 = 4 parti beklenir, {len(g.govdeler)} geldi"
        assert sorted(g.cihaz_kodlari()) == sorted(kaynak), "cihaz kaybi var"
        for z in g.govdeler:
            assert len(z["devices"]) <= 50, "parti siniri asildi"
            assert z["device_total"] == 200
            assert z["snapshot"] is True
        # Siralama MONOTONIK ve partiler arasinda ARTAR.
        siralar = [z["sequence"] for z in g.govdeler]
        assert siralar == sorted(siralar) == list(range(1, 5))
    finally:
        y.stop()


def test_d12_gercekci_govde_boyutu_makul_kalir() -> None:
    """Uzun cihaz kodlariyla bile tek parti ongorulebilir boyutta olmali."""
    uzun = {f"SAHA-ISTASYON-{i:03d}-FIDER-{i:03d}-SN2-{'X' * 20}": _saglik() for i in range(50)}
    g = SahteGonderici()
    y = _yayinci(lambda: uzun, g, batch_max=50)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1)
        bayt = len(json.dumps(g.govdeler[0]).encode("utf-8"))
        assert bayt < 128 * 1024, f"tek parti {bayt} bayt — fazla buyuk"
        # Ve BASLIK tavaninin cok uzerinde: tam da bu yuzden govde kullaniyoruz.
        assert bayt > health_header.MAX_HEADER_BYTES
    finally:
        y.stop()


def test_d13_parti_govdede_gider_baslikta_degil() -> None:
    """Kanalin varlik sebebi: bu veri BASLIGA sigmaz ve sigdirilmaya
    calisilirsa komut kanali tehlikeye girer.

    Kontrol AST ile GOVDE uzerinde yapilir. Duz metin aramasi docstring'i de
    tarardi ve o docstring tam da `X-E1-Gateway-Health`in NEDEN kullanilmadigini
    anlatiyor — yani dogru yazilmis bir aciklama testi kirmis olurdu.
    """
    from dnp3_gateway.backend.config_client import BackendConfigClient

    govde = _govde_kaynagi(BackendConfigClient.post_device_health)
    assert "json=payload" in govde, "govde JSON olarak gonderilmiyor"
    # Saglik verisi BASLIGA konmuyor.
    assert "X-E1-Gateway-Health" not in govde
    assert "health_header" not in govde
    # Kimlik basliklari kanonik uretecten; komut duzlemi credential'i BILEREK
    # gonderilmiyor (saglik telemetrisi komut yetkisi gerektirmez).
    assert "build_config_request_headers" in govde
    assert "build_command_request_headers" not in govde, "saglik kanali komut duzlemi credential'ini tasiyor"


def test_d14_pending_basligi_200_cihazda_da_sinirli_kalir() -> None:
    """Toplu baslik BUYUTULMEDI — yeni kanal onu rahatlatmali, sismemeli."""
    ozet = {
        "total": 200,
        "online": 120,
        "smart_idle": 70,
        "lost": 8,
        "smart_lost": 8,
        "late": 5,
        "recovering": 2,
        "unknown": 0,
    }
    per_device = {f"SN2-{i:04d}": {"state": "lost" if i < 8 else "smart_idle"} for i in range(200)}
    baslik = health_header.encode_header(
        health_header.build_payload(status="degraded", device_summary=ozet, per_device=per_device)
    )
    assert baslik is not None
    assert len(baslik.encode("utf-8")) <= health_header.MAX_HEADER_BYTES, (
        "toplu saglik basligi tavani asti — komut kanali risk altinda"
    )


# ==========================================================================
# D15-D17 — DELTA / SNAPSHOT / DURUM GECISLERI
# ==========================================================================


def test_d15_degismeyen_cihaz_tekrar_gonderilmez() -> None:
    durum = {"D1": _saglik(), "D2": _saglik()}
    g = SahteGonderici()
    y = _yayinci(lambda: durum, g, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1)
        ilk = len(g.govdeler)
        for _ in range(5):
            y.mark_dirty()
            time.sleep(0.05)
        time.sleep(0.3)
        assert len(g.govdeler) == ilk, "degisiklik yokken tekrar gonderim yapildi"
    finally:
        y.stop()


def test_d16_durum_gecisi_online_smart_idle_late_lost_ve_kurtarma() -> None:
    """Gecisler DERHAL yayinlanmali ve yalnizca DEGISEN cihaz gitmeli."""
    durum = {"D1": _saglik(state="online"), "SABIT": _saglik(state="online")}
    g = SahteGonderici()
    y = _yayinci(lambda: durum, g, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1)
        onceki = len(g.govdeler)

        def gecis(yeni: dict[str, Any], beklenen_durum: str) -> None:
            nonlocal onceki
            durum["D1"] = yeni
            y.mark_dirty()
            assert _bekle(lambda: len(g.govdeler) > onceki), f"{beklenen_durum} yayinlanmadi"
            z = g.govdeler[-1]
            assert z["snapshot"] is False, "delta olmali"
            assert [d["device_code"] for d in z["devices"]] == ["D1"], "degismeyen cihaz da gonderildi"
            assert z["devices"][0]["connection_state"] == beklenen_durum
            onceki = len(g.govdeler)

        gecis(_saglik(state="smart_idle", connected=False, reachable=False), "smart_idle")
        # smart_idle + late -> HALA smart_idle (lost DEGIL)
        durum["D1"] = _saglik(state="smart_idle", connected=False, reachable=False, late=True)
        y.mark_dirty()
        assert _bekle(lambda: len(g.govdeler) > onceki)
        son = g.govdeler[-1]["devices"][0]
        assert son["report_late"] is True and son["connection_state"] == "smart_idle"
        onceki = len(g.govdeler)

        gecis(_saglik(state="lost", connected=False, reachable=False), "lost")
        gecis(_saglik(state="online"), "online")  # kurtarma
    finally:
        y.stop()


def test_d17_periyodik_snapshot_uzlastirir() -> None:
    durum = {"D1": _saglik(), "D2": _saglik()}
    g = SahteGonderici()
    y = _yayinci(lambda: durum, g, snapshot_interval_sec=30.0)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1)
        assert g.govdeler[0]["snapshot"] is True
        onceki = len(g.govdeler)

        # Elle uzlastirma istegi (config degisimi vb.)
        y.request_snapshot()
        assert _bekle(lambda: len(g.govdeler) > onceki)
        z = g.govdeler[-1]
        assert z["snapshot"] is True
        assert sorted(d["device_code"] for d in z["devices"]) == ["D1", "D2"], (
            "snapshot TUM cihazlari tasimali"
        )
    finally:
        y.stop()


# ==========================================================================
# D18-D21 — HATA / BACKPRESSURE / IZOLASYON
# ==========================================================================


def test_d18_backend_erisilemezken_bellek_sinirli_kalir() -> None:
    """Kesinti ne kadar surerse sursun kayit sayisi CIHAZ SAYISI kadar.

    Gecis basina biriktiren bir tasarim, uzun bir kesintide RAM'i sinirsiz
    buyuturdu. Bu kanal EN SON durumu tutar (coalescing).
    """
    durum = {f"D{i}": _saglik(state="online") for i in range(20)}
    g = SahteGonderici(hata=RuntimeError("backend down"))
    y = _yayinci(lambda: durum, g, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: g.cagri >= 1)
        # Kesinti boyunca cihazlar durum degistirmeye devam etsin.
        for tur in range(30):
            for i in range(20):
                durum[f"D{i}"] = _saglik(state="smart_idle" if (tur + i) % 2 else "online")
            y.mark_dirty()
            time.sleep(0.01)
        time.sleep(0.2)
        ist = y.stats()
        assert ist["tracked_devices"] <= 20, f"gecis basina birikim var: {ist['tracked_devices']} kayit"
        assert ist["failed_attempts"] >= 1
    finally:
        y.stop()


def test_d19_yeniden_deneme_sinirli_backoff_ile() -> None:
    g = SahteGonderici(hata=RuntimeError("backend down"))
    y = _yayinci(lambda: {"D1": _saglik()}, g)
    y.start()
    try:
        assert _bekle(lambda: g.cagri >= 2, timeout=8.0), "yeniden deneme yapilmadi"
        # Sinirli: sonsuz hizli dongu YOK.
        assert g.cagri < 60, f"{g.cagri} deneme — backoff calismiyor olabilir"
        assert y.stats()["last_error"].startswith("RuntimeError")
    finally:
        y.stop()
    assert BACKOFF_MAX_SEC <= 300, "geri cekilme tavani makul olmali"


def test_d20_backend_geri_gelince_durum_uzlasir() -> None:
    durum = {"D1": _saglik(state="online")}
    g = SahteGonderici(hata=RuntimeError("down"))
    y = _yayinci(lambda: durum, g)
    y.start()
    try:
        assert _bekle(lambda: g.cagri >= 1)
        durum["D1"] = _saglik(state="lost", connected=False, reachable=False)
        g.hata = None  # backend geri geldi
        y.mark_dirty()
        assert _bekle(lambda: len(g.govdeler) >= 1, timeout=8.0)
        # EN SON durum gitmeli; ara gecisler DEGIL.
        assert g.govdeler[-1]["devices"][0]["connection_state"] == "lost"
    finally:
        y.stop()


def test_d21_saglik_kaynagi_patlasa_bile_yayinci_yasar() -> None:
    """Adapter hatasi bu kanali DUSURMEMELI."""
    patla = {"n": 0}

    def kaynak() -> dict[str, Any]:
        patla["n"] += 1
        if patla["n"] < 3:
            raise RuntimeError("adapter mesgul")
        return {"D1": _saglik()}

    g = SahteGonderici()
    y = _yayinci(kaynak, g)
    y.start()
    try:
        for _ in range(6):
            y.mark_dirty()
            time.sleep(0.05)
        assert _bekle(lambda: len(g.govdeler) >= 1, timeout=8.0), "kaynak hatasindan sonra toparlamadi"
    finally:
        y.stop()


def test_d22_mark_dirty_bloklamaz() -> None:
    """Poll thread'inden cagrilir; ag isi ya da kilit beklemesi OLMAMALI.

    Gonderici BILEREK yavas: `mark_dirty` yine de aninda donmeli.
    """
    yavas = threading.Event()

    def yavas_gonder(payload: dict[str, Any]) -> None:
        yavas.wait(timeout=3.0)

    y = _yayinci(lambda: {"D1": _saglik()}, yavas_gonder)
    y.start()
    try:
        time.sleep(0.2)  # gonderici ucusta
        basla = time.monotonic()
        for _ in range(1000):
            y.mark_dirty()
        gecen = time.monotonic() - basla
        assert gecen < 0.5, f"1000 mark_dirty {gecen:.2f}s surdu — poll yolu bloke oluyor"
    finally:
        yavas.set()
        y.stop()


# ==========================================================================
# D23-D25 — SIRALAMA / RESTART / KAPANIS
# ==========================================================================


def test_d23_sequence_monotonik_artar() -> None:
    durum = {"D1": _saglik(state="online")}
    g = SahteGonderici()
    y = _yayinci(lambda: durum, g, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1)
        for i in range(4):
            durum["D1"] = _saglik(state="lost" if i % 2 else "online", connected=i % 2 == 0)
            y.mark_dirty()
            time.sleep(0.08)
        assert _bekle(lambda: len(g.govdeler) >= 3)
        siralar = [z["sequence"] for z in g.govdeler]
        assert siralar == sorted(siralar), f"sequence geriye gitti: {siralar}"
        assert len(set(siralar)) == len(siralar), "sequence tekrar etti"
        assert all(z["boot_id"] == 7 for z in g.govdeler), "boot_id proses icinde degisti"
    finally:
        y.stop()


def test_d24_boot_id_her_baslangicta_artar(tmp_path) -> None:
    """`gateway_instance_id` restart'ta AYNI kalir — tek basina yetmez.

    Bayat bir yeniden gonderimin daha yeni durumu ezmemesi icin backend
    `(boot_id, sequence)` ikilisine bakar; `boot_id` her acilista artar.
    """
    ilk = next_boot_id(tmp_path, gateway_code="GW-001")
    ikinci = next_boot_id(tmp_path, gateway_code="GW-001")
    ucuncu = next_boot_id(tmp_path, gateway_code="GW-001")
    assert ilk < ikinci < ucuncu, f"boot_id artmadi: {ilk}, {ikinci}, {ucuncu}"

    # Ayri gateway'ler AYNI state dizinini paylasabilir; sayaclari KARISMAZ.
    baska = next_boot_id(tmp_path, gateway_code="GW-002")
    assert baska == 1, "gateway'ler arasi boot_id sayaci karisti"

    # Diske yazilamasa bile yayin DURMAZ (best-effort kanal).
    assert next_boot_id(tmp_path / "olmayan" / "derin", gateway_code="GW-003") >= 1


def test_d25_bayat_yeniden_gonderim_daha_yeniyi_ezemez_sozlesmesi(tmp_path) -> None:
    """Sozlesme dogrulamasi: eski calismanin partisi HER ZAMAN daha kucuk
    `boot_id` tasir, dolayisiyla backend onu reddedebilir.

    Bu, gateway'in backend'e VERDIGI garantidir; backend tarafinda
    `(boot_id, sequence)` leksikografik karsilastirmasi yeterlidir.
    """
    eski_boot = next_boot_id(tmp_path, gateway_code="GW-001")
    yeni_boot = next_boot_id(tmp_path, gateway_code="GW-001")

    eski = build_envelope(
        gateway_code="GW-001",
        gateway_instance_id="inst-abc",
        boot_id=eski_boot,
        sequence=9999,  # eski calisma COK ilerlemis olsa bile
        snapshot=False,
        devices=[build_device_record("D1", _saglik(state="online"))],
        device_total=1,
    )
    yeni = build_envelope(
        gateway_code="GW-001",
        gateway_instance_id="inst-abc",
        boot_id=yeni_boot,
        sequence=1,  # yeni calisma daha yeni BASLAMIS
        snapshot=True,
        devices=[build_device_record("D1", _saglik(state="lost", connected=False))],
        device_total=1,
    )
    assert (yeni["boot_id"], yeni["sequence"]) > (eski["boot_id"], eski["sequence"]), (
        "yuksek sequence'li BAYAT parti daha yeni sayiliyor"
    )
    # Ayni instance_id: kimlik TEK BASINA ayirt etmiyor — boot_id sart.
    assert eski["gateway_instance_id"] == yeni["gateway_instance_id"]


def test_d26_kapanis_guvenli_ve_thread_birakmaz() -> None:
    g = SahteGonderici()
    y = _yayinci(lambda: {"D1": _saglik()}, g)
    y.start()
    assert _bekle(lambda: len(g.govdeler) >= 1)
    assert y.stop(timeout=_OLAY_TIMEOUT) is True
    assert not any(t.name == "device-health" and t.is_alive() for t in threading.enumerate())
    # Idempotent
    assert y.stop(timeout=1.0) is True


def test_d27_devre_disi_iken_hicbir_thread_baslamaz() -> None:
    """Varsayilan KAPALI: backend ucu tanimadan acilirsa her turda 404 gelir."""
    g = SahteGonderici()
    y = _yayinci(lambda: {"D1": _saglik()}, g, enabled=False)
    y.start()
    try:
        y.mark_dirty()
        time.sleep(0.3)
        assert g.cagri == 0, "kapaliyken gonderim yapildi"
        assert not any(t.name == "device-health" for t in threading.enumerate())
    finally:
        y.stop()


# ==========================================================================
# D28 — KOMUT DUZLEMI DOKUNULMADI
# ==========================================================================


def test_d28_komut_duzlemi_ve_toplu_baslik_degismedi() -> None:
    from dnp3_gateway.backend.config_client import BackendConfigClient

    # Toplu baslik sabitleri AYNEN — yeni kanal onu rahatlatir, DEGISTIRMEZ.
    assert health_header.MAX_HEADER_BYTES == 1600
    assert health_header.HEADER_NAME == "X-E1-Gateway-Health"

    # `/pending` yolu saglik kanalindan HABERSIZ olmali.
    pending = _govde_kaynagi(BackendConfigClient.fetch_pending_commands)
    assert "device_health" not in pending, "komut poll yoluna saglik kanali sizdi"
    assert "device-health" not in pending

    # Saglik ucu komut duzlemi MEKANIZMALARINA dokunmaz. Tokenler DAR
    # secildi: `DeviceHealthDeliveryError` mesru olarak "delivery" icerir,
    # dolayisiyla ciplak kelime aramasi yanlis alarm verirdi.
    dh = _govde_kaynagi(BackendConfigClient.post_device_health)
    for yasak in (
        "_command_delivery_token",
        "_delivery_provider",
        "command_ledger",
        "/pending",
        "command-results",
        "COMMAND_TOKEN_HEADER",
    ):
        assert yasak not in dh, f"saglik ucu komut duzlemine dokunuyor: {yasak}"


# ==========================================================================
# D29-D31 — DEPLOYMENT SOZLESMESI (Grid uyumluluk tespiti)
# ==========================================================================


def _sozlesme() -> dict[str, Any]:
    import json as _json
    from pathlib import Path as _Path

    kok = _Path(__file__).resolve().parents[1]
    return _json.loads((kok / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))


def test_d29_yetenek_isareti_ayri_bir_blok() -> None:
    """`smart_session` / `initiating_endpoint` OVERLOAD EDILMEZ.

    Grid'in su ayrimi yapabilmesi gerekiyor:
      1.14.0 -> Smart Listening VAR, cihaz-saglik tasiyicisi YOK
      1.15.0 -> ikisi de VAR
    Yetenek `smart_session` icine gomulseydi bu ayrim IMKANSIZ olurdu.
    """
    d = _sozlesme()
    blok = d["device_runtime_health_transport"]
    assert blok["supported"] is True
    assert blok["min_gateway_version"] == "1.15.0"
    assert blok["schema"] == SCHEMA_VERSION

    # AYRI blok: mevcut yetenekler kirlenmemis olmali.
    assert "device_runtime_health" not in d["smart_session"]
    assert "device_health" not in _json_metin(d["smart_session"])
    assert "device_health" not in _json_metin(d["initiating_endpoint"])


def _json_metin(x: Any) -> str:
    import json as _json

    return _json.dumps(x, ensure_ascii=False)


def test_d30_sozlesme_http_ve_siralama_semantigini_tasir() -> None:
    blok = _sozlesme()["device_runtime_health_transport"]
    assert blok["http_method"] == "POST"
    assert blok["path"] == "/gateways/{gateway_code}/device-health"
    assert blok["transport"] == "http_body"
    assert blok["aggregate_header_unchanged"] is True
    assert blok["direction"] == "outbound_gateway_to_backend"

    # EN KRITIK SOZLESME MADDELERI — bozulursa saha davranisi sessizce
    # tersine doner.
    assert blok["probes_affect_connection_state"] is False
    assert blok["blocks_dnp3_read_loop"] is False
    assert blok["blocks_command_plane"] is False
    assert blok["blocks_telemetry"] is False
    assert blok["default_enabled"] is False

    assert "boot_id" in blok["ordering_model"] and "sequence" in blok["ordering_model"]
    # Snapshot korelasyonu (review madde 3) — `device_total` tek basina yetmez.
    assert blok["snapshot_retry_creates_new_snapshot_id"] is True
    assert "snapshot_id" in blok["snapshot_correlation"]
    assert len(blok["backend_snapshot_rules"]) == 3
    # Poll yoluna baglilik (review madde 1) ve compose yolu (madde 2).
    assert "finally" in blok["poll_loop_notification"]
    assert "request_snapshot" in blok["poll_loop_notification"]
    assert "${VAR:-" in blok["compose_enable_path"]
    assert "DUZENLEMEDEN" in blok["compose_enable_path"]
    assert "coalescing" in blok["backpressure"]
    assert sorted(blok["connection_states"]) == sorted(CONNECTION_STATES)


def test_d31_sozlesme_ve_kod_ayni_varsayilanlari_soyluyor() -> None:
    """Sozlesme dosyasi Grid'in okudugu yerdir; koddan SAPAMAZ."""
    from dnp3_gateway.backend import device_health_publisher as dhp
    from dnp3_gateway.config import Settings

    blok = _sozlesme()["device_runtime_health_transport"]
    assert blok["batch_max_default"] == dhp.DEFAULT_BATCH_MAX
    assert blok["snapshot_interval_sec_default"] == int(dhp.DEFAULT_SNAPSHOT_INTERVAL_SEC)

    alanlar = Settings.model_fields
    assert alanlar["device_health_publish_enabled"].default is False
    assert alanlar["device_health_batch_max"].default == dhp.DEFAULT_BATCH_MAX
    assert alanlar["device_health_snapshot_interval_sec"].default == int(dhp.DEFAULT_SNAPSHOT_INTERVAL_SEC)
    for env in blok["env"]:
        assert env.lower() in alanlar, f"sozlesmede var, Settings'te YOK: {env}"

    # Grid dokumani mevcut ve sozlesmeden isaret ediliyor.
    from pathlib import Path as _Path

    kok = _Path(__file__).resolve().parents[1]
    dok = kok / blok["integration_doc"]
    assert dok.is_file(), f"entegrasyon dokumani yok: {blok['integration_doc']}"
    metin = dok.read_text(encoding="utf-8")
    for zorunlu in ("device_health_v1", "boot_id", "smart_idle", "report_late", "X-Gateway-Token"):
        assert zorunlu in metin, f"Grid dokumaninda eksik: {zorunlu}"


# ==========================================================================
# D32-D34 — GERCEK CALISMA YOLUNA BAGLILIK (PR #33 review, madde 1)
#
# Yayinciyi izole test etmek YETMEZ: `mark_dirty` gercek poll dongusune
# baglanmazsa ozellik uretimde HIC tetiklenmez ve durum degisiklikleri ancak
# yayincinin 30sn'lik YEDEK uyanmasinda gorulur. Kabul olcutu:
#
#     durum degisimi -> ~poll araligi + debounce   (30sn yedek DEGIL)
# ==========================================================================


def test_d32_poll_dongusu_her_cycle_sonrasi_yayinciyi_uyarir() -> None:
    """`run_poll_cycle` cagrisindan sonra `mark_dirty()` MUTLAKA cagrilmali."""
    import ast
    import inspect

    from dnp3_gateway import main as main_mod

    kaynak = inspect.getsource(main_mod)
    i = kaynak.index("published = run_poll_cycle(")
    pencere = kaynak[i : i + 2500]
    assert "device_health_publisher.mark_dirty()" in pencere, (
        "poll dongusu cihaz saglik yayincisini UYARMIYOR — ozellik uretimde tetiklenmez"
    )

    # `finally` DALINDA olmali: bir cycle PATLASA bile durum degismis
    # olabilir (`read_device` bir cihazi `lost`a cekip sonrakinde hata verir).
    # `try` icinde olsaydi tam da en ilginc gecisler bildirilmezdi.
    agac = ast.parse(inspect.getsource(main_mod))
    bulundu = False
    for dugum in ast.walk(agac):
        if not isinstance(dugum, ast.Try) or not dugum.finalbody:
            continue
        govde_metni = "\n".join(ast.unparse(n) for n in dugum.body)
        final_metni = "\n".join(ast.unparse(n) for n in dugum.finalbody)
        if "run_poll_cycle" in govde_metni and "mark_dirty" in final_metni:
            bulundu = True
            break
    assert bulundu, "mark_dirty() `finally` dalinda degil — arizali cycle'lar gozlemlenmez"


def test_d33_config_degisiminde_tam_snapshot_istenir() -> None:
    """Cihaz seti degistiginde delta YETMEZ.

    Delta yalnizca "degisen cihazlari" tasir; SILINEN bir cihaz hicbir
    delta'da GORUNMEZ ve backend onu sonsuza kadar eski durumuyla gosterirdi.
    Uzlastirma ancak TAM snapshot ile olur.
    """
    import ast
    import inspect
    import textwrap

    from dnp3_gateway import main as main_mod

    kaynak = textwrap.dedent(inspect.getsource(main_mod._run_config_refresh))
    assert "request_snapshot()" in kaynak, "config degisiminde tam snapshot istenmiyor"

    # KOSULU AST ile dogrula: duz metin penceresi araya giren yorum bloklari
    # yuzunden kirilgan olurdu (ve tam da oyle oldu).
    bulundu = False
    for dugum in ast.walk(ast.parse(kaynak)):
        if not isinstance(dugum, ast.If):
            continue
        if "request_snapshot" not in "\n".join(ast.unparse(n) for n in dugum.body):
            continue
        if "changed" in ast.unparse(dugum.test):
            bulundu = True
            break
    assert bulundu, "snapshot istegi `changed` kosuluna bagli degil — her refresh'te tam snapshot gider"


def test_d34_uyarma_gecikmesi_poll_araligi_mertebesinde() -> None:
    """Durum degisimi ~poll araligi + debounce icinde yayinlanmali.

    Yayincinin 30sn'lik YEDEK uyanmasini beklemek KABUL EDILMEZ: bir cihazin
    kopmasi backend'e yarim dakika gec ulasirsa operator arizayi gec gorur.

    Burada gercek poll dongusu taklit ediliyor: her "cycle" sonrasi
    `mark_dirty` cagriliyor ve gecikme OLCULUYOR.
    """
    durum = {"D1": _saglik(state="online")}
    g = SahteGonderici()
    y = _yayinci(lambda: durum, g, change_debounce_sec=0.1, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1), "ilk snapshot gelmedi"
        onceki = len(g.govdeler)

        poll_araligi = 0.05  # gercek gateway'de 1sn
        durum["D1"] = _saglik(state="lost", connected=False, reachable=False)
        basla = time.monotonic()
        # Poll dongusunun yaptigi TAM olarak bu: her cycle sonrasi uyar.
        for _ in range(5):
            y.mark_dirty()
            time.sleep(poll_araligi)
        assert _bekle(lambda: len(g.govdeler) > onceki, timeout=3.0), "gecis yayinlanmadi"
        gecen = time.monotonic() - basla

        # Yedek uyanma 30sn; poll+debounce mertebesinde olmali.
        assert gecen < 2.0, f"gecis {gecen:.1f}s surdu — yedek uyanmaya dusmus olabilir"
        assert g.govdeler[-1]["devices"][0]["connection_state"] == "lost"
    finally:
        y.stop()


# ==========================================================================
# D35-D37 — STANDART KURULUM YOLU (PR #33 review, madde 2)
# ==========================================================================


def _render_compose(**kw: Any) -> str:
    import sys
    from pathlib import Path as _Path

    kok = _Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(kok / "scripts"))
    import render_compose  # noqa: PLC0415

    varsayilan = {
        "code": "GW-001",
        "token": "x" * 40,
        "name": "Test",
        "backend_url": "https://api.local/api/v1",
        "nats_url": "nats://n:4222",
        "host_port": 8021,
        "install_mode": "local",
    }
    varsayilan.update(kw)
    return render_compose.render_compose(**varsayilan)


def test_d35_standart_render_device_health_degiskenlerini_gecirir() -> None:
    """Sablon bu ayarlari container'a GECIRMEZSE ozellik normal kurulumlarda
    compose ELLE duzenlenmeden ACILAMAZ."""
    import yaml

    cikti = _render_compose()
    env = yaml.safe_load(cikti)["services"]["gateway"]["environment"]
    for anahtar in (
        "DEVICE_HEALTH_PUBLISH_ENABLED",
        "DEVICE_HEALTH_BATCH_MAX",
        "DEVICE_HEALTH_SNAPSHOT_INTERVAL_SEC",
        "DEVICE_HEALTH_CHANGE_DEBOUNCE_SEC",
    ):
        assert anahtar in env, f"render edilmis compose'da YOK: {anahtar}"

    # VARSAYILAN KAPALI kalmali.
    assert env["DEVICE_HEALTH_PUBLISH_ENABLED"] == "${DEVICE_HEALTH_PUBLISH_ENABLED:-false}"


def test_d36_compose_duzenlenmeden_sonradan_acilabilir() -> None:
    """ASIL KABUL OLCUTU.

    Render edilmis dosya `${VAR:-varsayilan}` kullanmali; operator compose'u
    DUZENLEMEDEN, yalnizca yanindaki `.env` ile ozelligi acabilmeli. Elle
    duzenleme bir sonraki render'da SESSIZCE geri alinirdi.
    """
    import yaml

    cikti = _render_compose()
    env = yaml.safe_load(cikti)["services"]["gateway"]["environment"]
    for anahtar in (
        "DEVICE_HEALTH_PUBLISH_ENABLED",
        "DEVICE_HEALTH_BATCH_MAX",
        "DEVICE_HEALTH_SNAPSHOT_INTERVAL_SEC",
        "DEVICE_HEALTH_CHANGE_DEBOUNCE_SEC",
    ):
        deger = env[anahtar]
        assert deger.startswith("${" + anahtar + ":-"), (
            f"{anahtar} compose degisken ikamesi kullanmiyor: {deger!r} — "
            "operator acmak icin compose'u DUZENLEMEK zorunda kalir"
        )

    # Compose'un `.env` ikamesini gercekten uyguladigini dogrula.
    import os
    import re

    def _ikame(metin: str, ortam: dict[str, str]) -> str:
        return re.sub(
            r"\$\{([A-Z0-9_]+):-([^}]*)\}",
            lambda m: ortam.get(m.group(1), m.group(2)),
            metin,
        )

    assert _ikame(env["DEVICE_HEALTH_PUBLISH_ENABLED"], {}) == "false"
    assert _ikame(env["DEVICE_HEALTH_PUBLISH_ENABLED"], {"DEVICE_HEALTH_PUBLISH_ENABLED": "true"}) == "true"
    assert os.sep  # (lint: kullanilmayan import olmasin)


def test_d37_render_bayragi_varsayilani_acik_yapabilir() -> None:
    """Grid renderer'i isterse acik render edebilir; varsayilan YINE kapali."""
    import yaml

    acik = yaml.safe_load(_render_compose(device_health_enabled=True))["services"]["gateway"]["environment"]
    assert acik["DEVICE_HEALTH_PUBLISH_ENABLED"] == "${DEVICE_HEALTH_PUBLISH_ENABLED:-true}"

    kapali = yaml.safe_load(_render_compose())["services"]["gateway"]["environment"]
    assert kapali["DEVICE_HEALTH_PUBLISH_ENABLED"] == "${DEVICE_HEALTH_PUBLISH_ENABLED:-false}"


# ==========================================================================
# D38-D40 — COK PARCALI SNAPSHOT KORELASYONU (PR #33 review, madde 3)
# ==========================================================================


def test_d38_snapshot_partileri_ayni_snapshot_id_paylasir() -> None:
    kaynak = {f"D{i:03d}": _saglik() for i in range(200)}
    g = SahteGonderici()
    y = _yayinci(lambda: kaynak, g, batch_max=50)
    y.start()
    try:
        assert _bekle(lambda: len(g.cihaz_kodlari()) >= 200)
        assert len(g.govdeler) == 4
        kimlikler = {z["snapshot_id"] for z in g.govdeler}
        assert len(kimlikler) == 1, f"ayni snapshot'in partileri farkli kimlik tasiyor: {kimlikler}"
        assert all(z["snapshot_batch_count"] == 4 for z in g.govdeler)
        assert sorted(z["snapshot_batch_index"] for z in g.govdeler) == [0, 1, 2, 3]
    finally:
        y.stop()


def test_d39_delta_partilerinde_snapshot_alanlari_null() -> None:
    durum = {"D1": _saglik(state="online")}
    g = SahteGonderici()
    y = _yayinci(lambda: durum, g, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: len(g.govdeler) >= 1)
        onceki = len(g.govdeler)
        durum["D1"] = _saglik(state="lost", connected=False)
        y.mark_dirty()
        assert _bekle(lambda: len(g.govdeler) > onceki)
        z = g.govdeler[-1]
        assert z["snapshot"] is False
        assert z["snapshot_id"] is None
        assert z["snapshot_batch_index"] is None
        assert z["snapshot_batch_count"] is None
    finally:
        y.stop()


def test_d40_kismi_basarisizlik_sonrasi_yeni_snapshot_id_uretilir() -> None:
    """ASIL SENARYO (review madde 3).

    200 cihaz -> 1. parti BASARILI -> 2. parti BASARISIZ
    -> cihaz seti DEGISIR ama toplam YINE 200
    -> yeniden deneme

    `device_total` TEK BASINA YETMEZ: iki turda da 200'dur. Backend yarim
    kalan ESKI snapshot ile yenisini `snapshot_id` olmadan AYIRT EDEMEZ ve
    ikisini birlestirip TUTARSIZ bir tablo kurar — "eksik kalanlari sil"
    mantigi varsa var olan cihazlari SILER.
    """
    ilk_set = {f"ESKI-{i:03d}": _saglik(state="online") for i in range(200)}
    durum = dict(ilk_set)

    g = SahteGonderici()
    orijinal = g.__call__
    sayac = {"n": 0}

    def _iki_parti_sonra_patla(payload: dict[str, Any]) -> None:
        sayac["n"] += 1
        if sayac["n"] == 2:  # 2. parti DUSER
            raise RuntimeError("backend gecici hata")
        orijinal(payload)

    y = _yayinci(lambda: durum, _iki_parti_sonra_patla, batch_max=50, snapshot_interval_sec=3600.0)
    y.start()
    try:
        assert _bekle(lambda: sayac["n"] >= 2, timeout=6.0), "ilk tur baslamadi"
        assert len(g.govdeler) == 1, "yalnizca 1. parti gecmeliydi"
        yarim = g.govdeler[0]
        assert yarim["snapshot"] is True
        yarim_id = yarim["snapshot_id"]
        assert yarim["snapshot_batch_count"] == 4

        # CIHAZ SETI DEGISIR — toplam YINE 200.
        durum.clear()
        durum.update({f"YENI-{i:03d}": _saglik(state="smart_idle", connected=False) for i in range(200)})
        y.request_snapshot()

        assert _bekle(lambda: len(g.govdeler) >= 5, timeout=10.0), "yeniden deneme tamamlanmadi"
        yeni = [z for z in g.govdeler if z["snapshot_id"] != yarim_id]
        assert yeni, "yeniden deneme AYNI snapshot_id ile geldi — yarim snapshot'la karisir"

        yeni_id = {z["snapshot_id"] for z in yeni}
        assert len(yeni_id) == 1, f"yeni snapshot birden fazla kimlik tasiyor: {yeni_id}"
        assert yeni_id != {yarim_id}

        # Backend SOZLESMESI: iki snapshot kesin ayrilabilir.
        assert yarim["device_total"] == 200 and yeni[0]["device_total"] == 200, (
            "senaryo kurulumu: toplam iki turda da 200 olmali"
        )
        assert all(z["snapshot_batch_count"] == 4 for z in yeni)
        assert sorted(z["snapshot_batch_index"] for z in yeni) == [0, 1, 2, 3], (
            "yeni snapshot TAM gelmedi; backend uzlastirmayi baslatamaz"
        )
        # Yeni snapshot ESKI cihazlari HIC icermez -> backend silinenleri
        # ancak TAM snapshot geldikten sonra uzlastirir.
        yeni_kodlar = {d["device_code"] for z in yeni for d in z["devices"]}
        assert len(yeni_kodlar) == 200
        assert not any(k.startswith("ESKI-") for k in yeni_kodlar)
    finally:
        y.stop()
