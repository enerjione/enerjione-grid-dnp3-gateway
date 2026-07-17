"""Gateway'in calisma anindaki durumunu tutan thread-safe yapidir.

Ayni state'i birden fazla thread okur/yazar:

  * `main._run_config_refresh`  -> `update()` ile yeni config bastirir
  * `main._poll_cycle`          -> `due_devices()` / `signals()` ile okuma yapar
  * `main._poll_cycle`          -> `mark_read()` ile son okuma zamanini yazar
  * `health_server`             -> `snapshot()` ile durum raporu uretir

Bu yuzden tum erisimler tek bir `Lock` altindadir.

Persistence: opsiyonel olarak disk'e (`/app/.gateway_state/config.json`)
yazilir. Boylece backend kapali iken / container restart'ta gateway en son
gordugu config ile (cihaz IP'leri, sinyal listesi, master_address) calismaya
devam eder; backend'e ulasilmadan da DNP3 cihazlari okunmaya basar. Yeni
config geldiginde diskteki snapshot uzerine yazilir.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from typing import Any

from dnp3_gateway.backend import DeviceConfig, GatewayConfig, PendingCommand, SignalConfig

logger = logging.getLogger(__name__)

# Cache disk'te bu kadar saatten daha eski ise stale kabul edilir; load_from_cache
# yine de yukler ama bunu loglar/snapshot'a yansitir. Backend uzun sure down ise
# operator backend baglantisini cozmedikce eski cihaz IP'leriyle calismaya
# devam eder — bu surec uzun olmamali (cihaz IP'leri/sinyal config'i degismis
# olabilir). 24 saat varsayilan; production'da CONFIG_CACHE_MAX_AGE_HOURS ile
# override edilebilir.
DEFAULT_CACHE_MAX_AGE_HOURS = 24


class GatewayState:
    def __init__(
        self,
        *,
        cache_path: str | os.PathLike | None = None,
        cache_max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS,
    ) -> None:
        self._lock = Lock()
        self._devices: list[DeviceConfig] = []
        self._signals: list[SignalConfig] = []
        self._last_read_at: dict[str, float] = {}
        self._config_version: str = ""
        self._gateway_active: bool = False
        self._gateway_name: str = ""
        # Operator "tum cihazlara sorgu at" sayaci. Backend her tetik
        # icin 1 artirir. Gateway en son gordugu degeri burada tutar;
        # update() sirasinda yeni nonce buyukse take_refresh_request()
        # bayrak doner ve poller integrity poll tetikler.
        self._refresh_nonce: int = 0
        self._refresh_pending: bool = False
        # Bekleyen cihaz komutlari (config-poll ile gelir). update() yeni
        # (gorulmemis id) komutlari kuyruga alir; poll loop take_pending_commands
        # ile OKUYUP TEMIZLER, calistirir. _seen_command_ids idempotent dedup:
        # ayni komut config'te tekrar gelirse (result bildirilene kadar) yeniden
        # calistirilmaz.
        self._pending_commands: list[PendingCommand] = []
        self._seen_command_ids: set[int] = set()
        self._cache_path: Path | None = Path(cache_path) if cache_path else None
        self._cache_max_age_sec: float = max(60.0, float(cache_max_age_hours) * 3600.0)
        # Wall-clock zamanda son basarili config update timestamp'i (cache age
        # hesabi icin; cache load'da diskten okunur, update()'de simdiki zamanla
        # gunceller)
        self._config_loaded_at_unix: float | None = None
        # Last config refresh attempt error (None ise saglikli). /health
        # endpoint'i bu degeri okuyup gateway saglik durumunu raporlar.
        self._last_refresh_error: str | None = None
        self._last_refresh_ok_unix: float | None = None
        self._last_refresh_attempt_unix: float | None = None

    def update(self, config: GatewayConfig) -> bool:
        """Yeni config gelince state'i gunceller. Degistiyse True doner.

        Backend'den ya da disk cache'den config geldiginde cagirilir. Cache
        timestamp'i da update edilir; "stale config" kontrolu icin /health
        endpoint'i bunu kullanir.
        """
        now_unix = time.time()
        with self._lock:
            changed = config.config_version != self._config_version
            self._devices = list(config.devices)
            self._signals = list(config.signals)
            self._gateway_active = config.is_active
            self._gateway_name = config.gateway_name
            known = {device.code for device in self._devices}
            for key in list(self._last_read_at.keys()):
                if key not in known:
                    self._last_read_at.pop(key, None)
            self._config_version = config.config_version
            self._config_loaded_at_unix = now_unix
            self._last_refresh_ok_unix = now_unix
            self._last_refresh_attempt_unix = now_unix
            self._last_refresh_error = None
            # Refresh nonce karsilastirma: backend artirilmis bir nonce
            # gonderdiyse "tum cihazlara sorgu at" tetigini isaretle.
            # Disk cache'den ilk yuklendiginde ayni nonce gelir, gereksiz
            # tetik olmaz.
            try:
                new_nonce = int(getattr(config, "refresh_nonce", 0) or 0)
            except (TypeError, ValueError):
                new_nonce = 0
            if new_nonce > self._refresh_nonce:
                self._refresh_pending = True
                self._refresh_nonce = new_nonce
            elif new_nonce < self._refresh_nonce:
                # backend reset yapmis olabilir (test) — sessizce takip et
                self._refresh_nonce = new_nonce
            # Bekleyen komutlar: gorulmemis id'leri kuyruga al (idempotent dedup).
            # Ayni komut config'te tekrar gelirse (backend result bildirilene
            # kadar 'sent' olarak tutar; ama ETag miss'te tekrar gonderebilir)
            # yeniden calistirilmaz.
            for cmd in getattr(config, "pending_commands", ()) or ():
                if cmd.id not in self._seen_command_ids:
                    self._seen_command_ids.add(cmd.id)
                    self._pending_commands.append(cmd)
        # disk yazimi lock disinda — dosya I/O sirasinda okuyucular bloklanmasin
        if changed:
            self._persist_unsafe(config, loaded_at_unix=now_unix)
        return changed

    def take_refresh_request(self) -> bool:
        """Operator tetikli refresh-all bayragini OKUYUP TEMIZLER.

        Poller her cycle basinda cagirir; True donerse reader'in
        refresh_all_devices() metodunu cagirip integrity poll tetikler.
        Bayrak tek seferlik tuketilir."""
        with self._lock:
            if self._refresh_pending:
                self._refresh_pending = False
                return True
            return False

    def take_pending_commands(self) -> list[PendingCommand]:
        """Bekleyen komut kuyrugunu OKUYUP TEMIZLER (tek seferlik).

        Poll loop her cycle cagirir; donen komutlari operate_device ile
        calistirir ve sonuclari backend'e bildirir. Kuyruk temizlenir ama
        _seen_command_ids KORUNUR (ayni id tekrar gelirse yeniden calistirilmaz).
        """
        with self._lock:
            cmds = self._pending_commands
            self._pending_commands = []
            return cmds

    def record_refresh_error(self, error: str) -> None:
        """Config refresh denemesi basarisiz olursa caller cagirir.

        /health endpoint'i bu kaydi okuyup "config_fetch_failed" durumunu
        gosterir. last_refresh_ok_unix degismez (cache hala valid).
        """
        with self._lock:
            self._last_refresh_error = error[:500]
            self._last_refresh_attempt_unix = time.time()

    def load_from_cache(self) -> bool:
        """Disk'teki son config snapshot'unu okur (varsa). True donerse hazirdir.

        Backend baslangicta kapali olsa bile gateway hemen polling'e baslar.
        Snapshot yoksa veya bozuksa sessizce False doner.

        Cache age: snapshot icinde 'cached_at_unix' alani vardir. Eger cache
        cache_max_age_sec'den eskise yine yuklenir (gateway down kalmasin)
        ama warning loglanir; /health "stale_config" durumu yansitir.
        """

        if self._cache_path is None or not self._cache_path.exists():
            return False
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            devices = [
                DeviceConfig(**{k: v for k, v in d.items() if k in DeviceConfig.__dataclass_fields__})
                for d in data.get("devices", [])
            ]
            signals = [
                SignalConfig(**{k: v for k, v in s.items() if k in SignalConfig.__dataclass_fields__})
                for s in data.get("signals", [])
            ]
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("config_cache_load_failed path=%s error=%s", self._cache_path, exc)
            return False

        cached_at_unix = data.get("cached_at_unix")
        try:
            cached_at_unix = float(cached_at_unix) if cached_at_unix is not None else None
        except (TypeError, ValueError):
            cached_at_unix = None

        # Yas kontrolu: cache cok eskiyse warning logla
        age_sec: float | None = None
        if cached_at_unix:
            age_sec = max(0.0, time.time() - cached_at_unix)
            if age_sec > self._cache_max_age_sec:
                logger.warning(
                    "config_cache_stale path=%s age_hours=%.1f max_age_hours=%.1f — "
                    "cihaz IP/port/sinyal listesi guncel olmayabilir; backend baglantisi kontrol edin",
                    self._cache_path,
                    age_sec / 3600.0,
                    self._cache_max_age_sec / 3600.0,
                )

        with self._lock:
            self._devices = devices
            self._signals = signals
            self._gateway_active = bool(data.get("is_active", True))
            self._gateway_name = str(data.get("gateway_name") or "")
            self._config_version = str(data.get("config_version") or "")
            self._config_loaded_at_unix = cached_at_unix  # cache zamani; refresh sonrasi update
            # refresh_nonce — backend operator-trigger sayaci. Restart sonrasi
            # 0'dan baslarsa backend ayni nonce'u tekrar yollasa bile
            # `new_nonce > current` testi tetiklenir → gereksiz integrity poll.
            # Cache'ten geri yukleyerek monotonic davranisi sagla.
            try:
                self._refresh_nonce = int(data.get("refresh_nonce", 0) or 0)
            except (TypeError, ValueError):
                self._refresh_nonce = 0
        logger.info(
            "config_cache_loaded path=%s devices=%d signals=%d version=%s age_hours=%s",
            self._cache_path,
            len(devices),
            len(signals),
            data.get("config_version"),
            f"{age_sec / 3600.0:.1f}" if age_sec is not None else "?",
        )
        return True

    def _persist_unsafe(self, config: GatewayConfig, *, loaded_at_unix: float | None = None) -> None:
        """Best-effort: yeni config'i disk'e atomik (tmp + rename) yazar.

        `loaded_at_unix` cache yas hesabi icin kaydedilir; load_from_cache()
        bunu okur ve threshold'u asarsa stale uyarisi verir.
        """

        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "config_version": config.config_version,
                "gateway_code": config.gateway_code,
                "gateway_name": config.gateway_name,
                "is_active": config.is_active,
                "batch_interval_sec": config.batch_interval_sec,
                "max_devices": config.max_devices,
                "cached_at_unix": loaded_at_unix or time.time(),
                # refresh_nonce'i persist et — restart sonrasi monotonic davranis.
                # `_persist_unsafe` lock altinda cagrilir (_apply_config_unsafe icinden);
                # _refresh_nonce'a guvenli erisim.
                "refresh_nonce": self._refresh_nonce,
                "devices": [asdict(d) for d in config.devices],
                "signals": [asdict(s) for s in config.signals],
            }
            # Atomic write: tmp + os.replace
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._cache_path.parent),
                prefix=".config-",
                suffix=".json.tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._cache_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning(
                "config_cache_persist_failed path=%s error=%s",
                self._cache_path,
                exc,
            )

    def devices(self) -> list[DeviceConfig]:
        with self._lock:
            return list(self._devices)

    def signals(self) -> list[SignalConfig]:
        with self._lock:
            return list(self._signals)

    def is_active(self) -> bool:
        with self._lock:
            return self._gateway_active

    def config_version(self) -> str:
        with self._lock:
            return self._config_version

    def mark_read(self, device_code: str, monotonic_ts: float) -> None:
        with self._lock:
            self._last_read_at[device_code] = monotonic_ts

    def due_devices(self, now_monotonic: float) -> list[DeviceConfig]:
        """poll_interval_sec gecmisse okunmasi gereken cihazlari listeler."""
        with self._lock:
            due: list[DeviceConfig] = []
            for device in self._devices:
                last = self._last_read_at.get(device.code, 0.0)
                interval = max(1, device.poll_interval_sec)
                if now_monotonic - last >= interval:
                    due.append(device)
            return due

    def config_loaded_at_unix(self) -> float | None:
        with self._lock:
            return self._config_loaded_at_unix

    def cache_age_seconds(self) -> float | None:
        """Mevcut config kac saniye onceki refresh'ten geliyor."""
        with self._lock:
            if self._config_loaded_at_unix is None:
                return None
            return max(0.0, time.time() - self._config_loaded_at_unix)

    def is_cache_stale(self) -> bool:
        """Cache cok eski mi (max_age_sec'i asti mi)? /health icin."""
        age = self.cache_age_seconds()
        if age is None:
            return False
        return age > self._cache_max_age_sec

    def last_refresh_error(self) -> str | None:
        with self._lock:
            return self._last_refresh_error

    def last_refresh_ok_unix(self) -> float | None:
        with self._lock:
            return self._last_refresh_ok_unix

    def last_refresh_attempt_unix(self) -> float | None:
        with self._lock:
            return self._last_refresh_attempt_unix

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cache_age: float | None = None
            if self._config_loaded_at_unix is not None:
                cache_age = max(0.0, time.time() - self._config_loaded_at_unix)
            since_last_ok: float | None = None
            if self._last_refresh_ok_unix is not None:
                since_last_ok = max(0.0, time.time() - self._last_refresh_ok_unix)
            return {
                "gateway_name": self._gateway_name,
                "config_version": self._config_version,
                "device_count": len(self._devices),
                "signal_count": len(self._signals),
                "active": self._gateway_active,
                "config_cache_age_sec": cache_age,
                "config_cache_stale": (
                    cache_age is not None and cache_age > self._cache_max_age_sec
                ),
                "config_cache_max_age_sec": self._cache_max_age_sec,
                "last_refresh_error": self._last_refresh_error,
                "seconds_since_last_refresh_ok": since_last_ok,
                "devices": [device.code for device in self._devices],
                "devices_detail": [
                    {
                        "code": d.code,
                        "ip_address": d.ip_address,
                        "dnp3_tcp_port": d.dnp3_tcp_port,
                        "dnp3_address": d.dnp3_address,
                    }
                    for d in self._devices
                ],
            }
