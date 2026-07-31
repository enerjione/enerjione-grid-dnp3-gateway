from __future__ import annotations

import pytest

from dnp3_gateway.auth import resolve_instance_id
from dnp3_gateway.auth.identity import ensure_credentials_allowed
from dnp3_gateway.config import Settings


def test_resolve_instance_id_uses_env_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("GATEWAY_INSTANCE_ID", "fixed-id-1")
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        gateway_code="GW-001",
        gateway_instance_id="fixed-id-1",
        gateway_state_dir=str(tmp_path / "st"),
    )
    assert resolve_instance_id(settings=s) == "fixed-id-1"


def test_resolve_instance_id_persists_to_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("GATEWAY_INSTANCE_ID", raising=False)
    state_dir = tmp_path / "st"
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        gateway_code="GW-A",
        gateway_instance_id="",
        gateway_state_dir=str(state_dir),
    )
    a = resolve_instance_id(settings=s)
    b = resolve_instance_id(settings=s)
    assert a == b
    assert len(a) == 36  # uuid4 string
    assert any(state_dir.glob("instance_*.id"))


def _set_prod_safe_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production safeguard validator'inin URL kontrollerini gecmek icin
    test ortaminda gecerli URL'leri set et. Bu testlerin amaci token
    dogrulamasi (ensure_credentials_allowed); URL validator bagimsiz olmali.

    Gateway 0.4.x'te:
      * BACKEND_API_URL prod'da https:// olmali
      * NATS_URL prod'da bos olamaz, tls:// veya nats:// scheme olmali
      * RABBITMQ_URL gateway tarafindan kullanilmiyor (LEGACY)
    """
    monkeypatch.setenv("BACKEND_API_URL", "https://backend.test/api/v1")
    monkeypatch.setenv("NATS_URL", "tls://nats.test:4222")


def test_staging_rejects_placeholder_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ensure_credentials_allowed` exact-match placeholder reddi (staging).

    NOT: production'da bu kontrol Settings model_validator tarafindan ZATEN
    yakalanir (prefix-bazli `_is_placeholder_token`). Bu test auth katmanindaki
    literal `_PLACEHOLDER_TOKENS` set'inin staging'de hala devrede oldugunu
    dogrular — savunma derinligi.
    """
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("GATEWAY_TOKEN", "gw-default-token")
    monkeypatch.setenv("GATEWAY_CODE", "GW-001")
    _set_prod_safe_urls(monkeypatch)
    # staging'de placeholder + 15 char min length; gw-default-token 16 char,
    # min'e takilmaz, ama exact-match'le placeholder olarak yakalanir.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(SystemExit):
        ensure_credentials_allowed(s)


def test_staging_requires_min_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging'de cok kisa token reddedilir (min 16 char)."""
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("GATEWAY_TOKEN", "short")
    monkeypatch.setenv("GATEWAY_CODE", "GW-001")
    _set_prod_safe_urls(monkeypatch)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(SystemExit):
        ensure_credentials_allowed(s)


def test_production_validator_blocks_placeholder_at_construct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production'da Settings() construction'i bile geçmez — erken yakalama."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")  # prod\'da mock yasak
    monkeypatch.setenv("GATEWAY_TOKEN", "change-me-and-fill-in-real-token-32-ch")
    monkeypatch.setenv("GATEWAY_CODE", "GW-001")
    _set_prod_safe_urls(monkeypatch)
    # Settings construction sirasinda ValueError; ensure_credentials_allowed
    # noktasina kadar varmaz.
    with pytest.raises(ValueError, match="placeholder"):
        Settings(_env_file=None)  # type: ignore[call-arg]
