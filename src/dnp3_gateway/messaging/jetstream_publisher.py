"""NATS JetStream publisher — gateway'in PRIMARY yayincisi.

Tasarim:
  * Gateway artik telemetriyi DIRECT JetStream'e basar. RabbitMQ telemetri
    akisindan kaldirildi (alarm/bildirim icin backend tarafinda kalmaya devam
    ediyor — gateway onunla ilgilenmez).
  * Sync facade: nats-py asyncio tabanli, gateway kodu sync (pika gibi).
    Bir background asyncio loop thread'inde calistirilir; `publish()` sync.
  * At-least-once garantisi:
      - `publish()` basarisiz olursa raise eder. ResilientPublisher mesaji
        outbox'a (SQLite) yazar. Retrier thread daha sonra yeniden dener.
      - NATS server'a baglanti yoksa publish hemen raise eder → outbox.
      - Baglanti gelince retrier outbox'i bosaltir.
  * Lazy/background connect:
      - Eski tasarim: `create()` block edip bagklanti deniyordu, fail edince
        None donuyordu. YENI: baglanti background thread'de baslar; yokken bile
        publisher INSTANCE doner. publish() raise eder → mesaj outbox'a duser
        → bagklanti gelince retrier gonderir. Boylece NATS olmadan da gateway
        ayaga kalkar ve ilk mesajlar outbox'ta birikir (mesaj kaybi YOK).
  * Lazy import: nats-py paketi yoksa modul yine import edilebilir, ama
    `create()` None doner (paket olmadan calismaz; production'da requirements
    icinde, dolayisiyla bu sadece dev/test icin koruyucu).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

try:
    import nats  # type: ignore[import-not-found]

    NATS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dev fallback
    nats = None  # type: ignore[assignment]
    NATS_AVAILABLE = False


class JetStreamPublishError(Exception):
    """JetStream publish basarisiz oldu — outbox'a yazilir, retrier yeniden dener."""


class JetStreamNotReadyError(JetStreamPublishError):
    """Broker baglantisi henuz kurulmadi / koptu — TRANSIENT hata.

    Bu istisna semantigi: "fiziksel mesaj reddedilmedi, sadece publisher
    su an hazir degil". OutboxRetrier bu hatayi gorunce retry_count'u
    ARTIRMAMALIDIR (aksi takdirde NATS uzun outage'inda mesajlar dead-
    letter'a sessizce migre olur). Sadece backoff'a gecip beklemeli."""


class JetStreamPublisher:
    """Sync facade over async nats-py JetStream publish.

    Kullanim:
      pub = JetStreamPublisher.create(url=..., subject_prefix=..., gateway_code=...)
      if pub is not None:
          try:
              pub.publish(payload, message_id=..., correlation_id=..., headers=...)
          except JetStreamPublishError:
              # caller (ResilientPublisher) outbox'a yazar
              pass
      pub.close()  # shutdown
    """

    def __init__(
        self,
        *,
        url: str,
        subject_prefix: str,
        gateway_code: str,
        connect_timeout_sec: int,
        publish_timeout_sec: float,
        tls_ca_path: str = "",
        credentials_path: str = "",
    ) -> None:
        self.url = url
        self.subject_prefix = subject_prefix.rstrip(".")
        self.gateway_code = gateway_code
        self.subject = f"{self.subject_prefix}.{gateway_code}"
        self._connect_timeout = connect_timeout_sec
        self._publish_timeout = publish_timeout_sec
        # NATS auth + TLS — bos string ise nats.connect default davranisi
        # kullanilir (URL'deki user:pass + system trust store).
        self._tls_ca_path = (tls_ca_path or "").strip()
        self._credentials_path = (credentials_path or "").strip()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._nc: Any = None
        self._js: Any = None
        # Background reconnect coroutine'in handle'i (close() icinde cancel
        # edebilmek icin saklanir). None ise henuz olusturulmadi veya
        # zaten resolved.
        self._reconnect_task: Any = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        # Counter erisimi GIL altinda int++ atomic'tir; yine de defansif
        # olarak _counter_lock altinda guncelliyoruz ki snapshot tutarlilik
        # bozulmasin (publish_failures + publish_successes ayni anda okunabilsin).
        self._counter_lock = threading.Lock()
        self._publish_failures = 0
        self._publish_successes = 0

    # ---- Factory --------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        url: str,
        subject_prefix: str,
        gateway_code: str,
        connect_timeout_sec: int = 5,
        publish_timeout_sec: float = 2.0,
        tls_ca_path: str = "",
        credentials_path: str = "",
    ) -> JetStreamPublisher | None:
        """Publisher instance olustur ve background thread'i baslat.

        nats-py paketi yoksa None doner (production'da requirements'ta
        oldugu icin gerceklesmemeli; sadece dev/test korumasi).

        NATS server'a baglanti SAGLANMASA bile non-None bir publisher
        doner — bagklanti background'da denenir, gelene kadar `publish()`
        cagrilari raise eder ve mesajlar outbox'a yazilir. Bu sayede
        NATS down iken bile gateway ayaga kalkar.
        """
        if not NATS_AVAILABLE:
            logger.error(
                "jetstream_publisher_unavailable reason=nats_py_missing "
                "(pip install nats-py>=2.6 ile yukleyin). Gateway telemetri "
                "yayinlayamaz! Production'da requirements.txt icinde olmali."
            )
            return None
        inst = cls(
            url=url,
            subject_prefix=subject_prefix,
            gateway_code=gateway_code,
            connect_timeout_sec=connect_timeout_sec,
            publish_timeout_sec=publish_timeout_sec,
            tls_ca_path=tls_ca_path,
            credentials_path=credentials_path,
        )
        inst._start()
        return inst

    # ---- Lifecycle ------------------------------------------------------
    def _start(self) -> None:
        """Background asyncio loop'u baslatir ve connect dener (non-blocking)."""
        self._loop = asyncio.new_event_loop()

        def _run() -> None:
            assert self._loop is not None
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:  # noqa: BLE001
                    logger.debug("jetstream_loop_close_error", exc_info=True)

        self._thread = threading.Thread(target=_run, name=f"jetstream-{self.gateway_code}", daemon=True)
        self._thread.start()

        # Connect best-effort (block kisa sure, fail edince devam et).
        # Bagklanti basarisiz olsa bile publisher instance doner; nats-py
        # `max_reconnect_attempts=-1` ile background'da denemeye devam eder
        # (publish failure ile outbox'a yazilir, baglanti gelince retrier
        # bosaltir).
        future = asyncio.run_coroutine_threadsafe(self._connect_safe(), self._loop)
        try:
            future.result(timeout=self._connect_timeout + 5)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "jetstream_publisher_initial_connect_failed url=%s gateway=%s "
                "error=%s — gateway ayaga kalkmaya devam ediyor. Mesajlar "
                "outbox'a yazilacak, baglanti gelince retrier bosaltacak.",
                self.url,
                self.gateway_code,
                exc,
            )

    async def _connect_safe(self) -> None:
        """Connect denemesi — basarisiz olunca background'da denemeye devam et.

        nats-py'nin kendi reconnect mekanizmasi (max_reconnect_attempts=-1)
        ile birlestirilir; bizim is_ready Event'i ilk basari ile set olur.
        """
        # NATS auth + TLS opsiyonel parametreleri; sadece set edilenler nats.connect'e
        # gecirilir ki kutuphane default davranisini ezmeyelim.
        connect_kwargs: dict[str, Any] = {
            "servers": [self.url],
            "connect_timeout": self._connect_timeout,
            "max_reconnect_attempts": -1,
            "reconnect_time_wait": 2,
            "name": f"e1-dnp3-gw-{self.gateway_code}",
            "error_cb": self._on_nats_error,
            "reconnected_cb": self._on_reconnected,
            "disconnected_cb": self._on_disconnected,
            "closed_cb": self._on_closed,
        }
        # NKEY/JWT credentials (.creds dosyasi). nsc ile uretilir; user_credentials
        # parametresi nats-py'de path olarak veya tuple (jwt, seed) olarak kabul edilir.
        if self._credentials_path:
            connect_kwargs["user_credentials"] = self._credentials_path
        # Custom CA bundle (PEM) — kurumsal/self-signed CA icin SSLContext insa et.
        if self._tls_ca_path:
            import ssl as _ssl

            try:
                ctx = _ssl.create_default_context(cafile=self._tls_ca_path)
                connect_kwargs["tls"] = ctx
            except (OSError, _ssl.SSLError) as exc:
                logger.error(
                    "jetstream_publisher_tls_ca_load_failed path=%s error=%s — "
                    "system trust store'a dusuyor (kurumsal CA dogrulama yapamayabilir)",
                    self._tls_ca_path,
                    exc,
                )

        try:
            self._nc = await nats.connect(**connect_kwargs)  # type: ignore[union-attr]
            self._js = self._nc.jetstream()
            self._ready.set()
            logger.info(
                "jetstream_publisher_ready url=%s subject=%s",
                self.url,
                self.subject,
            )
        except Exception:
            # Connect olmadi ama nats-py reconnect denemeye devam etmiyor
            # (cunku ilk connect bile basaramadi). Background'da ayri bir
            # retry coroutine baslatalim — task referansini sakla ki close()
            # icinde cancel edebilelim.
            self._reconnect_task = asyncio.create_task(self._background_reconnect())
            raise

    async def _background_reconnect(self) -> None:
        """Ilk connect basarisiz oldu — yeniden denemeye devam et.

        `_closed.is_set()` ile loop sonlanmasi yumuşak (sleep tamamlandiktan
        sonra). Hizli sonlanma icin close() bu task'i `task.cancel()` ile
        kirar — bu coroutine `CancelledError` ile cikar.
        """
        backoff = 2.0
        while not self._closed.is_set():
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                logger.debug("jetstream_background_reconnect_cancelled")
                return
            try:
                new_nc = await nats.connect(  # type: ignore[union-attr]
                    servers=[self.url],
                    connect_timeout=self._connect_timeout,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=2,
                    name=f"e1-dnp3-gw-{self.gateway_code}",
                    error_cb=self._on_nats_error,
                    reconnected_cb=self._on_reconnected,
                    disconnected_cb=self._on_disconnected,
                    closed_cb=self._on_closed,
                )
                # Eski yarim-kapanmis nc varsa drain et (resource leak onlemi).
                # nc.connect() basarisiz oldugunda nats-py bazen yarim socket
                # birakir; defansif close cagiriyoruz.
                if self._nc is not None and self._nc is not new_nc:
                    try:
                        await self._nc.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._nc = new_nc
                self._js = self._nc.jetstream()
                self._ready.set()
                logger.info(
                    "jetstream_publisher_connected_after_retry url=%s subject=%s",
                    self.url,
                    self.subject,
                )
                return
            except Exception as exc:  # noqa: BLE001
                backoff = min(backoff * 1.5, 30.0)
                logger.debug(
                    "jetstream_reconnect_attempt_failed url=%s error=%s next_wait=%.1fs",
                    self.url,
                    exc,
                    backoff,
                )

    async def _on_nats_error(self, err: Exception) -> None:
        logger.debug("jetstream_nats_error: %s", err)

    async def _on_reconnected(self) -> None:
        self._ready.set()
        logger.info("jetstream_reconnected url=%s", self.url)

    async def _on_disconnected(self) -> None:
        self._ready.clear()
        logger.warning("jetstream_disconnected url=%s", self.url)

    async def _on_closed(self) -> None:
        self._ready.clear()
        logger.warning("jetstream_connection_closed url=%s", self.url)

    def close(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
        if self._loop is None:
            return
        # Asama 1: background reconnect task'ini iptal et (sleeping coroutine'i
        # yumusak sonlandirir, aksi halde 5+ sn beklerdik).
        try:
            future = asyncio.run_coroutine_threadsafe(self._cancel_reconnect(), self._loop)
            future.result(timeout=2)
        except Exception:  # noqa: BLE001
            logger.debug("jetstream_reconnect_cancel_error", exc_info=True)
        # Asama 2: drain (kisa timeout); broker yavasladiginda 30sn beklemek
        # yerine 2sn cap koyup zorla cik.
        try:
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            future.result(timeout=3)
        except Exception:  # noqa: BLE001
            logger.debug("jetstream_publisher_disconnect_error", exc_info=True)
        self._shutdown_loop()

    async def _cancel_reconnect(self) -> None:
        task = self._reconnect_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _disconnect(self) -> None:
        if self._nc is None:
            return
        # `nc.drain()` default 30sn timeout kullanir; bu shutdown'i bekletir.
        # 2sn'lik agresif cap koyuyoruz — broker yavasliyorsa zaten outbox'a
        # birikmis durumda; daha fazla beklemek anlamsiz.
        try:
            await asyncio.wait_for(self._nc.drain(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning(
                "jetstream_drain_timeout — broker drain 2sn icinde tamamlanmadi, fallback close cagiriliyor"
            )
            try:
                await self._nc.close()
            except Exception:  # noqa: BLE001
                logger.debug("jetstream_fallback_close_error", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.debug("jetstream_drain_error", exc_info=True)

    def _shutdown_loop(self) -> None:
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._loop = None
        self._thread = None
        self._nc = None
        self._js = None
        self._ready.clear()

    # ---- Publisher API (RabbitPublisher.publish ile uyumlu) -------------
    def publish(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        """Telemetri mesajini JetStream subject'ine yayinla.

        Hata durumlari → JetStreamPublishError raise (ResilientPublisher
        bunu outbox'a yazar, retrier daha sonra yeniden dener):
          - Publisher hazir degil (baglanti yok / kapali)
          - JetStream publish timeout
          - JetStream server hata kodu
        """
        if not self._ready.is_set() or self._loop is None or self._js is None:
            with self._counter_lock:
                self._publish_failures += 1
            raise JetStreamNotReadyError("publisher not ready (NATS connection unavailable)")

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # JetStream dedup: Nats-Msg-Id ile broker tarafi 2dk pencerede ayni
        # message_id'yi tek seferde kabul eder. At-least-once'i tag-engine'in
        # idempotent islemesiyle birlestirir.
        js_headers: dict[str, str] = {"Nats-Msg-Id": message_id}
        if correlation_id:
            js_headers["X-Correlation-Id"] = correlation_id
        if headers:
            for k, v in headers.items():
                if v is None:
                    continue
                js_headers[str(k)] = str(v)

        async def _do_publish() -> None:
            await self._js.publish(self.subject, body, headers=js_headers)

        try:
            future = asyncio.run_coroutine_threadsafe(_do_publish(), self._loop)
            future.result(timeout=self._publish_timeout)
            with self._counter_lock:
                self._publish_successes += 1
        except Exception as exc:
            with self._counter_lock:
                self._publish_failures += 1
            raise JetStreamPublishError(str(exc)) from exc

    # ---- Telemetry / health ---------------------------------------------
    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def publish_failures(self) -> int:
        with self._counter_lock:
            return self._publish_failures

    @property
    def publish_successes(self) -> int:
        with self._counter_lock:
            return self._publish_successes

    def counters_snapshot(self) -> dict[str, int]:
        """Iki sayaci tek lock altinda tutarli snapshot olarak doner.
        Health endpoint icin yararli — failures/successes ratio hesabinda
        yarisi okuyup yarisi guncellenmesinin onune gecer.
        """
        with self._counter_lock:
            return {
                "publish_failures": self._publish_failures,
                "publish_successes": self._publish_successes,
            }
