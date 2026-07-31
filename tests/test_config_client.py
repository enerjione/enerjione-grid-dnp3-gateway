from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient, GatewayConfigError


def _dev_identity(*, code: str = "GW-001", token: str = "tok") -> GatewayIdentity:
    return GatewayIdentity(
        gateway_code=code,
        token=token,
        instance_id="test-instance",
        app_version="0.0.0-test",
        app_environment="development",
    )


class _DummyRaw:
    """response.raw mock — config_client `stream=True + raw.read(...)` cagiriyor."""

    def __init__(self, body_bytes: bytes) -> None:
        self._body = body_bytes
        self._offset = 0

    def read(self, n: int = -1, decode_content: bool = False) -> bytes:
        _ = decode_content
        if n < 0:
            chunk = self._body[self._offset:]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset : self._offset + n]
        self._offset += len(chunk)
        return chunk


class _DummyResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        body = json.dumps(payload) if isinstance(payload, dict | list) else str(payload)
        self.text = body
        self.content = body.encode("utf-8")
        self.headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.content)),
        }
        self.raw = _DummyRaw(self.content)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size: int = 65536, decode_unicode: bool = False):  # noqa: ARG002
        # Tek chunk halinde dondur — testler icin yeterli.
        if self.content:
            yield self.content

    def close(self) -> None:
        return None


class _DummySession:
    def __init__(self, response: _DummyResponse | Exception) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 5,
        stream: bool = False,  # config_client `stream=True` cagiriyor
        verify: bool | str = True,  # response_max_bytes streaming icin
        **_kwargs: Any,
    ):
        _ = timeout, stream, verify
        self.last_url = url
        self.last_headers = headers
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_fetch_config_parses_response() -> None:
    payload = {
        "gateway_code": "GW-001",
        "gateway_name": "Main GW",
        "batch_interval_sec": 5,
        "max_devices": 100,
        "is_active": True,
        "config_version": "abc123",
        "devices": [
            {
                "code": "DEV-1",
                "name": "Dev 1",
                "ip_address": "10.0.0.1",
                "dnp3_address": 2,
                "poll_interval_sec": 10,
            }
        ],
        "signals": [
            {
                "key": "master.actual_current",
                "label": "Actual Current",
                "unit": "mA",
                "source": "master",
                "dnp3_class": "Class 1",
                "data_type": "analog",
                "dnp3_object_group": 30,
                "dnp3_index": 5,
                "scale": 1.0,
                "offset": 0.0,
                "supports_alarm": True,
            }
        ],
    }
    session = _DummySession(_DummyResponse(200, payload))
    client = BackendConfigClient(
        base_url="http://api/api/v1",
        identity=_dev_identity(token="tok"),
        session=session,  # type: ignore[arg-type]
    )

    cfg = client.fetch_config()

    assert cfg.gateway_code == "GW-001"
    assert cfg.is_active is True
    assert cfg.config_version == "abc123"
    assert len(cfg.devices) == 1
    assert cfg.devices[0].ip_address == "10.0.0.1"
    assert cfg.devices[0].dnp3_address == 2
    assert cfg.devices[0].dnp3_tcp_port is None
    assert len(cfg.signals) == 1
    assert cfg.signals[0].key == "master.actual_current"
    assert cfg.signals[0].supports_alarm is True

    assert session.last_url == "http://api/api/v1/gateways/GW-001/config"
    assert session.last_headers is not None
    assert session.last_headers.get("X-Gateway-Token") == "tok"
    assert session.last_headers.get("X-Gateway-Code") == "GW-001"
    assert session.last_headers.get("X-Gateway-Instance-Id") == "test-instance"
    assert "X-Request-Id" in session.last_headers
    assert "User-Agent" in session.last_headers


def test_fetch_config_raises_on_http_error() -> None:
    session = _DummySession(_DummyResponse(401, {"detail": "bad token"}))
    client = BackendConfigClient(
        base_url="http://api/api/v1",
        identity=_dev_identity(token="tok"),
        session=session,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayConfigError):
        client.fetch_config()


def test_fetch_config_raises_on_network_error() -> None:
    session = _DummySession(requests.ConnectionError("no route"))
    client = BackendConfigClient(
        base_url="http://api/api/v1",
        identity=_dev_identity(token="tok"),
        session=session,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayConfigError):
        client.fetch_config()


def test_fetch_config_device_tcp_port() -> None:
    payload = {
        "config_version": "v2",
        "devices": [
            {
                "code": "A",
                "ip_address": "192.168.0.1",
                "dnp3_address": 10,
                "dnp3_tcp_port": 20000,
            },
            {"code": "B", "ip_address": "192.168.0.2", "dnp3_address": 11, "tcp_port": 15000},
        ],
        "signals": [],
    }
    session = _DummySession(_DummyResponse(200, payload))
    client = BackendConfigClient(
        base_url="http://api/api/v1",
        identity=_dev_identity(),
        session=session,  # type: ignore[arg-type]
    )
    cfg = client.fetch_config()
    assert cfg.devices[0].dnp3_tcp_port == 20000
    assert cfg.devices[1].dnp3_tcp_port == 15000


def test_fetch_config_ignores_missing_items() -> None:
    payload = {
        "gateway_code": "GW-001",
        "devices": [{"name": "no-code"}, {"code": "OK", "ip_address": "1.1.1.1"}],
        "signals": [{"label": "no-key"}, {"key": "master.test", "data_type": "binary"}],
    }
    session = _DummySession(_DummyResponse(200, payload))
    client = BackendConfigClient(
        base_url="http://api/api/v1",
        identity=_dev_identity(token="tok"),
        session=session,  # type: ignore[arg-type]
    )
    cfg = client.fetch_config()
    assert [d.code for d in cfg.devices] == ["OK"]
    assert [s.key for s in cfg.signals] == ["master.test"]
