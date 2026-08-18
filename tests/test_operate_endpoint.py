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
from datetime import datetime, timezone
from http.client import HTTPConnection
from threading import Event
from typing import Any

import pytest

from dnp3_gateway.backend import DeviceConfig
from dnp3_gateway.health_server import start_health_server
from dnp3_gateway.state import GatewayState

from .conftest import make_gateway_config, make_signal

_TOKEN = "komut-token-yeterince-uzun-1234567890"

#: Gercek Horstmann katalogunun alt kumesi (saha runtime'indan alindi).
#: `index 2 = firmware_update` bilincli olarak burada: F2'nin "gecerli ama
#: YANLIS nokta" vakasini yakaladigini kanitlayan test bunu kullanir.
_KATALOG = [
    make_signal("master.firmware_update", data_type="binary_output", object_group=10, index=2),
    make_signal("master.start_csv_upload", data_type="binary_output", object_group=10, index=3),
    make_signal("master.reset_all_fcis", data_type="binary_output", object_group=10, index=7),
    # Binary INPUT — komut hedefi OLAMAZ (data_type filtresi).
    make_signal("master.overcurrent_tripped", data_type="binary", object_group=1, index=3),
]


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
            signals=list(_KATALOG),
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


def _taze() -> str:
    """Simdi (UTC, timezone-aware) — F7 sonrasi `/operate` damga ZORUNLU."""
    return datetime.now(timezone.utc).isoformat()


def _operate(port: int, govde: dict[str, Any], *, token: str = _TOKEN) -> tuple[int, dict[str, Any]]:
    """`created_at` VERILMEDIYSE taze bir damga eklenir.

    F7 ile damga zorunlu oldu. Yardimciya konmasinin sebebi: asagidaki
    testlerin her biri BASKA bir seyi olcuyor (duplicate bastirma, F1/F2 reddi,
    404, ...) ve damgayi her govdeye elle yazmak o niyeti gurultuye bogardi.
    Tazeligin KENDISI ayrica ve acikca test ediliyor (bkz. F7 bolumu).
    """
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        if "created_at" not in govde:
            govde = {**govde, "created_at": _taze()}
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

    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3, "command_id": 11}
    )

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

    status, _g = _operate(
        port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3}, token="yanlis"
    )
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

    _operate(port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3, "command_id": 42})
    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3, "command_id": 42}
    )

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

    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3, "command_id": 77}
    )

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

    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3, "command_id": 5}
    )

    assert status == 503
    assert reader.calls == [], "defter erisilemezken CROB gonderilmis (fail-open)"
    assert govde["ok"] is False
    assert govde["status"] == "rejected"


def test_command_id_yoksa_komut_calismaz(sunucu) -> None:
    """F7: idempotency anahtari ZORUNLU (eskiden yalnizca uyarilirdi).

    Eski davranis "anahtar yoksa yine calistir, sadece logla" idi. Olculen
    sonucu: ayni istek uc kez gonderildiginde UC FIZIKSEL CROB. Backend'in
    HTTP timeout'u CROB suresinden kisa oldugu icin retry olagan; bu yuzden
    anahtarsiz istek artik en bastan reddedilir.
    """
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3})

    assert status == 400
    assert govde["status"] == "command_id_missing"
    assert reader.calls == [], "anahtarsiz istek CROB uretti"


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

    status, _g = _operate(
        port, {"device_code": "DEV-1", "command": "start_csv_upload", "index": 3, "command_id": "abc"}
    )
    assert status == 400
    assert reader.calls == []


# --------------------------------------------------------------------------
# F1+F2 — yerel cikis yetkilendirmesi (pull yoluyla AYNI fonksiyon)
# --------------------------------------------------------------------------


def test_niyet_uyusmazliginda_crob_gonderilmez(sunucu) -> None:
    """`reset_all_fcis` + index 2 (firmware_update) -> 403, CROB YOK.

    Index 2 katalogda GECERLI bir output; yalnizca index allowlist'i bunu
    gecirirdi. Durduran sey komut niyeti dogrulamasidir (F2).
    """
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "reset_all_fcis", "index": 2, "command_id": 101}
    )

    assert status == 403
    assert reader.calls == [], "yetkisiz komut icin CROB GONDERILDI"
    assert govde["status"] == "command_index_mismatch"


def test_katalogda_olmayan_index_reddedilir(sunucu) -> None:
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "reset_all_fcis", "index": 999, "command_id": 102}
    )

    assert status == 403
    assert reader.calls == []
    assert govde["status"] == "index_not_authorized"


def test_command_alani_zorunlu(sunucu) -> None:
    """Slug olmadan F2 dogrulanamaz -> komut reddedilir (bypass kapali)."""
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(port, {"device_code": "DEV-1", "index": 3, "command_id": 103})

    assert status == 403
    assert reader.calls == []
    assert govde["status"] == "command_missing"


def test_binary_input_index_i_reddedilir(sunucu) -> None:
    """G1 index 3 katalogda var ama data_type='binary' — komut hedefi olamaz."""
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    tutucu["ledger"] = _FakeLedger()

    status, govde = _operate(
        port,
        {"device_code": "DEV-1", "command": "overcurrent_tripped", "index": 3, "command_id": 104},
    )

    assert status == 403
    assert reader.calls == []
    assert govde["status"] == "command_index_mismatch"


def test_yetkisiz_komut_command_id_yi_tuketmez(sunucu) -> None:
    """Reddedilen komut ledger rezervasyonu YAPMAMALI.

    Operator duzeltilmis komutu AYNI command_id ile yeniden gonderebilmeli;
    aksi halde 'duplicate' cevabi alir ve dogru komut hic calismaz.
    """
    tutucu, port = sunucu
    reader = _FakeReader()
    tutucu["reader"] = reader
    ledger = _FakeLedger()
    tutucu["ledger"] = ledger

    # Yanlis index ile red
    _operate(port, {"device_code": "DEV-1", "command": "reset_all_fcis", "index": 2, "command_id": 7})
    assert ledger.dispatched == set(), "yetkisiz komut command_id'yi tuketmis"

    # Ayni id, DOGRU index -> calismali
    status, govde = _operate(
        port, {"device_code": "DEV-1", "command": "reset_all_fcis", "index": 7, "command_id": 7}
    )
    assert status == 200
    assert govde["ok"] is True
    assert len(reader.calls) == 1
