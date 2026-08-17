"""F6 parametre guvenligi — GERCEK giris sinirlarinda (fail-closed parse).

NEDEN AYRI DOSYA
----------------
`test_command_parameters.py` validator'in KENDISINI test ediyor. Ama validator
ne kadar dogru olursa olsun, giris yolu degeri ONCE sessizce donusturuyorsa
tip kontrolleri hicbir zaman calismaz. Bu dosya iki GERCEK sinira bakar:

    /pending JSON  -> fetch_pending_commands -> PendingCommand
                   -> _execute_pending_commands -> operate_device
    POST /operate  -> HTTP govdesi -> operate_device

KAPATILAN SESSIZ DONUSUMLER
---------------------------
Once:

    count=int(item.get("count", 1) or 1)          # kuyruk yolu
    count=int(payload.get("count", 1))            # /operate

Sonuclari:

    ham count = 0     -> 1      "hicbir sey yapma" -> "bir kez sur"
    ham count = "1"   -> 1      metin sessizce kabul
    ham count = 1.5   -> 1      ondalik sessizce kirpildi
    ham count = True  -> 1      bool sessizce kabul
    ham count = None  -> 1      ACIK null varsayilana dondu

`count = 0` en kotusuydu: DNP3'te gecerli bir kodlamadir ve "islemi SIFIR kez
uygula" demektir. Cagiran taraf 0 gonderdiginde gateway 1 gonderiyordu — yani
istenmeyen bir fiziksel manevra.

KURAL
-----
    alan YOK            -> belgelenmis varsayilan
    alan VAR, gecerli   -> aynen
    alan VAR, gecersiz  -> AYNEN tasinir, validator REDDEDER (fail-closed)

Gecersiz komut parse dongusunde SESSIZCE DUSURULMEZ; terminal bir sonuc
uretir ki backend sebebi ogrenebilsin — `created_at` (F3) ile ayni felsefe.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from threading import Event
from typing import Any

import pytest

from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader
from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient, DeviceConfig, PendingCommand
from dnp3_gateway.command_parameters import TIME_MS_MAX, raw_command_parameter
from dnp3_gateway.health_server import start_health_server
from dnp3_gateway.main import _execute_pending_commands
from dnp3_gateway.state import GatewayState

from .conftest import imzala, make_gateway_config, make_signal

_TOKEN = "komut-token-yeterince-uzun-1234567890"

_KATALOG = [
    make_signal("master.reset_all_fcis", data_type="binary_output", object_group=10, index=7),
]


# ==========================================================================
# GECERSIZ HAM DEGER MATRISI — iki sinirda da AYNISI kullanilir
# ==========================================================================
#
# Ayni listeyi hem kuyruk hem /operate testleri tuketiyor: iki yolun
# fail-closed davranisinin GERCEKTEN ayni oldugunu boyle kanitlariz.

#: (etiket, ham deger, beklenen terminal status)
GECERSIZ_COUNT: list[tuple[str, Any, str]] = [
    ("acik_sifir", 0, "invalid_count"),
    ("negatif", -1, "invalid_count"),
    ("uint8_ustu", 256, "invalid_count"),
    ("bool_true", True, "invalid_count"),
    ("bool_false", False, "invalid_count"),
    ("metin", "1", "invalid_count"),
    ("ondalik", 1.5, "invalid_count"),
    ("json_null", None, "invalid_count"),
]

GECERSIZ_TIMING: list[tuple[str, str, Any, str]] = [
    ("on_negatif", "on_time_ms", -1, "invalid_timing"),
    ("off_negatif", "off_time_ms", -1, "invalid_timing"),
    ("on_uint32_ustu", "on_time_ms", TIME_MS_MAX + 1, "invalid_timing"),
    ("off_uint32_ustu", "off_time_ms", TIME_MS_MAX + 1, "invalid_timing"),
    ("on_metin", "on_time_ms", "100", "invalid_timing"),
    ("off_ondalik", "off_time_ms", 100.5, "invalid_timing"),
    ("on_bool", "on_time_ms", True, "invalid_timing"),
    ("off_null", "off_time_ms", None, "invalid_timing"),
]


# ==========================================================================
# GERCEK VALIDATOR TASIYAN OKUYUCU
# ==========================================================================


class _SayanMaster:
    def __init__(self, kayit: list[dict[str, Any]]) -> None:
        self._kayit = kayit

    def operate_crob(self, **kw: Any) -> dict[str, Any]:
        self._kayit.append(kw)
        return {"ok": True, "status": "success", "control": kw.get("op_type")}


class _GercekReader:
    """`operate_device` GERCEK implementasyondur — F6 validator'i dahil.

    `_FakeReader` gibi cagriyi kaydedip donmez; gercek kod yolunu kosturur.
    Boylece "fiziksel cagri sayisi" olcumu validator'dan SONRAYI olcer.
    """

    def __init__(self) -> None:
        self.fiziksel: list[dict[str, Any]] = []
        self.oturum = 0  # _ensure_master cagri sayisi

    def _ensure_master(self, device: DeviceConfig) -> Any:  # noqa: ARG002
        self.oturum += 1
        return _SayanMaster(self.fiziksel)

    def operate_device(self, **kw: Any) -> dict[str, Any]:
        return Yadnp3TelemetryReader.operate_device(self, **kw)


# ==========================================================================
# 1. raw_command_parameter — alan yok / var ayrimi
# ==========================================================================


def test_alan_yoksa_belgelenmis_varsayilan() -> None:
    assert raw_command_parameter({}, "count", 1) == 1
    assert raw_command_parameter({}, "on_time_ms", 100) == 100


@pytest.mark.parametrize("ham", [0, -1, 256, True, False, "1", 1.5, None])
def test_alan_varsa_ham_deger_aynen_tasinir(ham: Any) -> None:
    """Hicbiri varsayilana DONMEZ — `or default` kalibinin kapattigi acik."""
    sonuc = raw_command_parameter({"count": ham}, "count", 1)
    assert sonuc is ham or sonuc == ham
    assert type(sonuc) is type(ham)


def test_acik_null_varsayilana_donmez() -> None:
    """JSON `null` ACIK bir gonderimdir, alanin yoklugu degil."""
    assert raw_command_parameter({"count": None}, "count", 1) is None


# ==========================================================================
# 2. KUYRUK SINIRI — /pending ham JSON -> PendingCommand
# ==========================================================================


class _Resp:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        body = json.dumps(payload)
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.content)),
            "X-Config-Signature": imzala(body.encode("utf-8")),
        }

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int = 65536, decode_unicode: bool = False):  # noqa: ARG002
        if self.content:
            yield self.content

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def get(self, url: str, **_kw: Any) -> _Resp:  # noqa: ARG002
        return self._resp

    def post(self, url: str, **_kw: Any) -> _Resp:  # noqa: ARG002
        return self._resp


def _ham_pending(**alanlar: Any) -> PendingCommand:
    """GERCEK `/pending` parse yolundan gecirir (mock'lanmis HTTP oturumu)."""
    item: dict[str, Any] = {
        "id": 1,
        "device_code": "SN2_0",
        "command": "reset_all_fcis",
        "dnp3_index": 7,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    item.update(alanlar)
    istemci = BackendConfigClient(
        base_url="http://backend/api/v1",
        identity=GatewayIdentity(
            gateway_code="GW-002",
            token="tok",
            instance_id="test",
            app_version="0.0.0-test",
            app_environment="development",
        ),
        session=_Session(_Resp({"commands": [item]})),  # type: ignore[arg-type]
    )
    poll = istemci.fetch_pending_commands()
    assert len(poll.commands) == 1, "komut SESSIZCE DUSURULDU — fail-closed degil"
    return poll.commands[0]


def test_kuyruk_count_alani_yoksa_varsayilan_bir() -> None:
    assert _ham_pending().count == 1


@pytest.mark.parametrize(("etiket", "ham", "_status"), GECERSIZ_COUNT, ids=lambda v: str(v)[:18])
def test_kuyruk_gecersiz_count_ham_kalir_dusurulmez(etiket: str, ham: Any, _status: str) -> None:
    """REGRESYON: `int(... or 1)` bunlarin HEPSINI 1'e ceviriyordu."""
    cmd = _ham_pending(count=ham)
    assert cmd.count is ham or cmd.count == ham
    assert type(cmd.count) is type(ham), f"{etiket}: tip degisti -> sessiz coercion"


def test_kuyruk_acik_sifir_bire_donmez() -> None:
    """Eski `or 1` kalibinin somut acigi."""
    assert _ham_pending(count=0).count == 0


@pytest.mark.parametrize(("etiket", "alan", "ham", "_s"), GECERSIZ_TIMING, ids=lambda v: str(v)[:18])
def test_kuyruk_gecersiz_zamanlama_ham_kalir(etiket: str, alan: str, ham: Any, _s: str) -> None:
    cmd = _ham_pending(**{alan: ham})
    assert getattr(cmd, alan) is ham or getattr(cmd, alan) == ham
    assert type(getattr(cmd, alan)) is type(ham), f"{etiket}: sessiz coercion"


# ==========================================================================
# 3. KUYRUK ZINCIRI — fiziksel cagri sayisi
# ==========================================================================


def _state() -> GatewayState:
    st = GatewayState()
    st.update(
        make_gateway_config(
            devices=[DeviceConfig(code="SN2_0", name="SN2_0", ip_address="10.0.0.9", dnp3_address=4)],
            signals=list(_KATALOG),
        )
    )
    return st


def _kuyrukta_calistir(cmd: PendingCommand) -> tuple[_GercekReader, list[dict[str, Any]]]:
    reader = _GercekReader()
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-002", max_age_sec=120.0)
    return reader, sonuclar


@pytest.mark.parametrize(("etiket", "ham", "status"), GECERSIZ_COUNT, ids=lambda v: str(v)[:18])
def test_kuyruk_gecersiz_count_sifir_fiziksel(etiket: str, ham: Any, status: str) -> None:
    reader, sonuclar = _kuyrukta_calistir(_ham_pending(count=ham))

    assert reader.fiziksel == [], f"{etiket}: CROB GONDERILDI"
    assert reader.oturum == 0, f"{etiket}: DNP3 oturumu acildi"
    assert len(sonuclar) == 1, "terminal sonuc uretilmedi — backend ogrenemez"
    assert sonuclar[0]["ok"] is False
    assert sonuclar[0]["status"] == status
    assert sonuclar[0]["id"] == 1


@pytest.mark.parametrize(("etiket", "alan", "ham", "status"), GECERSIZ_TIMING, ids=lambda v: str(v)[:18])
def test_kuyruk_gecersiz_zamanlama_sifir_fiziksel(etiket: str, alan: str, ham: Any, status: str) -> None:
    reader, sonuclar = _kuyrukta_calistir(_ham_pending(**{alan: ham}))

    assert reader.fiziksel == [], f"{etiket}: CROB GONDERILDI"
    assert reader.oturum == 0
    assert sonuclar[0]["ok"] is False
    assert sonuclar[0]["status"] == status


def test_kuyruk_uretim_komutu_tam_bir_fiziksel_cagri() -> None:
    """Sahadaki gercek sozlesme: latch_on / count=1 / 0 / 0."""
    reader, sonuclar = _kuyrukta_calistir(_ham_pending())

    assert len(reader.fiziksel) == 1, "gecerli komut TAM BIR KEZ gonderilmeli"
    assert reader.oturum == 1
    assert reader.fiziksel[0]["op_type"] == "latch_on"
    assert reader.fiziksel[0]["count"] == 1
    assert sonuclar[0]["ok"] is True


def test_kuyruk_alanlar_hic_gelmezse_calisir() -> None:
    """Varsayilanlar korundu: eski backend count/on/off gondermese de komut gider."""
    cmd = _ham_pending()
    assert (cmd.count, cmd.on_time_ms, cmd.off_time_ms) == (1, 0, 0)
    reader, _ = _kuyrukta_calistir(cmd)
    assert len(reader.fiziksel) == 1


def test_kuyruk_gecersiz_komut_gecerli_olani_engellemez() -> None:
    """Bir komutun reddi ayni yanittaki digerini durdurmaz."""
    kotu = PendingCommand(
        id=1,
        device_code="SN2_0",
        command="reset_all_fcis",
        dnp3_index=7,
        count=0,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    iyi = PendingCommand(
        id=2,
        device_code="SN2_0",
        command="reset_all_fcis",
        dnp3_index=7,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    reader = _GercekReader()
    sonuclar = _execute_pending_commands(
        reader, _state(), [kotu, iyi], gateway_code="GW-002", max_age_sec=120.0
    )
    assert len(reader.fiziksel) == 1, "yalnizca gecerli komut gonderilmeli"
    assert [(s["id"], s["ok"]) for s in sonuclar] == [(1, False), (2, True)]


def test_kuyruk_f3_tazelik_hala_once_calisir() -> None:
    """F6 F3'un onune GECMEDI: bayat komut zaten CROB uretmiyordu."""
    bayat = PendingCommand(
        id=3,
        device_code="SN2_0",
        command="reset_all_fcis",
        dnp3_index=7,
        created_at=(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
    )
    reader, sonuclar = _kuyrukta_calistir(bayat)
    assert reader.fiziksel == []
    assert sonuclar[0]["ok"] is False
    assert sonuclar[0]["status"] != "invalid_count"  # F3 reddi, F6 degil


# ==========================================================================
# 4. DOGRUDAN /operate SINIRI — gercek HTTP govdesi
# ==========================================================================


@pytest.fixture
def sunucu():
    tutucu: dict[str, Any] = {"reader": None}
    st = _state()
    hazir = Event()
    hazir.set()
    server, _metrics, port = start_health_server(
        host="127.0.0.1",
        port=0,
        state=st,
        gateway_code="GW-002",
        gateway_mode="dnp3",
        config_ready=hazir,
        instance_id="test",
        app_environment="development",
        reader_provider=lambda: tutucu["reader"],
        ledger_provider=lambda: None,
        command_token=_TOKEN,
    )
    try:
        yield tutucu, port
    finally:
        server.shutdown()
        server.server_close()


def _post_operate(port: int, govde: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        veri = json.dumps(govde).encode("utf-8")
        conn.request(
            "POST",
            "/operate",
            body=veri,
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
                "Content-Length": str(len(veri)),
            },
        )
        resp = conn.getresponse()
        ham = resp.read().decode("utf-8")
        return resp.status, (json.loads(ham) if ham else {})
    finally:
        conn.close()


def _operate_govdesi(**alanlar: Any) -> dict[str, Any]:
    g: dict[str, Any] = {"device_code": "SN2_0", "command": "reset_all_fcis", "index": 7}
    g.update(alanlar)
    return g


@pytest.mark.parametrize(("etiket", "ham", "status"), GECERSIZ_COUNT, ids=lambda v: str(v)[:18])
def test_operate_gecersiz_count_sifir_fiziksel(sunucu, etiket: str, ham: Any, status: str) -> None:
    """`int(payload.get("count", 1))` bunlari validator'a GELMEDEN 1 yapiyordu."""
    tutucu, port = sunucu
    reader = _GercekReader()
    tutucu["reader"] = reader

    kod, govde = _post_operate(port, _operate_govdesi(count=ham))

    assert reader.fiziksel == [], f"{etiket}: CROB GONDERILDI"
    assert reader.oturum == 0, f"{etiket}: DNP3 oturumu acildi"
    assert kod == 200
    assert govde["ok"] is False
    assert govde["status"] == status


@pytest.mark.parametrize(("etiket", "alan", "ham", "status"), GECERSIZ_TIMING, ids=lambda v: str(v)[:18])
def test_operate_gecersiz_zamanlama_sifir_fiziksel(
    sunucu, etiket: str, alan: str, ham: Any, status: str
) -> None:
    tutucu, port = sunucu
    reader = _GercekReader()
    tutucu["reader"] = reader

    _kod, govde = _post_operate(port, _operate_govdesi(**{alan: ham}))

    assert reader.fiziksel == [], f"{etiket}: CROB GONDERILDI"
    assert reader.oturum == 0
    assert govde["ok"] is False
    assert govde["status"] == status


def test_operate_acik_sifir_count_reddedilir(sunucu) -> None:
    """BUGUN ULASILABILIR ACIK: `"count": 0` cihaza gidiyordu.

    DNP3 SUCCESS donuyordu ama kesici SURULMUYORDU — operator komutu basarili
    gorurdu. Sessiz basarisizlik artik gorunur bir red.
    """
    tutucu, port = sunucu
    reader = _GercekReader()
    tutucu["reader"] = reader

    _kod, govde = _post_operate(port, _operate_govdesi(count=0))

    assert reader.fiziksel == []
    assert govde["status"] == "invalid_count"
    assert "0" in govde["result"]["error"]


def test_operate_gecerli_komut_tam_bir_fiziksel_cagri(sunucu) -> None:
    """Mevcut basari akisi bozulmadi (alanlar yok -> varsayilan 1/100/100)."""
    tutucu, port = sunucu
    reader = _GercekReader()
    tutucu["reader"] = reader

    kod, govde = _post_operate(port, _operate_govdesi())

    assert kod == 200
    assert govde["ok"] is True
    assert len(reader.fiziksel) == 1, "gecerli komut TAM BIR KEZ gonderilmeli"
    assert reader.fiziksel[0]["count"] == 1
    assert reader.fiziksel[0]["on_time_ms"] == 100
    assert reader.fiziksel[0]["off_time_ms"] == 100


def test_operate_gecerli_acik_degerler_calisir(sunucu) -> None:
    tutucu, port = sunucu
    reader = _GercekReader()
    tutucu["reader"] = reader

    _kod, govde = _post_operate(port, _operate_govdesi(count=2, on_time_ms=0, off_time_ms=0))

    assert govde["ok"] is True
    assert len(reader.fiziksel) == 1
    assert reader.fiziksel[0]["count"] == 2


def test_operate_timeout_sec_f6_alani_degil(sunucu) -> None:
    """`timeout_sec` cihaza GITMEZ (CROB parametresi degil); F6 disinda birakildi.

    Bu test kapsam sinirini kilitler: birisi ileride onu da F6'ya cekerse
    burasi kirilir ve bilincli bir karar olmasi gerektigi gorunur.
    """
    tutucu, port = sunucu
    reader = _GercekReader()
    tutucu["reader"] = reader

    _kod, govde = _post_operate(port, _operate_govdesi(timeout_sec="10"))

    assert govde["ok"] is True, "timeout_sec hala coerce ediliyor (mevcut davranis)"
    assert len(reader.fiziksel) == 1
