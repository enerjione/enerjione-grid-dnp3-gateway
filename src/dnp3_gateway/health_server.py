"""Kucuk HTTP sunucusu: `/health`, `/info`, `/metrics` endpoint'leri.

Cati yazilimin operasyon panosu / Smart Logger backend bu endpoint'leri TCP
sondajlayarak gateway'in ayakta olup olmadigini, anlik metriklerini ve
yapilandirma versiyonunu ogrenir. Sayilar `GatewayMetrics` icinde tutulur ve
poller tarafindan thread-safe artirilir.

/health durum semantigi:
  * status="ok"       — her sey saglikli, polling ve broker calisiyor
  * status="starting" — ilk config bekleniyor
  * status="degraded" — calisiyor ama bir uyari var (cache stale,
                        config_fetch_error, uzun outage vs.)
  * status="unhealthy"— ciddi sorun, container restart gerekebilir
                        (outbox dolu, vs.) → HTTP 503 doner

Multi-instance: Ayni PC'de N gateway calistirilabilir; port=0 verilirse OS
rastgele bos port atar (frontend / supervisor portu instance_id ile keseyilir).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from threading import Event, Lock, Thread
from typing import Any

from dnp3_gateway import __version__
from dnp3_gateway.command_authorization import authorize_output_command
from dnp3_gateway.state import GatewayState

logger = logging.getLogger(__name__)


def _parse_trusted_proxies(raw: str) -> list[Any]:
    """`10.0.0.0/8,192.168.1.5/32` formatindaki CIDR listesini parse et.

    Gecersiz/bos giris -> bos liste. `_client_ip` bos liste durumunda
    `X-Forwarded-For`'u TAMAMEN yok sayar (en guvenli default — attacker
    XFF spoofing yapip rate-limit bypass edemez).
    """
    out: list[Any] = []
    for part in (raw or "").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            logger.warning("health_trusted_proxies_parse_failed entry=%r — atlandi", s)
    return out


def _ip_in_networks(ip: str, networks: list[Any]) -> bool:
    """IP, networks listesindeki herhangi bir CIDR'a giriyor mu?"""
    if not networks or not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


# Config refresh basarisiz olduginda kac saniye gectikten sonra durumu degraded
# kabul ederiz. config_refresh_sec * 5 makul; default 30s * 5 = 150s.
DEFAULT_REFRESH_DEGRADED_THRESHOLD_SEC = 150


# Rate-limit: per-IP sliding window. /health auth-suz oldugu icin yanlis
# konfigure edilmis bir saha proxy'si flood gondermesin diye defansif limit.
# /refresh-all ise auth'lu ama token leak senaryosunda gateway -> saha cihazi
# DoS yontemi olmasin diye daha sıkı limit.
#
# Localhost (127.0.0.1, ::1) muaf — gateway ile ayni host'taki backend
# proxy/cati paneli normal saglik probe'unu sinirsiz yapabilsin (kucuk-yuk
# senaryo). Sınır yalnızca uzaktan gelen istekler icin.
_HEALTH_RATE_LIMIT_PER_MIN = 120  # /health, /healthz, /info, /metrics
_REFRESH_RATE_LIMIT_PER_MIN = 10  # POST /refresh-all
_RATE_LIMIT_WINDOW_SEC = 60.0

_LOCALHOST_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


class _SlidingWindowRateLimiter:
    """Per-key (IP) sliding window sayaci.

    Thread-safe; window suresi icindeki istekleri deque'ta tutar, yeni istek
    geldiginde eskimisleri pop'lar. O(1) amortized; bellek tuketimi N farkli
    IP × ortalama pencere boyu mesaj.

    Saha gateway'inde tipik IP cesitliligi cok dusuk (cati paneli + birkac
    operator host); bellek smaller sorun.
    """

    def __init__(self, *, max_per_window: int, window_sec: float) -> None:
        self._max = max(1, int(max_per_window))
        self._window = max(1.0, float(window_sec))
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """True: istek izin verildi (sayac artirildi). False: rate-limited."""
        if not key or key in _LOCALHOST_IPS:
            return True  # localhost muaf
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True

    def cleanup_stale(self) -> int:
        """Eski IP entry'lerini temizle (memory growth onlemi).

        Tipik kullanim: health_server'in shutdown / periyodik cagrisi.
        Tek bir cagri ile expired bucket'lari kaldirir. Donus: temizlenen
        IP sayisi.
        """
        now = time.monotonic()
        cutoff = now - self._window
        removed = 0
        with self._lock:
            stale_keys = []
            for key, bucket in self._buckets.items():
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if not bucket:
                    stale_keys.append(key)
            for key in stale_keys:
                del self._buckets[key]
                removed += 1
        return removed


class GatewayMetrics:
    """Thread-safe metrik sayaclari. Poller tarafindan artirilir, /metrics yansitir."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at_monotonic = time.monotonic()
        self._started_at_epoch = time.time()
        self.poll_cycles_total = 0
        self.signals_published_total = 0
        # Broker'a DOGRUDAN gidemeyip outbox'a dusen mesajlar. Eskiden bunlar
        # da "published" sayiliyordu; backend 30 dakikadir tum POST'lari
        # reddederken /health `signals_published_total`'i artirmaya devam
        # ediyor ve izleme "yayin akiyor" goruyordu.
        self.signals_outboxed_total = 0
        self.signals_skipped_no_change_total = 0
        self.publish_errors_total = 0
        self.read_errors_total = 0
        self.last_cycle_at_epoch: float | None = None
        self.last_cycle_published: int = 0
        self.last_cycle_devices: int = 0

    def record_cycle(self, *, devices: int, published: int) -> None:
        with self._lock:
            self.poll_cycles_total += 1
            self.signals_published_total += published
            self.last_cycle_at_epoch = time.time()
            self.last_cycle_published = published
            self.last_cycle_devices = devices

    def inc_outboxed(self, n: int = 1) -> None:
        with self._lock:
            self.signals_outboxed_total += n

    def inc_skipped_no_change(self, n: int = 1) -> None:
        with self._lock:
            self.signals_skipped_no_change_total += n

    def inc_publish_error(self, n: int = 1) -> None:
        with self._lock:
            self.publish_errors_total += n

    def inc_read_error(self, n: int = 1) -> None:
        with self._lock:
            self.read_errors_total += n

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "started_at_epoch": self._started_at_epoch,
                "uptime_sec": round(time.monotonic() - self._started_at_monotonic, 2),
                "poll_cycles_total": self.poll_cycles_total,
                "signals_published_total": self.signals_published_total,
                "signals_outboxed_total": self.signals_outboxed_total,
                "signals_skipped_no_change_total": self.signals_skipped_no_change_total,
                "publish_errors_total": self.publish_errors_total,
                "read_errors_total": self.read_errors_total,
                "last_cycle_at_epoch": self.last_cycle_at_epoch,
                "last_cycle_published": self.last_cycle_published,
                "last_cycle_devices": self.last_cycle_devices,
            }


class ThreadLiveness:
    """Arkaplan thread'lerinin canlilik kaydi (/health icin).

    NEDEN: config-refresh, command-poll ve outbox-retrier thread'lerinden biri
    beklenmedik bir hatayla olurse gateway "calisiyor" gorunmeye devam
    ediyordu — /health "ok", Docker healthcheck yesil, backend panelinde
    gateway saglikli. Oysa retrier olduyse telemetri teslimi, command-poll
    olduyse TUM SCADA komut kanali kalici olarak durmus oluyordu.

    main.py thread'leri burada kaydeder; health endpoint'i `is_alive()`
    sonuclarini rapor eder ve olu thread'i `unhealthy` sayar.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._threads: dict[str, Any] = {}

    def register(self, name: str, thread: Any) -> None:
        with self._lock:
            self._threads[name] = thread

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._threads.items())
        alive: dict[str, bool | None] = {}
        for name, t in items:
            try:
                is_alive_fn = getattr(t, "is_alive", None)
                alive[name] = bool(is_alive_fn()) if callable(is_alive_fn) else None
            except Exception:  # noqa: BLE001
                alive[name] = None
        return {"alive": alive}


def _ledger_result_for(ledger: Any, command_id: int) -> dict[str, Any] | None:
    """Ledger'dan bir komutun kayitli sonucunu getir (yoksa None)."""
    try:
        for res in ledger.pending_results():
            if int(res.get("id", -1)) == command_id:
                return res
    except Exception:  # noqa: BLE001
        logger.debug("ledger_result_lookup_failed id=%s", command_id, exc_info=True)
    return None


def _ledger_record(ledger: Any, command_id: int, result: dict[str, Any]) -> None:
    """Komut sonucunu ledger'a yaz; hata yayin akisini bozmamali."""
    if ledger is None:
        return
    try:
        ledger.record_result(result)
    except Exception:  # noqa: BLE001
        logger.exception("ledger_record_failed id=%s", command_id)


def _guard_snapshot(guard: Any) -> dict[str, Any]:
    """DiskGuard/ClockGuard snapshot'i; guard yoksa bos sozluk."""
    if guard is None:
        return {}
    try:
        return dict(guard.snapshot())
    except Exception:  # noqa: BLE001
        return {}


def _thread_snapshot(liveness: Any) -> dict[str, Any]:
    if liveness is None:
        return {"alive": {}}
    try:
        return liveness.snapshot()
    except Exception:  # noqa: BLE001
        return {"alive": {}}


def _quarantined_dbs() -> list[str]:
    """Bu proses omrunde bozuk bulunup karantinaya alinan state dosyalari."""
    try:
        from dnp3_gateway.messaging.sqlite_support import quarantined_files

        return [str(p) for p in quarantined_files()]
    except Exception:  # noqa: BLE001
        return []


#: Komut-poll ardisik hata esikleri. 1sn'lik poll'de 15 hata ~15sn kesinti
#: demek (backend restart penceresi normaldir); 60 hata ~1dk — bu artik
#: gecici degil, mudahale gerektiren bir ariza.
_COMMAND_POLL_DEGRADED_ERRORS = 15
_COMMAND_POLL_UNHEALTHY_ERRORS = 60


def _komut_poll_snapshot(state: Any) -> dict[str, Any]:
    """Komut kanali saglik ozeti; state desteklemiyorsa bos (geriye uyum)."""
    fn = getattr(state, "command_poll_snapshot", None)
    if not callable(fn):
        return {}
    try:
        return dict(fn())
    except Exception:  # noqa: BLE001
        return {}


def _ledger_snapshot(ledger_provider: Any) -> dict[str, Any]:
    """Komut defterinin durumu; erisilemezse bos sozluk.

    Ayri raporlaniyor cunku bu dosyanin kaybi outbox kaybiyla ayni sey degil:
    burada kaybolan FIZIKSEL KOMUT gecmisi ve tekrar-onleme garantisidir.
    """
    if ledger_provider is None:
        return {}
    try:
        ledger = ledger_provider()
    except Exception:  # noqa: BLE001
        return {}
    if ledger is None:
        return {}
    fn = getattr(ledger, "status_snapshot", None)
    if not callable(fn):
        return {}
    try:
        return dict(fn())
    except Exception:  # noqa: BLE001
        return {}


def _device_health_snapshot(reader: Any, configured_count: int) -> dict[str, Any]:
    """Adapter'dan cihaz basina haberlesme durumunu ozetler.

    Donen ozet SAYIMDIR; cihaz kodu/IP icermez cunku /health auth'suzdur.
    `unknown`: config'te tanimli ama adapter'da henuz master acilmamis cihazlar
    (ilk baglanti bekleniyor veya cihaz hic ele alinmamis).
    """
    summary: dict[str, Any] = {
        "total": int(configured_count or 0),
        "online": 0,
        "recovering": 0,
        "lost": 0,
        "unknown": 0,
        "oldest_frame_age_sec": None,
    }
    if reader is None:
        summary["unknown"] = summary["total"]
        return summary
    health_fn = getattr(reader, "device_health", None)
    if not callable(health_fn):
        summary["unknown"] = summary["total"]
        return summary
    try:
        per_device = health_fn() or {}
    except Exception:  # noqa: BLE001
        summary["unknown"] = summary["total"]
        return summary

    now = time.time()
    oldest_age: float | None = None
    for info in per_device.values():
        state_name = str(info.get("state") or "unknown")
        if state_name in ("online", "recovering", "lost"):
            summary[state_name] += 1
        else:
            summary["unknown"] += 1
        last_frame = info.get("last_frame_epoch")
        if last_frame:
            age = max(0.0, now - float(last_frame))
            oldest_age = age if oldest_age is None else max(oldest_age, age)

    tracked = summary["online"] + summary["recovering"] + summary["lost"] + summary["unknown"]
    if summary["total"] < tracked:
        summary["total"] = tracked
    elif summary["total"] > tracked:
        # Config'te olup adapter'da henuz master'i olmayan cihazlar
        summary["unknown"] += summary["total"] - tracked
    if oldest_age is not None:
        summary["oldest_frame_age_sec"] = round(oldest_age, 1)
    # Kopuk cihaz kurtarma sayaclari. "Gateway acaba yeniden baglanmayi
    # deniyor mu?" sorusu sahada yalnizca soket durumunu ornekleyerek
    # (SYN_SENT saymak) cevaplanabiliyordu; artik tek GET yetiyor.
    stats_fn = getattr(reader, "recovery_stats", None)
    if callable(stats_fn):
        try:
            recovery = dict(stats_fn())
            summary["recovery"] = recovery
            # "Cevap veriyor ama olcum gelmiyor" SAYIMI ust seviyeye de
            # cikariliyor: `recovery` sozlugu backend'e tasinmiyor, oysa bu
            # sayim sahte kopmanin yerini alan durumu anlatiyor ve merkezden
            # gorulmesi gerekiyor (bkz. build_payload whitelist'i).
            summary["alive_no_data"] = int(recovery.get("devices_alive_no_data") or 0)
        except Exception:  # noqa: BLE001
            pass
    return summary


def _outbox_snapshot(publisher: Any) -> dict[str, Any]:
    """ResilientPublisher'dan outbox + circuit breaker + broker durumunu cikarir."""
    snap: dict[str, Any] = {
        "outbox_full": False,
        "outbox_full_since_epoch": None,
        "outbox_pending": None,
        "outbox_dead_letter": None,
        "outbox_max_pending": None,
        "last_outbox_error": None,
        # Broker (HTTP/NATS) telemetrisi
        "broker_ready": None,
        "broker_publish_failures": None,
        "broker_publish_successes": None,
        # Aktif tasima yolu. Sahada en pahali sorulardan biri "su an veri
        # hangi yoldan gidiyor?" idi ve log'a bakmadan cevaplanamiyordu;
        # yanlis yoldan (HTTP yedegi) akan bir sistem "calisiyor" gorunup
        # performans hedefini sessizce kaybediyordu.
        "telemetry_transport": None,
    }
    if publisher is None:
        return snap
    try:
        snap["outbox_full"] = bool(getattr(publisher, "outbox_full", False))
        full_since = getattr(publisher, "outbox_full_since", None)
        if full_since is not None:
            snap["outbox_full_since_epoch"] = float(full_since)
        last_err = getattr(publisher, "last_outbox_error", None)
        if last_err:
            snap["last_outbox_error"] = str(last_err)[:200]
        # Public API: ResilientPublisher uzerinden pending_count() / dead_letter_count()
        try:
            snap["outbox_pending"] = int(publisher.pending_count())
        except Exception:  # noqa: BLE001
            pass
        try:
            snap["outbox_dead_letter"] = int(publisher.dead_letter_count())
        except Exception:  # noqa: BLE001
            pass
        try:
            snap["outbox_max_pending"] = int(publisher.outbox_max_pending)
        except Exception:  # noqa: BLE001
            pass
        # Broker (HTTP/NATS) counters — `_broker` private attribute ama
        # ResilientPublisher API'sinde public bir erişim yok; geçici olarak
        # buradan okuyoruz. Daha sonra ResilientPublisher.broker_status()
        # public method ile temizlenebilir.
        broker = getattr(publisher, "_broker", None)
        if broker is not None:
            try:
                snap["broker_ready"] = bool(getattr(broker, "is_ready", False))
            except Exception:  # noqa: BLE001
                pass
            counters_fn = getattr(broker, "counters_snapshot", None)
            if callable(counters_fn):
                try:
                    counters = counters_fn()
                    snap["broker_publish_failures"] = int(counters.get("publish_failures", 0))
                    snap["broker_publish_successes"] = int(counters.get("publish_successes", 0))
                except Exception:  # noqa: BLE001
                    pass
            # TelemetryTransportRouter varsa aktif yolu da rapor et. Router
            # yoksa (dogrudan tek publisher) alan None kalir — eski
            # kurulumlarla uyumlu.
            transport_fn = getattr(broker, "transport_status", None)
            if callable(transport_fn):
                try:
                    snap["telemetry_transport"] = dict(transport_fn())
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        # Saglik endpoint'i hicbir sekilde crash etmemeli
        pass
    return snap


def _make_handler(
    *,
    state: GatewayState,
    gateway_code: str,
    gateway_mode: str,
    config_ready: Event,
    instance_id: str,
    app_environment: str,
    metrics: GatewayMetrics,
    actual_port_provider: Any,
    publisher_provider: Any,
    reader_provider: Any = None,
    ledger_provider: Any = None,
    refresh_token: str = "",
    command_token: str = "",
    info_metrics_auth_required: bool = True,
    health_rate_limiter: _SlidingWindowRateLimiter | None = None,
    refresh_rate_limiter: _SlidingWindowRateLimiter | None = None,
    trusted_proxy_networks: list[Any] | None = None,
    liveness: Any = None,
    poll_interval_sec: float = 1.0,
    disk_guard: Any = None,
    clock_guard: Any = None,
) -> type[BaseHTTPRequestHandler]:
    """HTTP handler class'ini state'e + metrics'e bagli olarak dinamik ureten helper.

    Auth modeli:
      * `/health` ve `/healthz`: HER ZAMAN auth-suz (LB/orchestrator probe icin).
        Yanit minimal status + issues; recon malzemesi ICERMEZ.
      * `/info` ve `/metrics`: `info_metrics_auth_required=True` (default) ise
        Bearer + refresh_token zorunlu. Bu endpoint'ler `instance_id`,
        `version`, `config_version`, `outbox_pending`, `dead_letter_count`
        gibi operasyonel sizinti veren bilgileri doner.
      * `POST /refresh-all`: HER ZAMAN Bearer auth; refresh_token bos ise 503.

    Rate-limit:
      * Tum GET endpoint'leri: `health_rate_limiter` (default 120 req/min/IP).
        Bilinmeyen path'ler (404 doner) icin de uygulanir — fast-path bypass yok.
      * POST /refresh-all: `refresh_rate_limiter` (default 10 req/min/IP).
      * Localhost (127.0.0.1, ::1) muaf — saha proxy/cati paneli ayni host'ta
        ise sinirsiz probe yapabilir.

    `X-Forwarded-For` trust:
      * `trusted_proxy_networks` bos ise XFF YOK SAYILIR (direkt TCP adresi).
        Bu en guvenli default — attacker spoofing yapip rate-limit bypass
        edemez.
      * Reverse proxy arkasinda calistiriliyorsa proxy IP/subnet'i set edilir;
        sadece proxy'den gelen XFF okunur, dogrudan gelen istekte XFF goz ardi.
    """
    _trusted_networks = trusted_proxy_networks or []

    class _Handler(BaseHTTPRequestHandler):
        def _client_ip(self) -> str:
            """Gercek client IP'sini doner.

            Mantik:
              1. Direkt TCP client adresini al.
              2. Eger trusted_proxy_networks set edilmis VE direkt adres bu
                 networks icindeyse, `X-Forwarded-For` header'inin ILK degerini
                 al (proxy chain'in en disindaki client).
              3. Aksi halde TCP adresi.

            Bu trust model nginx/Caddy `real_ip` modulu mantigi ile aynidir.
            """
            direct_ip = (self.client_address[0] if self.client_address else "").strip()
            if _trusted_networks and _ip_in_networks(direct_ip, _trusted_networks):
                xff = self.headers.get("X-Forwarded-For", "").strip()
                if xff:
                    # `client, proxy1, proxy2` formati — ilki en disteki client
                    return xff.split(",")[0].strip()
            return direct_ip

        def _check_rate_limit(self, limiter: _SlidingWindowRateLimiter | None, *, label: str) -> bool:
            """Returns True if request allowed. Otherwise sends 429 + logs."""
            if limiter is None:
                return True
            ip = self._client_ip()
            if limiter.allow(ip):
                return True
            # Rate-limited; 429 doner. Detayli mesaj VERILMEZ (recon malzemesi
            # olmasin) — sadece Retry-After hint. Content-Length=0 HTTP/1.1
            # spec uyumu (response framing); aksi halde bazi client'lar
            # connection'i Half-close gorur, keep-alive bozulur.
            self.send_response(429)
            self.send_header("Retry-After", str(int(_RATE_LIMIT_WINDOW_SEC)))
            self.send_header("Content-Length", "0")
            self.end_headers()
            logger.warning(
                "rate_limit_exceeded endpoint=%s ip=%s label=%s",
                self.path,
                ip,
                label,
            )
            return False

        def _check_bearer_auth(self, expected_token: str = refresh_token) -> bool:
            """Returns True if request carries valid Bearer token.
            Otherwise sends 401/503 response and returns False.

            Timing-safe karsilastirma (hmac.compare_digest); expected_token
            bos ise 503 (endpoint devre disi). Default refresh_token; komut
            endpoint'i command_token gecer. Content-Length=0 HTTP/1.1
            framing icin gerekli."""
            if not expected_token:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {expected_token}"
            if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def do_GET(self):  # noqa: N802
            # Rate-limit: tum GET endpoint'leri ayni bucket'i paylasir.
            # Localhost muaf (limiter.allow icinde). 429 doner ve early return.
            if not self._check_rate_limit(health_rate_limiter, label="get"):
                return
            # /health, /healthz: HER ZAMAN auth-suz. LB/orchestrator probe.
            if self.path in ("/health", "/healthz"):
                body, status_code = _build_health_body(
                    state=state,
                    gateway_code=gateway_code,
                    gateway_mode=gateway_mode,
                    config_ready=config_ready,
                    instance_id=instance_id,
                    app_environment=app_environment,
                    health_port=actual_port_provider(),
                    publisher=publisher_provider() if publisher_provider else None,
                    metrics=metrics,
                    reader=reader_provider() if reader_provider else None,
                    liveness=liveness,
                    poll_interval_sec=poll_interval_sec,
                    disk_guard=disk_guard,
                    clock_guard=clock_guard,
                    ledger_provider=ledger_provider,
                )
                self._respond_json(body, status_code=status_code)
                return
            # /info ve /metrics: operasyonel detay icerir; auth gerektir.
            if self.path == "/info":
                if info_metrics_auth_required and not self._check_bearer_auth():
                    return  # 401/503 zaten gonderildi
                body = {
                    "service": "dnp3-gateway",
                    "version": __version__,
                    "gateway_code": gateway_code,
                    "gateway_instance_id": instance_id,
                    "app_environment": app_environment,
                    "mode": gateway_mode,
                    "worker_health_port": actual_port_provider(),
                    "config_version": state.config_version(),
                    "active": state.is_active(),
                }
                self._respond_json(body)
                return
            if self.path == "/metrics":
                if info_metrics_auth_required and not self._check_bearer_auth():
                    return
                outbox_snap = _outbox_snapshot(publisher_provider() if publisher_provider else None)
                body = {
                    "gateway_code": gateway_code,
                    "gateway_instance_id": instance_id,
                    "config_version": state.config_version(),
                    **metrics.snapshot(),
                    **outbox_snap,
                }
                # CIHAZ BAZINDA TESHIS — "kopuk mu, yoksa sadece sessiz mi?"
                # `/health` bunu TASIYAMAZ: auth'suz oldugu icin bilerek yalnizca
                # SAYIM doner (cihaz kodu/IP sizdirmaz). Bu uc ise Bearer ile
                # korunuyor, dolayisiyla cihaz bazinda `evidence_age_sec` /
                # `data_age_sec` / `reachable` verilebilir. Bu ucu olmadan saha
                # teshisi ancak container log'una girerek yapilabiliyordu.
                metrics_reader = reader_provider() if reader_provider else None
                dh_fn = getattr(metrics_reader, "device_health", None)
                if callable(dh_fn):
                    try:
                        body["devices_detail"] = dh_fn() or {}
                    except Exception:  # noqa: BLE001
                        # Teshis alani metrik ucunu DUSURMEMELI.
                        body["devices_detail"] = {}
                self._respond_json(body)
                return
            # Bilinmeyen GET path — rate-limit ZATEN method girisinde yapildi,
            # 404 fast-path bypass yok. Content-Length=0 HTTP/1.1 framing.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):  # noqa: N802
            """POST /refresh-all — tum cihazlara anlik integrity poll.

            Auth: Authorization header'i Bearer + refresh_token ile eslesmeli.
            Karsilastirma timing-safe (hmac.compare_digest). refresh_token
            bos ise endpoint TAMAMEN devre disidir — guvenlik amacli, "local
            allow" ozel durumu kaldirildi (container'da client_ip 127.0.0.1
            yanilticidir).

            Backend bu endpoint'i proxy ederek operator'in 'tum sinyalleri
            yenile' butonunu uygular.
            """
            # Rate-limit EN BASTA: hem /refresh-all hem bilinmeyen POST path'leri
            # icin. /refresh-all icin sıkı (10/min), bilinmeyen path'ler icin de
            # ayni bucket (saldirgan POST /random spam'le sunucuyu yoramaz).
            # Auth fail olsa bile bu sayim devam eder (brute-force teyit).
            if not self._check_rate_limit(refresh_rate_limiter, label="post"):
                return
            if self.path == "/operate":
                self._handle_operate()
                return
            if self.path != "/refresh-all":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._check_bearer_auth():
                return  # 401 veya 503 zaten gonderildi
            reader = reader_provider() if reader_provider else None
            if reader is None or not hasattr(reader, "refresh_all_devices"):
                self._respond_json(
                    {"ok": False, "detail": "Reader integrity-poll desteklemiyor"},
                    status_code=503,
                )
                return
            try:
                ok, total = reader.refresh_all_devices()
                logger.info("manual_refresh_all_requested ok=%d total=%d", ok, total)
                self._respond_json({"ok": True, "requested": ok, "total_devices": total})
            except Exception as exc:  # noqa: BLE001
                logger.exception("manual_refresh_all_failed")
                self._respond_json({"ok": False, "detail": str(exc)}, status_code=500)

        def _handle_operate(self) -> None:
            """POST /operate — tek cihaza DNP3 binary output (CROB) komutu.

            Auth: Bearer + command_token (refresh_token'dan AYRI). command_token
            bos ise 503. Backend cihaz komut butonlarini (Trigger Download,
            Config Update, Reset...) bu endpoint'e proxy eder.

            Body (JSON): {device_code, command, index, op_type?, count?,
            on_time_ms?, off_time_ms?, timeout_sec?, command_id?}
              * `command`: **ZORUNLU** komut slug'i (`master.` oneki OLMADAN,
                orn. `reset_all_fcis`). Pull yoluyla ayni yerel yetkilendirmeden
                gecer (F1+F2); slug olmadan komut NIYETI dogrulanamaz ve istek
                `command_missing` ile reddedilir.

                Bu alan zorunlu kilinirken sozlesme kirilma riski olculdu:
                filodaki HICBIR gateway'de `command_token` tanimli degil (yani
                endpoint her yerde 503) ve backend'de bu endpoint'i cagiran kod
                YOK — yalnizca `gateways.command_token` / `control_host`
                kolonlari mevcut. Canli cagiran olmadigi icin siki sozlesme
                simdi, ilk kullanicisi olmadan once konuyor.
              * `index`: cihazin binary output index'i
              * `op_type`: **default `latch_on`** (docstring eskiden `pulse_on`
                diyordu ama kod `latch_on` kullaniyordu — bu ikisi DNP3'te
                TAMAMEN farkli davranistir: PULSE kendiliginden acilir, LATCH
                cikisi KALICI olarak enerjili birakir. Entegratorler docstring'i
                API sozlesmesi olarak okudugu icin "kisa darbe" bekleyip kalici
                latch gonderilmesine yol aciyordu.)
              * `command_id`: OPSIYONEL ama ONERILEN idempotency anahtari.
                Verilirse komut CommandLedger'a yazilir ve ayni id ikinci kez
                gelirse CROB TEKRAR GONDERILMEZ. Backend'in HTTP timeout'u
                (~5sn) cihazin CROB suresinden (SN2'de 1-10sn) kisa oldugu icin
                retry cok olasi; idempotency olmadan ayni kesici IKI KEZ
                surulebiliyordu.
            """
            if not self._check_bearer_auth(command_token):
                return  # 401 veya 503 zaten gonderildi
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            # Komut body'si kucuk; buyuk payload'i reddet (DoS/hata onlemi).
            if length <= 0 or length > 4096:
                self._respond_json({"ok": False, "detail": "gecersiz veya bos body"}, status_code=400)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._respond_json({"ok": False, "detail": "body JSON parse edilemedi"}, status_code=400)
                return
            device_code = str(payload.get("device_code", "")).strip()
            if not device_code or "index" not in payload:
                self._respond_json(
                    {"ok": False, "detail": "device_code ve index zorunlu"},
                    status_code=400,
                )
                return
            try:
                index = int(payload["index"])
            except (TypeError, ValueError):
                self._respond_json({"ok": False, "detail": "index tamsayi olmali"}, status_code=400)
                return
            device = next((d for d in state.devices() if d.code == device_code), None)
            if device is None:
                self._respond_json(
                    {"ok": False, "detail": f"cihaz bulunamadi: {device_code}"},
                    status_code=404,
                )
                return
            reader = reader_provider() if reader_provider else None
            if reader is None or not hasattr(reader, "operate_device"):
                self._respond_json(
                    {"ok": False, "detail": "Reader komut desteklemiyor"},
                    status_code=503,
                )
                return

            # ---- F1 + F2: yerel cikis yetkilendirmesi -----------------------
            # Pull yolundakiyle AYNI fonksiyon; iki kanalin karari ayrisamaz.
            # Ledger rezervasyonundan ONCE calisir: yetkisiz bir komut
            # command_id'yi tuketmemeli, cunku operator duzeltilmis komutu
            # ayni id ile yeniden gonderebilmeli.
            karar = authorize_output_command(
                state.signals_for(device),
                dnp3_index=index,
                command=payload.get("command"),
            )
            if not karar.authorized:
                logger.warning(
                    "command_authorization_rejected gateway=%s device=%s command_id=%s "
                    "command=%s dnp3_index=%s reason=%s detail=%s — CROB GONDERILMEDI",
                    gateway_code,
                    device_code,
                    payload.get("command_id"),
                    payload.get("command"),
                    index,
                    karar.status,
                    karar.detail,
                )
                self._respond_json(
                    {"ok": False, "status": karar.status, "detail": karar.detail},
                    status_code=403,
                )
                return

            op_type = str(payload.get("op_type", "latch_on"))
            # ---- IDEMPOTENCY ------------------------------------------------
            # Bu endpoint CommandLedger'i TAMAMEN ATLIYORDU. Backend'in HTTP
            # timeout'u (~5sn) cihazin CROB suresinden (SN2'de 1-10sn) kisa
            # oldugu icin retry cok olasi; gateway ikinci istegi YENI bir komut
            # sanip AYNI binary output'a IKINCI KEZ CROB gonderiyordu. Fiziksel
            # manevrada bu kabul edilemez. Ayrica pull kanaliyla (komut-poll)
            # ayni komut es zamanli gelirse cift surme riski vardi.
            ledger = ledger_provider() if ledger_provider else None
            command_id = payload.get("command_id")
            ledger_key: int | None = None
            if command_id is not None and ledger is not None:
                try:
                    ledger_key = int(command_id)
                except (TypeError, ValueError):
                    self._respond_json(
                        {"ok": False, "detail": "command_id tamsayi olmali"},
                        status_code=400,
                    )
                    return
                try:
                    yeni_kayit = bool(ledger.start_dispatch(ledger_key))
                except Exception:  # noqa: BLE001
                    # DEFTER ERISILEMIYOR — komutu CALISTIRMA.
                    #
                    # Eski davranis fail-open idi: `ledger_key = None` ile
                    # devam edip CROB'u gonderiyordu. Iki sessiz sonucu vardi:
                    #   1. Tekrar-onleme yok — backend'in HTTP timeout retry'i
                    #      (cok olasi: timeout ~5sn, CROB 1-10sn) AYNI kesiciyi
                    #      ikinci kez surerdi.
                    #   2. Sonuc kaydi yok — komut calissa bile backend sonucu
                    #      hicbir zaman ogrenemezdi.
                    # Fiziksel manevrada "belki iki kez surdum" kabul edilemez;
                    # "suremedim, sebebi bu" kabul edilebilir ve gorunurdur.
                    logger.exception(
                        "operate_ledger_unavailable device=%s command_id=%s — komut REDDEDILDI "
                        "(tekrar-onleme garantisi verilemiyor)",
                        device_code,
                        ledger_key,
                    )
                    self._respond_json(
                        {
                            "ok": False,
                            "status": "rejected",
                            "detail": (
                                "komut defteri erisilemiyor; tekrar-onleme garanti "
                                "edilemedigi icin komut gonderilmedi"
                            ),
                        },
                        status_code=503,
                    )
                    return

                if not yeni_kayit:
                    # Bu komut daha once gonderilmis. TEKRAR GONDERME;
                    # varsa kayitli sonucu don.
                    prior = _ledger_result_for(ledger, ledger_key)
                    logger.info(
                        "operate_duplicate_suppressed device=%s index=%s command_id=%s "
                        "kayitli_sonuc=%s — komut daha once gonderildi, CROB TEKRARLANMADI",
                        device_code,
                        index,
                        ledger_key,
                        "var" if prior else "yok",
                    )
                    # SONUCU BILMIYORSAK `ok:false` DEMEK YANLIS.
                    #
                    # Kayitli sonuc iki durumda bulunmaz: (a) ilk deneme HALA
                    # sururken retry geldi, (b) sonuc backend'e teslim edilip
                    # kayittan dusuruldu. Ikisinde de komut KABUL EDILMISTIR.
                    # `ok:false` donmek cagirana "basarisiz" dedirtir; operator
                    # YENI bir command_id ile tekrar dener ve kesici GERCEKTEN
                    # iki kez surulur — tam da bu defterin onlemek icin var
                    # oldugu sey.
                    #
                    # Bilinmeyen sonuc `status:"pending"` ile bildirilir;
                    # cagiran taraf sonucu bekler, yeniden denemez.
                    govde: dict[str, Any] = {
                        "duplicate": True,
                        "detail": "komut daha once islendi; CROB tekrarlanmadi",
                        "result": prior,
                    }
                    if prior is not None:
                        govde["ok"] = bool(prior.get("ok"))
                        govde["status"] = str(prior.get("status", "unknown"))
                    else:
                        govde["ok"] = None
                        govde["status"] = "pending"
                        govde["detail"] = (
                            "komut daha once kabul edildi; sonucu henuz bilinmiyor "
                            "(YENI bir command_id ile TEKRAR DENEMEYIN)"
                        )
                    self._respond_json(govde, status_code=200)
                    return
            elif command_id is None:
                # Idempotency anahtari YOK. Komut yine de calisir (backend eski
                # surum olabilir) ama retry'da cift surme riski vardir; bu
                # sessiz kalmamali.
                logger.warning(
                    "operate_without_command_id device=%s index=%s — idempotency "
                    "anahtari gonderilmedi; istek tekrarlanirsa CROB DA TEKRARLANIR",
                    device_code,
                    index,
                )
            else:
                # command_id verilmis ama defter hic yok (provider bagli degil).
                logger.error(
                    "operate_ledger_missing device=%s command_id=%s — komut defteri "
                    "yapilandirilmamis; tekrar-onleme YOK",
                    device_code,
                    command_id,
                )

            try:
                result = reader.operate_device(
                    device=device,
                    index=index,
                    op_type=op_type,
                    count=int(payload.get("count", 1)),
                    on_time_ms=int(payload.get("on_time_ms", 100)),
                    off_time_ms=int(payload.get("off_time_ms", 100)),
                    timeout_sec=float(payload.get("timeout_sec", 10.0)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("operate_failed device=%s index=%s", device_code, index)
                if ledger_key is not None:
                    _ledger_record(
                        ledger,
                        ledger_key,
                        {
                            "id": ledger_key,
                            "ok": False,
                            "status": "error",
                            "error": str(exc)[:400],
                        },
                    )
                self._respond_json({"ok": False, "detail": str(exc)}, status_code=500)
                return
            ok = bool(result.get("ok"))
            if ledger_key is not None:
                _ledger_record(
                    ledger,
                    ledger_key,
                    {
                        "id": ledger_key,
                        "ok": ok,
                        "status": str(result.get("status", "unknown")),
                        "error": result.get("error"),
                        "control": result.get("control"),
                        "dnp3_status": result.get("dnp3_status"),
                    },
                )
            # Gonderilen CROB tipini de logla: olay sonrasi "hangi komut
            # gonderildi" sorusu eskiden log'dan cevaplanamiyordu.
            logger.info(
                "operate_requested device=%s index=%s control=%s count=%s "
                "on_time=%s off_time=%s command_id=%s ok=%s status=%s",
                device_code,
                index,
                op_type,
                payload.get("count", 1),
                payload.get("on_time_ms", 100),
                payload.get("off_time_ms", 100),
                ledger_key,
                ok,
                result.get("status"),
            )
            # Endpoint her zaman 200 doner; komut sonucu result.ok'ta. Cihaz
            # reddettiyse (unsupported/inactive/timeout) ok=False + status kalir,
            # backend buna bakip operator'a gosterir.
            #
            # `ok` / `status` / `duplicate` TEPEDE de veriliyor: duplicate
            # cevabi bu alanlari tasiyor, basari cevabi tasimiyordu. Farkli
            # sekiller cagiran tarafta iki ayri ayristirma yolu demek; biri
            # gozden kacarsa "sonuc bilinmiyor" ile "basarisiz" karisir.
            self._respond_json(
                {
                    "ok": ok,
                    "status": str(result.get("status", "unknown")),
                    "duplicate": False,
                    "result": result,
                }
            )

        def _respond_json(self, body: dict[str, Any], *, status_code: int = 200) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            _ = format, args

    return _Handler


def _build_health_body(
    *,
    state: GatewayState,
    gateway_code: str,
    gateway_mode: str,
    config_ready: Event,
    instance_id: str,
    app_environment: str,
    health_port: int,
    publisher: Any,
    metrics: GatewayMetrics,
    reader: Any = None,
    liveness: Any = None,
    poll_interval_sec: float = 1.0,
    disk_guard: Any = None,
    clock_guard: Any = None,
    ledger_provider: Any = None,
) -> tuple[dict[str, Any], int]:
    """Health body + HTTP status code uretir.

    Status semantigi:
      * "starting" -> ilk config bekleniyor (HTTP 200, kibarca; LB henuz prob cevabini bekleyebilir)
      * "ok"       -> her sey iyi (HTTP 200)
      * "degraded" -> uyarilar var (HTTP 200; monitoring alert'i tetikleyebilir)
      * "unhealthy"-> ciddi (HTTP 503; LB / orchestrator restart isaretiyle)
    """
    cfg_snapshot = state.snapshot()
    outbox_snap = _outbox_snapshot(publisher)
    metrics_snap = metrics.snapshot()
    devices_snap = _device_health_snapshot(reader, cfg_snapshot.get("device_count", 0))
    threads_snap = _thread_snapshot(liveness)

    issues: list[str] = []
    severity_score = 0  # 0=ok 1=degraded 2=unhealthy

    if not config_ready.is_set():
        # Henuz ilk config alinmadi; gateway acilis surecinde
        severity_score = max(severity_score, 0)
        # Note: starting status'u asagida secilir
    if cfg_snapshot.get("config_cache_stale"):
        issues.append("config_cache_stale")
        severity_score = max(severity_score, 1)
    if cfg_snapshot.get("last_refresh_error"):
        # Backend'e ulasilamadi son denemede; cache valid ise degraded
        secs_since_ok = cfg_snapshot.get("seconds_since_last_refresh_ok")
        if secs_since_ok is None or secs_since_ok > DEFAULT_REFRESH_DEGRADED_THRESHOLD_SEC:
            issues.append("config_refresh_failing")
            severity_score = max(severity_score, 1)
    # --- SCADA komut kanali ------------------------------------------------
    # Thread YASIYOR olabilir ama her turda hata aliyor olabilir; o zaman
    # `thread_dead:` tetiklenmez ve /health "ok" der. Sahada tam bu oldu:
    # backend `/pending`e 500 dondu, komut kanali 660 ardisik hatayla TAMAMEN
    # oluydu ve panel saglikli gorunuyordu. Komut yolu SCADA'nin manevra
    # kanalidir; sessiz kalmasi kabul edilemez.
    komut_poll = _komut_poll_snapshot(state)
    ardisik = int(komut_poll.get("consecutive_errors") or 0)
    if ardisik >= _COMMAND_POLL_UNHEALTHY_ERRORS:
        issues.append("command_channel_down")
        severity_score = max(severity_score, 2)
    elif ardisik >= _COMMAND_POLL_DEGRADED_ERRORS:
        issues.append("command_channel_failing")
        severity_score = max(severity_score, 1)

    # --- Telemetri hedefi (backend ingest) ---------------------------------
    # `broker_ready` uzun suredir govdede vardi ama HICBIR sorun kodu
    # uretmiyordu: backend'e telemetri hic gitmezken `/health` "ok" diyebilir,
    # ariza yalnizca outbox dolmaya basladiginda (dakikalar sonra) gorunurdu.
    # Gateway<->backend haberlesmesi kopar kopmaz gorunmeli.
    if outbox_snap.get("broker_ready") is False:
        issues.append("telemetry_backend_unreachable")
        severity_score = max(severity_score, 1)

    # --- Yedek tasima yoluna dusus ----------------------------------------
    # Uzak kurulumda HTTP yedegi BEKLENEN davranistir; veri akmaya devam
    # ettigi icin "unhealthy" degil. Ama sessiz de kalmamali: NATS yolu
    # kapaliyken sistem calisir gorunur, olculen throughput ise kat kat
    # dusuktur. Operator bunu panelde gormeli ki NATS erisimini duzeltsin.
    transport = outbox_snap.get("telemetry_transport") or {}
    if transport.get("active_transport") == "http" and transport.get("fallback_enabled"):
        issues.append("telemetry_transport_fallback_http")
        severity_score = max(severity_score, 1)

    if outbox_snap.get("outbox_full"):
        issues.append("outbox_full")
        severity_score = max(severity_score, 2)
    if outbox_snap.get("outbox_pending") and outbox_snap.get("outbox_max_pending"):
        # Outbox %80 dolu ise warn
        pending = int(outbox_snap["outbox_pending"])
        cap = int(outbox_snap["outbox_max_pending"])
        if cap > 0 and pending >= int(cap * 0.8):
            issues.append("outbox_near_capacity")
            severity_score = max(severity_score, 1)
    if outbox_snap.get("outbox_dead_letter") and int(outbox_snap["outbox_dead_letter"]) > 0:
        issues.append("dead_letter_messages_present")
        severity_score = max(severity_score, 1)

    # Polling durumu kontrolu — gateway aktif ama hic cycle calismadiysa
    if state.is_active() and metrics_snap["poll_cycles_total"] == 0:
        # Yeni baslamis olabilir; uptime kontrolu
        if metrics_snap["uptime_sec"] > 60:
            issues.append("no_poll_cycles_yet")
            severity_score = max(severity_score, 1)

    # --- Poll dongusu WATCHDOG'u -------------------------------------------
    # `last_cycle_at_epoch` yaziliyordu ama HICBIR esige sokulmuyordu. Ana
    # dongu donarsa (orn. adapter lock'unda takilma) health server ayri
    # thread'te oldugu icin saatlerce 200/ok donmeye devam ediyordu; Docker
    # healthcheck yesil kaliyor ve kimse restart etmiyordu.
    last_cycle = metrics_snap.get("last_cycle_at_epoch")
    if state.is_active() and last_cycle:
        stall_threshold = max(5.0 * float(poll_interval_sec), 60.0)
        since_cycle = max(0.0, time.time() - float(last_cycle))
        if since_cycle > stall_threshold:
            issues.append("poll_loop_stalled")
            severity_score = max(severity_score, 2)

    # --- Arkaplan thread'lerinin canliligi ---------------------------------
    # Bir thread sessizce olurse (retrier -> telemetri teslimi durur,
    # command-poll -> SCADA komutlari calismaz) disaridan gorulebilecek
    # HICBIR gosterge yoktu.
    for name, alive in threads_snap.get("alive", {}).items():
        if alive is False:
            issues.append(f"thread_dead:{name}")
            severity_score = max(severity_score, 2)

    # --- Cihaz haberlesme sagligi ------------------------------------------
    # Eskiden 300 cihazin TAMAMI comm_lost iken bile status "ok" donuyordu.
    total_dev = int(devices_snap.get("total") or 0)
    lost_dev = int(devices_snap.get("lost") or 0)
    if total_dev > 0 and lost_dev > 0:
        lost_ratio = lost_dev / total_dev
        if lost_ratio >= 1.0:
            issues.append("all_devices_comm_lost")
            severity_score = max(severity_score, 2)
        elif lost_ratio >= 0.5:
            issues.append("majority_devices_comm_lost")
            severity_score = max(severity_score, 1)
        else:
            issues.append("some_devices_comm_lost")
            severity_score = max(severity_score, 1)

    # --- Cevap veriyor ama olcum gelmiyor ----------------------------------
    # Bu durum comm_lost DEGILDIR (cihaz konusuyor) ve artik sahte kopma
    # uretmiyor; ama gorunmez de olmamali: kalici olmasi Class 0 integrity
    # poll'unun dustugune isaret eder. `issues` backend'e `gateway_health`
    # tablosundaki ayni adli kolonla tasindigi icin merkezden gorulebilir —
    # yeni bir alan/migration gerektirmeden.
    alive_no_data = int(devices_snap.get("alive_no_data") or 0)
    if alive_no_data > 0:
        issues.append("devices_alive_no_data")
        severity_score = max(severity_score, 1)

    # --- Bozuk/karantinaya alinmis veritabani ------------------------------
    quarantined = _quarantined_dbs()
    if quarantined:
        issues.append("state_db_quarantined")
        severity_score = max(severity_score, 1)

    # --- Komut defteri sifirlandi mi? --------------------------------------
    # `state_db_quarantined` ile AYNI SEY DEGIL: orada kaybedilen birikmis
    # telemetridir. Burada kaybedilen fiziksel komut gecmisi ve tekrar-onleme
    # garantisidir — operator bekleyen komutlari elle dogrulamali. Ayni
    # severity, AYRI kod: karistirilmasi mudahaleyi yanlis yone cevirirdi.
    ledger_snap = _ledger_snapshot(ledger_provider)
    if ledger_snap.get("journal_reset"):
        issues.append("command_journal_reset")
        severity_score = max(severity_score, 1)

    # --- Disk alani ---------------------------------------------------------
    # "Disk-full breaker" aslinda MESAJ sayiyordu, bayt saymiyordu; disk
    # dolmasi uc ayri arizanin (retrier olumu, log rotation kirilmasi, poller
    # durmasi) ortak tetikleyicisiydi ve hicbir yerde olculmuyordu.
    disk_snap = _guard_snapshot(disk_guard)
    if disk_snap.get("level") == "critical":
        issues.append("disk_space_critical")
        severity_score = max(severity_score, 2)
    elif disk_snap.get("level") == "low":
        issues.append("disk_space_low")
        severity_score = max(severity_score, 1)

    # --- Sistem saati -------------------------------------------------------
    # Gateway saati TUM arsivin tek zaman referansi; hicbir yerde
    # dogrulanmiyordu. Sapma buyukse hem telemetri damgalari yanlis arsivlenir
    # hem de (zaman-senk acikken) yanlis saat outstation'lara YAZILIR.
    clock_snap = _guard_snapshot(clock_guard)
    skew = clock_snap.get("skew_sec")
    if skew is not None:
        if not clock_snap.get("safe_for_time_sync", True):
            issues.append("clock_skew_unsafe")
            severity_score = max(severity_score, 1)
        elif abs(float(skew)) >= 2.0:
            issues.append("clock_skew_detected")
            severity_score = max(severity_score, 1)

    if not config_ready.is_set():
        status = "starting"
        http_code = 200
    elif severity_score >= 2:
        status = "unhealthy"
        http_code = 503
    elif severity_score >= 1:
        status = "degraded"
        http_code = 200
    else:
        status = "ok"
        http_code = 200

    # /health auth-suz erisilebilir oldugundan cihaz detaylarini (IP + DNP3
    # port + address) sizdirmayalim — recon icin yeterli bilgi. Sadece sayim
    # ve durum bilgisi kalsin; tam detay /info ve /metrics (auth'lu)
    # endpoint'lerinde tasinabilir.
    cfg_safe = dict(cfg_snapshot)
    cfg_safe.pop("devices", None)
    cfg_safe.pop("devices_detail", None)

    body = {
        "status": status,
        "issues": issues,
        "service": "dnp3-gateway",
        "version": __version__,
        "gateway_code": gateway_code,
        "gateway_instance_id": instance_id,
        "app_environment": app_environment,
        "worker_health_port": health_port,
        "mode": gateway_mode,
        "config": cfg_safe,
        "outbox": outbox_snap,
        # Cihaz haberlesme ozeti. Cihaz KODLARI ve IP'leri burada YOK
        # (/health auth'suz; recon malzemesi vermeyelim) — yalnizca sayimlar.
        # Kod bazli detay auth'lu /metrics ve /info endpoint'lerinde.
        "devices": devices_snap,
        "threads": threads_snap,
        "disk": disk_snap,
        "clock": clock_snap,
        "metrics": {
            "uptime_sec": metrics_snap["uptime_sec"],
            "poll_cycles_total": metrics_snap["poll_cycles_total"],
            "signals_published_total": metrics_snap["signals_published_total"],
            "signals_outboxed_total": metrics_snap["signals_outboxed_total"],
            "publish_errors_total": metrics_snap["publish_errors_total"],
            "read_errors_total": metrics_snap["read_errors_total"],
            "last_cycle_at_epoch": metrics_snap["last_cycle_at_epoch"],
            "seconds_since_last_cycle": (
                round(max(0.0, time.time() - float(last_cycle)), 2) if last_cycle else None
            ),
            "last_cycle_devices": metrics_snap["last_cycle_devices"],
            "last_cycle_published": metrics_snap["last_cycle_published"],
        },
    }
    if quarantined:
        body["quarantined_state_files"] = quarantined
    if komut_poll:
        body["command_channel"] = komut_poll
    if ledger_snap:
        # Komut defteri ozeti: sifirlanma bayragi + bekleyen/olu sonuc sayisi.
        # Karantina dosyasinin YOLU burada YOK — /health auth'suz.
        body["command_ledger"] = {k: v for k, v in ledger_snap.items() if k != "journal_reset_path"}
    return body, http_code


def start_health_server(
    *,
    host: str,
    port: int,
    state: GatewayState,
    gateway_code: str,
    gateway_mode: str,
    config_ready: Event,
    instance_id: str,
    app_environment: str,
    metrics: GatewayMetrics | None = None,
    publisher_provider: Any = None,
    reader_provider: Any = None,
    ledger_provider: Any = None,
    refresh_token: str = "",
    command_token: str = "",
    info_metrics_auth_required: bool = True,
    health_rate_limit_per_min: int = _HEALTH_RATE_LIMIT_PER_MIN,
    refresh_rate_limit_per_min: int = _REFRESH_RATE_LIMIT_PER_MIN,
    trusted_proxies: str = "",
    liveness: ThreadLiveness | None = None,
    poll_interval_sec: float = 1.0,
    disk_guard: Any = None,
    clock_guard: Any = None,
) -> tuple[HTTPServer, GatewayMetrics, int]:
    """Sunucuyu ayaga kaldirir.

    `port=0` verilirse OS rastgele bos port atar; gercek port `actual_port`
    olarak doner. Caller bu portu log + /health'te gosterir.

    `publisher_provider`: caller tarafindan saglanan ve mevcut
    ResilientPublisher'i donen 0-arglik callable. /health endpoint'i bu
    publisher uzerinden outbox/circuit-breaker durumunu raporlar.
    Boylece health_server publisher'a dogrudan referans tutmaz; restart/replace
    senaryolarinda yine canli pointer'i okur.
    """
    metrics = metrics or GatewayMetrics()

    # actual_port baslangicta bilinmiyor (port=0); bind sonrasi server.server_address[1]
    # uzerinden okunur. Handler handler kapsami icinde okunabilir olsun diye closure.
    actual_port_holder: dict[str, int] = {"port": port}

    def _actual_port_provider() -> int:
        return actual_port_holder["port"]

    # Per-IP rate limiter'lar — defansif DoS koruma. Localhost muaf (cati
    # paneli/backend ayni host'tan sinirsiz probe yapabilir).
    health_rl = _SlidingWindowRateLimiter(
        max_per_window=health_rate_limit_per_min,
        window_sec=_RATE_LIMIT_WINDOW_SEC,
    )
    refresh_rl = _SlidingWindowRateLimiter(
        max_per_window=refresh_rate_limit_per_min,
        window_sec=_RATE_LIMIT_WINDOW_SEC,
    )
    # Background cleanup thread: stale IP entry'lerini periyodik olarak siler.
    # Bucket sayisi = unique-IP-per-window; saha gateway'inde tipik <10 ama
    # uzak attacker spam'i ile binlerce IP birikebilir. cleanup_stale O(N) ama
    # 5dk araliklarla calistigi icin amortize edilmis maliyet ihmal edilir.
    # Thread daemon + stop_event ile temiz shutdown saglanir.
    rl_cleanup_stop = Event()

    def _rl_cleanup_loop() -> None:
        # Window suresi kadar bekle; her uyaninca her iki limiter'i temizle.
        # window_sec * 2 = 120s default — saglikli IP'lerin bucket'i hala
        # ayakta kalir, tamamen eskimisler silinir.
        interval = max(60.0, _RATE_LIMIT_WINDOW_SEC * 2.0)
        while not rl_cleanup_stop.wait(interval):
            try:
                removed_h = health_rl.cleanup_stale()
                removed_r = refresh_rl.cleanup_stale()
                total = removed_h + removed_r
                if total > 0:
                    logger.debug(
                        "rate_limit_cleanup removed=%d (health=%d refresh=%d)",
                        total,
                        removed_h,
                        removed_r,
                    )
            except Exception:  # noqa: BLE001
                # Cleanup hatasi server'i durdurmamali
                logger.debug("rate_limit_cleanup_error", exc_info=True)

    Thread(
        target=_rl_cleanup_loop,
        name="rate-limit-cleanup",
        daemon=True,
    ).start()

    # Trusted proxy CIDR'lerini parse et. Bos liste = XFF yok sayilir
    # (saldirgan spoofing yapamaz). Reverse proxy varsa operator buraya
    # proxy IP/subnet'ini yazar.
    trusted_networks = _parse_trusted_proxies(trusted_proxies)
    if trusted_networks:
        logger.info(
            "health_trusted_proxies_loaded count=%d (X-Forwarded-For aktif)",
            len(trusted_networks),
        )

    handler_cls = _make_handler(
        state=state,
        gateway_code=gateway_code,
        gateway_mode=gateway_mode,
        config_ready=config_ready,
        instance_id=instance_id,
        app_environment=app_environment,
        metrics=metrics,
        actual_port_provider=_actual_port_provider,
        publisher_provider=publisher_provider,
        reader_provider=reader_provider,
        ledger_provider=ledger_provider,
        refresh_token=refresh_token,
        command_token=command_token,
        info_metrics_auth_required=info_metrics_auth_required,
        health_rate_limiter=health_rl,
        refresh_rate_limiter=refresh_rl,
        trusted_proxy_networks=trusted_networks,
        liveness=liveness,
        poll_interval_sec=poll_interval_sec,
        disk_guard=disk_guard,
        clock_guard=clock_guard,
    )
    # ThreadingHTTPServer: yavas client (slow-loris) tek istegi durdursa bile
    # diger probe'lar bloklanmaz. HTTPServer single-threaded olsaydi cati
    # paneli /health probe'u bir baska yavas baglanti yuzunden zaman asimina
    # ugrar, gateway "down" goruntusu olusur.
    server = ThreadingHTTPServer((host, port), handler_cls)
    actual_port = int(server.server_address[1])
    actual_port_holder["port"] = actual_port
    Thread(target=server.serve_forever, name="health-http", daemon=True).start()
    if port == 0:
        logger.info(
            "health_server_started host=%s port=%s (auto-assigned, requested=0)",
            host,
            actual_port,
        )
    else:
        logger.info("health_server_started host=%s port=%s", host, actual_port)
    return server, metrics, actual_port
