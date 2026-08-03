"""`/pending` komut-poll parse yolu regresyon testleri.

Bu kod yolu HIC test edilmemisti ve icindeki `_logging` NameError'u yuzunden
backend tek bir bozuk komut kaydi donduginde:
  * ayni yanittaki GECERLI komutlar da calistirilmiyordu,
  * config_nonce / refresh_nonce islenmiyordu,
  * komut sonuclari backend'e hic bildirilmiyordu,
yani TUM SCADA komut kanali sessizce oluyordu (logda yalnizca bir satir).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient
from dnp3_gateway.backend.config_client import GatewayConfigError


def _identity() -> GatewayIdentity:
    return GatewayIdentity(
        gateway_code="GW-001",
        token="tok",
        instance_id="test-instance",
        app_version="0.0.0-test",
        app_environment="development",
    )


class _Resp:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        body = json.dumps(payload)
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.content)),
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

    def get(self, url: str, **_kwargs: Any) -> _Resp:  # noqa: ARG002
        return self._resp

    def post(self, url: str, **_kwargs: Any) -> _Resp:  # noqa: ARG002
        return self._resp


def _client(payload: Any) -> BackendConfigClient:
    return BackendConfigClient(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=_Session(_Resp(payload)),  # type: ignore[arg-type]
    )


_GOOD = {"id": 2, "device_code": "DEV-1", "command": "reset", "dnp3_index": 3}


def test_gecerli_komutlar_parse_edilir() -> None:
    poll = _client({"commands": [_GOOD], "config_nonce": 4, "refresh_nonce": 7}).fetch_pending_commands()
    assert len(poll.commands) == 1
    assert poll.commands[0].id == 2
    assert poll.commands[0].dnp3_index == 3
    assert poll.config_nonce == 4
    assert poll.refresh_nonce == 7


def test_bozuk_komut_atlanir_gecerli_olan_calisir() -> None:
    """REGRESYON: bozuk komut except yolunda NameError -> tum kanal oluyordu."""
    payload = {
        "commands": [
            {"id": "abc", "device_code": "DEV-9", "command": "x", "dnp3_index": 1},
            _GOOD,
        ],
        "config_nonce": 11,
        "refresh_nonce": 12,
    }
    poll = _client(payload).fetch_pending_commands()
    # Bozuk olan elendi, gecerli olan HAYATTA
    assert [c.id for c in poll.commands] == [2]
    # Nonce'lar da islendi (eskiden exception yuzunden hic islenmiyordu)
    assert poll.config_nonce == 11
    assert poll.refresh_nonce == 12


def test_eksik_zorunlu_alan_atlanir() -> None:
    payload = {"commands": [{"id": 5, "device_code": "DEV-1"}, _GOOD]}
    poll = _client(payload).fetch_pending_commands()
    assert [c.id for c in poll.commands] == [2]


def test_null_dnp3_index_atlanir() -> None:
    payload = {
        "commands": [
            {"id": 5, "device_code": "DEV-1", "command": "x", "dnp3_index": None},
            _GOOD,
        ]
    }
    poll = _client(payload).fetch_pending_commands()
    assert [c.id for c in poll.commands] == [2]


def test_tum_komutlar_bozuksa_bos_liste_ve_nonce_yine_islenir() -> None:
    payload = {
        "commands": [
            {"id": "a"},
            {"id": None, "device_code": "D", "command": "x", "dnp3_index": 1},
        ],
        "config_nonce": 99,
    }
    poll = _client(payload).fetch_pending_commands()
    assert poll.commands == ()
    assert poll.config_nonce == 99


def test_commands_alani_liste_degilse_yok_sayilir() -> None:
    poll = _client({"commands": "bozuk", "config_nonce": 3}).fetch_pending_commands()
    assert poll.commands == ()
    assert poll.config_nonce == 3


def test_nonce_alanlari_bozuksa_sifira_duser() -> None:
    poll = _client({"config_nonce": "abc", "refresh_nonce": None}).fetch_pending_commands()
    assert poll.config_nonce == 0
    assert poll.refresh_nonce == 0


# --------------------------------------------------------------------------
# saglik basligi komut kanalini DUSURMEMELI
# --------------------------------------------------------------------------


class _BasligaGoreYanit:
    """Saglik basligi varsa 500, yoksa 200 donen backend taklidi.

    Sahada aynen bu oldu: backend basligi `gateway_health` tablosuna yazmaya
    calisti, tablo yoktu (migration eksik), transaction bozuldu ve `/pending`
    500 dondu.
    """

    def __init__(self) -> None:
        self.baslikli_istekler = 0
        self.basliksiz_istekler = 0

    def get(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
        from dnp3_gateway.backend import health_header

        headers = kwargs.get("headers") or {}
        if health_header.HEADER_NAME in headers:
            self.baslikli_istekler += 1
            return _Resp({"detail": "Internal Server Error"}, status_code=500)
        self.basliksiz_istekler += 1
        return _Resp({"commands": [], "config_nonce": 7, "refresh_nonce": 2, "is_active": True})

    def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
        return _Resp({"ok": True})


def _client_saglikli_baslikla(session: Any) -> BackendConfigClient:
    return BackendConfigClient(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=session,
        health_provider=lambda: {"status": "ok", "devices": {"total": 1}},
    )


def test_saglik_basligi_komut_kanalini_dusurmez() -> None:
    """REGRESYON: baslik 500'e sebep olunca komut kanali SESSIZCE oluyordu.

    Sahadaki sonuc: `/pending` her saniye 500 dondu, `config_nonce`
    okunamadigi icin yeni eklenen cihaz 5 dakikaya kadar gorulmedi ve SCADA
    komutlari gateway'e HIC ulasmadi. Modulun kendi sozu ("komut kanali
    kutsal") yalnizca basligi URETIRKEN cikan hatalari kapsiyordu; baslik
    uretilip gonderildiginde ve BACKEND patladiginda savunma yoktu.
    """
    session = _BasligaGoreYanit()
    client = _client_saglikli_baslikla(session)

    poll = client.fetch_pending_commands()

    # Komut kanali CALISTI: nonce okundu
    assert poll.config_nonce == 7
    assert poll.refresh_nonce == 2
    assert session.baslikli_istekler == 1, "baslikli istek bir kez denenmeli"
    assert session.basliksiz_istekler == 1, "500 alinca basliksiz yeniden denenmeli"


def test_suclu_baslik_birakilir_ve_cift_istek_surmez() -> None:
    """Baslik suclu bulununca birakilmali; her poll'de iki istek atilmamali."""
    session = _BasligaGoreYanit()
    client = _client_saglikli_baslikla(session)

    client.fetch_pending_commands()  # 1 baslikli + 1 basliksiz
    for _ in range(5):
        poll = client.fetch_pending_commands()
        assert poll.config_nonce == 7

    assert session.baslikli_istekler == 1, (
        f"baslik birakilmamis; {session.baslikli_istekler} kez daha denendi "
        "(her poll'de gereksiz cift istek demek)"
    )
    assert session.basliksiz_istekler == 6


def test_saglikli_backendde_baslik_gonderilmeye_devam_eder() -> None:
    """Yanlis pozitif olmamali: backend sorunsuzsa baslik birakilmaz."""

    class _HerZamanOk:
        def __init__(self) -> None:
            self.baslikli = 0

        def get(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            from dnp3_gateway.backend import health_header

            if health_header.HEADER_NAME in (kwargs.get("headers") or {}):
                self.baslikli += 1
            return _Resp({"commands": [], "config_nonce": 1, "refresh_nonce": 0, "is_active": True})

        def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            return _Resp({"ok": True})

    session = _HerZamanOk()
    client = _client_saglikli_baslikla(session)
    for _ in range(4):
        client.fetch_pending_commands()
    assert session.baslikli == 4, "saglikli backendde baslik gonderilmeye devam etmeli"


def test_baslik_disi_500_yutulmaz() -> None:
    """Baslikla ilgisi olmayan bir 500 GIZLENMEMELI — hata yine raise edilir."""

    class _HerZaman500:
        def get(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            return _Resp({"detail": "bozuk"}, status_code=500)

        def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            return _Resp({"ok": True})

    client = _client_saglikli_baslikla(_HerZaman500())
    with pytest.raises(GatewayConfigError):
        client.fetch_pending_commands()
