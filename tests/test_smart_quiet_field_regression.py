"""G-SMART-QUIET-FIELD-01 — Smart oturumu UYGULAMA KATMANINDA susmali.

SAHA OLAYI (2026-08-20 ~15:10)
------------------------------
Horstmann Smart Navigator 2.0, `listening` uc, cihaz panelinde Operation
Mode = SMART, Horstmann oturum hareketsizlik zaman asimi 600 saniye.
tcpdump gateway -> cihaz yonunde HER ~5.73 SANIYEDE bir `Flags [P.]`,
`length 24` uygulama yuku gosterdi. Bu trafik 600 saniyelik sayacin
DOLMASINI ENGELLER; modem hicbir zaman uyuyamaz.

CERCEVE COZUMLEMESI (yadnp3 3.2.1.1 ile OLCULDU, tahmin DEGIL)
--------------------------------------------------------------
    3 sinif obje basligi (60/2, 60/3, 60/4 = Class 1,2,3)  -> 24 BAYT
    4 sinif obje basligi (+60/1 = Class 0 integrity)       -> 27 BAYT
    bos (NULL) yanit, veri yok                             -> 17 BAYT

Sahada gozlenen 24/17 ikilisi TAM OLARAK "Class 1/2/3 event scan +
olay yok yaniti" imzasidir. 27 baytlik integrity DEGILDIR.

Class 1/2/3 event scan'i kuran TEK yer `_periyodik_scan_ekle()`; o da
YALNIZCA `session_policy != "smart"` iken cagrilir. Yani saha cihazi
`smart` DEGIL `continuous` etkin politikayla kosuyordu.

BU DOSYA NEYI PINLER
--------------------
Politikanin `DeviceConfig`ten `_ManagedMaster`a DOGRU tasindigini ve
`smart` etkinken periyodik taramalarin HIC kurulmadigini — MASTER'I ELLE
`session_policy="smart"` ile kurarak DEGIL, gercek `read_device()` ->
`_ensure_master()` yolundan gecerek. Saha hatasi tam da o entegrasyon
katmanindaydi; elle kurulan bir master onu KACIRIRDI.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend import DeviceConfig

from .conftest import make_device, make_signal

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

AKIM_IDX = 2
MOD_IDX = 15

SINYALLER = [
    make_signal("master.actual_current", data_type="analog", object_group=30, index=AKIM_IDX),
]
MOD_SINYALI = make_signal(
    "master.operation_mode", source="master", data_type="binary", object_group=1, index=MOD_IDX
)


def _bos_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class BulusmaProxy:
    """IKI TCP client'i birlestiren proxy; gateway -> cihaz baytlarini SAYAR.

    NEDEN BULUSMA: yadnp3 `AddOutstationTCPServer` SUNMUYOR (yalnizca
    `AddOutstationTCPClient`). `listening` senaryosunda gateway de TCP
    CLIENT'tir. Iki client'i baglamanin tek yolu ortada iki port acip
    baglantilari BIRLESTIRMEKTIR.

    `gateway_to_device` gateway'in URETTIGI uygulama trafigidir — Smart
    sessizliginin TEK objektif olcusu budur. Saf TCP ACK'leri yuk tasimaz
    ve zaten burada gorunmez (recv yalnizca veri dondurur).
    """

    def __init__(self) -> None:
        self.gw_port = _bos_port()
        self.os_port = _bos_port()
        self.gateway_to_device = 0
        self.device_to_gateway = 0
        self._lock = threading.Lock()
        self._dur = threading.Event()
        self._soketler: list[socket.socket] = []
        self._gw_srv = self._dinle(self.gw_port)
        self._os_srv = self._dinle(self.os_port)
        self._t = threading.Thread(target=self._calis, daemon=True)
        self._t.start()

    def _dinle(self, port: int) -> socket.socket:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        s.settimeout(0.3)
        self._soketler.append(s)
        return s

    def _kabul(self, srv: socket.socket) -> socket.socket | None:
        while not self._dur.is_set():
            try:
                c, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return None
            c.settimeout(0.2)
            self._soketler.append(c)
            return c
        return None

    def _calis(self) -> None:
        gw = self._kabul(self._gw_srv)
        os_ = self._kabul(self._os_srv)
        if gw is None or os_ is None:
            return
        while not self._dur.is_set():
            for kaynak, hedef, gateway_yonu in ((gw, os_, True), (os_, gw, False)):
                try:
                    d = kaynak.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not d:
                    return
                with self._lock:
                    if gateway_yonu:
                        self.gateway_to_device += len(d)
                    else:
                        self.device_to_gateway += len(d)
                try:
                    hedef.sendall(d)
                except OSError:
                    return

    def sifirla(self) -> None:
        with self._lock:
            self.gateway_to_device = 0
            self.device_to_gateway = 0

    def gw_bayt(self) -> int:
        with self._lock:
            return self.gateway_to_device

    def kapat(self) -> None:
        self._dur.set()
        for s in self._soketler:
            try:
                s.close()
            except OSError:
                pass


class _OutstationApp(opendnp3.IOutstationApplication):
    def __init__(self) -> None:
        super().__init__()


class _KomutIsleyici(opendnp3.ICommandHandler):
    def __init__(self) -> None:
        super().__init__()

    def Begin(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass

    def End(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass


class SahteHorstmann:
    """Gercek opendnp3 outstation; proxy'nin outstation portuna baglanir."""

    def __init__(self, proxy_port: int, *, dnp3_adresi: int = 10, master_adresi: int = 1) -> None:
        self._manager = opendnp3.DNP3Manager(2)
        self._channel = self._manager.AddOutstationTCPClient(
            "os_ch",
            opendnp3.levels.NORMAL,
            opendnp3.ChannelRetry.Default(),
            [opendnp3.IPEndpoint("127.0.0.1", proxy_port)],
            "0.0.0.0",
            None,
        )
        cfg = opendnp3.OutstationStackConfig(opendnp3.DatabaseConfig(40))
        cfg.link.LocalAddr = dnp3_adresi
        cfg.link.RemoteAddr = master_adresi
        self._outstation = self._channel.AddOutstation("os", _KomutIsleyici(), _OutstationApp(), cfg)
        self._outstation.Enable()

    def analog_yaz(self, index: int, deger: float, bayraklar: int = 0x01) -> None:
        b = opendnp3.UpdateBuilder()
        b.Update(opendnp3.Analog(float(deger), opendnp3.Flags(bayraklar)), index)
        self._outstation.Apply(b.Build())

    def binary_yaz(self, index: int, deger: bool, bayraklar: int = 0x01) -> None:
        b = opendnp3.UpdateBuilder()
        b.Update(opendnp3.Binary(bool(deger), opendnp3.Flags(bayraklar)), index)
        self._outstation.Apply(b.Build())

    def kapat(self) -> None:
        for fn in (self._outstation.Disable, self._manager.Shutdown):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def _saha(tmp_path, *, session_policy: str, mod_degeri: bool | None = None):
    """GERCEK entegrasyon yolu: DeviceConfig -> read_device -> _ensure_master.

    Master ELLE kurulmaz. Saha hatasi tam da bu katmandaydi.
    """
    proxy = BulusmaProxy()
    reader = mod.Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=proxy.gw_port,
        # SAHADAKI AGRESIF TEMPO: continuous'ta 1sn event + 2sn baseline.
        # smart'ta bu taramalarin HIC kurulmadigi bu tempoda kanitlanir.
        scan_interval_sec=1,
        baseline_interval_sec=2,
        session_store_path=str(tmp_path / "session_state.json"),
    )
    device = replace(
        make_device("SN2-1"),
        ip_address="127.0.0.1",
        dnp3_address=10,
        dnp3_tcp_port=proxy.gw_port,
        ip_endpoint_type="listening",
        session_policy=session_policy,
        smart_max_silence_sec=86400,
    )
    os_ = SahteHorstmann(proxy.os_port)
    os_.analog_yaz(AKIM_IDX, 42.0)
    if mod_degeri is not None:
        os_.binary_yaz(MOD_IDX, mod_degeri)
    return proxy, reader, device, os_


def _oku_kadar(reader, device, signals, *, saniye: float, aralik: float = 0.2) -> list:
    son, sonuc = time.monotonic() + saniye, []
    while time.monotonic() < son:
        try:
            sonuc = reader.read_device(device=device, signals=signals)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(aralik)
    return sonuc


def _iyi_okuma_bekle(reader, device, signals, *, timeout: float = 25.0) -> bool:
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        try:
            r = reader.read_device(device=device, signals=signals)
        except Exception:  # noqa: BLE001
            r = []
        if any(x.quality == "good" for x in r):
            return True
        time.sleep(0.2)
    return False


def _kapat(proxy, reader, os_) -> None:
    for fn in (os_.kapat, reader.close, proxy.kapat):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# A + H — LISTENING + ACIK SMART: uygulama katmani SUSAR
# ==========================================================================


def test_a_listening_smart_uygulama_trafigi_uretmez(tmp_path) -> None:
    """EN KRITIK SAHA REGRESYONU.

    Global tarama ayarlari AGRESIF (1sn event, 2sn baseline) oldugu halde
    `smart` etkin bir cihazda ilk aktarimdan sonra gateway -> cihaz yonunde
    UYGULAMA YUKU olmamali. Sahada tam burada ~5.73sn'de bir 24 baytlik
    Class 1/2/3 istegi gorulmustu.

    Olculen sey iddia degil GERCEK TCP baytidir.
    """
    proxy, reader, device, os_ = _saha(tmp_path, session_policy="smart")
    try:
        assert _iyi_okuma_bekle(reader, device, SINYALLER), "ilk gecerli okuma gelmedi"

        # Aktarim bitti; simdi SESSIZLIK penceresi. 1sn'lik event scan
        # kuruluysa bu pencerede >=5 istek gorulur.
        time.sleep(1.0)
        proxy.sifirla()
        olcum = 6.0
        _oku_kadar(reader, device, SINYALLER, saniye=olcum)
        gonderilen = proxy.gw_bayt()

        assert gonderilen == 0, (
            f"{olcum:.0f} saniyede gateway -> cihaz yonunde {gonderilen} bayt UYGULAMA yuku "
            "gonderildi; Horstmann'in hareketsizlik sayaci ASLA dolamaz (saha hatasi geri geldi)"
        )
    finally:
        _kapat(proxy, reader, os_)


def test_a2_master_periyodik_tarama_kurmaz(tmp_path) -> None:
    """Trafik olcumunun yaninda YAPISAL kanit: scan tutamaklari None."""
    proxy, reader, device, os_ = _saha(tmp_path, session_policy="smart")
    try:
        assert _iyi_okuma_bekle(reader, device, SINYALLER)
        mm = reader._masters["SN2-1"]
        assert mm.configured_session_policy == "smart"
        assert mm.session_policy == "smart"
        assert mm._scan_event is None, "smart cihazda event scan KURULMUS"
        assert mm._scan_class0 is None, "smart cihazda baseline scan KURULMUS"
    finally:
        _kapat(proxy, reader, os_)


# ==========================================================================
# B — LISTENING + CONTINUOUS: davranis DEGISMEZ (regresyon guvenligi)
# ==========================================================================


def test_b_listening_continuous_trafik_devam_eder(tmp_path) -> None:
    """`continuous` icin periyodik tarama BEKLENEN davranistir.

    Smart bozuk diye `DNP3_EVENT_SCAN_INTERVAL_SEC` global olarak
    kapatilmadi; duzeltme CIHAZ BAZINDADIR. Bu test bunu pinler.
    """
    proxy, reader, device, os_ = _saha(tmp_path, session_policy="continuous")
    try:
        assert _iyi_okuma_bekle(reader, device, SINYALLER), "ilk gecerli okuma gelmedi"
        time.sleep(1.0)
        proxy.sifirla()
        _oku_kadar(reader, device, SINYALLER, saniye=5.0)
        assert proxy.gw_bayt() > 0, "continuous cihazda periyodik tarama DURMUS — bu bir regresyon"

        mm = reader._masters["SN2-1"]
        assert mm.session_policy == "continuous"
        assert mm._scan_event is not None, "continuous cihazda event scan kurulmamis"
        assert mm._scan_class0 is not None, "continuous cihazda baseline scan kurulmamis"
    finally:
        _kapat(proxy, reader, os_)


# ==========================================================================
# G — ENTEGRASYON: politika DeviceConfig'ten _ManagedMaster'a DOGRU tasinir
# ==========================================================================


@pytest.mark.parametrize(
    ("politika", "beklenen_etkin", "beklenen_periyodik"),
    [
        ("continuous", "continuous", True),
        ("smart", "smart", False),
        # `auto` + mod HENUZ bilinmiyor -> SESSIZ basla (tarama yok).
        ("auto", "smart", False),
    ],
)
def test_g_politika_devicecfg_den_mastera_tasinir(
    tmp_path, monkeypatch, politika, beklenen_etkin, beklenen_periyodik
) -> None:
    """Master ELLE kurulmaz; `_ensure_master` gercek yolundan gecilir.

    Saha hatasinin sinifi tam olarak buydu: "backend/politika ne soyledi"
    ile "master ne kuruldu" arasindaki kopukluk. `_ManagedMaster`i elle
    `session_policy="smart"` ile kuran bir test bunu KACIRIR.
    """
    kurulan: dict[str, Any] = {}
    gercek_init = mod._ManagedMaster.__init__

    def _izle(self, manager, **kw):
        kurulan.update(kw)
        gercek_init(self, manager, **kw)

    monkeypatch.setattr(mod._ManagedMaster, "__init__", _izle)

    proxy = BulusmaProxy()
    reader = mod.Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=proxy.gw_port,
        scan_interval_sec=1,
        baseline_interval_sec=2,
        session_store_path=str(tmp_path / "s.json"),
    )
    device = replace(
        make_device("SN2-G"),
        ip_address="127.0.0.1",
        dnp3_address=10,
        dnp3_tcp_port=proxy.gw_port,
        ip_endpoint_type="listening",
        session_policy=politika,
    )
    try:
        reader.read_device(device=device, signals=SINYALLER)
        # 1) DeviceConfig'teki politika _ManagedMaster'a AYNEN gecti mi?
        assert kurulan.get("session_policy") == politika, (
            f"politika master'a yanlis gecti: {kurulan.get('session_policy')!r} != {politika!r}"
        )
        mm = reader._masters["SN2-G"]
        # 2) Yapilandirilan KORUNDU, etkin DOGRU cozuldu.
        assert mm.configured_session_policy == politika
        assert mm.session_policy == beklenen_etkin
        # 3) Periyodik tarama tutamaklari politikaya UYGUN.
        assert (mm._scan_event is not None) is beklenen_periyodik
        assert (mm._scan_class0 is not None) is beklenen_periyodik
    finally:
        for fn in (reader.close, proxy.kapat):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def test_g2_session_policy_disk_onbelleginden_kaybolmaz(tmp_path) -> None:
    """Propagasyonun sessiz kirilma noktasi: disk onbellegi backend parser'ini
    ATLAR. `session_policy` orada dusseydi restart sonrasi cihaz sessizce
    `continuous`a doner ve saha hatasi AYNEN tekrarlanirdi.
    """
    from dataclasses import asdict

    from dnp3_gateway.backend import GatewayConfig
    from dnp3_gateway.state import GatewayState

    device = replace(
        make_device("SN2-C"),
        ip_endpoint_type="listening",
        session_policy="smart",
        smart_max_silence_sec=86400,
        dial_in_interval_min=720,
    )
    # Onbellege giden temsil TUM alanlari tasimali.
    ham = asdict(device)
    for alan in ("session_policy", "ip_endpoint_type", "smart_max_silence_sec", "dial_in_interval_min"):
        assert alan in ham, f"asdict cikitisinda YOK: {alan}"
    assert ham["session_policy"] == "smart"

    # Ve onbellekten geri yuklenen DeviceConfig politikayi KORUMALI.
    geri = DeviceConfig(**{k: v for k, v in ham.items() if k in DeviceConfig.__dataclass_fields__})
    assert geri.session_policy == "smart"
    assert mod.Yadnp3TelemetryReader._session_policy(geri) == "smart"

    # Gercek state yolundan da gecir.
    yol = tmp_path / "config-cache.json"
    st = GatewayState(cache_path=yol)
    st.update(
        GatewayConfig(
            gateway_code="GW-001",
            gateway_name="gw",
            batch_interval_sec=5,
            max_devices=200,
            is_active=True,
            config_version="v1",
            devices=[device],
            signals=list(SINYALLER),
        )
    )
    st2 = GatewayState(cache_path=yol)
    assert st2.load_from_cache(), "onbellek yuklenemedi"
    yuklenen = next(d for d in st2.devices() if d.code == "SN2-C")
    assert yuklenen.session_policy == "smart", "session_policy disk onbelleginde KAYBOLDU"


# ==========================================================================
# TESHIS LOGU — tcpdump ACMADAN gorulebilmeli
# ==========================================================================


def test_teshis_logu_politikayi_ve_taramalari_acikca_basar(tmp_path, caplog) -> None:
    """SAHA DERSI: 2026-08-20'de "bu cihaz kazara continuous mu kosuyor"
    sorusunu cevaplamak icin PAKET YAKALAMAK gerekti.

    `yadnp3_master_enabled` satiri artik `periodic_scans` bayragini ve
    configured/effective ayrimini ACIKCA basar.
    """
    proxy = BulusmaProxy()
    reader = mod.Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=proxy.gw_port,
        scan_interval_sec=5,
        baseline_interval_sec=30,
        session_store_path=str(tmp_path / "s.json"),
    )
    device = replace(
        make_device("SN2-L"),
        ip_address="127.0.0.1",
        dnp3_address=10,
        dnp3_tcp_port=proxy.gw_port,
        ip_endpoint_type="listening",
        session_policy="continuous",
    )
    try:
        with caplog.at_level("INFO"):
            reader.read_device(device=device, signals=SINYALLER)
        satirlar = [r.getMessage() for r in caplog.records if "yadnp3_master_enabled" in r.getMessage()]
        assert satirlar, "master kurulum logu basilmadi"
        s = satirlar[0]
        # Saha teshisi icin GEREKLI alanlar.
        assert "periodic_scans=true" in s, "periyodik tarama bayragi loglanmiyor"
        assert "configured_policy=continuous" in s
        assert "effective_policy=continuous" in s
        assert "ip_endpoint_type=listening" in s
        assert "event_scan=5s" in s
        assert "baseline_scan=30s" in s
        # Kimlik bilgisi SIZMAMALI.
        assert "token" not in s.lower()
    finally:
        for fn in (reader.close, proxy.kapat):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def test_teshis_logu_smart_cihazda_taramalari_kapali_gosterir(tmp_path, caplog) -> None:
    proxy = BulusmaProxy()
    reader = mod.Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=proxy.gw_port,
        scan_interval_sec=5,
        baseline_interval_sec=30,
        session_store_path=str(tmp_path / "s.json"),
    )
    device = replace(
        make_device("SN2-S"),
        ip_address="127.0.0.1",
        dnp3_address=10,
        dnp3_tcp_port=proxy.gw_port,
        ip_endpoint_type="listening",
        session_policy="smart",
    )
    try:
        with caplog.at_level("INFO"):
            reader.read_device(device=device, signals=SINYALLER)
        s = next(r.getMessage() for r in caplog.records if "yadnp3_master_enabled" in r.getMessage())
        assert "periodic_scans=false" in s, "smart cihazda periyodik tarama ACIK gorunuyor"
        assert "effective_policy=smart" in s
        assert "event_scan=-" in s
        assert "baseline_scan=-" in s
    finally:
        for fn in (reader.close, proxy.kapat):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


# ==========================================================================
# C-F — AUTO GECISLERI (§9)
#
# `auto` + Operation Mode gozlemi ETKIN politikayi belirler. Yapilandirilan
# politika `auto` KALIR ve ASLA yeniden yazilmaz — Grid'in gonderdigi niyet
# gateway tarafindan degistirilemez.
# ==========================================================================


def _auto_saha(tmp_path, *, smart_mi: bool):
    """`auto` cihaz + Operation Mode noktasi RAPORLANIYOR.

    smart_mi=True  -> ham 1 -> SMART   (0x81'in STATE biti)
    smart_mi=False -> ham 0 -> BOOST   (0x01)
    """
    proxy = BulusmaProxy()
    reader = mod.Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=proxy.gw_port,
        scan_interval_sec=1,
        baseline_interval_sec=2,
        session_store_path=str(tmp_path / "session_state.json"),
    )
    device = replace(
        make_device("SN2-A"),
        ip_address="127.0.0.1",
        dnp3_address=10,
        dnp3_tcp_port=proxy.gw_port,
        ip_endpoint_type="listening",
        session_policy="auto",
        smart_max_silence_sec=86400,
    )
    os_ = SahteHorstmann(proxy.os_port)
    os_.analog_yaz(AKIM_IDX, 42.0)
    os_.binary_yaz(MOD_IDX, smart_mi)
    return proxy, reader, device, os_


def _mod_bekle(reader, device, signals, beklenen, *, timeout: float = 30.0) -> bool:
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        try:
            reader.read_device(device=device, signals=signals)
        except Exception:  # noqa: BLE001
            pass
        mm = reader._masters.get(device.code)
        if mm is not None and mm.operation_mode == beklenen:
            return True
        time.sleep(0.2)
    return False


def test_c_auto_operation_mode_smart_taramalari_kapali_tutar(tmp_path) -> None:
    """AUTO + Operation Mode SMART -> etkin smart -> periyodik tarama YOK."""
    proxy, reader, device, os_ = _auto_saha(tmp_path, smart_mi=True)
    sinyaller = [*SINYALLER, MOD_SINYALI]
    try:
        assert _mod_bekle(reader, device, sinyaller, "smart"), "Operation Mode SMART gozlenemedi"
        mm = reader._masters["SN2-A"]
        # YAPILANDIRILAN politika DEGISMEZ.
        assert mm.configured_session_policy == "auto"
        assert mm.session_policy == "smart"
        assert mm._scan_event is None, "smart cozulmus auto cihazda event scan KURULMUS"
        assert mm._scan_class0 is None

        # Ve GERCEK trafik: sessiz.
        time.sleep(1.0)
        proxy.sifirla()
        _oku_kadar(reader, device, sinyaller, saniye=5.0)
        assert proxy.gw_bayt() == 0, f"auto->smart cihazda {proxy.gw_bayt()} bayt uygulama yuku gonderildi"
    finally:
        _kapat(proxy, reader, os_)


def test_d_auto_operation_mode_boost_taramalari_acar(tmp_path) -> None:
    """AUTO + Operation Mode BOOST -> etkin continuous -> periyodik tarama VAR."""
    proxy, reader, device, os_ = _auto_saha(tmp_path, smart_mi=False)
    sinyaller = [*SINYALLER, MOD_SINYALI]
    try:
        assert _mod_bekle(reader, device, sinyaller, "boost"), "Operation Mode BOOST gozlenemedi"
        mm = reader._masters["SN2-A"]
        assert mm.configured_session_policy == "auto", "yapilandirilan politika YENIDEN YAZILDI"
        assert mm.session_policy == "continuous"
        assert mm._scan_event is not None, "boost cozulmus auto cihazda tarama kurulmamis"

        time.sleep(1.0)
        proxy.sifirla()
        _oku_kadar(reader, device, sinyaller, saniye=4.0)
        assert proxy.gw_bayt() > 0, "boost cihazda periyodik tarama trafigi YOK"
    finally:
        _kapat(proxy, reader, os_)


def test_ef_auto_boost_smart_gecisleri_taramayi_durdurup_baslatir(tmp_path) -> None:
    """E: Boost -> Smart  => taramalar DURUR (bayat gorev KALMAZ).
    F: Smart -> Boost  => taramalar YENIDEN BASLAR.

    Yapilandirilan politika her iki gecistede `auto` KALIR.

    TAKLIT SINIRI (bilincli): Smart'a gecen bir gateway hicbir sey SORMAZ,
    dolayisiyla mod noktasinin YENI degerini ancak cihaz KENDILIGINDEN
    (unsolicited) bildirirse gorebilir. Sahada Horstmann Boost'a gecince
    zaten surekli bagli kalir ve olaylarini gonderir. Buradaki sahte
    outstation olay sinifi atayamadigi icin (yadnp3 wheel `AnalogConfig`
    sunmuyor) unsolicited URETEMEZ; F yarisinda o teslim, degeri dogrudan
    cache'e yazarak taklit edilir — `_auto_politikayi_coz` zaten TAM OLARAK
    oradan okur, yani olculen kod yolu AYNIDIR.
    """
    proxy, reader, device, os_ = _auto_saha(tmp_path, smart_mi=False)  # BOOST ile basla
    sinyaller = [*SINYALLER, MOD_SINYALI]
    try:
        assert _mod_bekle(reader, device, sinyaller, "boost"), "baslangic BOOST gozlenemedi"
        assert reader._masters["SN2-A"].session_policy == "continuous"

        # --- E: BOOST -> SMART (GERCEK protokol yolu) --------------------
        os_.binary_yaz(MOD_IDX, True)
        assert _mod_bekle(reader, device, sinyaller, "smart"), "SMART gecisi gozlenemedi"
        mm = reader._masters["SN2-A"]
        assert mm.configured_session_policy == "auto"
        assert mm.session_policy == "smart"
        assert mm._scan_event is None, "SMART'a gecen cihazda tarama tutamagi KALMIS"

        # BAYAT GOREV KALMAMALI: GERCEK trafikle dogrula.
        time.sleep(1.0)
        proxy.sifirla()
        _oku_kadar(reader, device, sinyaller, saniye=5.0)
        assert proxy.gw_bayt() == 0, (
            f"Boost->Smart sonrasi {proxy.gw_bayt()} bayt trafik SURUYOR (bayat tarama gorevi)"
        )

        # --- F: SMART -> BOOST (unsolicited teslim taklidi) --------------
        mm = reader._masters["SN2-A"]
        mm.cache.set(1, MOD_IDX, 0.0)  # cihaz mod noktasini BOOST olarak bildirdi
        son = time.monotonic() + 15.0
        while time.monotonic() < son:
            try:
                reader.read_device(device=device, signals=sinyaller)
            except Exception:  # noqa: BLE001
                pass
            mm = reader._masters.get("SN2-A")
            if mm is not None and mm.session_policy == "continuous":
                break
            time.sleep(0.2)

        mm = reader._masters["SN2-A"]
        assert mm.operation_mode == "boost", "BOOST gozlemi islenmedi"
        assert mm.configured_session_policy == "auto", "yapilandirilan politika YENIDEN YAZILDI"
        assert mm.session_policy == "continuous", "Smart->Boost sonrasi etkin politika donmedi"
        assert mm._scan_event is not None, "Smart->Boost sonrasi taramalar YENIDEN KURULMADI"
    finally:
        _kapat(proxy, reader, os_)


# ==========================================================================
# SMART SESSIZLIK SIZINTILARI (§6)
#
# Bunlar saha bulgusunun KOK NEDENI DEGILDIR (o bir yapilandirma
# uyusmazligiydi) ama config duzeltilip politika `smart`a cevrildikten SONRA
# isirirlardi: modem YINE uyuyamazdi. Ayri ayri kanitlanip kapatildilar.
# ==========================================================================


def test_s2_smart_cihazda_link_keepalive_kapali(tmp_path) -> None:
    """EN CIDDI SIZINTI: opendnp3 varsayilani 60 SANIYELIK link keepalive.

    OLCULDU (yadnp3 3.2.1.1): `cfg.link.KeepAliveTimeout` varsayilani
    60000 ms ve gateway onu HIC ayarlamiyordu. Tamamen sessiz bir master
    bile 60. saniyede 10 baytlik LINK_STATUS gonderiyor.

    Horstmann dokumantasyonu hareketsizlik sayacinin "HER TCP/DNP
    trafigiyle" sifirlandigini soyler -> 60sn'de bir keepalive, 600sn'lik
    oturum sayacini SONSUZA KADAR sifirlar. Taramalar kapatilsa BILE modem
    uyuyamaz.

    Saha yakalamasi 17 saniyelikti ve bu cerceveyi GORMEDI.
    """
    yakalanan: dict[str, Any] = {}
    gercek = mod._ManagedMaster.__init__

    def _izle(self, manager, **kw):
        gercek(self, manager, **kw)
        yakalanan[kw["device"].code] = self._master  # noqa: SLF001

    proxy = BulusmaProxy()
    reader = mod.Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=proxy.gw_port,
        scan_interval_sec=1,
        baseline_interval_sec=2,
        session_store_path=str(tmp_path / "s.json"),
    )
    try:
        # Kaynak duzeyinde pinle: keepalive SMART'ta kapatiliyor, CONTINUOUS'ta
        # DOKUNULMUYOR (cihaz bazli duzeltme — bkz. review §11).
        import ast
        import inspect
        import textwrap

        kaynak = textwrap.dedent(inspect.getsource(mod._ManagedMaster.__init__))
        agac = ast.parse(kaynak)
        bulundu = False
        for dugum in ast.walk(agac):
            if not isinstance(dugum, ast.If):
                continue
            kosul = ast.unparse(dugum.test)
            govde = "\n".join(ast.unparse(n) for n in dugum.body)
            if "KeepAliveTimeout" in govde and "session_policy == " in kosul and "smart" in kosul:
                bulundu = True
                assert "TimeDuration.Max()" in govde, "keepalive devre disi birakilmiyor"
                break
        assert bulundu, (
            "link keepalive SMART'ta kapatilmiyor — 60sn'de bir LINK_STATUS "
            "Horstmann'in 600sn sayacini sifirlar"
        )
    finally:
        for fn in (reader.close, proxy.kapat):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def test_s2b_keepalive_kapatmasi_gercekten_susturuyor() -> None:
    """Kutuphane davranisi VARSAYILMAZ, OLCULUR.

    Varsayilanla 60. saniyede LINK_STATUS gelir; `TimeDuration.Max()` ile
    gelmez. Bu test kutuphane surumu degisirse haber verir.
    """
    import socket as _socket
    import threading as _threading

    def _olc(keepalive_kapali: bool) -> list[int]:
        srv = _socket.socket()
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        kayit: list[int] = []

        def _os() -> None:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.settimeout(0.4)
            son = time.monotonic() + 68
            while time.monotonic() < son:
                try:
                    d = c.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not d:
                    break
                kayit.append(len(d))
            c.close()

        _threading.Thread(target=_os, daemon=True).start()

        class _S(opendnp3.ISOEHandler):
            def __init__(self) -> None:
                super().__init__()

        class _A(opendnp3.IMasterApplication):
            def __init__(self) -> None:
                super().__init__()

            def AssignClassDuringStartup(self):  # noqa: N802 — kutuphane imzasi
                return False

        mgr = opendnp3.DNP3Manager(2)
        ch = mgr.AddTCPClient(
            "t",
            opendnp3.levels.NORMAL,
            opendnp3.ChannelRetry.Default(),
            [opendnp3.IPEndpoint("127.0.0.1", port)],
            "0.0.0.0",
            None,
        )
        cfg = opendnp3.MasterStackConfig()
        cfg.link.LocalAddr = 1
        cfg.link.RemoteAddr = 10
        cfg.master.disableUnsolOnStartup = False
        if keepalive_kapali:
            cfg.link.KeepAliveTimeout = opendnp3.TimeDuration.Max()
        ch.AddMaster("m", _S(), _A(), cfg).Enable()
        time.sleep(66)
        mgr.Shutdown()
        srv.close()
        return kayit

    # Varsayilan 60 saniye olmali — degistiyse bu testin gerekcesi degisir.
    varsayilan = int(str(opendnp3.MasterStackConfig().link.KeepAliveTimeout))
    assert varsayilan == 60_000, f"kutuphane keepalive varsayilani degismis: {varsayilan}"

    kapali = _olc(True)
    assert 10 not in kapali, f"keepalive kapatildigi halde LINK_STATUS gonderildi: {kapali}"


def test_s1_smart_cihazda_g110_oturum_basina_tek_denenir() -> None:
    """G110 string okumasi `smart` kapisiyla KORUNMUYORDU.

    Ayni fonksiyondaki diger TUM yoklamalar `sessiz_yoklama_serbest = not
    smart` ile korunuyordu; G110 yolu korumasizdi. Cihaz G110 dondurmezse
    15/30/60/120/240 sn backoff ile 6 ScanRange gider — tam da sessizlik
    penceresinin ortasina dusen TEKRARLAYAN uygulama istekleri.

    TEK deneme KORUNUR: seri no/IMEI/firmware operator icin degerlidir ve
    tek atislik acilis isi sozlesmece serbesttir.
    """
    cache = mod._DeviceCache()
    cache.g110_iste()

    # SMART: yalnizca BIR deneme.
    assert cache.g110_gerekli(max_deneme=1) is True
    assert cache.g110_gerekli(max_deneme=1) is False, "smart cihazda ikinci G110 denemesi yapildi"

    # CONTINUOUS: backoff yeniden denemeleri AYNEN korunur (regresyon guvenligi).
    cache2 = mod._DeviceCache()
    cache2.g110_iste()
    assert cache2.g110_gerekli() is True
    with cache2._lock:  # noqa: SLF001 — backoff penceresini ileri sar
        cache2._g110_sonraki_at = 0.0
    assert cache2.g110_gerekli() is True, "continuous cihazda G110 yeniden denemesi KAYBOLDU"


def test_s1b_okuma_yolu_smart_cihazda_tek_deneme_gecirir() -> None:
    """Cagiran taraf `max_deneme=1`i GERCEKTEN gecirmeli (statik kontrol)."""
    import ast
    import inspect
    import pathlib

    # Cagri `_oku` yardimcisindadir; modul genelinde arayip HER cagriyi
    # kontrol ediyoruz ki ileride baska bir yere tasinirsa da yakalansin.
    kaynak = pathlib.Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
    cagrilar = [
        ast.unparse(n)
        for n in ast.walk(ast.parse(kaynak))
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "g110_gerekli"
    ]
    assert cagrilar, "g110_gerekli cagrisi bulunamadi"
    for metin in cagrilar:
        assert "max_deneme" in metin, f"g110_gerekli smart kapisi olmadan cagriliyor: {metin}"
        assert "smart" in metin


# ==========================================================================
# GOZLEM / EYLEM AYRIMI — uyusmazlik GORUNUR, politika EZILMEZ
# ==========================================================================


def test_uyusmazlik_gorunur_ama_politika_ezilmez(tmp_path, caplog) -> None:
    """SAHA DERSININ OZU.

    `continuous` yapilandirilmis bir cihaz Operation Mode = SMART bildirirse:
      * mod OKUNUR ve raporlanir (once "unknown" kaliyordu),
      * KENAR-TETIKLI bir WARNING basilir,
      * ETKIN POLITIKA **DEGISMEZ** — Grid'in yapilandirdigi otoriterdir.

    Ikincisi ZORUNLU: gateway'in "Operation Mode Smart gorunce politikayi
    ezmesi" mimariyi bozardi (bkz. review §4).
    """
    proxy, reader, device, os_ = _saha(tmp_path, session_policy="continuous")
    sinyaller = [*SINYALLER, MOD_SINYALI]
    try:
        os_.binary_yaz(MOD_IDX, True)  # cihaz SMART bildiriyor
        son = time.monotonic() + 30
        with caplog.at_level("WARNING"):
            while time.monotonic() < son:
                try:
                    reader.read_device(device=device, signals=sinyaller)
                except Exception:  # noqa: BLE001
                    pass
                mm = reader._masters.get("SN2-1")
                if mm is not None and mm.operation_mode == "smart":
                    break
                time.sleep(0.2)

        mm = reader._masters["SN2-1"]
        # 1) Mod artik GORUNUYOR (once sonsuza kadar "unknown" kaliyordu).
        assert mm.operation_mode == "smart", "continuous cihazda Operation Mode HALA okunmuyor"
        assert mm.operation_mode_raw == 1.0

        # 2) POLITIKA EZILMEDI — en kritik iddia.
        assert mm.configured_session_policy == "continuous"
        assert mm.session_policy == "continuous", (
            "gateway Operation Mode'a bakip politikayi EZDI — mimari ihlali"
        )
        assert mm._scan_event is not None, "continuous cihazda taramalar durduruldu"

        # 3) Operator UYARILDI.
        uyari = [r.getMessage() for r in caplog.records if "device_policy_mismatch" in r.getMessage()]
        assert uyari, "politika uyusmazligi uyarisi basilmadi"
        assert "observed_operation_mode=smart" in uyari[0]
        assert "effective_policy=continuous" in uyari[0]
        assert "session_policy" in uyari[0], "operatore ne yapacagi soylenmemis"

        # 4) Ve /health bunu DISARI veriyor.
        saglik = reader.device_health()["SN2-1"]
        assert saglik["operation_mode"] == "smart"
        assert saglik["effective_session_policy"] == "continuous"
    finally:
        _kapat(proxy, reader, os_)


def test_uyusmazlik_uyarisi_kenar_tetikli(tmp_path, caplog) -> None:
    """Kosul KALICIDIR; her cycle loglamak defteri doldurur."""
    proxy, reader, device, os_ = _saha(tmp_path, session_policy="continuous")
    sinyaller = [*SINYALLER, MOD_SINYALI]
    try:
        os_.binary_yaz(MOD_IDX, True)
        son = time.monotonic() + 30
        while time.monotonic() < son:
            try:
                reader.read_device(device=device, signals=sinyaller)
            except Exception:  # noqa: BLE001
                pass
            mm = reader._masters.get("SN2-1")
            if mm is not None and mm.operation_mode == "smart":
                break
            time.sleep(0.2)

        caplog.clear()
        with caplog.at_level("WARNING"):
            for _ in range(10):
                reader.read_device(device=device, signals=sinyaller)
                time.sleep(0.05)
        tekrar = [r for r in caplog.records if "device_policy_mismatch" in r.getMessage()]
        assert not tekrar, f"uyari {len(tekrar)} kez tekrarlandi (kenar-tetikli olmali)"
    finally:
        _kapat(proxy, reader, os_)
