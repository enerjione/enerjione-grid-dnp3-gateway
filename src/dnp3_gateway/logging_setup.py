"""Minimal yapilandirilabilir log ayarlari + sirli verileri redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Modul-level redaction registry: bilinen sirlari (token, parola) tutar.
# Logger formatter'i mesajlarda bu degerleri ***REDACTED*** ile yer degistirir.
# Boylece exception stack-trace'leri veya 3rd party library log'lari sirri
# kazara yazsa bile docker logs / ELK / Loki'ye gidemez.
_redacted_secrets: list[str] = []
_redacted_lock_safe = True  # GIL altinda atomic; lock gereksiz


def register_secret(value: str | None) -> None:
    """Verilen string'i redaction listesine ekler. Bos/None ignore.

    main.py boot'ta gateway_token, rabbitmq_url password vs. cagirir.
    """
    if not value:
        return
    v = value.strip()
    if len(v) < 4:
        # Cok kisa string'leri redact etme — false positive riski yuksek
        return
    if v not in _redacted_secrets:
        _redacted_secrets.append(v)


def _scrub_message(text: str) -> str:
    """Tum kayitli sirlari mesajdan redact eder."""
    if not text or not _redacted_secrets:
        return text
    # En uzundan basla (kisa secret uzun bir secret'in alt-string'i olabilir)
    for secret in sorted(_redacted_secrets, key=len, reverse=True):
        if secret and secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


# Broker URL parolasini regex ile maskeler:
#   amqp://user:password@host  →  amqp://user:***@host
#   nats://user:password@host  →  nats://user:***@host
#   tls://user:password@host   →  tls://user:***@host   (NATS TLS varyanti)
# Genel safety net: kullanici register_secret unutsa bile parola log'a sizmasin.
# Onceden sadece AMQP yakaliyordu; NATS cutover sonrasi NATS URL'leri de
# log'lara sizabilir.
_BROKER_PASSWORD_RE = re.compile(
    r"((?:amqp|amqps|nats|tls)://[^:@\s/]+):([^@\s/]+)@",
    re.IGNORECASE,
)


def _scrub_broker_passwords(text: str) -> str:
    if not text:
        return text
    low = text.lower()
    # Early-out: hicbir broker scheme yoksa regex'i atla (CPU tasarruf)
    if "amqp" not in low and "nats" not in low and "tls://" not in low:
        return text
    return _BROKER_PASSWORD_RE.sub(r"\1:***@", text)


# Geriye uyumluluk: eski `_scrub_amqp_passwords` cagirilari yeni broker
# fonksiyonuna delege edilir.
def _scrub_amqp_passwords(text: str) -> str:
    return _scrub_broker_passwords(text)


class _RedactionFilter(logging.Filter):
    """Tum log record'lardaki secret'leri redact eder.

    Hem `record.msg` hem `record.args` islenir; record.getMessage() her ikisini
    de kullanir. Exception text de redact edilir.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Mesaj: % formatlama oncesi
            if isinstance(record.msg, str):
                record.msg = _scrub_amqp_passwords(_scrub_message(record.msg))
            # Args (tuple veya dict)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        _scrub_amqp_passwords(_scrub_message(a)) if isinstance(a, str) else a
                        for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (_scrub_amqp_passwords(_scrub_message(v)) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
        except Exception:  # noqa: BLE001 — filter asla crash etmemeli
            pass
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        # Bir kere daha redact (bazi exception path'lerde args zaten consume edilmis olabilir)
        message = _scrub_amqp_passwords(_scrub_message(message))
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            payload["exc"] = _scrub_amqp_passwords(_scrub_message(exc_text))
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = (
                    _scrub_amqp_passwords(_scrub_message(value)) if isinstance(value, str) else value
                )
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """Default text formatter + final redaction safety net."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return _scrub_amqp_passwords(_scrub_message(text))


def _build_formatter(fmt: str) -> logging.Formatter:
    if fmt.strip().lower() == "json":
        return _JsonFormatter()
    return _TextFormatter(
        fmt="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _resolve_log_file_path(
    template: str,
    *,
    gateway_code: str = "",
    instance_id: str = "",
) -> Path:
    """{gateway_code} ve {instance_id} yer tutucularini resolve eder.

    Cozulemeyen yer tutucu (eksik kwarg) literal kalir — file open() asamasinda
    OSError verirse caller handle eder.
    """
    safe_code = (gateway_code or "default").strip() or "default"
    safe_instance = (instance_id or "").strip() or "noid"
    # Path safety: gateway_code'da slash/backslash izin verilmiyor (Settings
    # zaten regex ile sinirliyor) ama defansif olarak temizle.
    safe_code = safe_code.replace("/", "_").replace("\\", "_")
    safe_instance = safe_instance.replace("/", "_").replace("\\", "_")
    try:
        rendered = template.format(
            gateway_code=safe_code,
            instance_id=safe_instance,
        )
    except (KeyError, IndexError):
        # Bilinmeyen yer tutucu — template aynen kalsin
        rendered = template
    return Path(rendered)


def configure_logging(
    level: str = "INFO",
    fmt: str = "text",
    *,
    file_path: str = "",
    file_max_bytes: int = 20 * 1024 * 1024,
    file_backup_count: int = 10,
    gateway_code: str = "",
    instance_id: str = "",
) -> None:
    """Root logger'i idempotent bicimde yapilandirir.

    Birden fazla cagri guvenlidir (handler cogaltilmaz).

    Onemli: Bu noktadan SONRA register_secret() cagirilmasi onerilir; cunku
    filter aktif olduktan sonra eklenen secret'lar bir sonraki log'dan itibaren
    geçerli olur. Boot sirasinda ilk cagri configure_logging, sonra
    register_secret(token) ve register_secret(rabbitmq_url) yapilir.

    Argumanlar:
      level: log seviyesi (INFO, DEBUG vb.)
      fmt: "text" veya "json"
      file_path: Bos degilse RotatingFileHandler acilir. {gateway_code} ve
        {instance_id} yer tutuculari resolve edilir. Disk yazma hatasi
        gateway'i durdurmamak icin yutulur (sadece stderr'a yazilir).
      file_max_bytes: Tek log dosyasi maksimum boyut (rotate threshold).
      file_backup_count: Rotation sonrasi tutulan eski dosya sayisi.
    """

    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "_dnp3_gateway", False):
            root.removeHandler(handler)

    redaction_filter = _RedactionFilter()

    # 1) stdout handler her zaman acik kalir — Docker / NSSM bunu yakalar.
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler._dnp3_gateway = True  # type: ignore[attr-defined]
    stream_handler.setFormatter(_build_formatter(fmt))
    stream_handler.addFilter(redaction_filter)
    root.addHandler(stream_handler)

    # 2) Rotating dosya handler — sadece LOG_FILE_PATH set edilirse. Per-instance
    #    log path izolasyonu icin {gateway_code} yer tutucusu resolve edilir.
    if file_path and file_path.strip():
        log_path = _resolve_log_file_path(
            file_path.strip(),
            gateway_code=gateway_code,
            instance_id=instance_id,
        )
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                filename=str(log_path),
                maxBytes=max(1024 * 1024, int(file_max_bytes)),
                backupCount=max(1, int(file_backup_count)),
                encoding="utf-8",
                delay=False,
            )
            file_handler._dnp3_gateway = True  # type: ignore[attr-defined]
            file_handler.setFormatter(_build_formatter(fmt))
            file_handler.addFilter(redaction_filter)
            # NTFS multi-instance collision'i onlemek icin dosya basina handler
            # mutex'i Python tarafindan zaten saglanir; ama AYNI dosyaya iki
            # process yazarsa interleave olur. Caller {gateway_code} yer tutucu
            # ile per-instance path kullanmali — runtime sade defansif kontrol:
            try:
                # Diger Python instance ile cakisma uyarisi (kalin garanti yok,
                # advisory): aciliminda dosya zaten varsa ve baska proses lock
                # tutuyorsa OSError gelir — burada en azindan stderr'a not dus.
                _ = os.stat(str(log_path))
            except OSError:
                pass
            root.addHandler(file_handler)
            # configure_logging henuz dnp3_gateway logger'i tarafindan
            # cagiriliyor; ilk log mesajini handler eklendikten SONRA atalim.
            logging.getLogger("dnp3_gateway").info(
                "log_file_handler_opened path=%s max_bytes=%s backup_count=%s",
                log_path,
                file_max_bytes,
                file_backup_count,
            )
        except OSError as exc:
            # Disk dolu, izin yok, dizin yaratilamadi — gateway calismaya
            # devam etsin, sadece stdout'a yazilsin.
            sys.stderr.write(
                f"[logging_setup] WARNING: rotating log dosyasi acilamadi "
                f"path={log_path} error={exc}; sadece stdout aktif.\n"
            )
            sys.stderr.flush()

    # 3rd-party kutuphane loglarini sessizlestir. Gateway log'unda genelde
    # sadece dnp3_gateway namespace'i altindaki kayitlar gozlenir.
    #
    # NOT: `pika` 0.4.x cutover'i sonrasi runtime'da kullanilmiyor (RabbitMQ
    # kaldirildi) — modul yuklu olmadigi icin logger zaten aktif degil. Eski
    # kontrolun zararı yok ama gereksiz. `nats` ve `urllib3` daha kritik:
    # baglanti retry log'lari INFO seviyesinde gurultu yapabilir, WARNING'e
    # cek.
    logging.getLogger("nats").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
