"""Disk alani ve sistem saati denetimi — /health'in gormedigi iki kor nokta.

DISK
----
"Disk-full circuit breaker" diye adlandirilan mekanizma aslinda DISKI HIC
GORMUYORDU: `outbox_max_pending` MESAJ SAYAR, bayt saymaz. `shutil.disk_usage`
cagrisi tum kod tabaninda yoktu.

Sinirsiz buyuyen kalemler: outbox (500K x ~400B ~ 200MB), dead_letter, WAL,
command_ledger, log (20MB x 10 = 200MB) — hepsi genelde ayni surucude.
Saha PC'sinin C: surucusunde 500MB kaldiginda breaker TETIKLENMEZ cunku
pending sayisi henuz 300.000'dir. Disk dolar -> `outbox.enqueue`
`OperationalError` atar -> retrier olur, log rotation kirilir, poller durur.
Uc ayri arizanin ortak tetikleyicisi olan "disk" degiskeni hicbir yerde
olculmuyordu.

SAAT
----
Gateway host saati TUM arsivin tek zaman referansidir (telemetri damgasi,
stale/recovery kararlari) ve hicbir yerde dogrulanmiyordu. Boot'ta saat
sagligi kontrolu yok, /health'te saat alani yok, NTP kurulum on kosulu olarak
yazilmamis.

Windows Server uzun sure WAN'siz kalip w32time senkronize olunca saat ileri/geri
adim atar. Sonuc: (a) tum cihazlar ayni anda "stale" gorunup toplu sahte
comm_lost dalgasi, (b) arsivde zaman ekseninde cakisan/geri giden kayitlar,
(c) DNP3 zaman senkronizasyonu acikken ayni yanlis saat 300 outstation'a
YAZILIR ve saha cihazlarinin olay tamponlari da bozulur.

Bu modul sapmayi backend HTTP yanitindaki `Date` header'i ile olcer (ek altyapi
gerektirmez) ve sapma buyukse zaman yazimini durdurur.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Serbest alan esikleri (byte).
DISK_WARN_BYTES = 1024 * 1024 * 1024      # 1 GB  -> /health degraded
DISK_CRITICAL_BYTES = 256 * 1024 * 1024   # 256 MB -> yeni yayin durdurulur

# Saat sapmasi esikleri (saniye).
CLOCK_WARN_SKEW_SEC = 2.0     # /health degraded
CLOCK_UNSAFE_SKEW_SEC = 30.0  # DNP3 zaman yazimi durdurulur


class DiskGuard:
    """State dizininin serbest alanini izler."""

    def __init__(self, state_dir: str | Path) -> None:
        self._path = Path(state_dir)
        self._lock = threading.Lock()
        self._last: dict[str, Any] = {}

    def check(self) -> dict[str, Any]:
        """Serbest alani olcer ve durum sozlugu doner (asla exception atmaz)."""
        result: dict[str, Any] = {
            "path": str(self._path),
            "free_bytes": None,
            "total_bytes": None,
            "level": "unknown",
        }
        try:
            probe = self._path if self._path.exists() else self._path.parent
            usage = shutil.disk_usage(str(probe))
            free = int(usage.free)
            result["free_bytes"] = free
            result["total_bytes"] = int(usage.total)
            if free <= DISK_CRITICAL_BYTES:
                result["level"] = "critical"
            elif free <= DISK_WARN_BYTES:
                result["level"] = "low"
            else:
                result["level"] = "ok"
        except OSError as exc:
            result["error"] = str(exc)[:200]
        with self._lock:
            previous = self._last.get("level")
            self._last = result
        # Edge-trigger log: seviye degistiginde bir kez.
        if previous != result["level"]:
            free_mb = (result["free_bytes"] or 0) / (1024 * 1024)
            if result["level"] == "critical":
                logger.error(
                    "disk_space_critical path=%s free_mb=%.0f — yeni telemetri "
                    "diske yazilamayabilir; outbox/log/dead-letter temizligi GEREK",
                    result["path"], free_mb,
                )
            elif result["level"] == "low":
                logger.warning(
                    "disk_space_low path=%s free_mb=%.0f", result["path"], free_mb
                )
            elif result["level"] == "ok" and previous in ("low", "critical"):
                logger.info("disk_space_recovered path=%s free_mb=%.0f", result["path"], free_mb)
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last)

    @property
    def is_critical(self) -> bool:
        with self._lock:
            return self._last.get("level") == "critical"


class ClockGuard:
    """Gateway saatinin backend saatine gore sapmasini izler.

    Ek altyapi (NTP istemcisi) gerektirmez: backend zaten her config/komut
    isteginde HTTP `Date` basligi donuyor.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._skew_sec: float | None = None
        self._checked_at: float | None = None
        self._warned_level: str | None = None

    def observe_http_date(self, header_value: str | None) -> None:
        """Backend yanitindaki `Date` basligindan sapmayi hesapla."""
        if not header_value:
            return
        try:
            server_dt = parsedate_to_datetime(header_value)
        except (TypeError, ValueError):
            return
        if server_dt is None:
            return
        try:
            server_epoch = server_dt.timestamp()
        except (OverflowError, ValueError, OSError):
            return
        # HTTP Date saniye cozunurluklu; +-1sn'lik kirpma hatasi normaldir.
        skew = time.time() - server_epoch
        with self._lock:
            self._skew_sec = skew
            self._checked_at = time.time()
        self._maybe_log(skew)

    def _maybe_log(self, skew: float) -> None:
        magnitude = abs(skew)
        if magnitude >= CLOCK_UNSAFE_SKEW_SEC:
            level = "unsafe"
        elif magnitude >= CLOCK_WARN_SKEW_SEC:
            level = "warn"
        else:
            level = "ok"
        with self._lock:
            previous = self._warned_level
            self._warned_level = level
        if previous == level:
            return
        if level == "unsafe":
            logger.error(
                "clock_skew_unsafe skew_sec=%.1f — gateway saati backend'den ciddi "
                "sapiyor. Telemetri zaman damgalari yanlis arsivlenir ve DNP3 zaman "
                "senkronizasyonu DURDURULDU (yanlis saati 300 cihaza yazmaktansa hic "
                "yazmamak yeglenir). Sunucuda NTP/w32time servisini kontrol edin.",
                skew,
            )
        elif level == "warn":
            logger.warning("clock_skew_detected skew_sec=%.1f", skew)
        elif previous in ("warn", "unsafe"):
            logger.info("clock_skew_recovered skew_sec=%.1f", skew)

    @property
    def is_safe_for_time_sync(self) -> bool:
        """Outstation'lara saat yazmak guvenli mi?

        Hic olcum yapilmadiysa True (backend'e hic ulasilamamis olabilir;
        eski davranisi bozmayalim).
        """
        with self._lock:
            if self._skew_sec is None:
                return True
            return abs(self._skew_sec) < CLOCK_UNSAFE_SKEW_SEC

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            skew = self._skew_sec
            checked = self._checked_at
        return {
            "skew_sec": round(skew, 2) if skew is not None else None,
            "checked_at_epoch": checked,
            "safe_for_time_sync": self.is_safe_for_time_sync,
        }
