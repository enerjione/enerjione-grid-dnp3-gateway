"""GERCEK DNP3 trafigi ile `session_policy="smart"` — en kritik dogrulama.

BU DOSYA NEYI KANITLIYOR
------------------------
Birim testler durum makinesini dogrular ama SAHADAKI ASIL SORUYU
cevaplayamaz: "gateway gercekten SUSUYOR mu?"

Smart Navigator 2.0'in Initiating Endpoint hareketsizlik zaman asimi
15 SANIYE SABITTIR ve **her TCP/DNP trafigi bu sayaci sifirlar**. Gateway
5 saniyede bir Class taramasi gonderirse (ya da acilista integrity poll'u
yaparsa) sayac hicbir zaman dolmaz ve modem kapanmaz — kod ne kadar dogru
gorunurse gorunsun saha davranisi bozuk kalir.

Burada gercek bir DNP3 outstation ayaga kaldiriliyor ve aradaki TCP trafigi
BAYT BAYT sayiliyor. Iddia olculuyor, varsayilmiyor.

TOPOLOJI — GERCEK SAHA DUZENI
-----------------------------
Horstmann "Initiating Endpoint" modunda cihazin KENDISI master'a baglanir
(4G/SIM saha cihazi, NAT arkasinda). Bu yuzden:

    outstation (TCP client)  ->  sayac/proxy  ->  gateway (TCP server)

Proxy her iki yonu aynen aktarir ve GATEWAY -> CIHAZ yonundeki baytlari
sayar. "Gateway kaynakli trafik" tam olarak budur.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

from dnp3_gateway.adapters.dnp3_yadnp3_master import (  # noqa: E402
    HORSTMANN_IDLE_TIMEOUT_SEC,
    Yadnp3TelemetryReader,
)
from dnp3_gateway.backend import DeviceConfig  # noqa: E402

from .conftest import make_signal  # noqa: E402

BAGLANTI_TIMEOUT = 40.0

AKIM_IDX = 2
SINYALLER = [
    make_signal(
        "master.actual_current", source="master", data_type="analog", object_group=30, index=AKIM_IDX
    ),
]


def _bos_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _bekle(kosul, timeout: float = BAGLANTI_TIMEOUT, aralik: float = 0.2):
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        sonuc = kosul()
        if sonuc:
            return sonuc
        time.sleep(aralik)
    return None


class ByteSayaci:
    """outstation <-> gateway arasinda seffaf TCP proxy; her yonu ayri sayar.

    Cihaz (TCP client) bu proxy'ye baglanir, proxy de gateway'in TCP server
    portuna. `gateway_to_device` bayti, gateway'in URETTIGI DNP3 trafigidir —
    Horstmann'in bosta kalma sayacini sifirlayan sey tam olarak budur.
    """

    def __init__(self, gateway_port: int) -> None:
        self.gateway_port = gateway_port
        self.port = _bos_port()
        self.gateway_to_device = 0
        self.device_to_gateway = 0
        self._kapandi = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.port))
        self._srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._kapandi.is_set():
            try:
                cihaz, _ = self._srv.accept()
            except OSError:
                return
            try:
                gw = socket.create_connection(("127.0.0.1", self.gateway_port), timeout=5)
            except OSError:
                cihaz.close()
                continue
            threading.Thread(target=self._pompa, args=(gw, cihaz, "gw"), daemon=True).start()
            threading.Thread(target=self._pompa, args=(cihaz, gw, "dev"), daemon=True).start()

    def _pompa(self, kaynak: socket.socket, hedef: socket.socket, yon: str) -> None:
        try:
            while True:
                veri = kaynak.recv(4096)
                if not veri:
                    break
                if yon == "gw":
                    self.gateway_to_device += len(veri)
                else:
                    self.device_to_gateway += len(veri)
                hedef.sendall(veri)
        except OSError:
            pass
        finally:
            for s in (kaynak, hedef):
                try:
                    s.close()
                except OSError:
                    pass

    def kapat(self) -> None:
        self._kapandi.set()
        try:
            self._srv.close()
        except OSError:
            pass


class _OutstationApp(opendnp3.IOutstationApplication):
    def __init__(self) -> None:
        super().__init__()


class _KomutIsleyici(opendnp3.ICommandHandler):
    def __init__(self) -> None:
        super().__init__()

    def Begin(self) -> None:  # noqa: N802
        pass

    def End(self) -> Any:  # noqa: N802
        return opendnp3.CommandStatus.SUCCESS

    def Select(self, command: Any, index: int) -> Any:  # noqa: N802, ARG002
        return opendnp3.CommandStatus.SUCCESS

    def Operate(self, command: Any, index: int, handler: Any, op_type: Any) -> Any:  # noqa: N802, ARG002
        return opendnp3.CommandStatus.SUCCESS


class InitiatingOutstation:
    """Horstmann taklidi: cihazin KENDISI master'a baglanir (dial-in)."""

    def __init__(self, hedef_port: int, dnp3_adresi: int = 10, master_adresi: int = 1) -> None:
        self._manager = opendnp3.DNP3Manager(2)
        self._channel = self._manager.AddOutstationTCPClient(
            "os_ch",
            opendnp3.levels.NORMAL,
            opendnp3.ChannelRetry.Default(),
            [opendnp3.IPEndpoint("127.0.0.1", hedef_port)],
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
        try:
            self._outstation.Disable()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._manager.Shutdown()
        except Exception:  # noqa: BLE001
            pass


def _saha_kur(tmp_path, *, session_policy: str):
    """(outstation, sayac, reader, device) — politika ACIKCA yapilandirilir."""
    gw_port = _bos_port()
    reader = Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=gw_port,
        # Sahadaki agresif tempo: continuous'ta 1sn event scan + 2sn baseline.
        # smart'ta bu taramalarin HIC kurulmadigi bu tempoda kanitlanir.
        scan_interval_sec=1,
        baseline_interval_sec=2,
        publish_quality_flags=True,
        session_store_path=str(tmp_path / "session_state.json"),
    )
    sayac = ByteSayaci(gw_port)
    device = DeviceConfig(
        code="SN2-1",
        name="sn2",
        ip_address="127.0.0.1",
        dnp3_address=10,
        ip_endpoint_type="initiating",
        master_ip_port=gw_port,
        session_policy=session_policy,
    )
    os_ = InitiatingOutstation(sayac.port)
    os_.analog_yaz(AKIM_IDX, 42.0)
    return os_, sayac, reader, device


def _ilk_iyi_okuma(reader, device):
    def dene():
        r = reader.read_device(device=device, signals=SINYALLER)
        return r if any(x.quality == "good" for x in r) else None

    return _bekle(dene)


# ==========================================================================
# 20 — EN KRITIK TEST: aktarimdan sonra >=15 saniye SIFIR gateway trafigi
# ==========================================================================


def test_e_smart_baglanti_sonrasi_15_saniye_sifir_trafik(tmp_path) -> None:
    """Modemin kapanabilmesi icin gateway TAMAMEN susmali.

    Horstmann Initiating Endpoint bosta kalma suresi 15 SANIYE. Bu pencerede
    gateway TEK BAYT gonderirse cihaz modemini kapatamaz ve Smart Mode'un
    tum anlami kaybolur.

    Olculen sey iddia degil GERCEK TCP trafigi: aradaki proxy gateway -> cihaz
    yonundeki her bayti sayar.
    """
    os_, sayac, reader, device = _saha_kur(tmp_path, session_policy="smart")
    try:
        okumalar = _ilk_iyi_okuma(reader, device)
        assert okumalar is not None, "on kosul: cihaz baglanip veri gondermeli"

        # KABUL C + D: initiating uc TCP SERVER kullanir, smart politikada
        # TEKRARLAYAN tarama gorevi HIC kurulmaz.
        mm = reader._masters["SN2-1"]
        assert mm.session_policy == "smart"
        assert mm._scan_event is None, "smart politikada Class 1/2/3 scan gorevi kurulmus"
        assert mm._scan_class0 is None, "smart politikada Class 0 integrity scan gorevi kurulmus"

        for _ in range(5):
            reader.read_device(device=device, signals=SINYALLER)
            time.sleep(0.4)

        # ---- SESSIZLIK PENCERESI ----
        olcum_suresi = HORSTMANN_IDLE_TIMEOUT_SEC + 5.0
        baslangic = sayac.gateway_to_device
        bitis_at = time.monotonic() + olcum_suresi
        while time.monotonic() < bitis_at:
            # Poll dongusu NORMAL hizinda kosmaya devam ediyor — sessizlik
            # "gateway'i durdurduk" degil, "gateway cihaza sormuyor" demek.
            reader.read_device(device=device, signals=SINYALLER)
            time.sleep(0.25)
        gonderilen = sayac.gateway_to_device - baslangic

        assert gonderilen == 0, (
            f"{olcum_suresi:.0f} saniyede gateway -> cihaz yonunde {gonderilen} bayt "
            f"gonderildi. Horstmann'in {HORSTMANN_IDLE_TIMEOUT_SEC:.0f} saniyelik "
            "bosta kalma sayaci HICBIR ZAMAN dolmaz ve modem kapanmaz."
        )
    finally:
        reader.close()
        sayac.kapat()
        os_.kapat()


def test_ab_continuous_cihazda_trafik_devam_eder(tmp_path) -> None:
    """Kontrol testi: sessizlik Smart'a OZEL olmali.

    Bu test gecmezse yukaridaki sessizlik olcumu bir sey kanitlamaz — gateway
    her kosulda susuyor olabilirdi. continuous cihazda tarama SURMELI.
    """
    os_, sayac, reader, device = _saha_kur(tmp_path, session_policy="continuous")
    try:
        assert _ilk_iyi_okuma(reader, device) is not None, "on kosul: baglanti"
        mm = reader._masters["SN2-1"]
        assert mm.session_policy == "continuous"
        assert mm._scan_event is not None, "continuous cihazda event scan KURULMALI"
        assert mm._scan_class0 is not None, "continuous cihazda integrity scan KURULMALI"

        baslangic = sayac.gateway_to_device
        bitis_at = time.monotonic() + 8.0
        while time.monotonic() < bitis_at:
            reader.read_device(device=device, signals=SINYALLER)
            time.sleep(0.25)
        gonderilen = sayac.gateway_to_device - baslangic

        assert gonderilen > 0, (
            "continuous cihazda gateway hic trafik uretmedi — surekli haberlesme "
            "davranisi bozulmus (smart politika tum cihazlari susturmus olabilir)"
        )
    finally:
        reader.close()
        sayac.kapat()
        os_.kapat()


# ==========================================================================
# 6 — Uyanma -> olay aktarimi -> yeniden sessizlik
# ==========================================================================


def test_k_kanitsiz_yeniden_baglanti_sessizce_saglikli_gorunmez(tmp_path) -> None:
    """Yeniden baglanan ama KONUSMAYAN cihaz `smart_idle`de SAKLANMAZ.

    En tehlikeli sessiz basarisizlik bu olurdu: cihaz TCP kurar, DNP3 katmani
    hic konusmaz, gateway de "smart, susuyor olmali" deyip sonsuza kadar
    saglikli gosterir. Mevcut recovery grace mekanizmasi bunu yakalar:
    kanit gelmezse `recovering` -> `lost` -> comm_lost.

    Yani smart politikanin basarisizlik modu SESSIZ DEGIL, GORUNUR.

    FIXTURE SINIRI (bilincli): bu wheel `AnalogConfig`i Python'a acmadigi icin
    simule outstation noktalarini OLAY SINIFINA atayamaz ve bu yuzden uyanma
    aninda kendiliginden (unsolicited) rapor GONDEREMEZ. Sahada Horstmann tam
    olarak bunu yapar ("Wake up Modem" isaretli ariza noktalari). Uyanma ->
    olay teslimi yolu bu yuzden GERCEK CIHAZLA dogrulanmalidir; bkz.
    docs/HORSTMANN_SMART_MODE.md saha kabul proseduru.
    """
    os_, sayac, reader, device = _saha_kur(tmp_path, session_policy="smart")
    try:
        assert _ilk_iyi_okuma(reader, device) is not None, "on kosul: baglanti + veri"
        mm = reader._masters["SN2-1"]
        assert mm.cache.session_evidence() is True, "ilk oturumda kanit gorulmedi"
        reader.commit_published(device=device, readings=reader.read_device(device=device, signals=SINYALLER))

        # ---- Cihaz modemini kapatiyor -> smart_idle ----
        os_.kapat()
        os_ = None
        assert _bekle(lambda: mm.cache.is_smart_idle(), timeout=25), "smart_idle'e gecilmedi"

        # ---- Sessiz bir cihaz geri baglaniyor (DNP3 katmani konusmuyor) ----
        os_ = InitiatingOutstation(sayac.port)
        assert _bekle(lambda: mm.cache.is_connected(), timeout=40), "yeniden baglanmadi"
        assert mm.cache.is_smart_idle() is False, "baglanti idle'i temizlemeli"

        # Kanit gelmedigi icin recovery grace dolar ve cihaz GORUNUR sekilde
        # kopuk ilan edilir — `smart_idle`de sessizce saklanmaz.
        def kopuk_ilan_edildi():
            r = reader.read_device(device=device, signals=SINYALLER)
            return r if any(x.quality == "comm_lost" for x in r) else None

        assert _bekle(kopuk_ilan_edildi, timeout=60) is not None, (
            "kanitsiz yeniden baglanti sessizce saglikli gorundu — smart politika bir arizayi gizliyor"
        )
        # `recovering` de `lost` da kabul: ikisinde de SCADA comm_lost gorur
        # (mevcut semantik — sahte "online" yayinlamamak icin).
        assert mm.cache.state() in ("recovering", "lost")
        assert mm.cache.is_smart_idle() is False, "kanitsiz oturum idle'de saklanmis"
    finally:
        reader.close()
        sayac.kapat()
        if os_ is not None:
            os_.kapat()


# ==========================================================================
# G + H — Modem kapandi: comm_lost YOK, yeniden baglanma FIRTINASI YOK
# ==========================================================================


def test_gl_modem_kapaninca_idle_ve_sessizlik(tmp_path) -> None:
    """Cihaz oturumu kapatir (modem kapandi) -> smart_idle, comm_lost YOK.

    Ayrica: idle sirasinda gateway zorla relink / yoklama dongusune GIRMEZ.
    """
    os_, sayac, reader, device = _saha_kur(tmp_path, session_policy="smart")
    try:
        assert _ilk_iyi_okuma(reader, device) is not None, "on kosul: baglanti"
        mm = reader._masters["SN2-1"]
        assert mm.session_policy == "smart"
        reader.read_device(device=device, signals=SINYALLER)

        # ---- Modem kapandi: cihaz TCP oturumunu kapatiyor ----
        os_.kapat()
        os_ = None

        uyudu = _bekle(lambda: mm.cache.is_smart_idle(), timeout=25)
        assert uyudu, "beklenen kapanma sonrasi cihaz smart_idle'e gecmedi"

        saglik = reader.device_health()["SN2-1"]
        assert saglik["state"] == "smart_idle"
        assert saglik["connected"] is False

        # 12 saniye boyunca oku: comm_lost YOK, relink/yoklama YOK.
        baslangic_relink = reader.recovery_stats()["forced_relink_total"]
        bitis_at = time.monotonic() + 12.0
        while time.monotonic() < bitis_at:
            r = reader.read_device(device=device, signals=SINYALLER)
            assert all(x.quality != "comm_lost" for x in r), "SAGLIKLI smart_idle comm_lost olarak yayinlandi"
            time.sleep(0.25)

        st = reader.recovery_stats()
        assert st["forced_relink_total"] == baslangic_relink, "idle iken zorla relink denendi"
        assert st["lost_probe_total"] == 0, "idle cihaza yoklama gonderildi"
    finally:
        reader.close()
        sayac.kapat()
        if os_ is not None:
            os_.kapat()
