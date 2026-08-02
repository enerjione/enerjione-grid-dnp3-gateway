"""`POST /operate` — fiziksel komut yolunun HTTP seviyesinde testleri.

Bu endpoint bir KESICIYI surer. Test edilmemis olmasi, asagidaki iki hatanin
sahaya cikmasina yetmisti:

1. **Duplicate cevabinda `ok:false`.** Ilk deneme HALA sururken backend'in
   HTTP timeout retry'i geliyordu (timeout ~5sn, CROB 1-10sn). Gateway CROB'u
   dogru sekilde TEKRARLAMIYOR ama cevaba `ok:false` yaziyordu. Cagiran taraf
   bunu "komut basarisiz" okur; operator YENI bir command_id ile tekrar dener
   ve kesici GERCEKTEN iki kez surulur — defterin onlemek icin var oldugu sey.

2. **Defter erisilemezken fail-open.** `start_dispatch` hata verirse eski kod
   `ledger_key = None` deyip CROB'u YINE de gonderiyordu: ne tekrar-onleme ne
   de sonuc kaydi kaliyordu. Fiziksel manevrada "belki iki kez surdum"
   kabul edilemez; "suremedim, sebebi bu" gorunur ve kabul edilebilir.
"""

from __future__ import annotations

import json
from http.client import HTTPConnection
from threading import Event
from typing import Any

import pytest

from dnp3_gateway.backend import DeviceConfig
from dnp3_gateway.health_server import start_health_server
from dnp3_gateway.state import GatewayState

from .conftest import make_gateway_config

_TOKEN = "komut-token-yeterince-uzun-1234567890"


class _FakeReader:
    """operate_device cagrilarini sayar; sonucu testten kontrol edilir."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or {"ok": True, "status": "success", "control": "latch_on"}

    def operate_device(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return dict(self.result)


class _FakeLedger:
    """CommandLedger'in /operate'in kullandigi yuzeyi."""

    def __init__(self, *, patlat: bool = False) -> None:
        self.patlat = patlat
        self.dispatched: set[int] = set()
        self.results: dict[int, dict[str, Any]] = {}

    def start_dispatch(self, command_id: int) -> bool:
        if self.patlat:
            raise RuntimeError("veritabani kilitli")
        if command_id in self.dispatched:
            return False
        self.dispatched.add(command_id)
        return True

    def record_result(self, result: dict[str, Any]) -> None:
        self.results[int(result["id"])] = result

    def pending_results(self) -> list[dict[str, Any]]:
        return list(self.results.values())


@pytest.fixture
def sunucu():
    """Gercek HTTP sunucusu; her test kendi reader/ledger'ini enjekte eder."""
    tutucu: dict[str, Any] = {"reader": None, "ledger": None}
    state = GatewayState()
    state.update(
        make_gateway_config(
            devices=[DeviceConfig(code="DEV-1", name="DEV-1", ip_address="192.168.10.5", dnp3_address=1)],
            signals=[],
        )
    )
    hazir = Event()
    hazir.set()
    server, _metrics, port = start_health_server(
        host="127.0.0.1",
        port=0,
        state=state,
        gateway_code="GW-001",
        gateway_mode="dnp3",
        config_ready=hazir,
        instance_id="test",
        app_environment="development",
        reader_provider=lambda: tutucu["reader"],
        ledger_provider=lambda: tutucu["ledger"],
        command_token=_TOKEN,
    )
    try:
        yield tutucu, port
    finally:
        server.shutdown()
        server.server_close()


def _operate(port: int, govde: dict[str, Any], *, token: str = _TOKEN) -> tuple[int, dict[str, Any]]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        veri = json.dumps(govde).encode("utf-8")
        conn.request(
            "POST",
            "/operate",
            body=veri,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(veri)),
            },
        )
        resp = conn.getresponse()
        ham = resp.read().decode("utf-8")
        return resp.status, (json.loads(ham) if ham else {})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# temel akis
# --------------------------------------------------------------------------


def test_basarili_komut_crob_gonderir(sunucu) -> None:
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(port, {"device_code": "DEV-1", "index": 3, "command_id": 11})

    assert status == 200
    assert len(reader.calls) == 1
    assert reader.calls[0]["index"] == 3
    assert reader.calls[0]["op_type"] == "latch_on"
    # Basari cevabi da duplicate cevabiyla AYNI sekli tasimali.
    assert govde["ok"] is True
    assert govde["duplicate"] is False
    assert govde["status"] == "success"
    assert govde["result"]["ok"] is True


def test_yanlis_token_reddedilir(sunucu) -> None:
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, _g = _operate(port, {"device_code": "DEV-1", "index": 3}, token="yanlis")
    assert status == 401
    assert reader.calls == [], "auth basarisizken CROB gonderilmis"


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_ayni_command_id_crob_u_tekrarlamaz(sunucu) -> None:
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    _operate(port, {"device_code": "DEV-1", "index": 3, "command_id": 42})
    status, govde = _operate(port, {"device_code": "DEV-1", "index": 3, "command_id": 42})

    assert status == 200
    assert len(reader.calls) == 1, "AYNI command_id ile kesici IKI KEZ surulmus"
    assert govde["duplicate"] is True
    # Ilk deneme sonucu kayitli -> gercek sonuc geri verilir
    assert govde["ok"] is True
    assert govde["status"] == "success"


def test_sonucu_bilinmeyen_duplicate_ok_false_donmez(sunucu) -> None:
    """REGRESYON: sonuc kayitli degilken `ok:false` donuyordu.

    Cagiran taraf bunu "basarisiz" okuyup YENI bir command_id ile tekrar
    dener; kesici gercekten iki kez surulur. Dogru cevap "kabul edildi,
    sonucu henuz bilinmiyor"dur.
    """
    tutucu, port = sunucu
    tutucu["reader"] = _FakeReader()
    ledger = _FakeLedger()
    tutucu["ledger"] = ledger

    # Komut daha once dispatch edilmis ama sonucu HENUZ yok (ilk deneme
    # suruyor ya da sonuc backend'e teslim edilip kayittan dusuruldu).
    ledger.dispatched.add(77)

    status, govde = _operate(port, {"device_code": "DEV-1", "index": 3, "command_id": 77})

    assert status == 200
    assert govde["duplicate"] is True
    assert govde["ok"] is not False, "sonuc bilinmiyorken 'basarisiz' bildirildi"
    assert govde["ok"] is None
    assert govde["status"] == "pending"
    assert "TEKRAR DENEMEYIN" in govde["detail"]


# --------------------------------------------------------------------------
# defter erisilemez — fail-closed
# --------------------------------------------------------------------------


def test_defter_erisilemezken_komut_gonderilmez(sunucu) -> None:
    """REGRESYON: eski kod fail-open idi ve CROB'u yine gonderiyordu."""
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger(patlat=True)

    status, govde = _operate(port, {"device_code": "DEV-1", "index": 3, "command_id": 5})

    assert status == 503
    assert reader.calls == [], "defter erisilemezken CROB gonderilmis (fail-open)"
    assert govde["ok"] is False
    assert govde["status"] == "rejected"


def test_command_id_yoksa_komut_yine_calisir(sunucu) -> None:
    """Eski backend uyumu: anahtar yoksa komut engellenmez (yalnizca uyarilir)."""
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(port, {"device_code": "DEV-1", "index": 3})

    assert status == 200
    assert len(reader.calls) == 1
    assert govde["ok"] is True


# --------------------------------------------------------------------------
# dogrulama
# --------------------------------------------------------------------------


def test_bilinmeyen_cihaz_404(sunucu) -> None:
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, _g = _operate(port, {"device_code": "YOK", "index": 1, "command_id": 9})
    assert status == 404
    assert reader.calls == []


def test_gecersiz_command_id_400(sunucu) -> None:
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, _g = _operate(port, {"device_code": "DEV-1", "index": 1, "command_id": "abc"})
    assert status == 400
    assert reader.calls == []
