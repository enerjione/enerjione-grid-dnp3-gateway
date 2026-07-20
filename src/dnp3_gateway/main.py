"""Gateway ana giris noktasi.

Calisma akisi:
    1. Loglama + health HTTP sunucusu acilir.
    2. Arkaplan thread'i backend API'den `CONFIG_REFRESH_SEC` araliklarla
       konfigurasyon ceker ve `GatewayState`'i gunceller.
    3. Ana thread `default_poll_interval_sec` araliginda uyanip okunma vakti
       gelen cihazlari `poller.run_poll_cycle` ile okur/yayinlar.
       Telemetri backend HTTP ingest'e yollanir (varsayilan publisher).
    4. SIGINT/SIGTERM/SIGBREAK alindiginda graceful shutdown:
         - config refresh thread durdurulur
         - poller kapanir
         - DNP3 session'lari + JetStream baglantisi kapanir
         - outbox retrier durdurulur
         - health HTTP sunucusu kapatilir
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any

from dnp3_gateway import __version__
from dnp3_gateway.adapters import TelemetryReader, build_adapter
from dnp3_gateway.auth import GatewayIdentity, bootstrap_gateway_identity
from dnp3_gateway.auth.instance_lock import acquire_instance_lock
from dnp3_gateway.backend import BackendConfigClient, GatewayConfigError
from dnp3_gateway.config import Settings, settings
from dnp3_gateway.health_server import GatewayMetrics, start_health_server
from dnp3_gateway.logging_setup import configure_logging, register_secret
from dnp3_gateway.messaging import Outbox, OutboxRetrier
from dnp3_gateway.messaging.resilient_publisher import ResilientPublisher
from dnp3_gateway.poller import run_poll_cycle
from dnp3_gateway.state import GatewayState

logger = logging.getLogger("dnp3_gateway")


def _print_console_banner(
    *,
    cfg: Settings,
    identity: GatewayIdentity,
    actual_health_port: int,
) -> None:
    """Konsolda port / URL / coklu instance kurallarini net gosterir."""

    host = cfg.worker_health_host
    port = actual_health_port
    # Banner'da gosterilen URL: 0.0.0.0 bind container icindir, kullanici
    # tarayicidan/curl'den asla 0.0.0.0:PORT yazmaz. Loopback'e dusur.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    health_url = f"http://{display_host}:{port}/health"
    config_path = f"{cfg.backend_api_url.rstrip('/')}/gateways/{identity.gateway_code}/config"
    print("", flush=True)
    print("  --- EnerjiOne DNP3 Gateway ---", flush=True)
    print(f"  Surum ............ {__version__}", flush=True)
    print(f"  GATEWAY_CODE ..... {identity.gateway_code}", flush=True)
    print(f"  Instance id ...... {identity.instance_id}", flush=True)
    print(f"  Saglik (HTTP) .... {health_url}   <- bu portta dinler (WORKER_HEALTH_PORT)", flush=True)
    print(
        f"  DNP3 (cihaz) ...... varsayilan TCP {cfg.dnp3_tcp_port} (.env DNP3_TCP_PORT); "
        "IP/port/DNP3 adresi asil kaynak: backend config `devices[]`",
        flush=True,
    )
    print(f"  Backend config ... GET {config_path}", flush=True)
    print(
        "  Token ............ .env icinde GATEWAY_TOKEN; backend `gateways.token` ile AYNI olmali.",
        flush=True,
    )
    print(
        "  Coklu proses ..... Ayni GATEWAY_CODE+token ile iki kopya ACMAYIN. "
        "Ikinci sunucu/servis -> yeni kod (orn. GW-002), arayuzden yeni gateway + yeni token + ayri .env.",
        flush=True,
    )
    if cfg.is_mock_mode:
        print(
            "  *** UYARI: GATEWAY_MODE=mock  ->  SAHADAN DNP3 ILE OKUMA YOK; "
            "degerler ureticidir. Gercek cihaz icin .env: GATEWAY_MODE=dnp3 (+ pip install nfm-dnp3).",
            flush=True,
        )
    print("  ------------------------------", flush=True)
    print("", flush=True)


def _mask_secret(value: str, *, keep: int = 2) -> str:
    """Token mask: ilk + son N karakter haric ortayi gizle.

    Default keep=2 (eski 4'ten dusuruldu): 32 karakter token'da bile sadece
    4 karakter gozukur, brute-force riski yok.
    """
    v = value.strip()
    if len(v) <= 2 * keep + 2:
        return "***"
    return f"{v[:keep]}...{v[-keep:]}"


def _execute_pending_commands(reader, state: GatewayState, pending) -> list[dict]:
    """Bekleyen komutlari operate_device (CROB) ile calistirir, sonuc listesi doner.

    Her sonuc: {id, ok, status, error?} — backend command-results endpoint'inin
    bekledigi bicim. Cihaz bulunamaz/hata olursa ok=False + anlamli status.
    Bir komutun hatasi digerlerini durdurmaz.
    """
    results: list[dict] = []
    devices_by_code = {d.code: d for d in state.devices()}
    for cmd in pending:
        device = devices_by_code.get(cmd.device_code)
        if device is None:
            logger.warning(
                "pending_command_device_not_found id=%s device=%s",
                cmd.id, cmd.device_code,
            )
            results.append(
                {"id": cmd.id, "ok": False, "status": "device_not_found",
                 "error": f"cihaz config'te yok: {cmd.device_code}"}
            )
            continue
        try:
            res = reader.operate_device(
                device=device,
                index=cmd.dnp3_index,
                op_type=cmd.op_type,
                count=cmd.count,
                on_time_ms=cmd.on_time_ms,
                off_time_ms=cmd.off_time_ms,
            )
            ok = bool(res.get("ok"))
            logger.info(
                "pending_command_executed id=%s device=%s index=%s ok=%s status=%s",
                cmd.id, cmd.device_code, cmd.dnp3_index, ok, res.get("status"),
            )
            results.append(
                {"id": cmd.id, "ok": ok, "status": str(res.get("status", "unknown")),
                 "error": res.get("error")}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "pending_command_failed id=%s device=%s", cmd.id, cmd.device_code
            )
            results.append(
                {"id": cmd.id, "ok": False, "status": "error", "error": str(exc)[:400]}
            )
    return results


def _run_config_refresh(
    *,
    client: BackendConfigClient,
    state: GatewayState,
    config_ready: Event,
    stop_event: Event,
    refresh_sec: int,
) -> None:
    """Backend config refresh thread'i. Basarili durumda sabit interval;
    hata durumunda exponential backoff (`refresh_sec → 2*refresh_sec → ... → 300s cap`).

    401/403/404 hatalarinda spesifik mesaj uretir; bu durumlar genelde manuel
    duzeltme gerektirir (token degismis, gateway code yanlis vs.) — health
    endpoint'i `state.last_refresh_error` uzerinden bunu raporlar.
    """
    import random

    last_active_state: bool | None = None
    consecutive_failures = 0
    base_interval = max(5, refresh_sec)
    max_backoff = 300  # 5 dakika cap
    while not stop_event.is_set():
        try:
            config = client.fetch_config()
            changed = state.update(config)
            config_ready.set()
            if consecutive_failures:
                logger.info(
                    "config_refresh_recovered gateway=%s after_failures=%d",
                    config.gateway_code,
                    consecutive_failures,
                )
                consecutive_failures = 0
            if changed:
                logger.info(
                    "config_refresh gateway=%s version=%s devices=%s signals=%s active=%s",
                    config.gateway_code,
                    config.config_version,
                    len(config.devices),
                    len(config.signals),
                    config.is_active,
                )
            if last_active_state is None or last_active_state != config.is_active:
                last_active_state = config.is_active
                if config.is_active:
                    logger.info("gateway_polling_resumed gateway=%s", config.gateway_code)
                else:
                    logger.warning(
                        "gateway_polling_suspended gateway=%s (is_active=False)",
                        config.gateway_code,
                    )
            # Saglikli refresh — sabit interval'a don
            stop_event.wait(timeout=base_interval)
            continue
        except GatewayConfigError as exc:
            err = str(exc)
            consecutive_failures += 1
            state.record_refresh_error(err)
            # 401/403: token problemi — auto-recovery yok, manuel mudahale gerek
            if "401" in err or "403" in err:
                logger.error(
                    "config_auth_error gateway=%s consecutive=%d error=%s "
                    "— GATEWAY_TOKEN backend gateways.token ile eslesmiyor; "
                    ".env GATEWAY_TOKEN'i kontrol edin",
                    client.gateway_code,
                    consecutive_failures,
                    err,
                )
            elif "404" in err:
                logger.error(
                    "config_404_error gateway=%s consecutive=%d "
                    "— Backendde '%s' kodlu gateway yok. Arayüz: Mühendislik → "
                    "Gateway Yönetimi → ayni Kod + ayni Token ile kayit acin",
                    client.gateway_code,
                    consecutive_failures,
                    client.gateway_code,
                )
            else:
                # Network / 5xx / timeout — geri donusumlu, exponential backoff uygula
                if consecutive_failures in (1, 5, 30, 100, 1000):
                    # Sadece 1, 5, 30, 100, 1000 inci hatalarda WARN log; aksi
                    # halde DEBUG, log spam'i onlenir
                    logger.warning(
                        "config_refresh_error gateway=%s consecutive=%d error=%s",
                        client.gateway_code,
                        consecutive_failures,
                        err,
                    )
                else:
                    logger.debug(
                        "config_refresh_error gateway=%s consecutive=%d error=%s",
                        client.gateway_code,
                        consecutive_failures,
                        err,
                    )
        except Exception as exc:  # noqa: BLE001
            # GatewayConfigError disinda beklenmedik bir hata: pydantic
            # ValidationError, requests.exceptions.SSLError, ConnectionResetError,
            # urllib3 dekoder vb. Bu thread oldukcecekse gateway icin
            # "/health: config_refresh_failing" sessiz kalir ve config bir daha
            # YENILENMEZ. Bunu engellemek icin general Exception'i yakalayip
            # state'e yaziyoruz; backoff loop'u devam eder.
            err = f"unexpected_error: {type(exc).__name__}: {exc}"
            consecutive_failures += 1
            try:
                state.record_refresh_error(err)
            except Exception:  # noqa: BLE001
                # state.record_refresh_error de basarisiz olursa thread olmesin
                pass
            if consecutive_failures in (1, 5, 30, 100, 1000):
                logger.exception(
                    "config_refresh_unexpected_error gateway=%s consecutive=%d",
                    client.gateway_code,
                    consecutive_failures,
                )
            else:
                logger.debug(
                    "config_refresh_unexpected_error gateway=%s consecutive=%d error=%s",
                    client.gateway_code,
                    consecutive_failures,
                    err,
                )
        # Exponential backoff: base * 2^n, ±%20 jitter, cap 300s
        backoff = min(max_backoff, base_interval * (2 ** min(consecutive_failures - 1, 8)))
        jitter = backoff * random.uniform(-0.2, 0.2)
        wait_time = max(base_interval, backoff + jitter)
        stop_event.wait(timeout=wait_time)


def _install_signal_handlers(stop_event: Event) -> None:
    def _handler(signum, _frame):  # type: ignore[no-untyped-def]
        logger.info("signal_received signum=%s shutdown=starting", signum)
        stop_event.set()

    # Platform bazli sinyal listesi:
    #   - SIGINT (Ctrl+C): tum platformlarda
    #   - SIGTERM: POSIX (Linux/macOS); Windows'ta var ama event-driven
    #     (NSSM stop'ta tetiklenmez)
    #   - SIGBREAK (Ctrl+Break / Windows console close): SADECE Windows.
    #     NSSM "AppStopMethodConsole" Ctrl+Break gonderir -> SIGBREAK.
    signals_to_install = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals_to_install.append(sigbreak)
    for sig in signals_to_install:
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:
            # Windows alt thread'lerde bazi sinyaller atanamaz; sessiz gecmek
            # yerine WARN log atalim ki "neden Ctrl+Break calismadi" debug edilebilir.
            logger.warning(
                "signal_handler_install_failed signal=%s error=%s — bu sinyal "
                "graceful shutdown'i tetiklemeyecek; manuel restart gerekebilir",
                sig,
                exc,
            )


def _tls_verify_param(cfg: Settings) -> bool | str:
    """requests `verify` argumani: True | False | CA bundle yolu."""

    if cfg.backend_api_ca_path and str(cfg.backend_api_ca_path).strip():
        return str(cfg.backend_api_ca_path).strip()
    return cfg.backend_api_verify_ssl


def run(current_settings: Settings | None = None) -> int:
    cfg = current_settings or settings
    # Iki asamali logging: identity'yi bilmeden once minimal (stdout-only)
    # konfigure ederiz ki bootstrap log'lari kaybolmasin. Identity'yi
    # ogrendikten sonra TEKRAR konfigure ederiz; bu sefer per-instance
    # rotating file handler da acilir (LOG_FILE_PATH set ise).
    configure_logging(level=cfg.log_level, fmt=cfg.log_format)

    identity = bootstrap_gateway_identity(settings=cfg, app_version=__version__)
    # Multi-instance lock — ayni GATEWAY_CODE iki kez calismasin (outbox
    # corrupt + duplicate publish riskini onler). Lock dosyasi state_dir
    # icinde acik kalir; main fonksiyonu cikinca OS otomatik release eder.
    # Referansi local degiskende tutuyoruz ki garbage collect close etmesin.
    _instance_lock = acquire_instance_lock(
        state_dir=cfg.gateway_state_dir,
        gateway_code=identity.gateway_code,
    )
    # Identity hazir — rotating dosya handler'i da ekleyerek logging'i tekrar
    # yapilandir (idempotent: eski handler'lar temizlenir).
    configure_logging(
        level=cfg.log_level,
        fmt=cfg.log_format,
        file_path=cfg.log_file_path,
        file_max_bytes=cfg.log_file_max_bytes,
        file_backup_count=cfg.log_file_backup_count,
        gateway_code=identity.gateway_code,
        instance_id=identity.instance_id,
    )
    # Sirli verileri (token, NATS parolasi) log redaction listesine kaydet —
    # boylece exception/3rd party log'lara kazara sizmasin. Bu cagri
    # configure_logging SONRASI yapilmali (filter aktif olduktan).
    try:
        register_secret(identity.token)
        if cfg.gateway_refresh_token:
            register_secret(cfg.gateway_refresh_token)
        from urllib.parse import urlparse as _urlparse

        # NATS URL parolasi (primary publisher): nats://user:PASSWORD@host
        _nats_parsed = _urlparse(cfg.nats_url) if cfg.nats_url else None
        if _nats_parsed and _nats_parsed.password:
            register_secret(_nats_parsed.password)
    except Exception:  # noqa: BLE001
        # Secret kayit hatasi gateway'i durdurmamali
        pass
    # Konsola (stdout): log seviyesinden bagimsiz; kurulumda token eslemesini dogrulamak icin.
    if cfg.show_gateway_token_on_start:
        print(f"[dnp3-gateway] GATEWAY_TOKEN={identity.token}", flush=True)
    else:
        print(
            f"[dnp3-gateway] GATEWAY_TOKEN (maskeli)={_mask_secret(identity.token)}  "
            f"(tam metin icin .env: SHOW_GATEWAY_TOKEN_ON_START=true)",
            flush=True,
        )
    logger.info(
        "dnp3_gateway_starting version=%s gateway=%s instance=%s env=%s mode=%s publisher=%s backend=%s",
        __version__,
        identity.gateway_code,
        identity.instance_id,
        identity.app_environment,
        cfg.gateway_mode,
        cfg.telemetry_publisher,
        cfg.backend_api_url,
    )
    # Deprecation uyarisi: nats_dual_publish_enabled artik anlamsiz (JetStream
    # tek yol). Operator eski .env'den geliyorsa bilgilendir.
    if cfg.nats_dual_publish_enabled:
        logger.warning(
            "nats_dual_publish_enabled=True ayarlanmis ama DEPRECATED — "
            "gateway 0.4.x'te JetStream artik tek primary publisher, dual-publish "
            "modunu yok sayiyor. .env'den NATS_DUAL_PUBLISH_ENABLED=false yapin "
            "veya satiri silin."
        )
    # Loud uyari: insecure plaintext opt-out — operator clear-text http://public
    # ya da nats://public icin bilincli izin vermis. Boot'ta yuksek sesle log
    # atalim ki gozden kacmasin; uretim icin gecici (TLS kurulana kadar).
    if cfg.gateway_insecure_allow_plaintext:
        logger.warning(
            "GATEWAY_INSECURE_ALLOW_PLAINTEXT=true — production validator "
            "public host'a clear-text HTTP/nats:// izin veriyor. Token ve "
            "telemetri MITM riski altinda. Mumkun olan en kisa zamanda TLS "
            "kurun (backend Caddy/Let's Encrypt, NATS tls:// listener)."
        )
    # M5: Production'da LOG_LEVEL=DEBUG olursa loud WARN. DEBUG seviyesinde
    # 3rd-party kutuphaneler (requests, urllib3, sqlite3) cookie/payload/
    # header detaylari loglayabilir. `register_secret` + broker URL regex
    # cogu sirri yakalar AMA tum hassas verileri garanti edemez. Operator
    # gecici debug yapiyorsa OK; uzun sureli production DEBUG istenmez.
    if identity.app_environment == "production" and cfg.log_level.upper() == "DEBUG":
        logger.warning(
            "PRODUCTION_DEBUG_LOG_LEVEL: LOG_LEVEL=DEBUG bircok 3rd-party "
            "kutuphanenin (requests, urllib3, sqlite3 vb.) gizli detay "
            "logu uretmesine yol acar. Redaction filter saldirgan attack "
            "vector'lerini kapatsa da garanti vermez. Geçici debug oturumu "
            "icin OK; uzun sureli prod'da INFO/WARNING tavsiye edilir."
        )

    # Disk cache: backend kapali iken / container restart'ta gateway en son
    # gordugu config ile (cihaz IP'leri, sinyal listesi, master_address)
    # polling'e devam eder. Yeni config geldiginde uzerine yazilir.
    config_cache_path = Path(cfg.gateway_state_dir) / f"config_{identity.gateway_code}.json"
    state = GatewayState(
        cache_path=config_cache_path,
        cache_max_age_hours=cfg.config_cache_max_age_hours,
    )
    if state.load_from_cache():
        # Disk'te onceki config varsa polling hemen baslayabilir; backend
        # ulasilmazsa bile veri toplama durmaz.
        config_ready_initial = True
    else:
        config_ready_initial = False
    config_ready = Event()
    if config_ready_initial:
        config_ready.set()
    stop_event = Event()

    # publisher_holder: health server publisher'a referans tutmasin diye
    # 0-arglik callable; publisher daha sonra olusturulup buraya yazilir.
    # Boylece health_server start sonrasi publisher injection olabilir.
    publisher_holder: dict[str, Any] = {"publisher": None}

    def _publisher_provider() -> Any:
        return publisher_holder["publisher"]

    # reader_provider: /refresh-all endpoint'i icin reader'a erisim.
    # Reader bir sonraki adimlarda olusturulup state'e baglanacak; aradaki
    # holder pattern ile injection.
    reader_holder: dict[str, Any] = {"reader": None}

    def _reader_provider() -> Any:
        return reader_holder["reader"]

    # /refresh-all endpoint icin auth token. GATEWAY_REFRESH_TOKEN set edilmemis
    # ise endpoint TAMAMEN DEVRE DISI kalir (health_server.py refresh_token
    # bos olunca 503 doner). Eski fallback "GATEWAY_TOKEN'a dus" kaldirildi —
    # CHANGELOG ile tutarsizdi ve token leak'i tum gateway'e yayan attack
    # surface yaratiyordu.
    refresh_endpoint_token = (cfg.gateway_refresh_token or "").strip()
    if not refresh_endpoint_token:
        logger.warning(
            "refresh_all_endpoint_disabled gateway=%s — GATEWAY_REFRESH_TOKEN "
            ".env'de bos. Backend operator tetigi (`/refresh-all`) cagrildiginda "
            "503 doner. Operator paneli bu ozelligi kullanacaksa .env'e yuksek-entropy "
            "GATEWAY_REFRESH_TOKEN atayin.",
            identity.gateway_code,
        )

    command_endpoint_token = (cfg.gateway_command_token or "").strip()
    if command_endpoint_token:
        register_secret(command_endpoint_token)
    else:
        logger.warning(
            "operate_endpoint_disabled gateway=%s — GATEWAY_COMMAND_TOKEN "
            ".env'de bos. Backend cihaz komut proxy'si (`/operate`) cagrildiginda "
            "503 doner. Komut butonlari kullanilacaksa .env'e yuksek-entropy "
            "GATEWAY_COMMAND_TOKEN atayin.",
            identity.gateway_code,
        )

    health, metrics, actual_health_port = start_health_server(
        host=cfg.worker_health_host,
        port=cfg.worker_health_port,
        state=state,
        gateway_code=identity.gateway_code,
        gateway_mode=cfg.gateway_mode,
        config_ready=config_ready,
        instance_id=identity.instance_id,
        app_environment=identity.app_environment,
        publisher_provider=_publisher_provider,
        reader_provider=_reader_provider,
        # Backend bu endpoint'leri Bearer token ile cagirir.
        refresh_token=refresh_endpoint_token,
        command_token=command_endpoint_token,
        # Trusted reverse-proxy CIDR'leri — `X-Forwarded-For` sadece bu
        # subnet'lerden gelen istekler icin okunur. Bos ise XFF yok sayilir
        # (saldirgan rate-limit bypass yapamaz).
        trusted_proxies=cfg.health_trusted_proxies,
    )
    # Banner: gercek (auto-assigned olabilir) health portunu yansitabilmek icin
    # health_server start sonrasi yazdirilir.
    _print_console_banner(cfg=cfg, identity=identity, actual_health_port=actual_health_port)

    if cfg.telemetry_publisher == "nats":
        # Legacy/rollback publisher: NATS JetStream.
        from dnp3_gateway.messaging.jetstream_publisher import JetStreamPublisher

        broker = JetStreamPublisher.create(
            url=cfg.nats_url,
            subject_prefix=cfg.nats_subject_prefix,
            gateway_code=identity.gateway_code,
            connect_timeout_sec=cfg.nats_connect_timeout_sec,
            publish_timeout_sec=cfg.nats_publish_timeout_sec,
            tls_ca_path=cfg.nats_tls_ca_path,
            credentials_path=cfg.nats_credentials_path,
        )
        if broker is None:
            logger.error(
                "jetstream_publisher_create_failed — nats-py paketi yuklu degil. "
                "Gateway baslamiyor. requirements.txt + pip install -r"
            )
            raise SystemExit(1)
    else:
        # Varsayilan publisher: backend HTTP ingest. NAT arkasinda outbound HTTPS yeterli.
        from dnp3_gateway.messaging.http_publisher import HttpTelemetryPublisher

        broker = HttpTelemetryPublisher(
            base_url=cfg.backend_api_url,
            identity=identity,
            timeout_sec=cfg.config_timeout_sec,
            verify=_tls_verify_param(cfg),
        )

    # Outbox: broker'a yayinlanmamis mesajlari diske yazar,
    # retrier sonra gonderir. Process restart'a dayanikli (kalici SQLite);
    # at-least-once delivery. Disk-full koruma: max_pending asilirsa publisher
    # OutboxFullError raise eder, poller cycle'i durdurur (sessiz veri kaybi
    # yerine kontrollu duraklatma).
    outbox_path = Path(cfg.gateway_state_dir) / f"outbox_{identity.gateway_code}.db"
    outbox = Outbox(outbox_path, max_pending=cfg.outbox_max_pending)
    pending = outbox.pending_count()
    dead_letter_count = outbox.dead_letter_count()
    if pending:
        logger.warning(
            "outbox_pending_messages count=%s db=%s — retrier hizla bosaltacak",
            pending,
            outbox_path,
        )
    if dead_letter_count:
        logger.warning(
            "outbox_dead_letter_count count=%s — manuel inceleme gerekebilir "
            "(SELECT * FROM outbox_dead_letter ORDER BY moved_at DESC)",
            dead_letter_count,
        )

    publisher = ResilientPublisher(broker=broker, outbox=outbox)
    # Health server'in /health/metrics endpoint'leri publisher uzerinden
    # outbox + circuit breaker durumunu okuyabilsin diye holder'a yaz
    publisher_holder["publisher"] = publisher
    retrier = OutboxRetrier(
        outbox,
        publish_fn=publisher.publish_outbox_row,
        poll_interval_sec=cfg.outbox_retrier_poll_interval_sec,
        batch_size=cfg.outbox_retrier_batch_size,
        max_retries=cfg.outbox_max_retries,
        min_backoff_sec=cfg.outbox_retrier_min_backoff_sec,
        max_backoff_sec=cfg.outbox_retrier_max_backoff_sec,
    )
    retrier.start()

    reader: TelemetryReader = build_adapter(cfg)
    # Health server /refresh-all endpoint'i icin injection
    reader_holder["reader"] = reader
    logger.info("telemetry_adapter=%s", type(reader).__name__)
    if cfg.is_mock_mode:
        logger.warning(
            "TELEMETRI_KAYNAGI=MOCK — sahadan DNP3 okunmuyor; uretici degerler. "
            "JetStream akisi calisir ama kaynak cihaz degil. Gercek okuma: "
            "GATEWAY_MODE=dnp3, yadnp3 wheel kurulu, cihaz IP/port/DNP3 adresi backend config."
        )
    else:
        logger.info(
            "TELEMETRI_KAYNAGI=DNP3 — library=%s; baglanti: backend devices[] "
            "(ip, dnp3_address, istege bagli dnp3_tcp_port; yoksa varsayilan port=%s).",
            (cfg.dnp3_library or "yadnp3"),
            cfg.dnp3_tcp_port,
        )

    config_client = BackendConfigClient(
        base_url=cfg.backend_api_url,
        identity=identity,
        timeout_sec=cfg.config_timeout_sec,
        verify=_tls_verify_param(cfg),
        response_max_bytes=cfg.backend_response_max_bytes,
    )

    refresh_thread = Thread(
        target=_run_config_refresh,
        kwargs={
            "client": config_client,
            "state": state,
            "config_ready": config_ready,
            "stop_event": stop_event,
            "refresh_sec": cfg.config_refresh_sec,
        },
        name="config-refresh",
        daemon=True,
    )
    refresh_thread.start()

    _install_signal_handlers(stop_event)

    # Ilk config gelene kadar 15 saniye bekle. Backend erisilemezse yine de
    # dongu calismaya baslar ama `is_active` False oldugu icin mesaj yayinlamaz.
    if not config_ready.wait(timeout=15):
        logger.warning("first_config_not_received_yet gateway=%s", identity.gateway_code)

    logger.info(
        "dnp3_gateway_running health=%s:%s instance=%s",
        cfg.worker_health_host,
        actual_health_port,
        identity.instance_id,
    )

    try:
        while not stop_event.is_set():
            now_monotonic = time.monotonic()
            # Operator tetikli "tum cihazlara sorgu at" bayragi varsa
            # cycle baslamadan once integrity poll iste; cevaplar SOE
            # callback'i ile cache'e yazilacak ve sonraki cycle'larda
            # poller normal akista publish edecek.
            try:
                # Shutdown sirasinda refresh-all tetiklenirse reader.close()
                # ile yarisi olabilir; stop_event onceligi ver, tetikleme.
                if (
                    not stop_event.is_set()
                    and state.take_refresh_request()
                    and hasattr(reader, "refresh_all_devices")
                ):
                    ok, total = reader.refresh_all_devices()  # type: ignore[attr-defined]
                    logger.info(
                        "manual_refresh_all_triggered ok=%d total=%d", ok, total
                    )
            except Exception:  # noqa: BLE001
                logger.exception("manual_refresh_all_dispatch_failed")

            # Bekleyen cihaz komutlari (config-poll ile geldi): her birini
            # operate_device (CROB) ile calistir, sonuclari backend'e bildir.
            # Gateway NAT arkasinda oldugundan komut sadece bu pull kanaliyla
            # gelebilir (backend gateway'e ulasamaz).
            if not stop_event.is_set() and hasattr(reader, "operate_device"):
                pending = state.take_pending_commands()
                if pending:
                    results = _execute_pending_commands(reader, state, pending)
                    try:
                        config_client.report_command_results(results)
                    except Exception:  # noqa: BLE001
                        # Bildirim basarisiz — komut CALISTI ama sonuc gitmedi.
                        # Backend komutu 'sent' tutar; operator UI'da sonuc
                        # gormezse tekrar tetikleyebilir. Bir sonraki poll'de
                        # tekrar denemeyiz (kuyruk temizlendi) — kabul edilebilir.
                        logger.exception("command_results_report_failed")
            due_count = len(state.due_devices(now_monotonic))
            published = run_poll_cycle(
                gateway_code=identity.gateway_code,
                state=state,
                reader=reader,
                publisher=publisher,
                now_monotonic=now_monotonic,
                max_parallel=cfg.max_parallel_devices,
                device_timeout_sec=cfg.device_poll_timeout_sec,
                cycle_timeout_sec=cfg.cycle_timeout_sec,
                stop_event=stop_event,
            )
            if due_count or published:
                metrics.record_cycle(devices=due_count, published=published)
            if published:
                logger.info(
                    "poll_cycle gateway=%s published=%s devices=%s version=%s",
                    identity.gateway_code,
                    published,
                    due_count,
                    state.config_version(),
                )
            stop_event.wait(timeout=max(1, cfg.default_poll_interval_sec))
    except KeyboardInterrupt:
        logger.info("dnp3_gateway_interrupted")
    finally:
        # Graceful shutdown — kaynaklarin sirayla ve guvenli sekilde
        # kapatilmasi. Her bir adim try/except ile koruma altinda; bir
        # adimda hata olursa sonraki adim yine calisir, tum kaynak temizlenir.
        logger.info("dnp3_gateway_shutdown_starting")
        stop_event.set()

        # 1) Config refresh thread: stop_event set, thread'in mevcut wait()
        # sonlanir; join ile gerçekten bittigini bekle. Daemon=True olsa bile
        # join edilirse temiz cikis garantilenir.
        try:
            if refresh_thread.is_alive():
                refresh_thread.join(timeout=5.0)
                if refresh_thread.is_alive():
                    logger.warning(
                        "refresh_thread_join_timeout — thread 5s icinde sonlanmadi, "
                        "shutdown'a devam ediliyor"
                    )
        except Exception:  # noqa: BLE001
            logger.debug("refresh_thread_join_error", exc_info=True)

        # 2) Reader: DNP3 master/channel kapanir, TCP RST gonderir
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            logger.debug("reader_close_error", exc_info=True)

        # 3) OutboxRetrier: bg thread durur, in-flight publish'in bitmesini
        # bekler (timeout 3s)
        try:
            retrier.stop(timeout_sec=3.0)
        except Exception:  # noqa: BLE001
            logger.debug("retrier_stop_error", exc_info=True)

        # 4) Broker publisher (HTTP/NATS): connection kapanir
        try:
            publisher.close()
        except Exception:  # noqa: BLE001
            logger.debug("publisher_close_error", exc_info=True)

        # 5) Health HTTP server: yeni baglantilari reddeder ve in-flight
        # request'lerin bitmesini bekler
        try:
            health.shutdown()
            health.server_close()
        except Exception:  # noqa: BLE001
            logger.debug("health_server_shutdown_error", exc_info=True)

        # 6) Final durum raporu
        try:
            pending = outbox.pending_count()
            dl_count = outbox.dead_letter_count()
            if pending:
                logger.warning(
                    "shutdown_pending_outbox count=%s db=%s — sonraki baslangicta gonderilecek",
                    pending,
                    outbox_path,
                )
            if dl_count:
                logger.warning(
                    "shutdown_dead_letter count=%s — operator inceleyebilir",
                    dl_count,
                )
        except Exception:  # noqa: BLE001
            logger.debug("outbox_count_error", exc_info=True)
        logger.info("dnp3_gateway_stopped")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
