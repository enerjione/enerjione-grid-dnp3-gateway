"""GERCEK DNP3 trafigi ile ucdan uca testler — master <-> outstation loopback.

NEDEN BU DOSYA VAR
------------------
`dnp3_yadnp3_master.py` bu deponun en buyuk ve en kritik modulu: sahadaki TUM
olcum ve TUM kesici komutu buradan geciyor. Buna ragmen **tek satiri bile
gercek DNP3 trafigiyle test edilmemisti**. Mevcut testler `_DeviceCache` gibi
saf-Python parcalari kapsiyordu; `read_device`'in gercek yolu (opendnp3
binding'i, SOE handler, recovery durum makinesi, CROB) yalnizca SAHADA
kosuyordu. Yani bir regresyon ancak musteri sahasinda fark edilebilirdi.

opendnp3 wheel'i bir outstation API'si de sagliyor. Bu dosya localhost
uzerinde gercek bir DNP3 outstation ayaga kaldirip gateway'in kendi master'ini
ona baglar. Trafik gercektir: TCP, link layer, application layer, integrity
poll, event scan, CROB.

FIXTURE'IN SINIRI (bilincli)
----------------------------
Bu wheel `AnalogConfig`i Python'a acmiyor, dolayisiyla nokta bazinda
varyasyon/sinif ayarlanamiyor. Iki sonucu var:

  * Analog noktalar 32-bit TAMSAYI varyasyonuyla (Group30Var1) tasiniyor;
    kesirli deger fixture'da kaybolur. Bu GATEWAY'in davranisi DEGIL —
    `test_scale_gercek_trafikte_uygulanir` bunu ham/olcekli ayrimiyla
    kanitlar. Testlerde tamsayi ham degerler kullaniliyor.
  * Noktalar olay sinifina atanmadigi icin degisiklikler event scan ile
    degil, Class 0 baseline taramasiyla geliyor. Sahada cihazlar noktalarini
    olay sinifina atar ve degisim aninda gelir; bu yuzden testlerde baseline
    araligi kisa tutuldu.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader  # noqa: E402
from dnp3_gateway.backend import DeviceConfig, SignalConfig  # noqa: E402

# Baglanti + ilk integrity poll icin ust sinir. Loopback'te ~1-2sn suruyor;
# CI kutusunun yavasligina karsi genis pay birakildi. Testler POLLING yapar,
# sabit sleep KULLANMAZ — hizli makinede hizli biter.
BAGLANTI_TIMEOUT = 30.0
BASELINE_SEC = 2


def _bos_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _OutstationApp(opendnp3.IOutstationApplication):
    def __init__(self) -> None:
        super().__init__()


class _KomutIsleyici(opendnp3.ICommandHandler):
    """Gelen CROB'lari kaydeder — komut yolunun gercekten ulastigini kanitlar."""

    def __init__(self) -> None:
        super().__init__()
        self.operasyonlar: list[int] = []
        self.secimler: list[int] = []

    def Begin(self) -> None:  # noqa: N802
        pass

    def End(self) -> Any:  # noqa: N802
        return opendnp3.CommandStatus.SUCCESS

    # C++ arayuzunun imzasi birebir korunmali; pybind11 trampolini bu
    # metotlari ADA gore cagirir ve imza tutmazsa hata SESSIZCE yutulup
    # outstation `NOT_SUPPORTED` doner (yani "komut calismadi" gibi gorunur).
    #   Select (command, index)
    #   Operate(command, index, handler, opType)
    def Select(self, command: Any, index: int) -> Any:  # noqa: N802, ARG002
        self.secimler.append(int(index))
        return opendnp3.CommandStatus.SUCCESS

    def Operate(self, command: Any, index: int, handler: Any, op_type: Any) -> Any:  # noqa: N802, ARG002
        self.operasyonlar.append(int(index))
        return opendnp3.CommandStatus.SUCCESS


class Outstation:
    """Testlerin surdugu sahte saha cihazi."""

    def __init__(self, port: int, dnp3_adresi: int = 10, master_adresi: int = 1) -> None:
        self.port = port
        self.komutlar = _KomutIsleyici()
        self._app = _OutstationApp()
        self._manager = opendnp3.DNP3Manager(2)
        self._channel = self._manager.AddTCPServer(
            "os_ch",
            opendnp3.levels.NORMAL,
            opendnp3.ServerAcceptMode.CloseExisting,
            opendnp3.IPEndpoint("127.0.0.1", port),
            None,
        )
        cfg = opendnp3.OutstationStackConfig(opendnp3.DatabaseConfig(10))
        cfg.link.LocalAddr = dnp3_adresi
        cfg.link.RemoteAddr = master_adresi
        self._outstation = self._channel.AddOutstation("os", self.komutlar, self._app, cfg)
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


def _sinyal(
    key: str,
    index: int,
    *,
    scale: float = 1.0,
    offset: float = 0.0,
    data_type: str = "analog",
    group: int = 30,
) -> SignalConfig:
    return SignalConfig(
        key=key,
        label=key,
        unit=None,
        source="master",
        dnp3_class="Class 1",
        data_type=data_type,
        dnp3_object_group=group,
        dnp3_index=index,
        scale=scale,
        offset=offset,
        supports_alarm=False,
    )


def _bekle(kosul, timeout: float = BAGLANTI_TIMEOUT, aralik: float = 0.25):
    """Kosul dogru olana kadar yokla; sonucu doner, dolarsa None.

    Sabit `sleep` yerine polling: hizli makinede test hizli biter, yavas CI
    kutusunda da flake yapmaz.
    """
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        sonuc = kosul()
        if sonuc:
            return sonuc
        time.sleep(aralik)
    return None


@pytest.fixture
def saha():
    """Outstation + ona bagli gateway master'i. Her test kendi portunu alir."""
    port = _bos_port()
    os_ = Outstation(port)
    reader = Yadnp3TelemetryReader(
        local_address=1,
        default_dnp3_tcp_port=port,
        scan_interval_sec=1,
        baseline_interval_sec=BASELINE_SEC,
        publish_quality_flags=True,
    )
    device = DeviceConfig(code="OS-1", name="os", ip_address="127.0.0.1", dnp3_address=10)
    try:
        yield os_, reader, device
    finally:
        try:
            reader.close()
        finally:
            os_.kapat()


def _ilk_iyi_okuma(reader, device, sinyaller):
    """Cihaz baglanip gercek veri gelene kadar oku."""

    def dene():
        r = reader.read_device(device=device, signals=sinyaller)
        return r if any(x.quality == "good" for x in r) else None

    return _bekle(dene)


# --------------------------------------------------------------------------
# temel okuma yolu
# --------------------------------------------------------------------------


def test_gercek_dnp3_okumasi(saha) -> None:
    """En temel garanti: gateway gercek bir outstation'dan deger okuyabiliyor.

    Bu test gecmiyorsa sahada HICBIR sey calismaz. Daha once bu yolun
    tamamen test disi olmasi, bir regresyonun ancak musteri sahasinda fark
    edilmesi demekti.
    """
    os_, reader, device = saha
    os_.analog_yaz(0, 42.0)
    os_.binary_yaz(0, True)

    sinyaller = [_sinyal("master.a0", 0), _sinyal("master.b0", 0, data_type="binary", group=1)]
    okumalar = _ilk_iyi_okuma(reader, device, sinyaller)

    assert okumalar is not None, "gercek outstation'dan veri okunamadi"
    degerler = {x.signal_key: x for x in okumalar}
    assert degerler["master.a0"].scaled_value == 42.0
    assert degerler["master.a0"].quality == "good"
    assert degerler["master.b0"].scaled_value == 1.0

    # Kalite bayraklari GERCEK cihazdan geliyor (ONLINE biti)
    assert degerler["master.a0"].dnp3_flags is not None
    assert degerler["master.a0"].dnp3_flags & 0x01, "ONLINE biti tasinmadi"


def test_scale_ve_offset_gercek_trafikte_uygulanir(saha) -> None:
    """Olcekleme gercek DNP3 degeri uzerinde dogru calisiyor.

    Ayrica fixture'in tamsayi varyasyonu sinirinin GATEWAY kaynakli
    OLMADIGINI kanitlar: ham deger 115 gelir, olcekli deger 11.5 olur —
    kesirli hesap gateway tarafinda saglam.
    """
    os_, reader, device = saha
    os_.analog_yaz(0, 115.0)

    sinyaller = [_sinyal("master.a0", 0, scale=0.1, offset=1.0)]
    okumalar = _ilk_iyi_okuma(reader, device, sinyaller)

    assert okumalar is not None
    r = okumalar[0]
    assert r.raw_value == 115.0, "cihazdan gelen ham deger degismemeli"
    assert r.scaled_value == pytest.approx(12.5), "scale/offset yanlis uygulandi (115*0.1+1)"


def test_delta_only_yayin_gercek_trafikte(saha) -> None:
    """Onaylanan bir deger, degismedikce TEKRAR yayinlanmamali.

    Delta-only yayin bu sistemin temel throughput varsayimi: 300 cihaz x 265
    sinyal her cycle yayinlansa backend'i bogardi. Bu testte gercek Class 0
    baseline taramasi ayni degeri tekrar tekrar yaziyor; cache surum
    kontrolu sayesinde tekrar yayin OLMAMALI.
    """
    os_, reader, device = saha
    os_.analog_yaz(0, 7.0)
    sinyaller = [_sinyal("master.a0", 0)]

    okumalar = _ilk_iyi_okuma(reader, device, sinyaller)
    assert okumalar is not None
    reader.commit_published(device=device, readings=okumalar)

    # Baseline taramasi ayni degeri en az bir kez daha yazsin
    time.sleep(BASELINE_SEC + 1.5)

    tekrar = reader.read_device(device=device, signals=sinyaller)
    assert all(x.quality == "no_change" for x in tekrar), (
        f"degismeyen deger tekrar yayinlanmis: {[(x.signal_key, x.quality) for x in tekrar]}"
    )


def test_deger_degisimi_yayina_girer(saha) -> None:
    """Cihazdaki degisiklik gateway'e ulasip 'yayinlanacak' olarak isaretleniyor."""
    os_, reader, device = saha
    os_.analog_yaz(0, 7.0)
    sinyaller = [_sinyal("master.a0", 0)]

    okumalar = _ilk_iyi_okuma(reader, device, sinyaller)
    assert okumalar is not None
    reader.commit_published(device=device, readings=okumalar)

    os_.analog_yaz(0, 55.0)

    def degisti():
        r = reader.read_device(device=device, signals=sinyaller)
        return r if r and r[0].raw_value == 55.0 and r[0].quality == "good" else None

    yeni = _bekle(degisti)
    assert yeni is not None, "cihazdaki degisiklik gateway'e ulasmadi"
    assert yeni[0].scaled_value == 55.0


# --------------------------------------------------------------------------
# recovery durum makinesi — gercek link kopmasi
# --------------------------------------------------------------------------


def test_link_kopunca_comm_lost_geri_gelince_toparlanma(saha) -> None:
    """Sahadaki en sik olay: cihaz gidiyor, sonra geri geliyor.

    Bu test, `comm_lost` duyurusunun yayin onayina baglanmasi (commit-after-
    publish) duzeltmesinin GERCEK protokol yolundaki karsiligidir. Birim
    testler `_DeviceCache`i doguruyordu; burada gercekten TCP kopuyor.

    Beklenen: kopunca comm_lost bir kez duyurulur, onaydan sonra `no_change`e
    duser (flood yok). Cihaz geri gelince tum degerler yeniden yayinlanir —
    SCADA cihazi tekrar canli gorur.
    """
    os_, reader, device = saha
    os_.analog_yaz(0, 33.0)
    sinyaller = [_sinyal("master.a0", 0)]

    okumalar = _ilk_iyi_okuma(reader, device, sinyaller)
    assert okumalar is not None, "on kosul: once saglikli baglanti kurulmali"
    reader.commit_published(device=device, readings=okumalar)

    # Cihaz gitti
    os_.kapat()

    def koptu():
        r = reader.read_device(device=device, signals=sinyaller)
        return r if r and r[0].quality == "comm_lost" else None

    kopuk = _bekle(koptu, timeout=BAGLANTI_TIMEOUT)
    assert kopuk is not None, "link koptugu halde comm_lost yayinlanmadi"

    # Duyuru ONAYLANMADAN once tekrar comm_lost gelmeli (yayin garantisi)
    tekrar = reader.read_device(device=device, signals=sinyaller)
    assert tekrar[0].quality == "comm_lost", (
        "yayin onaylanmadan 'duyuruldu' sayilmis — kalicilastirilamayan bir "
        "comm_lost cihazi SCADA'da sonsuza kadar canli gosterirdi"
    )

    # Onaydan sonra susmali (flood yok)
    reader.commit_published(device=device, readings=tekrar)
    susmus = reader.read_device(device=device, signals=sinyaller)
    assert susmus[0].quality == "no_change", "comm_lost her cycle tekrar yayinlaniyor"

    saglik = reader.device_health()["OS-1"]
    assert saglik["state"] in ("lost", "recovering")


def test_cihaz_geri_gelince_tum_degerler_yeniden_yayinlanir(saha) -> None:
    """Toparlanma dogrulanınca son bilinen TUM degerler yeniden yayinlanir.

    Delta-only yayinda bu sart: cihaz kopukken degismeyen bir deger, link
    geri geldiginde de yayinlanmazdi ve SCADA cihazi olu gormeye devam
    ederdi.
    """
    os_, reader, device = saha
    port = os_.port
    os_.analog_yaz(0, 33.0)
    sinyaller = [_sinyal("master.a0", 0)]

    okumalar = _ilk_iyi_okuma(reader, device, sinyaller)
    assert okumalar is not None
    reader.commit_published(device=device, readings=okumalar)

    os_.kapat()
    kopuk = _bekle(
        lambda: (
            r
            if (r := reader.read_device(device=device, signals=sinyaller))[0].quality == "comm_lost"
            else None
        )
    )
    assert kopuk is not None
    reader.commit_published(device=device, readings=kopuk)

    # Cihaz AYNI portta geri geliyor
    yeni_os = Outstation(port)
    try:
        yeni_os.analog_yaz(0, 33.0)  # deger DEGISMEDI — yine de yayinlanmali

        def toparlandi():
            r = reader.read_device(device=device, signals=sinyaller)
            return r if r and r[0].quality == "good" else None

        geri = _bekle(toparlandi)
        assert geri is not None, (
            "cihaz geri geldigi halde degerler yeniden yayinlanmadi — "
            "delta-only yayinda SCADA cihazi olu gormeye devam ederdi"
        )
        assert geri[0].scaled_value == 33.0
        assert reader.device_health()["OS-1"]["state"] == "online"
    finally:
        yeni_os.kapat()


# --------------------------------------------------------------------------
# komut yolu — gercek CROB
# --------------------------------------------------------------------------


def test_crob_gercekten_outstationa_ulasir(saha) -> None:
    """Kesici komutu gercek DNP3 uzerinden cihaza gidiyor.

    `/operate` ve komut-poll yollarinin ucundaki tek gercek etki bu. Mock
    adapter her komuta "basarili" dondugu icin, bu dogrulama yalnizca burada
    yapilabilir.
    """
    os_, reader, device = saha
    os_.analog_yaz(0, 1.0)
    sinyaller = [_sinyal("master.a0", 0)]
    assert _ilk_iyi_okuma(reader, device, sinyaller) is not None, "on kosul: baglanti"

    sonuc = reader.operate_device(device=device, index=2, op_type="latch_on", timeout_sec=10.0)

    assert sonuc["ok"] is True, f"CROB basarisiz: {sonuc}"
    assert 2 in os_.komutlar.operasyonlar, (
        f"CROB outstation'a ULASMADI (gelen operasyonlar: {os_.komutlar.operasyonlar})"
    )


def test_kopuk_cihaza_komut_gonderilmez(saha) -> None:
    """Cihaz erisilemezken komut sessizce 'basarili' donmemeli."""
    os_, reader, device = saha
    os_.analog_yaz(0, 1.0)
    sinyaller = [_sinyal("master.a0", 0)]
    assert _ilk_iyi_okuma(reader, device, sinyaller) is not None

    os_.kapat()
    _bekle(
        lambda: (
            r
            if (r := reader.read_device(device=device, signals=sinyaller))[0].quality == "comm_lost"
            else None
        )
    )

    sonuc = reader.operate_device(device=device, index=2, op_type="latch_on", timeout_sec=5.0)
    assert sonuc["ok"] is False, "kopuk cihaza gonderilen komut basarili dondu"
