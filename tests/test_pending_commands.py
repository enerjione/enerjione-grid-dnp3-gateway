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

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient


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
