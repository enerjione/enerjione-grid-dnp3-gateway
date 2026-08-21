"""G22 COUNTER EVENT — AYRI AUDIT (uretim SOE handler'ina karsi OLCUM).

NEDEN AYRI DOSYA
----------------
DNP3 nesne semantigi birbirine KARISTIRILMAMALIDIR:

    G20 = Counter                G21 = Frozen Counter
    G22 = Counter Event          G23 = Frozen Counter Event

Horstmann Implementation Table G22V0 / G22V1 / G22V2 / G22V5 ILAN EDER ve
`horstmann_sn_2_0.json` profilindeki 6 sayac noktasinin HEPSI **Class 1**
tir — yani uretimde bu noktalar **G22 olayi** olarak gelir, G20 statigi
olarak DEGIL. Dolayisiyla G22 uretim-kritik bir yoldur ve tek basina
kanitlanmalidir.

BU DOSYA NEYI KANITLIYOR (hepsi GERCEK bir outstation'a karsi olculur)
---------------------------------------------------------------------
1. G22 Counter Event GERCEKTEN parse ediliyor,
2. callback tipi `opendnp3.Counter` (`FrozenCounter` DEGIL),
3. cache `object_group = 20` ile kaydediyor,
4. G22 olayi YANLISLIKLA G21'e YAZILMIYOR (G23 ayri kanitla ayirt ediliyor),
5. G22V5'te cihaz zaman damgasi KORUNUYOR, G22V1'de damga YOK sayiliyor,
6. counter flags dogru tasiniyor,
7. ROLLOVER / DISCONTINUITY `invalid` SAYILMIYOR (analog OVERRANGE ile
   ayni bitler olmasina RAGMEN).

HARNESS TUZAGI — BILEREK BELGELENDI
-----------------------------------
`OutstationStackConfig`in varsayilan `eventBufferConfig`i TUM TIPLER ICIN
**0**'dir. Bu ayarlanmazsa outstation hicbir olay URETMEZ ve test sessizce
"G22 gelmiyor" der — yani gateway'de olmayan bir kusur RAPOR EDILIRDI.
Ilk olcumde tam olarak bu oldu.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

G20 = mod._OBJECT_GROUP_COUNTER
G21 = mod._OBJECT_GROUP_FROZEN_COUNTER

#: Counter bayrak bitleri (DNP3): 5 = ROLLOVER, 6 = DISCONTINUITY.
#: ANALOG'da ayni bitler OVERRANGE / REFERENCE_ERR anlamina gelir — bu
#: dosyanin en kritik ayrimi budur.
FLAG_ONLINE = 0x01
FLAG_ROLLOVER = 0x20
FLAG_DISCONTINUITY = 0x40
FLAG_COUNTER_HEPSI = FLAG_ONLINE | FLAG_ROLLOVER | FLAG_DISCONTINUITY

#: `cache.get()` donusundeki alan sirasi.
RAW, METIN, FLAGS, DEVICE_TIME, TIME_QUALITY = range(5)

_DAMGA_MS = 1_755_600_000_000
_DAMGA_SN = 1_755_600_000.0

_IDX_SAYAC = 2  # yalnizca Counter olayi uretilecek index
_IDX_DONMUS = 3  # ayrica FREEZE edilecek index (G23 uretir)


def _bos_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Proxy:
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


class _Outstation(opendnp3.IOutstationApplication):
    def __init__(self) -> None:
        super().__init__()


class _KomutIsleyici(opendnp3.ICommandHandler):
    def __init__(self) -> None:
        super().__init__()

    def Begin(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass

    def End(self) -> None:  # noqa: N802 — kutuphane imzasi
        pass


class G22Olcumu:
    """Tek oturumdan toplanan G22 kaniti."""

    def __init__(self) -> None:
        #: (tip_adi, gv_adi, is_event, index, deger, flags, ts_ms, ts_quality)
        self.ham: list[tuple[str, str, bool, int, Any, int, int, str]] = []
        self.cache: mod._DeviceCache | None = None
        self.istek_fclari: set[int] = set()

    def olaylar(self, gv_onek: str) -> list[tuple]:
        return [k for k in self.ham if k[2] and k[1].startswith(gv_onek)]


def _oturum() -> G22Olcumu:
    olcum = G22Olcumu()
    proxy = _Proxy()
    cache = mod._DeviceCache()
    olcum.cache = cache
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
        oscfg = opendnp3.OutstationStackConfig(opendnp3.DatabaseConfig(8))
        # ZORUNLU (bkz. modul docstring'i): varsayilan olay tamponu 0'dir.
        oscfg.outstation.eventBufferConfig = opendnp3.EventBufferConfig.AllTypes(50)
        oscfg.outstation.params.allowUnsolicited = False
        oscfg.link.LocalAddr = 10
        oscfg.link.RemoteAddr = 1
        os_stack = os_ch.AddOutstation("os", _KomutIsleyici(), _Outstation(), oscfg)
        os_stack.Enable()

        # ---- URETIMDEKI GERCEK SOE HANDLER ----
        uretim_soe = mod._make_soe_handler(cache, "D1")

        class _Gozlemci(opendnp3.ISOEHandler):
            """Ham `HeaderInfo`yu kaydeder, sonra URETIM yolunu besler."""

            def __init__(self) -> None:
                super().__init__()

            def BeginFragment(self, info):  # noqa: N802 — kutuphane imzasi
                pass

            def EndFragment(self, info):  # noqa: N802 — kutuphane imzasi
                pass

            def OnDeviceAttribute(self, info, set_, variation, value):  # noqa: N802
                pass

            def Process(self, info, values):  # noqa: N802 — kutuphane imzasi
                gv = getattr(info, "gv", None)
                gv_ad = str(getattr(gv, "name", gv))
                is_ev = bool(getattr(info, "isEventVariation", False))
                for it in values:
                    v = it.value
                    t = getattr(v, "time", None)
                    olcum.ham.append(
                        (
                            type(v).__name__,
                            gv_ad,
                            is_ev,
                            it.index,
                            getattr(v, "value", None),
                            int(getattr(getattr(v, "flags", None), "value", -1)),
                            int(getattr(t, "value", 0) or 0),
                            str(getattr(t, "quality", "")),
                        )
                    )
                uretim_soe.Process(info, values)

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
        cfg.master.disableUnsolOnStartup = True
        soe = _Gozlemci()
        master = ch.AddMaster("mm", soe, _App(), cfg)
        master.Enable()
        time.sleep(4.0)

        bayrak = opendnp3.Flags(FLAG_COUNTER_HEPSI)

        # --- 1) Class 1/2/3 event scan (URETIMDEKI maske) ---
        u = opendnp3.UpdateBuilder()
        u.Update(opendnp3.Counter(4242, bayrak, opendnp3.DNPTime(_DAMGA_MS)), _IDX_SAYAC)
        u.Update(opendnp3.Counter(4242, bayrak, opendnp3.DNPTime(_DAMGA_MS)), _IDX_DONMUS)
        # FREEZE -> G23 (Frozen Counter Event) uretir. G22 ile G23'un AYRI
        # yollara gittigini gostermek icin bilincli.
        u.FreezeCounter(_IDX_DONMUS, False)
        os_stack.Apply(u.Build())
        time.sleep(1.5)
        master.ScanClasses(opendnp3.ClassField(False, True, True, True), soe, opendnp3.TaskConfig.Default())
        time.sleep(2.5)

        # --- 2) G22V5 (zaman damgali) ACIKCA iste ---
        u2 = opendnp3.UpdateBuilder()
        u2.Update(opendnp3.Counter(7777, bayrak, opendnp3.DNPTime(_DAMGA_MS)), _IDX_SAYAC)
        os_stack.Apply(u2.Build())
        time.sleep(1.0)
        master.Scan([opendnp3.Header.AllObjects(22, 5)], soe, opendnp3.TaskConfig.Default())
        time.sleep(2.5)
        olcum.g22v5_cache = cache.get(G20, _IDX_SAYAC)

        # --- 3) G22V1 (zaman damgasiz) ACIKCA iste ---
        u3 = opendnp3.UpdateBuilder()
        u3.Update(opendnp3.Counter(8888, bayrak, opendnp3.DNPTime(_DAMGA_MS)), _IDX_SAYAC)
        os_stack.Apply(u3.Build())
        time.sleep(1.0)
        master.Scan([opendnp3.Header.AllObjects(22, 1)], soe, opendnp3.TaskConfig.Default())
        time.sleep(2.5)
        olcum.g22v1_cache = cache.get(G20, _IDX_SAYAC)

        for c in proxy.gw_giden:
            if len(c) > 12 and c[0] == 0x05 and c[1] == 0x64:
                olcum.istek_fclari.add(c[12])
        return olcum
    finally:
        for kapat in (mgr.Shutdown, os_mgr.Shutdown, proxy.kapat):
            try:
                kapat()
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture(scope="module")
def olcum() -> G22Olcumu:
    return _oturum()


# ==========================================================================
# 1 / 2 — G22 PARSE EDILIYOR MU, HANGI TIPLE GELIYOR
# ==========================================================================


def test_g22_counter_event_gercekten_parse_ediliyor(olcum: G22Olcumu) -> None:
    """Class 1/2/3 event scan G22 tasiyor mu — TEL SEVIYESINDE."""
    g22 = olcum.olaylar("Group22")
    assert g22, (
        "hicbir G22 Counter Event alinmadi. "
        f"gorulen olay varyasyonlari: {sorted({k[1] for k in olcum.ham if k[2]})}"
    )
    assert any(k[4] == 4242 for k in g22), f"G22 degeri tasinmadi: {g22}"


def test_g22_callback_tipi_counter_frozencounter_degil(olcum: G22Olcumu) -> None:
    """G22 `opendnp3.Counter` ile gelir; `FrozenCounter` ile DEGIL.

    Ikisi AYRI Python tipidir (kalitim iliskisi YOKTUR) — yani
    `isinstance` dallanmasi birini digerine yanlislikla YOLLAYAMAZ.
    """
    for tip, gv, *_ in olcum.olaylar("Group22"):
        assert tip == "Counter", f"{gv} icin beklenmeyen olcum tipi: {tip}"
    assert not issubclass(opendnp3.FrozenCounter, opendnp3.Counter), (
        "FrozenCounter artik Counter'in alt sinifi — SOE dallanmasinin SIRASI "
        "kritik hale geldi, `isinstance(first, Counter)` frozen'i YUTAR"
    )
    assert not issubclass(opendnp3.Counter, opendnp3.FrozenCounter)


# ==========================================================================
# 3 / 4 — CACHE HANGI GRUBA YAZIYOR
# ==========================================================================


def test_g22_cache_e_group_20_olarak_yazilir(olcum: G22Olcumu) -> None:
    """G22 = Counter Event -> ayni NOKTANIN olay bicimi, yani grup 20.

    G22 icin ayri bir cache grubu acmak yanlis olurdu: G20 statigi ile G22
    olayi AYNI fiziksel sayaci anlatir; ayirmak backend'de ayni nokta icin
    iki farkli sinyal uretirdi.
    """
    kayit = olcum.cache.get(G20, _IDX_SAYAC)
    assert kayit is not None, "G22 olayi grup 20'ye HIC yazilmadi"
    assert kayit[RAW] in (4242.0, 7777.0, 8888.0), f"grup 20'deki deger beklenmedik: {kayit}"


def test_g22_yanlislikla_g21_e_yazilmaz(olcum: G22Olcumu) -> None:
    """EN KRITIK AYRIM: G22 (Counter Event) != G21/G23 (Frozen).

    Bu testte outstation'a AYRICA bir FREEZE uygulandi, yani G23 Frozen
    Counter Event de URETILDI.

    DIKKAT — "grup 21 bos olmali" DEMEK YANLIS OLURDU: acilis integrity
    poll'u G21 STATIKLERINI de okur ve her index icin bir kayit olusur.
    Dogru sinav su: G22 OLAYININ DEGERI grup 21'de GORUNMEMELI.
    """
    g22_degerleri = {4242.0, 7777.0, 8888.0}
    donmus = olcum.cache.get(G21, _IDX_SAYAC)
    if donmus is not None:
        assert donmus[RAW] not in g22_degerleri, (
            f"G22 Counter Event degeri ({donmus[RAW]}) FROZEN COUNTER grubuna sizdi — "
            "G22 ile G21 karistirilmis"
        )
        assert donmus[FLAGS] != FLAG_COUNTER_HEPSI, (
            "G22 olayinin bayraklari grup 21'e yazilmis — yol ayrimi bozuk"
        )

    # Ters yon: G23 GERCEKTEN uretildiginde grup 21'e yazilmali.
    g23 = olcum.olaylar("Group23")
    assert g23, "FREEZE uygulandi ama hicbir G23 Frozen Counter Event alinmadi"
    for tip, gv, *_ in g23:
        assert tip == "FrozenCounter", f"{gv} icin beklenmeyen tip: {tip}"
    donmus_hedef = olcum.cache.get(G21, _IDX_DONMUS)
    assert donmus_hedef is not None and donmus_hedef[RAW] == pytest.approx(4242.0), (
        f"G23 Frozen Counter Event grup 21'e yazilmadi: {donmus_hedef}"
    )


def test_gateway_freeze_fonksiyon_kodu_gondermez(olcum: G22Olcumu) -> None:
    """G21/G23 = NOT USED — cunku gateway FREEZE ISTEMEZ.

    Frozen counter'lar yalnizca FC=7/8/9/10 (IMMEDIATE/FREEZE...) sonrasi
    olusur. Gateway bunlarin hicbirini gondermez; profil destekliyor diye
    eklemek de §27 yasagidir.
    """
    freeze_fclari = {7, 8, 9, 10}
    gorulen = olcum.istek_fclari & freeze_fclari
    assert not gorulen, f"FREEZE fonksiyon kodu tele cikti: {sorted(gorulen)}"


# ==========================================================================
# 5 — G22V5 TIMESTAMP
# ==========================================================================


def test_g22v5_cihaz_zaman_damgasini_korur(olcum: G22Olcumu) -> None:
    """G22V5 = "Counter Event 32-bit with flag and TIME".

    Damga kaybolursa backend olayi GATEWAY saatiyle damgalar ve cihazin
    kendi olay sirasi bozulur.
    """
    v5 = [k for k in olcum.olaylar("Group22") if k[1] == "Group22Var5"]
    assert v5, "G22V5 hic alinmadi — acik varyasyon istegi cevapsiz kaldi"
    for *_, ts_ms, ts_kalite in v5:
        assert ts_ms == _DAMGA_MS, f"G22V5 damgasi degisti: {ts_ms}"
        assert "SYNCHRONIZED" in ts_kalite.upper(), f"damga kalitesi: {ts_kalite}"

    kayit = olcum.g22v5_cache
    assert kayit is not None
    assert kayit[DEVICE_TIME] == pytest.approx(_DAMGA_SN), f"G22V5 damgasi CACHE'E yazilmadi: {kayit}"
    assert kayit[TIME_QUALITY] == "synchronized"
    assert kayit[RAW] == pytest.approx(7777.0)


def test_g22v1_damgasiz_gelir_ve_uydurulmaz(olcum: G22Olcumu) -> None:
    """G22V1'de damga YOKTUR. Gateway damga UYDURMAZ.

    Olcum yine de KORUNUR — damga bir ek bilgidir, degerin kendisi degil.
    """
    v1 = [k for k in olcum.olaylar("Group22") if k[1] == "Group22Var1"]
    assert v1, "G22V1 hic alinmadi"

    kayit = olcum.g22v1_cache
    assert kayit is not None
    assert kayit[RAW] == pytest.approx(8888.0), "damgasiz olay OLCUMU DUSURDU"
    assert kayit[DEVICE_TIME] is None, f"damgasiz G22V1 icin cihaz zamani UYDURULDU: {kayit[DEVICE_TIME]}"


# ==========================================================================
# 6 / 7 — COUNTER FLAGS ve ROLLOVER / DISCONTINUITY
# ==========================================================================


def test_g22_counter_flagleri_oldugu_gibi_tasinir(olcum: G22Olcumu) -> None:
    """Ham bayrak byte'i KIRPILMADAN cache'e yazilir."""
    g22 = olcum.olaylar("Group22")
    assert g22
    for k in g22:
        assert k[5] == FLAG_COUNTER_HEPSI, f"tel uzerindeki bayrak degisti: 0x{k[5]:02x}"
    kayit = olcum.cache.get(G20, _IDX_SAYAC)
    assert kayit[FLAGS] == FLAG_COUNTER_HEPSI, (
        f"cache'teki ham bayrak 0x{kayit[FLAGS]:02x} — tel 0x{FLAG_COUNTER_HEPSI:02x}"
    )


def test_rollover_ve_discontinuity_invalid_sayilmaz() -> None:
    """G20/G22'de bit 5/6 = ROLLOVER / DISCONTINUITY.

    Bunlar noktanin DAVRANISI hakkinda bilgi verir (sayac dondu / sayim
    sureklidigi bozuldu) — DEGERI GECERSIZ KILMAZLAR. Analogdaki ayni
    bitler (OVERRANGE / REFERENCE_ERR) ise degeri gecersiz kilar.

    Bu ikisini karistirmak, her rollover'da sayaci `invalid` gostermek
    demektir — yani normal calisan bir enerji sayaci periyodik olarak
    "bozuk" gorunurdu.
    """
    assert mod.map_dnp3_quality(FLAG_COUNTER_HEPSI, G20) == "good"
    assert mod.map_dnp3_quality(FLAG_COUNTER_HEPSI, G21) == "good"
    # ... ama ANALOG'da ayni bitler degeri gecersiz kilar:
    assert mod.map_dnp3_quality(FLAG_COUNTER_HEPSI, mod._OBJECT_GROUP_ANALOG_INPUT) == "invalid"
    assert mod.map_dnp3_quality(FLAG_COUNTER_HEPSI, mod._OBJECT_GROUP_ANALOG_OUTPUT) == "invalid"


def test_counter_bitleri_deger_butunlugu_kumesinde_degil() -> None:
    """Niyet KAYNAKTA da sabitlensin: yalnizca ANALOG gruplar listede."""
    assert set(mod._DEGER_BUTUNLUGU_BITLERI) == {
        mod._OBJECT_GROUP_ANALOG_INPUT,
        mod._OBJECT_GROUP_ANALOG_OUTPUT,
    }, "bit 5/6'yi 'invalid' sayan grup kumesi degismis — counter eklendiyse rollover bozulur"


def test_g22_online_bitsiz_gelirse_invalid() -> None:
    """ONLINE biti TUM gruplarda ayni anlami tasir — o dusunce invalid."""
    assert mod.map_dnp3_quality(FLAG_ROLLOVER | FLAG_DISCONTINUITY, G20) == "invalid"
