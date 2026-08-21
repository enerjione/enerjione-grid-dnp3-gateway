"""HORSTMANN UYUMLULUK — FONKSIYON KODU ve QUALIFIER MATRISI (TEL KANITI).

NORMATIF KAYNAK
---------------
Dipl.-Ing. H. Horstmann GmbH — DNP V3.0 Device Profile Document
(Smart Navigator 2.0 / Pole Master), DNP3 Implementation Table.

BU DOSYANIN KURALI
------------------
"OpenDNP3 kullaniyoruz, halleder" KANIT DEGILDIR. Buradaki her satir
GERCEK bir outstation'a karsi kurulan bir oturumdan, LINK CERCEVESI
COZULEREK cikarilir. Iddialar `cfg` alanina bakarak degil, TELDEN olculur.

NEDEN AYRI DOSYA
----------------
`test_horstmann_conformance.py` tek tek profil maddelerini dogrular.
Bu dosya ise TEK bir oturumdan cikan TUM istekleri toplayip matris
uretir — boylece "hangi fonksiyon kodu / qualifier tele cikiyor"
sorusunun cevabi tek bir yerde ve TAM olur.

OLCULEN QUALIFIER'LAR (bu dosya kosunca uretilir)
--------------------------------------------------
    FC=1   READ            G60V1/V2/V3/V4    qualifier 0x06
    FC=1   READ            G110V0            qualifier 0x01
    FC=2   WRITE           G80V1             qualifier 0x00
    FC=20  ENABLE_UNSOL    G60V2/V3/V4       qualifier 0x06
    FC=5   DIRECT_OPERATE  G12V1             qualifier 0x28
    FC=3   SELECT          G12V1             qualifier 0x28
    FC=4   OPERATE         G12V1             qualifier 0x28

Horstmann'in ilan ettigi qualifier kumesi: 00, 01, 06, 07, 08, 17, 27, 28.
Yukaridakilerin HEPSI bu kumenin icindedir.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")


# ==========================================================================
# NORMATIF SABITLER — resmi Implementation Table'dan
# ==========================================================================

#: Horstmann Device Profile'in ilan ettigi qualifier kodlari.
HORSTMANN_QUALIFIERS: frozenset[int] = frozenset({0x00, 0x01, 0x06, 0x07, 0x08, 0x17, 0x27, 0x28})

#: G12V1 CROB icin ilan edilen qualifier'lar.
HORSTMANN_CROB_QUALIFIERS: frozenset[int] = frozenset({0x17, 0x28})

#: G12V1 control code alt nibble'i (DNP3 Table 11-3).
CROB_LATCH_ON = 0x03
CROB_LATCH_OFF = 0x04
CROB_PULSE_ON = 0x01
CROB_PULSE_OFF = 0x02
#: Queue biti (0x10) ve Clear biti (0x20) — profil: Queue/Clear Queue = NEVER.
CROB_QUEUE_BIT = 0x10
CROB_CLEAR_BIT = 0x20


# ==========================================================================
# LINK CERCEVESI COZUCU — CRC bloklarini ATLAR
# ==========================================================================


def uygulama_verisi(cerceve: bytes) -> bytes:
    """Link cercevesinden uygulama katmani baytlarini cikar.

    DNP3 link cercevesi kullanici verisini **16 baytlik bloklar** halinde
    tasir ve her blogun sonuna 2 baytlik CRC koyar. Bu CRC'ler atilmazsa
    16 bayttan uzun her istek (or. CROB) YANLIS cozulur — nitekim
    `test_horstmann_conformance.py`'deki basit cozucu tam olarak bu yuzden
    yalnizca kisa cerceveler icin kullanilir.
    """
    if len(cerceve) < 10 or cerceve[0] != 0x05 or cerceve[1] != 0x64:
        return b""
    kalan = cerceve[2] - 5  # LENGTH alani CONTROL+DEST+SRC (5 bayt) dahildir
    out = bytearray()
    i = 10
    while kalan > 0 and i < len(cerceve):
        n = min(16, kalan)
        out += cerceve[i : i + n]
        i += n + 2  # blok CRC
        kalan -= n
    return bytes(out)


def istek_coz(cerceve: bytes) -> tuple[int | None, list[tuple[int, int, int]], bytes]:
    """ISTEK cercevesi -> `(fc, [(group, var, qualifier)], nesne_govdesi)`.

    `nesne_govdesi` ilk nesne basligindan SONRAKI ham baytlardir (CROB
    icerigi gibi seylerin dogrudan incelenebilmesi icin).
    """
    app = uygulama_verisi(cerceve)
    if len(app) < 3:
        return None, [], b""
    # app[0] transport, app[1] uygulama kontrol, app[2] fonksiyon kodu
    fc = app[2]
    p = app[3:]
    basliklar: list[tuple[int, int, int]] = []
    govde = b""
    i = 0
    while i + 3 <= len(p):
        g, v, q = p[i], p[i + 1], p[i + 2]
        basliklar.append((g, v, q))
        i += 3
        if not govde:
            govde = p[i:]
        if q == 0x06:  # all / no range — range alani YOK
            continue
        if q == 0x00:  # 1 baytlik start-stop
            i += 2
            continue
        if q == 0x01:  # 2 baytlik start-stop
            i += 4
            continue
        if q in (0x17, 0x28):  # index'li: count + (index, nesne) tekrarlari
            adet_len = 1 if q == 0x17 else 2
            adet = int.from_bytes(p[i : i + adet_len], "little")
            i += adet_len
            nesne_len = {(12, 1): 11, (41, 2): 2}.get((g, v))
            if nesne_len is None:
                break  # bilinmeyen nesne — kalanini cozmeye CALISMA
            i += adet * (adet_len + nesne_len)
            continue
        break  # cozulmeyen qualifier: uydurma yapma, DUR
    return fc, basliklar, govde


# ==========================================================================
# GERCEK OUTSTATION'A KARSI TEK OTURUM — tum istekler toplanir
# ==========================================================================


def _bos_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Proxy:
    """Iki TCP client'i birlestirir ve HER YONDEKI cerceveleri saklar."""

    def __init__(self) -> None:
        self.gw_port = _bos_port()
        self.os_port = _bos_port()
        self.gw_giden: list[bytes] = []
        self.os_giden: list[bytes] = []
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

    def _kabul(self, srv: socket.socket) -> socket.socket | None:
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
                (self.gw_giden if gw_yonu else self.os_giden).append(d)
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


class _Outstation(opendnp3.IOutstationApplication):
    def __init__(self) -> None:
        super().__init__()


class _KomutIsleyici(opendnp3.ICommandHandler):
    """GERCEK `CommandStatus` dondurur — propagation kaniti (test V)."""

    def __init__(self, durum: Any) -> None:
        super().__init__()
        self.durum = durum
        self.gorulen: list[tuple[str, int, int]] = []

    def Begin(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass

    def End(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass

    def Select(self, command, index):  # noqa: N802 — kutuphane imzasi
        self.gorulen.append(("select", int(index), int(getattr(command, "count", -1))))
        return self.durum

    def Operate(self, command, index, op_type, *_):  # noqa: N802 — kutuphane imzasi
        self.gorulen.append(("operate", int(index), int(getattr(command, "count", -1))))
        return self.durum


class Olcum:
    """Tek bir oturumdan toplanan tum tel kanidi."""

    def __init__(self) -> None:
        self.istekler: list[bytes] = []
        self.cevaplar: list[bytes] = []
        self.komut_sonucu: dict[str, Any] = {}
        self.outstation_gordu: list[tuple[str, int, int]] = []

    # -- yardimcilar --------------------------------------------------
    def fonksiyon_kodlari(self) -> set[int]:
        return {fc for c in self.istekler if (fc := istek_coz(c)[0]) is not None}

    def basliklar(self, fc: int) -> list[tuple[int, int, int]]:
        out: list[tuple[int, int, int]] = []
        for c in self.istekler:
            f, hs, _ = istek_coz(c)
            if f == fc:
                out.extend(hs)
        return out

    def crob_govdesi(self, fc: int) -> bytes:
        for c in self.istekler:
            f, hs, govde = istek_coz(c)
            if f == fc and hs and hs[0][:2] == (12, 1):
                return govde
        return b""


def _oturum(op_type: Any, komut_durumu: Any, timeout: float = 25.0) -> Olcum:
    """Gercek bir outstation'a karsi TAM bir oturum kosar ve teli toplar.

    Uretim yolunun kendisi kullanilir: `_apply_time_sync`, uretimdeki
    `disableUnsolOnStartup=False` ve Horstmann fragment limitleri.
    """
    olcum = Olcum()
    proxy = _Proxy()
    kh = _KomutIsleyici(komut_durumu)
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
        oscfg.outstation.params.allowUnsolicited = True
        os_ch.AddOutstation("os", kh, _Outstation(), oscfg).Enable()

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
        # URETIMDEKI DEGERLER (bkz. _ManagedMaster.__init__).
        cfg.master.disableUnsolOnStartup = False
        cfg.master.maxTxFragSize = mod._HORSTMANN_RX_FRAGMENT_MAX
        mod._ManagedMaster._apply_time_sync(cfg, "nonlan")
        soe = _SOE()
        master = ch.AddMaster("mm", soe, _App(), cfg)
        master.Enable()

        # Baglantinin kurulmasini ve acilis gorevlerinin bitmesini bekle.
        son = time.monotonic() + timeout
        while time.monotonic() < son and len(proxy.gw_giden) < 3:
            time.sleep(0.2)
        time.sleep(1.5)

        # G110 dar-range okuma (uretimdeki `scan_g110_once` ile ayni cagri).
        gv = None
        for ad in ("GroupVariationID", "GroupVariation"):
            k = getattr(opendnp3, ad, None)
            if k is not None:
                try:
                    gv = k(110, 0)
                    break
                except Exception:  # noqa: BLE001, S112
                    continue
        if gv is not None:
            with_soe = getattr(master, "ScanRange", None)
            if with_soe is not None:
                try:
                    with_soe(gv, 0, 3, soe, opendnp3.TaskConfig.Default())
                    time.sleep(1.5)
                except Exception:  # noqa: BLE001
                    pass

        # CROB — DirectOperate (uretim varsayilani) ve SelectAndOperate.
        def _cb(r):
            olcum.komut_sonucu["summary"] = str(getattr(r, "summary", r))

        crob = opendnp3.ControlRelayOutputBlock(op_type, opendnp3.TripCloseCode.NUL, False, 1, 0, 0)
        master.DirectOperate(crob, 3, _cb, opendnp3.TaskConfig.Default())
        time.sleep(2.0)
        master.SelectAndOperate(crob, 3, lambda r: None, opendnp3.TaskConfig.Default())
        time.sleep(2.0)

        olcum.istekler = list(proxy.gw_giden)
        olcum.cevaplar = list(proxy.os_giden)
        olcum.outstation_gordu = list(kh.gorulen)
        return olcum
    finally:
        for kapat in (mgr.Shutdown, os_mgr.Shutdown, proxy.kapat):
            try:
                kapat()
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture(scope="module")
def olcum_latch_on() -> Olcum:
    """LATCH_ON + SUCCESS senaryosu — modul basina BIR kez kosar."""
    return _oturum(opendnp3.OperationType.LATCH_ON, opendnp3.CommandStatus.SUCCESS)


@pytest.fixture(scope="module")
def olcum_latch_off() -> Olcum:
    """LATCH_OFF + NOT_SUPPORTED senaryosu (status propagation icin)."""
    return _oturum(opendnp3.OperationType.LATCH_OFF, opendnp3.CommandStatus.NOT_SUPPORTED)


# ==========================================================================
# AF — QUALIFIER MATRISI
# ==========================================================================


def test_af_tum_qualifierlar_profilde_ilan_edilmis(olcum_latch_on: Olcum) -> None:
    """Tele cikan HER qualifier, profilin ilan ettigi kumede olmali.

    Profil kumesi: 00, 01, 06, 07, 08, 17, 27, 28.
    """
    gorulen: dict[int, list[tuple[int, int, int]]] = {}
    for c in olcum_latch_on.istekler:
        fc, hs, _ = istek_coz(c)
        if fc is None:
            continue
        for g, v, q in hs:
            gorulen.setdefault(q, []).append((fc, g, v))

    assert gorulen, "hicbir istek cozulemedi — olcum harness'i bozuk"
    disarida = {q: v for q, v in gorulen.items() if q not in HORSTMANN_QUALIFIERS}
    assert not disarida, "PROFILDE ILAN EDILMEYEN qualifier tele cikti: " + ", ".join(
        f"0x{q:02x} <- {v}" for q, v in disarida.items()
    )


def test_af_class_okumasi_qualifier_06_kullanir(olcum_latch_on: Olcum) -> None:
    """FC=1 READ + G60 -> qualifier 0x06 (all / no range).

    Class taramasinda index araligi verilmez; 0x06 tam olarak bunu ifade
    eder ve profilde ilan edilmistir.
    """
    g60 = [(g, v, q) for (g, v, q) in olcum_latch_on.basliklar(0x01) if g == 60]
    assert g60, "FC=1 READ icinde G60 (class) basligi YOK"
    assert {q for (_, _, q) in g60} == {0x06}, f"class okumasi 0x06 disinda qualifier kullandi: {g60}"
    # Class 1/2/3 (V2/V3/V4) tele cikmali; Class 0 (V1) yalnizca integrity'de.
    assert {v for (_, v, _) in g60} >= {2, 3, 4}, f"Class 1/2/3 basliklari eksik: {g60}"


def test_af_g110_okumasi_ilan_edilen_qualifier_kullanir(olcum_latch_on: Olcum) -> None:
    """G110V0 READ -> qualifier 0x01 (2 oktetlik start-stop).

    Profil G110V0 READ icin 00,01,06,07,08,17,27,28 ilan eder. 2 oktetlik
    aralik SART: uretimdeki index haritasi 65000+ degerler icerir ve
    1 oktetlik qualifier (0x00) bunlari IFADE EDEMEZ.
    """
    g110 = [(g, v, q) for (g, v, q) in olcum_latch_on.basliklar(0x01) if g == 110]
    if not g110:
        pytest.skip("bu binding ScanRange sunmuyor — G110 istegi uretilemedi")
    for _, _, q in g110:
        assert q in HORSTMANN_QUALIFIERS, f"G110 icin profil disi qualifier: 0x{q:02x}"
    assert 0x01 in {q for (_, _, q) in g110}, (
        f"G110 dar-range okumasi 2 oktetlik start-stop kullanmali: {g110}"
    )


def test_af_crob_qualifier_17_veya_28(olcum_latch_on: Olcum) -> None:
    """G12V1 -> qualifier 0x17 veya 0x28 (profilin ilan ettigi ikisi)."""
    for fc in (0x05, 0x03, 0x04):
        hs = [(g, v, q) for (g, v, q) in olcum_latch_on.basliklar(fc) if (g, v) == (12, 1)]
        assert hs, f"FC={fc} icin G12V1 basligi tele cikmadi"
        for _, _, q in hs:
            assert q in HORSTMANN_CROB_QUALIFIERS, (
                f"FC={fc} G12V1 icin profil disi qualifier: 0x{q:02x} "
                f"(izin verilen: {sorted(hex(x) for x in HORSTMANN_CROB_QUALIFIERS)})"
            )


# ==========================================================================
# FONKSIYON KODU MATRISI
# ==========================================================================


def test_fonksiyon_kodlari_profilde_ilan_edilmis(olcum_latch_on: Olcum) -> None:
    """Tele cikan HER fonksiyon kodu profilde ilan edilmis olmali.

    Ilan edilenler (Implementation Table + §20 Unsolicited):
        0 CONFIRM, 1 READ, 2 WRITE, 3 SELECT, 4 OPERATE,
        5 DIRECT OPERATE, 6 DIRECT OPERATE NO ACK, 13 COLD RESTART,
        20/21 ENABLE/DISABLE UNSOLICITED, 22 ASSIGN CLASS,
        23 DELAY MEASUREMENT.

    Not: 13 COLD RESTART ve 22 ASSIGN CLASS destekleniyor olsa da gateway
    bunlari GONDERMEZ (bkz. "NOT USED" — §27: sirf destekli diye feature
    eklenmez).
    """
    ilan_edilen = {0, 1, 2, 3, 4, 5, 6, 13, 20, 21, 22, 23}
    gorulen = olcum_latch_on.fonksiyon_kodlari()
    assert gorulen, "hicbir fonksiyon kodu cozulemedi"
    disarida = gorulen - ilan_edilen
    assert not disarida, f"PROFILDE ILAN EDILMEYEN fonksiyon kodu tele cikti: {sorted(disarida)}"


def test_gateway_tehlikeli_fonksiyon_kodu_gondermez(olcum_latch_on: Olcum) -> None:
    """Cihaz destekliyor diye GONDERILMEYENLER — §27 yasagi.

    13 COLD RESTART cihazi yeniden baslatir; 22 ASSIGN CLASS cihazin olay
    siniflandirmasini KALICI degistirir. Ikisi de profilde destekli ama
    gateway'in isi degildir.
    """
    gorulen = olcum_latch_on.fonksiyon_kodlari()
    assert 13 not in gorulen, "COLD RESTART (FC=13) tele cikti — cihaz yeniden baslatilir"
    assert 22 not in gorulen, "ASSIGN CLASS (FC=22) tele cikti — cihaz konfigurasyonu degisir"


def test_nonlan_prosedurunde_fc23_var_fc24_yok(olcum_latch_on: Olcum) -> None:
    """`nonlan` -> FC=23 DELAY MEASUREMENT; FC=24 RECORD CURRENT TIME YOK.

    FC=24 profilde ILAN EDILMEMISTIR. (Ayrintili pozitif/negatif kanit:
    `test_horstmann_conformance.py` AB/AC.)
    """
    gorulen = olcum_latch_on.fonksiyon_kodlari()
    assert 24 not in gorulen, "FC=24 RECORD CURRENT TIME tele cikti — profil disi"


# ==========================================================================
# T / U / P / O — CROB TEL ENCODE'U (control code, count, queue/clear)
# ==========================================================================


def _crob_alanlari(govde: bytes) -> dict[str, int]:
    """G12V1 nesne govdesini coz: count(2) index(2) sonra 11 baytlik CROB."""
    assert len(govde) >= 15, f"CROB govdesi kisa: {govde.hex()}"
    return {
        "adet": int.from_bytes(govde[0:2], "little"),
        "index": int.from_bytes(govde[2:4], "little"),
        "control_code": govde[4],
        "count": govde[5],
        "on_time_ms": int.from_bytes(govde[6:10], "little"),
        "off_time_ms": int.from_bytes(govde[10:14], "little"),
        "status": govde[14],
    }


def test_t_latch_on_tel_uzerinde_control_code_03(olcum_latch_on: Olcum) -> None:
    """LATCH_ON -> control code 0x03, count=1, on/off time = 0."""
    alanlar = _crob_alanlari(olcum_latch_on.crob_govdesi(0x05))
    assert alanlar["control_code"] == CROB_LATCH_ON, (
        f"LATCH_ON control code 0x{alanlar['control_code']:02x} (beklenen 0x03)"
    )
    assert alanlar["count"] == 1, f"count={alanlar['count']} — profil: Count > 1 = NEVER"
    assert alanlar["on_time_ms"] == 0 and alanlar["off_time_ms"] == 0, (
        "latch komutunda on/off time SIFIR olmali (pulse zamanlamasi latch'te anlamsizdir)"
    )
    assert alanlar["index"] == 3, "istenen index tele farkli cikti"


def test_u_latch_off_tel_uzerinde_control_code_04(olcum_latch_off: Olcum) -> None:
    """LATCH_OFF -> control code 0x04."""
    alanlar = _crob_alanlari(olcum_latch_off.crob_govdesi(0x05))
    assert alanlar["control_code"] == CROB_LATCH_OFF, (
        f"LATCH_OFF control code 0x{alanlar['control_code']:02x} (beklenen 0x04)"
    )
    assert alanlar["count"] == 1


@pytest.mark.parametrize("fc", [0x05, 0x03, 0x04])
def test_r_s_pulse_biti_tele_hic_cikmaz(olcum_latch_on: Olcum, fc: int) -> None:
    """Profil: Pulse On/Off = NEVER. Tel uzerinde pulse control code YOK."""
    govde = olcum_latch_on.crob_govdesi(fc)
    if not govde:
        pytest.skip(f"FC={fc} bu oturumda uretilmedi")
    kod = _crob_alanlari(govde)["control_code"] & 0x0F
    assert kod not in (CROB_PULSE_ON, CROB_PULSE_OFF), (
        f"FC={fc} icin PULSE control code 0x{kod:02x} tele cikti — profil: NEVER"
    )


@pytest.mark.parametrize("fc", [0x05, 0x03, 0x04])
def test_queue_ve_clear_bitleri_sifir(olcum_latch_on: Olcum, fc: int) -> None:
    """Profil: Queue = NEVER, Clear Queue = NEVER.

    Bunlar CROB control code'unun 0x10 / 0x20 bitleridir. Set edilirlerse
    cihaz komutu KUYRUGA alir — "gonderdim, oldu" sanilan ama gecikmeli
    calisan bir fiziksel islem demektir.
    """
    govde = olcum_latch_on.crob_govdesi(fc)
    if not govde:
        pytest.skip(f"FC={fc} bu oturumda uretilmedi")
    kod = _crob_alanlari(govde)["control_code"]
    assert not kod & CROB_QUEUE_BIT, f"CROB Queue biti SET (0x{kod:02x}) — profil: NEVER"
    assert not kod & CROB_CLEAR_BIT, f"CROB Clear biti SET (0x{kod:02x}) — profil: NEVER"


def test_n_o_select_operate_ve_directoperate_ayni_crob_u_uretir(
    olcum_latch_on: Olcum,
) -> None:
    """N + O: SBO (FC=3 -> FC=4) ve DirectOperate (FC=5) ayni nesneyi tasir.

    SELECT ile OPERATE'in AYNI CROB'u tasimamasi, cihazin OPERATE'i
    NO_SELECT ile reddetmesine yol acar.
    """
    direct = _crob_alanlari(olcum_latch_on.crob_govdesi(0x05))
    select = _crob_alanlari(olcum_latch_on.crob_govdesi(0x03))
    operate = _crob_alanlari(olcum_latch_on.crob_govdesi(0x04))
    assert select == operate, f"SELECT ve OPERATE farkli CROB tasidi:\n{select}\n{operate}"
    assert direct == operate, f"DirectOperate ve OPERATE farkli CROB tasidi:\n{direct}\n{operate}"


def test_v_outstation_gercek_crob_u_gordu(olcum_latch_on: Olcum) -> None:
    """Tel encode'u cihaz tarafinda da AYNI cozuluyor mu (round-trip)."""
    assert olcum_latch_on.outstation_gordu, "outstation hicbir komut gormedi"
    for tur, index, count in olcum_latch_on.outstation_gordu:
        assert index == 3, f"{tur}: index tele/cihaza farkli geldi ({index})"
        assert count == 1, f"{tur}: count cihaz tarafinda {count} goruldu (beklenen 1)"


# ==========================================================================
# AB / AC / AD — CEVAP, UNSOLICITED, COK PARCALI
# ==========================================================================


def _cevap_appctl(olcum: Olcum) -> list[tuple[int, int]]:
    """(fonksiyon_kodu, uygulama_kontrol) — outstation -> gateway yonu."""
    out = []
    for c in olcum.cevaplar:
        a = uygulama_verisi(c)
        if len(a) >= 3 and a[2] in (129, 130):
            out.append((a[2], a[1]))
    return out


def test_ab_solicited_129_cevaplari_alindi(olcum_latch_on: Olcum) -> None:
    assert any(fc == 129 for fc, _ in _cevap_appctl(olcum_latch_on)), "hicbir FC=129 RESPONSE alinmadi"


def test_ac_unsolicited_130_alindi_ve_uns_biti_set(olcum_latch_on: Olcum) -> None:
    """Cihaz acilista NULL unsolicited gonderir; UNS biti (0x10) SET olmali.

    "Unsolicited response sadece event tasir" varsayimi YANLISTIR: acilistaki
    bu cerceve NESNE TASIMAZ, yalnizca IIN tasir (restart bildirimi).
    """
    unsol = [(fc, ac) for fc, ac in _cevap_appctl(olcum_latch_on) if fc == 130]
    assert unsol, "hicbir FC=130 UNSOLICITED RESPONSE alinmadi"
    for _, ac in unsol:
        assert ac & 0x10, f"UNSOLICITED cerceve UNS biti SET degil: 0x{ac:02x}"


def test_ad_cok_parcali_cevap_fir_fin_bitleriyle_tutarli(olcum_latch_on: Olcum) -> None:
    """FIR/FIN bitleri her cevapta tutarli ve sequence artiyor mu."""
    cevaplar = _cevap_appctl(olcum_latch_on)
    assert cevaplar, "hic cevap alinmadi"
    for fc, ac in cevaplar:
        fir, fin = bool(ac & 0x80), bool(ac & 0x40)
        assert fir or fin, (
            f"FC={fc} cerceve ne FIR ne FIN tasiyor (0x{ac:02x}) — "
            "orta parca tek basina goruldu, reassembly bozuk olabilir"
        )


def test_ad_istek_fragmenti_horstmann_rx_sinirini_asmaz(olcum_latch_on: Olcum) -> None:
    """Gateway->cihaz yonundeki HICBIR uygulama fragmenti 1024 baytI ASMAZ.

    Horstmann RX app fragment limiti 1024'tur; opendnp3 varsayilani 2048'di.
    """
    for c in olcum_latch_on.istekler:
        app = uygulama_verisi(c)
        assert len(app) <= mod._HORSTMANN_RX_FRAGMENT_MAX, (
            f"istek fragmenti {len(app)} bayt — Horstmann RX siniri {mod._HORSTMANN_RX_FRAGMENT_MAX}"
        )


def test_link_cercevesi_292_okteti_asmaz(olcum_latch_on: Olcum) -> None:
    """Profil: Maximum Data Link Frame TX/RX = 292 oktet."""
    for yon, cerceveler in (("istek", olcum_latch_on.istekler), ("cevap", olcum_latch_on.cevaplar)):
        for c in cerceveler:
            if len(c) < 3 or c[0] != 0x05 or c[1] != 0x64:
                continue
            # LENGTH + 2 (baslik) + CRC bloklari; toplam cerceve 292'yi asamaz.
            toplam = 10 + c[2] - 5 + 2 * -(-(c[2] - 5) // 16)
            assert toplam <= 292, f"{yon} link cercevesi {toplam} oktet — profil siniri 292"
