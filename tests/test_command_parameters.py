"""Fiziksel komut parametrelerinin dogrulanmasi (F6-G).

KAPATILAN ACIK
--------------
F1/F2 komutun DOGRU NOKTAYA gittigini, F3 komutun TAZE oldugunu garanti
ediyordu. Ama parametreler (`op_type`, `count`, `on_time_ms`, `off_time_ms`)
fiziksel cagriya denetlenmeden ulasiyordu. Tek "koruma"
`operate_crob` icindeki `int(...)` cagrilariydi ve bu kasitli bir dogrulama
degildi:

  * `int("1")`, `int(1.5)`, `int(True)` SESSIZCE 1 uretiyordu
  * aralik disi degerler yalnizca pybind11'in `TypeError`i sayesinde
    reddediliyordu — DNP3 OTURUMU ACILDIKTAN SONRA, "CROB olusturulamadi"
    gibi sebebi gorunmeyen genel bir hatayla

Somut ve BUGUN ULASILABILIR olan vaka: `POST /operate` govdesinde
`"count": 0`. `int(0)` = 0 gecerli bir uint8'dir, CROB kurulur ve cihaza
gider. DNP3'te `count=0` "islemi SIFIR kez uygula" demektir: cihaz SUCCESS
doner ve HICBIR SEY YAPMAZ. Operator komutu basarili gorur, saha degismez.

SINIRLAR OLCULEREK BULUNDU
--------------------------
`opendnp3.ControlRelayOutputBlock` alan tipleri (yadnp3 3.2.1.1):
`count` uint8 (0..255), `onTimeMS`/`offTimeMS` uint32 (0..2**32-1).
Asagidaki sinir testleri bu olculen degerlere dayanir, uydurulmus bir
"guvenli gorunen" esige degil.
"""

from __future__ import annotations

from typing import Any

import pytest

from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader
from dnp3_gateway.backend import DeviceConfig
from dnp3_gateway.command_parameters import (
    COUNT_MAX,
    COUNT_MIN,
    TIME_MS_MAX,
    ParameterReason,
    validate_command_parameters,
)

_GECERLI = {"op_type": "latch_on", "count": 1, "on_time_ms": 0, "off_time_ms": 0}


def _dogrula(**kw: Any) -> ParameterReason:
    p = dict(_GECERLI)
    p.update(kw)
    return validate_command_parameters(**p).reason


# ==========================================================================
# 1. GECERLI — uretimdeki sozlesme
# ==========================================================================


def test_uretim_varsayilani_gecerli() -> None:
    """Uretimdeki 26 komutun tamami: latch_on / count=1 / 0 / 0."""
    assert _dogrula() is ParameterReason.VALID


@pytest.mark.parametrize("op", ["latch_on", "latch_off"])
def test_horstmann_profilinde_izinli_op_typelar(op: str) -> None:
    """Resmi Device Profile: Latch On/Off = ALWAYS."""
    assert _dogrula(op_type=op) is ParameterReason.VALID


@pytest.mark.parametrize("op", ["pulse_on", "pulse_off"])
def test_pulse_horstmann_profilinde_reddedilir(op: str) -> None:
    """P0 UYUMLULUK: resmi Device Profile `Pulse On/Off = NEVER` diyor.

    1.15.0'a kadar validator bunlari KABUL EDIYORDU; yani gateway cihazin
    "ASLA desteklemiyorum" dedigi bir istegi TELE KOYABILIYORDU. Bu bir
    "cihaz nasilsa reddeder" durumu degildir: fiziksel bir cikisa `pulse`
    gondermek, latch bekleyen operatorun niyetinden BASKA bir sonuc
    uretebilir.

    `_op_map` kutuphane kodlayicisidir ve dort tipi de KODLAYABILIR; kapi
    GATEWAY SINIRIDIR.
    """
    assert _dogrula(op_type=op) is ParameterReason.INVALID_OP_TYPE


def test_buyuk_harf_op_type_mevcut_davranis_korunur() -> None:
    """`.lower()` MEVCUT davranistir; F6 yeni alias EKLEMEDI ama var olani da bozmadi."""
    assert _dogrula(op_type="LATCH_ON") is ParameterReason.VALID
    assert _dogrula(op_type=" latch_on ") is ParameterReason.VALID


def test_latch_ile_sifir_disi_zamanlama_reddedilmez() -> None:
    """LATCH'te zamanlama cihazca yok sayilir; `/operate` varsayilani 100/100.

    Reddetseydik F6 kapsami disinda bir ucun mevcut sozlesmesini kirardik.
    Yalnizca teknik araliga bakilir.
    """
    assert _dogrula(on_time_ms=100, off_time_ms=100) is ParameterReason.VALID


# ==========================================================================
# 3-4. op_type
# ==========================================================================


@pytest.mark.parametrize(
    "op", ["", "trip", "close", "open", "pulse", "latch", "reset", "LATCH", "nul", "Undefined"]
)
def test_desteklenmeyen_op_type_reddedilir(op: str) -> None:
    assert _dogrula(op_type=op) is ParameterReason.INVALID_OP_TYPE


@pytest.mark.parametrize("op", ["latch-on", "latch on", "latchon", "latch_on_extra"])
def test_alias_ve_fuzzy_esleme_yok(op: str) -> None:
    """F1/F2'deki tam-eslesme felsefesi korunuyor; yakin yazimlar kabul edilmez."""
    assert _dogrula(op_type=op) is ParameterReason.INVALID_OP_TYPE


@pytest.mark.parametrize("op", [None, 1, 1.0, True, ["latch_on"], b"latch_on"])
def test_op_type_metin_degilse_reddedilir(op: Any) -> None:
    assert _dogrula(op_type=op) is ParameterReason.INVALID_OP_TYPE


# ==========================================================================
# 5-9, 13. count
# ==========================================================================


@pytest.mark.parametrize("c", [-1, -255, 0])
def test_sifir_ve_negatif_count_reddedilir(c: int) -> None:
    """`count=0` DNP3'te gecerli kodlamadir ama cihaz HICBIR SEY YAPMADAN
    SUCCESS doner — sessiz basarisizlik."""
    assert _dogrula(count=c) is ParameterReason.INVALID_COUNT


@pytest.mark.parametrize("c", [256, 1000, 10**9, 2**31])
def test_araligin_ustunde_count_reddedilir(c: int) -> None:
    """uint8 tavani; `opendnp3` da bunu reddeder, biz DAHA ONCE reddediyoruz."""
    assert _dogrula(count=c) is ParameterReason.INVALID_COUNT


def test_count_sinir_degerleri() -> None:
    """Tip/aralik sinirlari KORUNDU; ustune HORSTMANN PROFIL kisiti geldi.

    `COUNT_MIN..COUNT_MAX` (1..255) DNP3 `uint8` alan sinirinin ta kendisi
    ve kodlama katmani icin hala gecerli. Ama resmi Device Profile
    `Count > 1 = NEVER` dedigi icin gateway sinirinda TEK gecerli deger 1.
    """
    assert _dogrula(count=COUNT_MIN) is ParameterReason.VALID
    assert COUNT_MIN == 1, "profil kisiti COUNT_MIN=1 varsayar"
    # Alan sinirinin ustu ZATEN reddediliyordu...
    assert _dogrula(count=COUNT_MAX + 1) is ParameterReason.INVALID_COUNT
    # ...ve artik alan sinirinin ICINDE olan >1 degerler de reddediliyor.
    assert _dogrula(count=COUNT_MAX) is ParameterReason.INVALID_COUNT
    assert _dogrula(count=2) is ParameterReason.INVALID_COUNT


def test_count_bool_reddedilir() -> None:
    """`isinstance(True, int)` True'dur; tip kontrolu yapiyormus gibi gorunen
    bir kod `count=True`yi 1 olarak GECIRIRDI."""
    assert _dogrula(count=True) is ParameterReason.INVALID_COUNT
    assert _dogrula(count=False) is ParameterReason.INVALID_COUNT


@pytest.mark.parametrize("c", ["1", "abc", 1.0, 1.5, None, [1], {"v": 1}])
def test_count_yanlis_tip_reddedilir(c: Any) -> None:
    """Sessiz coercion YOK: `int("1")` gecerdi ama sozlesme tam sayi diyor."""
    assert _dogrula(count=c) is ParameterReason.INVALID_COUNT


# ==========================================================================
# 10-13. zamanlama
# ==========================================================================


@pytest.mark.parametrize("alan", ["on_time_ms", "off_time_ms"])
@pytest.mark.parametrize("v", [-1, -1000])
def test_negatif_zamanlama_reddedilir(alan: str, v: int) -> None:
    assert _dogrula(**{alan: v}) is ParameterReason.INVALID_TIMING


@pytest.mark.parametrize("alan", ["on_time_ms", "off_time_ms"])
def test_zamanlama_sinir_degerleri(alan: str) -> None:
    """uint32 tavani — olculerek bulundu."""
    assert _dogrula(**{alan: 0}) is ParameterReason.VALID
    assert _dogrula(**{alan: TIME_MS_MAX}) is ParameterReason.VALID
    assert _dogrula(**{alan: TIME_MS_MAX + 1}) is ParameterReason.INVALID_TIMING


@pytest.mark.parametrize("alan", ["on_time_ms", "off_time_ms"])
@pytest.mark.parametrize("v", ["100", 100.0, 100.5, None, True, False])
def test_zamanlama_yanlis_tip_reddedilir(alan: str, v: Any) -> None:
    assert _dogrula(**{alan: v}) is ParameterReason.INVALID_TIMING


def test_status_metinleri_backend_alanina_sigar() -> None:
    """Backend `result_status` alani String(40)."""
    for r in ParameterReason:
        assert len(r.value) <= 40


# ==========================================================================
# 2, 14-17. ADAPTER ENTEGRASYONU — fiziksel cagri sayisi
# ==========================================================================


class _StubOkuyucu:
    """`operate_device`'i gercek DNP3 yigini olmadan kosturur.

    `_ensure_master` cagrilirsa PATLAR: gecersiz parametrenin DNP3 oturumuna
    dokunmadigini kanitlamanin en dogrudan yolu budur.
    """

    def __init__(self, master: Any = None) -> None:
        self.ensure_master_calls = 0
        self._master = master

    def _ensure_master(self, device: DeviceConfig) -> Any:  # noqa: ARG002
        self.ensure_master_calls += 1
        if self._master is None:
            raise AssertionError("gecersiz parametrede DNP3 oturumuna DOKUNULDU")
        return self._master


class _SayanMaster:
    """`operate_crob` cagrilarini sayar — fiziksel gonderimin vekili."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def operate_crob(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"ok": True, "status": "ok", "control": kw.get("op_type")}


def _cihaz() -> DeviceConfig:
    return DeviceConfig(code="SN2_0", name="SN2_0", ip_address="10.0.0.9", dnp3_address=4)


def _operate(okuyucu: Any, **kw: Any) -> dict[str, Any]:
    p = dict(_GECERLI)
    p.update(kw)
    return Yadnp3TelemetryReader.operate_device(okuyucu, device=_cihaz(), index=7, **p)


def test_gecerli_komut_tam_bir_kez_fiziksel_cagri_uretir() -> None:
    """2/17: mevcut basari akisi bozulmadi."""
    master = _SayanMaster()
    okuyucu = _StubOkuyucu(master)
    sonuc = _operate(okuyucu)

    assert sonuc["ok"] is True
    assert len(master.calls) == 1, "fiziksel cagri sayisi 1 olmali"
    assert master.calls[0]["op_type"] == "latch_on"
    assert master.calls[0]["count"] == 1


@pytest.mark.parametrize(
    ("kw", "beklenen"),
    [
        ({"op_type": "trip"}, "invalid_op_type"),
        ({"op_type": "latch-on"}, "invalid_op_type"),
        ({"op_type": None}, "invalid_op_type"),
        ({"count": 0}, "invalid_count"),
        ({"count": -1}, "invalid_count"),
        ({"count": 256}, "invalid_count"),
        ({"count": True}, "invalid_count"),
        ({"count": "1"}, "invalid_count"),
        ({"on_time_ms": -1}, "invalid_timing"),
        ({"off_time_ms": -1}, "invalid_timing"),
        ({"on_time_ms": TIME_MS_MAX + 1}, "invalid_timing"),
        ({"off_time_ms": "100"}, "invalid_timing"),
    ],
)
def test_gecersiz_parametre_sifir_fiziksel_cagri(kw: dict, beklenen: str) -> None:
    """3-13: hicbir gecersiz deger CROB uretmez ve DNP3 oturumuna dokunmaz."""
    okuyucu = _StubOkuyucu(master=None)  # _ensure_master cagrilirsa AssertionError
    sonuc = _operate(okuyucu, **kw)

    assert okuyucu.ensure_master_calls == 0, "gecersiz parametrede oturum acildi"
    assert sonuc["ok"] is False
    assert sonuc["status"] == beklenen
    assert sonuc["error"]
    assert sonuc["device_code"] == "SN2_0"


def test_gecersiz_parametre_tekrar_denenirse_yine_sifir_fiziksel() -> None:
    """15: red deterministik — tekrar cagri da fiziksel gonderim uretmez."""
    okuyucu = _StubOkuyucu(master=None)
    for _ in range(3):
        sonuc = _operate(okuyucu, count=0)
        assert sonuc["status"] == "invalid_count"
    assert okuyucu.ensure_master_calls == 0


def test_red_sonucu_ledger_result_biciminde() -> None:
    """16: sonuc, `_execute_pending_commands` ve backend teslim yolunun
    bekledigi sekli tasimali (terminal, aciklanabilir)."""
    sonuc = _operate(_StubOkuyucu(master=None), count=-5)
    assert set(sonuc) >= {"ok", "status", "error"}
    assert sonuc["ok"] is False
    assert len(sonuc["status"]) <= 40
    assert "count" in sonuc["error"]


def _kaynak(modul: Any) -> str:
    import inspect

    return inspect.getsource(modul)


def test_her_iki_yol_da_operate_device_uzerinden_gider() -> None:
    """`/operate` ve kuyruk yolu AYNI primitive'i cagirir; dolayisiyla ikisi de
    F6 korumasinin altindadir. `/operate`'in kimlik/TTL sertlestirmesi F7'dir,
    burada DEGISTIRILMEDI."""
    from dnp3_gateway import health_server, main

    assert ".operate_device(" in _kaynak(main)
    assert ".operate_device(" in _kaynak(health_server)


def test_choke_point_korunuyor_operate_crob_disaridan_cagrilmiyor() -> None:
    """F6'nin butun degeri tek giris noktasi varsayimina dayanir.

    Biri ilerde `operate_crob`u dogrudan cagirirsa validator ATLANIR. Bu test
    o regresyonu yakalar: `operate_crob` YALNIZCA adapter icinden, yani
    `operate_device` dogrulamayi yaptiktan SONRA cagrilabilir.
    """
    import pathlib

    import dnp3_gateway

    kok = pathlib.Path(dnp3_gateway.__file__).parent
    adapter = kok / "adapters" / "dnp3_yadnp3_master.py"

    ihlaller = [
        f"{yol.relative_to(kok)}:{no}"
        for yol in kok.rglob("*.py")
        if yol != adapter
        for no, satir in enumerate(yol.read_text(encoding="utf-8").splitlines(), 1)
        if "operate_crob(" in satir and "def operate_crob" not in satir
    ]
    assert not ihlaller, f"validator'i atlayan dogrudan CROB cagrisi: {ihlaller}"
