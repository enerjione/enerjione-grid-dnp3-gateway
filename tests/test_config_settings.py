from __future__ import annotations

import pytest

from dnp3_gateway.config import Settings


def test_settings_defaults_are_sensible() -> None:
    # Test icin disk'teki .env'yi kullanma
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.gateway_mode == "mock"
    # 0.4.x: RabbitMQ default'lari bos (legacy field, runtime'da kullanilmiyor)
    assert s.rabbitmq_url == ""
    assert s.rabbitmq_exchange == ""
    assert s.rabbitmq_routing_key == ""
    assert s.worker_health_port == 8020
    assert s.is_mock_mode is True
    assert s.is_dnp3_mode is False
    # Default FALSE: token konsola yazilmasin (logging aggregator'larda leak
    # olmasin). Operator istege bagli SHOW_GATEWAY_TOKEN_ON_START=true ile
    # gecici acabilir; production'da validator zaten bunu engeller.
    assert s.show_gateway_token_on_start is False
    # GATEWAY_REFRESH_TOKEN default bos; /refresh-all endpoint'i devre disi
    assert s.gateway_refresh_token == ""
    # Default FALSE — public host'a clear-text HTTP/nats:// validator
    # tarafindan reddedilir; bilincli opt-out gerekir.
    assert s.gateway_insecure_allow_plaintext is False
    # Varsayilan telemetri yolu: NATS JetStream (STANDART) — telemetri
    # backend'e ugramaz. HTTP ingest yalnizca bilincli rollback.
    assert s.telemetry_publisher == "nats"
    assert s.nats_publish_timeout_sec == pytest.approx(2.0)


def test_dnp3_mode_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.is_dnp3_mode is True
    assert s.is_mock_mode is False


# ---- Production validator: private vs. public host esnemesi ---------------
# Validator artik host bazli karar veriyor:
#   - Public host (orn. 77.83.37.44, example.com) -> https://+tls:// zorunlu
#   - Private host (10.x, 192.168.x, 127.x, *.local, localhost) -> http://+nats:// ok
#   - GATEWAY_INSECURE_ALLOW_PLAINTEXT=true -> public host'a da clear-text izin


def _strong_token() -> str:
    # `x` placeholder prefix listesinde olmayan, 40 char rastgele-gibi token.
    # 32 char prod min'den uzun; `_is_placeholder_token` False doner.
    return "x" * 40


def test_prod_allows_http_to_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Internal deploy: backend 192.168.x'te, gateway prod ortaminda."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "http://192.168.1.10:8000/api/v1")
    monkeypatch.setenv("NATS_URL", "nats://192.168.1.10:4222")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.backend_api_url.startswith("http://")
    assert s.nats_url.startswith("nats://")


def test_prod_allows_http_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-host deploy: backend container 127.0.0.1'de."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "http://127.0.0.1:8000/api/v1")
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")
    Settings(_env_file=None)  # type: ignore[call-arg]  # raise etmemeli


def test_prod_rejects_http_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saha hatasi: public IP'ye clear-text HTTP — token MITM riski."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "http://77.83.37.44:8000/api/v1")
    monkeypatch.setenv("NATS_URL", "nats://77.83.37.44:4222")
    with pytest.raises(ValueError, match="public host"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_rejects_nats_clear_text_to_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy NATS publisher secilirse public clear-text reddedilmeli."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "https://api.example.com/api/v1")
    monkeypatch.setenv("TELEMETRY_PUBLISHER", "nats")
    monkeypatch.setenv("NATS_URL", "nats://nats.example.com:4222")
    with pytest.raises(ValueError, match="public NATS host"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_allows_public_http_when_insecure_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator bilincli opt-out: TLS henuz kurulamadi, public IP'de calisiyor."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "http://77.83.37.44:8000/api/v1")
    monkeypatch.setenv("NATS_URL", "nats://77.83.37.44:4222")
    monkeypatch.setenv("GATEWAY_INSECURE_ALLOW_PLAINTEXT", "true")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.gateway_insecure_allow_plaintext is True


def test_prod_default_nats_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production'da hicbir sey set edilmezse STANDART yol NATS'tir.

    Private/compose-ici host'a nats:// kabul (tls zorunlulugu yalnizca
    public host icin)."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "https://api.example.com/api/v1")
    monkeypatch.setenv("NATS_URL", "nats://192.168.1.10:4222")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.telemetry_publisher == "nats"


def test_prod_http_rollback_ignores_nats_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bilincli HTTP rollback'te NATS_URL validate edilmez."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("TELEMETRY_PUBLISHER", "http")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "https://api.example.com/api/v1")
    monkeypatch.setenv("NATS_URL", "bozuk-url")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.telemetry_publisher == "http"


def test_prod_accepts_https_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hedef deploy: backend HTTPS, NATS TLS — temiz prod konfigurasyonu."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("NATS_URL", "tls://nats.enerjione.local:4222")
    Settings(_env_file=None)  # type: ignore[call-arg]


# ---- Production validator: yeni 0.4.6 kontrolleri --------------------------


def test_prod_rejects_placeholder_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example`'dan kopyalanan `gw-001-token` reddedilmeli."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", "gw-001-token-with-some-extra-chars-to-pass-min-len")
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("NATS_URL", "tls://nats.enerjione.local:4222")
    with pytest.raises(ValueError, match="placeholder"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_rejects_change_me_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`change-me...` prefix'i reddedilmeli."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", "change-me-and-fill-with-real-token-32-chars-pls")
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("NATS_URL", "tls://nats.enerjione.local:4222")
    with pytest.raises(ValueError, match="placeholder"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_rejects_rabbitmq_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eski .env'den kalan `RABBITMQ_URL` production'da reddedilmeli."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("NATS_URL", "tls://nats.enerjione.local:4222")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://user:pass@rmq:5672/")
    with pytest.raises(ValueError, match="RABBITMQ_URL"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_rejects_dual_publish_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEPRECATED `NATS_DUAL_PUBLISH_ENABLED=true` production'da reddedilmeli."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", _strong_token())
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("NATS_URL", "tls://nats.enerjione.local:4222")
    monkeypatch.setenv("NATS_DUAL_PUBLISH_ENABLED", "true")
    with pytest.raises(ValueError, match="NATS_DUAL_PUBLISH_ENABLED"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_dev_allows_rabbitmq_url_and_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Development ortaminda eski placeholder + rabbitmq_url kabul edilir
    (operator yerel test yapabilir, deploy oncesi temizleyecek)."""
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    monkeypatch.setenv("GATEWAY_TOKEN", "gw-001-token")  # placeholder, dev OK
    monkeypatch.setenv("BACKEND_API_URL", "http://127.0.0.1:8000/api/v1")
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")
    monkeypatch.setenv("NATS_DUAL_PUBLISH_ENABLED", "true")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.gateway_token == "gw-001-token"
    assert s.rabbitmq_url.startswith("amqp://")
    assert s.nats_dual_publish_enabled is True
