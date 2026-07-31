"""`.env.example` ile `Settings` alanlarinin senkron kalmasini garanti eder.

NEDEN: `.env.example` config.py'nin 30 alanini KACIRIYORDU — aralarinda
300 cihazlik saha icin kritik olan `DNP3_MANAGER_THREADS`, `COMMAND_POLL_SEC`
ve tum `OUTBOX_RETRIER_*` ayarlari vardi. Operator opendnp3 thread doyumu
yasadiginda `.env.example` ve RUNBOOK'ta arıyor, boyle bir anahtarin
varligindan haberi olmuyor; ancak kaynak kodu okuyarak bulabiliyordu.

Bu test, yeni bir ayar eklendiginde dokumante edilmesini ZORUNLU kilar —
aksi halde CI kirmizi olur.
"""

from __future__ import annotations

import re
from pathlib import Path

from dnp3_gateway.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Bilincli olarak `.env.example`'da `KEY=` satiri OLMAYAN alanlar.
# Hepsi orada duz metin olarak aciklaniyor ya da operatorun set etmesi
# beklenmiyor.
_INTENTIONALLY_UNDOCUMENTED: frozenset[str] = frozenset(
    {
        # Otomatik uretilir (state dizinindeki kalici uuid dosyasi).
        "GATEWAY_INSTANCE_ID",
        # Validator ic esikleri; sahada degistirilmesi beklenmiyor.
        "GATEWAY_TOKEN_MIN_LENGTH_STAGING",
        "GATEWAY_TOKEN_MIN_LENGTH_PRODUCTION",
        # DEPRECATED — .env.example'da "SET ETMEYIN" bolumunde duz metin
        # olarak anlatiliyor; ornek satir vermek yanlis yonlendirir.
        "RABBITMQ_URL",
        "RABBITMQ_EXCHANGE",
        "RABBITMQ_ROUTING_KEY",
        "NATS_DUAL_PUBLISH_ENABLED",
    }
)


def _documented_keys() -> set[str]:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    # Hem aktif (`KEY=deger`) hem yorumlu ornek (`# KEY=deger`) satirlari sayilir.
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", text, re.M))


def _settings_env_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_dosyasi_var() -> None:
    assert _ENV_EXAMPLE.is_file(), ".env.example bulunamadi"


def test_tum_ayarlar_env_example_de_dokumante() -> None:
    documented = _documented_keys()
    eksik = sorted(_settings_env_keys() - documented - _INTENTIONALLY_UNDOCUMENTED)
    assert not eksik, (
        "Bu ayarlar .env.example'da dokumante edilmemis:\n  "
        + "\n  ".join(eksik)
        + "\n\nYeni bir Settings alani eklediyseniz .env.example'a yorumlu bir "
        "ornek satir ekleyin (`# KEY=deger`) ya da bilincli olarak "
        "tests/test_env_example_sync.py:_INTENTIONALLY_UNDOCUMENTED listesine alin."
    )


def test_env_example_de_olu_anahtar_yok() -> None:
    """`.env.example`'da karsiligi olmayan anahtar = operatoru yanıltan olu ayar."""
    documented = _documented_keys()
    known = _settings_env_keys()
    # Deprecated anahtarlar `.env.example`'da anlatiliyor ama Settings'te de
    # (validator reddetsin diye) mevcut — bu yuzden `known` icindeler.
    olu = sorted(documented - known)
    assert not olu, (
        ".env.example'da Settings karsiligi OLMAYAN anahtarlar var "
        "(operator bunlari set eder ve hicbir etkisi olmaz):\n  " + "\n  ".join(olu)
    )


def test_yeni_kritik_ayarlar_dokumante() -> None:
    """300 cihazlik saha icin kritik knob'lar acikca .env.example'da olmali."""
    documented = _documented_keys()
    for key in (
        "DNP3_MANAGER_THREADS",
        "DNP3_TIME_SYNC",
        "DNP3_PUBLISH_QUALITY_FLAGS",
        "COMMAND_POLL_SEC",
        "OUTBOX_RETRIER_BATCH_SIZE",
        "OUTBOX_DEAD_LETTER_RETAIN_DAYS",
        "COMMAND_LEDGER_RETAIN_DAYS",
        "GATEWAY_STATE_DIR",
        "REWRITE_LOOPBACK_TO_HOST",
    ):
        assert key in documented, f"{key} .env.example'da yok"
