"""Gateway kimlik modeli, kalıcı instance_id ve uretim ortami token validasyonu.

Tasarim hedefleri
-----------------
* **Bir proses = bir logical gateway** (`GATEWAY_CODE`). Ayni sunucuda
  birden cok proses, farkli `GATEWAY_CODE` + `GATEWAY_TOKEN` + `HEALTH_PORT`
  ile yuk paylasir; backend cihazlari `devices.gateway_code` ile dagitir.
* **Token paylasimi yok:** Her `gateways` satirinin gizli `token` degeri
  yalniz o instance'in `.env` / secret store'unda tutulur. Token URL'de
  tasınmaz; yalnizca `X-Gateway-Token` header'ında gider.
* **Ornek ID:** Ayni `GATEWAY_CODE` ile farkli makinada yanlislikla iki proses
  calisirsa backend `last_seen`/`config` cekme loglari karisir; `instance_id`
  destek loglarinda hangi node oldugunu ayirt eder. Varsayilan: diskte kalici
  UUID (servis her restart'ta ayni kalir).
* **Ileride mTLS / imza:** Http katmanindaki `build_config_request_headers` tek
  noktada; ileride `Authorization: HMAC ...` veya mTLS ayni client icin
  genisletilebilir.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from dnp3_gateway.config import Settings

# Backend seed / dokumanlardaki tam-string ornekler; uretimde kabul edilmez.
# Bu liste literal tam-eslesme icindir; daha genis prefix-bazli kontrol
# `config._PLACEHOLDER_TOKEN_PREFIXES` icinde — production validator orada
# `_is_placeholder_token` ile yakalar. Iki kontrol birbirini tamamlar:
#   * Bu liste (identity.py): exact-match, staging + production icin.
#   * Prefix listesi (config.py): broader pattern, sadece production'da.
_PLACEHOLDER_TOKENS = frozenset(
    {
        "gw-default-token",
        "change-me",
        "changeme",
        "please-change-me",
        "default-token",
        "example-token",
    }
)

_CODE_SAFE = re.compile(r"^[\w][\w-]{0,62}$")
_INSTANCE_SAFE = re.compile(r"^[\w.-]{0,120}$")


@dataclass(frozen=True)
class GatewayIdentity:
    """Bir prosesin backend'e kendini tanittigi alanlar."""

    gateway_code: str
    token: str
    instance_id: str
    app_version: str
    app_environment: str


def resolve_instance_id(*, settings: Settings) -> str:
    """Ortamda sabit `GATEWAY_INSTANCE_ID` yoksa state dosyasina kalici UUID yazar.

    Coklu gateway ayni `GATEWAY_STATE_DIR` paylasabilsin diye dosya adi
    `instance_{gateway_code}.id` seklindedir.
    """

    raw = (settings.gateway_instance_id or "").strip()
    if raw:
        if not _INSTANCE_SAFE.match(raw):
            msg = f"GATEWAY_INSTANCE_ID gecersiz format: {raw!r}"
            raise SystemExit(msg)
        return raw

    base = Path(settings.gateway_state_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"GATEWAY_STATE_DIR olusturulamadi: {base!s} hata={exc}. "
            "Dizin izinlerini kontrol edin veya GATEWAY_INSTANCE_ID ortam degeri verin."
        ) from exc

    safe_code = re.sub(r"[^\w-]+", "_", settings.gateway_code.strip())[:50] or "gw"
    path = base / f"instance_{safe_code}.id"
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"instance id okunamadi: {path!s} {exc}") from exc
        if existing and _INSTANCE_SAFE.match(existing):
            return existing

    new_id = str(uuid.uuid4())
    try:
        path.write_text(new_id + "\n", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(
            f"instance id yazilamadi: {path!s} hata={exc}. Gecici cozum: GATEWAY_INSTANCE_ID ortam degeri atayin."
        ) from exc
    return new_id


def _validate_gateway_code(value: str) -> None:
    if not value or not _CODE_SAFE.match(value.strip()):
        raise SystemExit(
            f"GATEWAY_CODE gecersiz: {value!r}. Sadece harf, rakam, tire, alt cizgi; 1-63 karakter."
        )


def ensure_credentials_allowed(settings: Settings) -> None:
    """Uretimde zayif token ile yanlislikla acilmasini engellemek icin sert kontrol.

    Gelistirme: `APP_ENVIRONMENT=development` (varsayilan) ile geysek.
    Staging/Production: minimum uzunluk + placeholder yasak.
    """

    code = settings.gateway_code.strip()
    token = settings.gateway_token.strip()
    _validate_gateway_code(code)
    if not token:
        raise SystemExit("GATEWAY_TOKEN bos olamaz. Backend gateways.token ile ayni gizli degeri atayin.")
    env = settings.app_environment.strip().lower()
    if env not in ("development", "staging", "production", "dev", "stg", "prod"):
        raise SystemExit(
            "APP_ENVIRONMENT degeri development | staging | production olmali (veya kisa: dev, stg, prod)."
        )
    if env in ("dev", "development"):
        return
    if token.lower() in {t.lower() for t in _PLACEHOLDER_TOKENS}:
        raise SystemExit(
            f"Uretim ortaminda (APP_ENVIRONMENT={env!r}) placeholder token kullanilamaz. "
            "Backendde guclu rastgele token olusturup .env ile esleyin."
        )
    is_staging = env in ("staging", "stg")
    min_len = (
        int(settings.gateway_token_min_length_staging)
        if is_staging
        else int(settings.gateway_token_min_length_production)
    )
    if len(token) < min_len:
        raise SystemExit(f"GATEWAY_TOKEN cok kisa: en az {min_len} karakter olmali (ortam: {env}).")


def bootstrap_gateway_identity(*, settings: Settings, app_version: str) -> GatewayIdentity:
    """Baslangiçta cagir: instance_id + normallesmis environment string."""

    ensure_credentials_allowed(settings)
    instance = resolve_instance_id(settings=settings)
    env = settings.app_environment.strip().lower()
    if env in ("dev", "development"):
        env_out = "development"
    elif env in ("stg", "staging"):
        env_out = "staging"
    elif env in ("prod", "production"):
        env_out = "production"
    else:
        env_out = env
    return GatewayIdentity(
        gateway_code=settings.gateway_code.strip(),
        token=settings.gateway_token.strip(),
        instance_id=instance,
        app_version=app_version,
        app_environment=env_out,
    )
