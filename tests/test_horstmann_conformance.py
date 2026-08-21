"""HORSTMANN DNP3 UYUMLULUK — resmi Device Profile'a karsi KANIT.

NORMATIF KAYNAK
---------------
Dipl.-Ing. H. Horstmann GmbH — DNP V3.0 Device Profile Document
(Smart Navigator 2.0 / Pole Master), DNP3 Implementation Table.

BU DOSYANIN KURALI: "OpenDNP3 kullaniyoruz, halleder" KANIT DEGILDIR.
Iddialar ya kutuphaneden OLCULUR ya da GERCEK bir outstation'a karsi
tel/API seviyesinde gosterilir.

KAPSANAN PROFIL MADDELERI
-------------------------
    Binary Output      Latch On/Off = ALWAYS, Pulse On/Off = NEVER,
                       Count > 1 = NEVER, Queue/Clear Queue = NEVER
    App fragment       Horstmann TX = 2048, RX = 1024
    G50V1              Time And Date, FC=2 WRITE
    FC=23              Delay Measurement (NonLAN proseduru)
    IIN1.4             Need Time — gercek time sync OLMADAN clear EDILMEZ

PROFILDE OLMAYAN ve TELE CIKMAMASI GEREKENLER
---------------------------------------------
    FC=24 RECORD CURRENT TIME, G50V3 — Implementation Table'da YOK.
    `DNP3_TIME_SYNC=nonlan` yolunda ikisinin de gonderilMEDIGI AC testinde
    NEGATIF olarak dogrulanir.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.command_parameters import (
    HORSTMANN_COUNT,
    HORSTMANN_OP_TYPES,
    ParameterReason,
    validate_command_parameters,
)

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")


# ==========================================================================
# Q/R/S/T/U — BINARY OUTPUT PROFIL KISITLARI (P0)
# ==========================================================================


@pytest.mark.parametrize("op", ["latch_on", "latch_off"])
def test_t_u_latch_profilde_izinli(op: str) -> None:
    """Profil: `Latch On = ALWAYS`, `Latch Off = ALWAYS`."""
    r = validate_command_parameters(op_type=op, count=1, on_time_ms=0, off_time_ms=0)
    assert r.reason is ParameterReason.VALID


@pytest.mark.parametrize("op", ["pulse_on", "pulse_off"])
def test_r_s_pulse_profilde_yasak(op: str) -> None:
    """Profil: `Pulse On = NEVER`, `Pulse Off = NEVER`.

    P0: 1.15.0'a kadar validator bunlari KABUL EDIYORDU — gateway, cihazin
    "ASLA desteklemiyorum" dedigi bir istegi tele koyabiliyordu.
    """
    r = validate_command_parameters(op_type=op, count=1, on_time_ms=100, off_time_ms=100)
    assert r.reason is ParameterReason.INVALID_OP_TYPE
    assert "NEVER" in r.detail or "DESTEKLENMIYOR" in r.detail


def test_p_count_1_gecerli() -> None:
    assert (
        validate_command_parameters(
            op_type="latch_on", count=HORSTMANN_COUNT, on_time_ms=0, off_time_ms=0
        ).reason
        is ParameterReason.VALID
    )


@pytest.mark.parametrize("count", [0, 2, 3, 255])
def test_q_count_1_disinda_reddedilir(count: int) -> None:
    """Profil: `Count > 1 = NEVER`. (`count=0` ayri gerekceyle zaten yasak.)"""
    r = validate_command_parameters(op_type="latch_on", count=count, on_time_ms=0, off_time_ms=0)
    assert r.reason is ParameterReason.INVALID_COUNT


def test_profil_kisiti_op_map_ile_tutarli() -> None:
    """Kodlayici dort tipi de KODLAYABILIR; kapi gateway sinirindadir.

    Bu ayrim bilincli: `_op_map` kutuphane kodlayicisidir, profil kisiti ise
    politika. Ikisini karistirmak, ileride profil disi bir cihaz eklendiginde
    kodlayiciyi da degistirmeyi gerektirirdi.
    """
    assert HORSTMANN_OP_TYPES == {"latch_on", "latch_off"}
    assert HORSTMANN_OP_TYPES < set(mod._ManagedMaster._op_map())


def test_adapter_savunma_derinligi_profil_disini_tele_cikarmaz() -> None:
    """Dogrulayici atlanirsa bile uyumsuz cerceve tele CIKMAMALI (statik)."""
    import ast
    import inspect
    import textwrap

    kaynak = textwrap.dedent(inspect.getsource(mod._ManagedMaster.operate_crob))
    govde = "\n".join(ast.unparse(n) for n in ast.parse(kaynak).body[0].body)
    assert "HORSTMANN_OP_TYPES" in govde, "adapter profil kisitini kontrol etmiyor"
    assert "HORSTMANN_COUNT" in govde


# ==========================================================================
# FRAGMENT LIMITLERI (P1)
# ==========================================================================


def test_fragment_limitleri_profile_uyar() -> None:
    """Profil: Horstmann TX=2048 (bize gonderir), RX=1024 (bizden alir).

    opendnp3 IKISINI DE 2048 varsayar; gateway 1.15.0'a kadar bunlari HIC
    ayarlamiyordu. `maxTxFragSize=2048` cihazin ilan ettigi alma sinirinin
    IKI KATIYDI.
    """
    assert mod._HORSTMANN_TX_FRAGMENT_MAX == 2048
    assert mod._HORSTMANN_RX_FRAGMENT_MAX == 1024

    varsayilan = opendnp3.MasterStackConfig().master
    assert int(varsayilan.maxTxFragSize) == 2048, (
        "kutuphane varsayilani degismis — bu duzeltmenin gerekcesi yeniden bakilmali"
    )

    import ast
    import inspect
    import textwrap

    kaynak = textwrap.dedent(inspect.getsource(mod._ManagedMaster.__init__))
    govde = "\n".join(ast.unparse(n) for n in ast.parse(kaynak).body[0].body)
    assert "maxTxFragSize = _HORSTMANN_RX_FRAGMENT_MAX" in govde.replace("cfg.master.", ""), (
        "TX tavani cihazin RX sinirina cekilmiyor"
    )


# ==========================================================================
# Z / AA — G50V1 TIME AND DATE + IIN1.4 (TEL ve API SEVIYESINDE KANIT)
# ==========================================================================


def _bos_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _ZamanIsteyenOutstation(opendnp3.IOutstationApplication):
    """IIN1.4 NEED_TIME asserted eden GERCEK outstation uygulamasi.

    `WriteAbsoluteTime` master G50V1 WRITE gonderdiginde cagrilir — yani
    time sync'in GERCEKTEN oldugunun API seviyesindeki kanitidir.
    """

    def __init__(self) -> None:
        super().__init__()
        self.need_time = True
        self.yazilan_zamanlar: list[int] = []

    def GetApplicationIIN(self):  # noqa: N802 — kutuphane imzasi
        iin = opendnp3.ApplicationIIN()
        iin.needTime = self.need_time
        return iin

    def SupportsWriteAbsoluteTime(self):  # noqa: N802 — kutuphane imzasi
        return True

    def WriteAbsoluteTime(self, ms):  # noqa: N802 — kutuphane imzasi
        self.yazilan_zamanlar.append(int(getattr(ms, "msSinceEpoch", ms)))
        # GERCEK SENKRONIZASYON OLDU -> ancak SIMDI clear.
        self.need_time = False
        return True


class _KomutIsleyici(opendnp3.ICommandHandler):
    def __init__(self) -> None:
        super().__init__()

    def Begin(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass

    def End(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass


class _Proxy:
    """Iki TCP client'i birlestirir ve HER YONDEKI cerceveleri saklar."""

    def __init__(self) -> None:
        self.gw_port = _bos_port()
        self.os_port = _bos_port()
        self.gw_giden: list[bytes] = []
        self._dur = threading.Event()
        self._sock: list[socket.socket] = []
        self._a = self._dinle(self.gw_port)
        self._b = self._dinle(self.os_port)
        threading.Thread(target=self._calis, daemon=True).start()

    def _dinle(self, port: int) -> socket.socket:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        s.settimeout(0.3)
        self._sock.append(s)
        return s

    def _kabul(self, srv):
        while not self._dur.is_set():
            try:
                c, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return None
            c.settimeout(0.2)
            self._sock.append(c)
            return c
        return None

    def _calis(self) -> None:
        gw, os_ = self._kabul(self._a), self._kabul(self._b)
        if gw is None or os_ is None:
            return
        while not self._dur.is_set():
            for kaynak, hedef, gw_yonu in ((gw, os_, True), (os_, gw, False)):
                try:
                    d = kaynak.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not d:
                    return
                if gw_yonu:
                    self.gw_giden.append(d)
                try:
                    hedef.sendall(d)
                except OSError:
                    return

    def kapat(self) -> None:
        self._dur.set()
        for s in self._sock:
            try:
                s.close()
            except OSError:
                pass


def _fc_ve_objeler(cerceve: bytes) -> tuple[int | None, list[tuple[int, int]]]:
    """(app FC, [(group, variation)]) — link/transport basliklarini atlar."""
    if len(cerceve) < 13 or cerceve[0] != 0x05 or cerceve[1] != 0x64:
        return None, []
    fc = cerceve[12]
    n_user = cerceve[2] - 5
    objeler = []
    i = 13
    while i + 1 < min(10 + n_user, len(cerceve)):
        objeler.append((cerceve[i], cerceve[i + 1]))
        i += 3
    return fc, objeler


def _time_sync_kosusu(mode: str, timeout: float = 30.0):
    """GERCEK bir NEED_TIME outstation'ina karsi `mode` prosedurunu kosar.

    Doner: `(outstation_app, gateway_yonundeki_ham_cerceveler)`.

    `cfg.master.timeSyncMode = X` GORMEK KANIT DEGILDIR (review §15) — bu
    kosucu master'i gercekten ayaga kaldirir, cerceveleri proxy'de yakalar
    ve outstation'in `WriteAbsoluteTime` geri cagrisini gozler.
    """
    proxy = _Proxy()
    os_app = _ZamanIsteyenOutstation()
    os_mgr = opendnp3.DNP3Manager(2)
    mgr = opendnp3.DNP3Manager(2)
    try:
        os_ch = os_mgr.AddOutstationTCPClient(
            "osch",
            opendnp3.levels.NORMAL,
            opendnp3.ChannelRetry.Default(),
            [opendnp3.IPEndpoint("127.0.0.1", proxy.os_port)],
            "0.0.0.0",
            None,
        )
        oscfg = opendnp3.OutstationStackConfig(opendnp3.DatabaseConfig(20))
        oscfg.link.LocalAddr = 10
        oscfg.link.RemoteAddr = 1
        os_ch.AddOutstation("os", _KomutIsleyici(), os_app, oscfg).Enable()

        class _SOE(opendnp3.ISOEHandler):
            def __init__(self) -> None:
                super().__init__()

        class _App(opendnp3.IMasterApplication):
            def __init__(self) -> None:
                super().__init__()

        ch = mgr.AddTCPClient(
            "m",
            opendnp3.levels.NORMAL,
            opendnp3.ChannelRetry.Default(),
            [opendnp3.IPEndpoint("127.0.0.1", proxy.gw_port)],
            "0.0.0.0",
            None,
        )
        cfg = opendnp3.MasterStackConfig()
        cfg.link.LocalAddr = 1
        cfg.link.RemoteAddr = 10
        cfg.master.disableUnsolOnStartup = False
        # URETIM YOLUNUN TA KENDISI — testte elle enum set EDILMEZ.
        mod._ManagedMaster._apply_time_sync(cfg, mode)
        ch.AddMaster("mm", _SOE(), _App(), cfg).Enable()

        son = time.monotonic() + timeout
        while time.monotonic() < son and not os_app.yazilan_zamanlar:
            time.sleep(0.2)
        # Senkronizasyondan SONRA da bir sure dinle: negatif iddialar
        # ("FC=24 hic gonderilmedi") icin akisin tamamlanmasi gerekir.
        time.sleep(1.0)
        return os_app, list(proxy.gw_giden)
    finally:
        for kapat in (mgr.Shutdown, os_mgr.Shutdown, proxy.kapat):
            try:
                kapat()
            except Exception:  # noqa: BLE001
                pass


def _fc_kumesi(cerceveler) -> set[int]:
    return {fc for c in cerceveler if (fc := _fc_ve_objeler(c)[0]) is not None}


def _g50_varyasyonlari(cerceveler) -> list[int]:
    yazma = [c for c in cerceveler if _fc_ve_objeler(c)[0] == 0x02]
    return sorted({v for c in yazma for (g, v) in _fc_ve_objeler(c)[1] if g == 50})


def test_z_aa_g50v1_time_sync_ve_iin14_sync_before_clear() -> None:
    """LAN prosedurunun OLCULEN tel davranisi + sync-before-clear (AA).

    Bu test LAN'i DOGRU secim olarak ONAYLAMAZ; LAN'in NE GONDERDIGINI
    pinler. Horstmann icin dogru prosedur `nonlan`dir (asagidaki AB testi).
    """
    os_app, cerceveler = _time_sync_kosusu("lan")

    # --- API SEVIYESI KANIT: geri cagri TETIKLENDI ---
    assert os_app.yazilan_zamanlar, "master NEED_TIME'a ragmen saat YAZMADI — time sync calismiyor"

    # --- SYNC-BEFORE-CLEAR (AA) ---
    # `need_time` YALNIZCA `WriteAbsoluteTime` icinde temizleniyor; yani
    # gercek senkronizasyon OLMADAN clear eden bir yol YOK.
    assert os_app.need_time is False, "sync sonrasi NEED_TIME temizlenmedi"

    # --- TEL SEVIYESI KANIT: hangi zaman NESNESI yazildi? ---
    varyasyonlar = _g50_varyasyonlari(cerceveler)
    assert varyasyonlar, "tel uzerinde G50 tasiyan FC=2 WRITE cercevesi YOK"

    # OLCULEN GERCEK (yadnp3 3.2.1.1): `LAN` modu G50V3 yazar ve FC=24
    # RECORD_CURRENT_TIME gonderir. Profil bunlarin IKISINI DE ilan ETMIYOR.
    assert varyasyonlar == [3], (
        f"LAN modunun yazdigi G50 varyasyonu degismis: {varyasyonlar} "
        "(bu testin gerekcesi yeniden degerlendirilmeli)"
    )
    assert 0x18 in _fc_kumesi(cerceveler), "LAN prosedurunde FC=24 RECORD_CURRENT_TIME beklenirdi"


# ==========================================================================
# AB / AC — NONLAN PROSEDURU: HORSTMANN PROFILINE UYAN YOL
# ==========================================================================


def test_ab_nonlan_fc23_ve_g50v1_ile_senkronize_eder() -> None:
    """POZITIF: NEED_TIME -> FC=23 DELAY_MEASUREMENT -> G50V1 WRITE -> clear.

    Horstmann resmi Device Profile'i FC=23 DELAY MEASUREMENT ve G50V1 TIME
    AND DATE ILAN EDER. Bu test `DNP3_TIME_SYNC=nonlan` yolunun tam olarak
    bu ikisini urettigini GERCEK bir outstation'a karsi gosterir.
    """
    os_app, cerceveler = _time_sync_kosusu("nonlan")

    # 1) API kaniti: saat GERCEKTEN yazildi.
    assert os_app.yazilan_zamanlar, "nonlan prosedurunde NEED_TIME'a ragmen saat YAZILMADI"

    # 2) FC=23 DELAY MEASUREMENT tel uzerinde.
    fclar = _fc_kumesi(cerceveler)
    assert mod._HORSTMANN_TIME_SYNC_FC in fclar, (
        f"FC=23 DELAY_MEASUREMENT gonderilmedi; gorulen FC'ler: {sorted(fclar)}"
    )

    # 3) Yazilan nesne G50V1 (profilin ilan ettigi varyasyon).
    varyasyonlar = _g50_varyasyonlari(cerceveler)
    assert varyasyonlar == [mod._HORSTMANN_TIME_OBJECT[1]], f"nonlan G50V1 yazmali; olculen: {varyasyonlar}"

    # 4) NEED_TIME senkronizasyondan SONRA temizlendi.
    assert os_app.need_time is False, "nonlan sonrasi NEED_TIME temizlenmedi"


def test_ac_nonlan_yolunda_fc24_ve_g50v3_hic_gonderilmez() -> None:
    """NEGATIF: profilde ILAN EDILMEYEN prosedur/nesne TELE CIKMAZ.

    Horstmann Implementation Table'inda 24 RECORD CURRENT TIME ve G50V3
    YOKTUR. `nonlan` seciliyken bunlardan biri bile gorunuyorsa,
    "profil-uyumlu prosedur" iddiasi COKER.
    """
    _os_app, cerceveler = _time_sync_kosusu("nonlan")

    fclar = _fc_kumesi(cerceveler)
    assert 0x18 not in fclar, (
        f"nonlan yolunda FC=24 RECORD_CURRENT_TIME gonderildi — profil disi; FC'ler: {sorted(fclar)}"
    )

    tum_g50 = sorted({v for c in cerceveler for (g, v) in _fc_ve_objeler(c)[1] if g == 50})
    assert 3 not in tum_g50, f"nonlan yolunda G50V3 gorundu — profil disi; G50 varyasyonlari: {tum_g50}"


# ==========================================================================
# AD — FAIL-CLOSED: cozulemeyen prosedur BASKA BIR PROSEDURE DUSMEZ
# ==========================================================================


def _sync_modu_adi(cfg) -> str:
    return str(cfg.master.timeSyncMode).rsplit(".", 1)[-1]


def test_ad_binding_enum_adlari_olculdu_varsayilmadi() -> None:
    """`TimeSyncMode` uyeleri OLCULUR; aday listeleri buna gore pinlenir."""
    uyeler = {ad for ad in dir(opendnp3.TimeSyncMode) if not ad.startswith("_")}
    assert {"LAN", "NonLAN", "None"} <= uyeler, f"binding enum uyeleri degismis: {sorted(uyeler)}"

    haritalar = mod._ManagedMaster._TIME_SYNC_ENUM_ADAYLARI
    assert set(haritalar) == {"lan", "nonlan", "none"}
    # Her prosedurun adaylari YALNIZCA kendi ailesinden olmali — aksi halde
    # eksik bir uye sessizce BASKA bir prosedure dusurur.
    assert haritalar["lan"] == ("LAN",)
    assert "LAN" not in haritalar["nonlan"], "nonlan adaylari arasinda duz 'LAN' var — fail-open"
    assert not (set(haritalar["nonlan"]) & set(haritalar["none"]))


@pytest.mark.parametrize(("mode", "beklenen"), [("lan", "LAN"), ("nonlan", "NonLAN"), ("none", "None")])
def test_ad_uc_prosedur_de_dogru_enum_uyesine_cozulur(mode: str, beklenen: str) -> None:
    cfg = opendnp3.MasterStackConfig()
    mod._ManagedMaster._apply_time_sync(cfg, mode)
    assert _sync_modu_adi(cfg) == beklenen, f"{mode} -> {_sync_modu_adi(cfg)} (beklenen {beklenen})"


@pytest.mark.parametrize("mode", ["nonlan1", "LANN", "auto", "off"])
def test_ad_taninmayan_deger_lan_sayilmaz(mode: str, caplog) -> None:
    """FAIL-CLOSED: eskiden taninmayan HER deger LAN'a dusuyordu.

    Bir yazim hatasi, operatore hicbir sey soylemeden, cihazin ilan ETMEDIGI
    bir prosedurle saat yazdirabiliyordu. (`off`/`disabled` takma adlari
    `Settings` dogrulayicisinda `none`a normalize edilir; adapter'a HAM
    gelirlerse artik prosedur SECMEZLER.)
    """
    cfg = opendnp3.MasterStackConfig()
    varsayilan = _sync_modu_adi(cfg)
    with caplog.at_level("ERROR"):
        mod._ManagedMaster._apply_time_sync(cfg, mode)
    assert _sync_modu_adi(cfg) == varsayilan, "taninmayan deger bir prosedur SECTI"
    assert any("yadnp3_time_sync_invalid" in r.message for r in caplog.records), (
        "gecersiz deger SESSIZCE yutuldu — ERROR bekleniyor"
    )


def test_ad_enum_uyesi_yoksa_diger_prosedure_dusmez(monkeypatch, caplog) -> None:
    """Binding'de `NonLAN` olmadigini taklit et: LAN'a DUSMEMELI."""

    class _EksikEnum:
        LAN = opendnp3.TimeSyncMode.LAN
        # NonLAN / NonLan / NONLAN / SerialTimeSync BILEREK yok.

    monkeypatch.setattr(mod.opendnp3, "TimeSyncMode", _EksikEnum, raising=True)
    cfg = opendnp3.MasterStackConfig()
    varsayilan = _sync_modu_adi(cfg)
    with caplog.at_level("ERROR"):
        mod._ManagedMaster._apply_time_sync(cfg, "nonlan")
    assert _sync_modu_adi(cfg) == varsayilan, (
        "NonLAN yokken BASKA bir prosedur secildi — yanlis prosedurle saat yazmak, hic yazmamaktan kotudur"
    )
    assert any("yadnp3_time_sync_enum_not_found" in r.message for r in caplog.records)


def test_z_g50_varyasyonu_profille_karsilastirilir() -> None:
    """P0 BULGUSU — OLCULDU, VARSAYILMADI.

    Resmi Horstmann Device Profile TIME AND DATE icin **G50V1**'i ilan eder
    (Function 2 WRITE, qualifier 07, quantity 1) ve fonksiyon kodu listesinde
    **23 DELAY MEASUREMENT** vardir. **24 RECORD CURRENT TIME** ve **G50V3**
    profilde YOKTUR.

    OLCULEN (yadnp3 3.2.1.1):

        timeSyncMode=LAN     -> FC=24 RECORD_CURRENT_TIME + WRITE **G50V3**
        timeSyncMode=NonLAN  -> FC=23 DELAY_MEASUREMENT   + WRITE **G50V1**

    ASIMETRI (kararin dayanagi): G50V1 profilde ACIKCA destekleniyor,
    G50V3 ise ILAN EDILMEMIS. Dolayisiyla `NonLAN` her iki varsayim altinda
    da GUVENLIDIR; `LAN` yalnizca dogrulanmamis varsayim altinda guvenlidir.

    COZUM (1.15.1): `DNP3_TIME_SYNC` `lan | nonlan | none` olarak
    genisletildi ve Horstmann kurulumlarinda `nonlan` verilmesi dokumante
    edildi. GENEL VARSAYILAN `lan` KALDI — varsayilani degistirmek Horstmann
    olmayan her kurulumun prosedurunu degistirirdi. Model bazli otomatik
    secim YAPILMADI: `DeviceConfig`te kanonik `model` alani yok, `signal_profile`
    ise sozlesme geregi gateway tarafindan ANLAMLANDIRILMAZ; ona bakmak bir
    string heuristic'i olurdu.
    """
    lan = opendnp3.MasterStackConfig()
    mod._ManagedMaster._apply_time_sync(lan, "lan")
    assert str(lan.master.timeSyncMode).endswith("LAN")

    nonlan = opendnp3.MasterStackConfig()
    mod._ManagedMaster._apply_time_sync(nonlan, "nonlan")
    assert str(nonlan.master.timeSyncMode).endswith("NonLAN")

    # Profilde ilan edilen degerler — sabit olarak pinlenir.
    assert mod._HORSTMANN_TIME_OBJECT == (50, 1)
    assert mod._HORSTMANN_TIME_SYNC_FC == 23


def test_delay_measurement_nonlan_prosedurune_aittir() -> None:
    """`DELAY MEASUREMENT` (FC=23) hangi prosedurun parcasi — OLCULDU.

    Bu testin eski adi/aciklamasi FC=23'u LAN akisina baglıyordu; OLCUM
    bunun yanlis oldugunu gosterdi (bkz. AB/AC testleri): FC=23
    **NonLAN** prosedurune aittir, LAN ise FC=24 RECORD_CURRENT_TIME
    gonderir. Horstmann profili FC=23 ilan ettigi icin dogru secim
    `nonlan`dir.

    Burada yalnizca prosedur -> enum cozumlemesi pinlenir; tel uzerindeki
    kanit AB/AC testlerindedir.
    """
    cfg = opendnp3.MasterStackConfig()
    assert str(cfg.master.timeSyncMode).endswith("None"), "kutuphane varsayilani degismis"
    mod._ManagedMaster._apply_time_sync(cfg, "nonlan")
    assert str(cfg.master.timeSyncMode).endswith("NonLAN"), (
        f"timeSyncMode NonLAN'a ayarlanmadi: {cfg.master.timeSyncMode}"
    )
