"""Broker + Outbox wrapper: at-least-once, kaybolmayan teslimat.

Gateway 0.4.x'te primary broker = JetStreamPublisher (NATS). Legacy
RabbitPublisher modulu rollback amaciyla duruyor (kullanilmiyor).

Akis:
  poller -> ResilientPublisher.publish(payload, ...)
              |
              v
        broker.publish (JetStream — gercek yayin)
              |
              +-- basarili    -> done (broker ack aldi)
              |
              +-- exception  -> Outbox.enqueue (SQLite, kalici)
                                ardindan return (no raise)
                                arka thread retry yapar

  Boylece poller ASLA mesaj kaybetmez; broker dusukse mesaj diskte birikir,
  broker geri gelince OutboxRetrier hizla bosaltir.

Disk-full / outbox-full circuit breaker:
  * Outbox dolarsa (Outbox.max_pending asilirsa) `OutboxFullError`
    raise edilir. Caller (poller) bu durumu yakalayip cycle'i durdurmali;
    health endpoint UNHEALTHY donmeli.
  * Cevirdigimiz davranis: ResilientPublisher OutboxFullError'i RAISE eder
    (eski "MESSAGE LOST" sessiz drop yerine). Poller yakalayip cycle'i kirar.

Broker API kontrati: `publish(payload, *, message_id, correlation_id, headers)`
imzasini destekleyen herhangi bir publisher (JetStreamPublisher, eski
RabbitPublisher). Caller bu wrapping ile broker degisikligine duyarsiz.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from dnp3_gateway.messaging.outbox import Outbox, OutboxFullError

logger = logging.getLogger(__name__)


class ResilientPublisher:
    def __init__(
        self,
        *,
        broker: Any,
        outbox: Outbox,
        secondary_publisher: Any = None,
    ) -> None:
        """
        broker: asil yayinci. JetStreamPublisher (yeni; default) veya legacy
          RabbitPublisher olabilir — ikisi de ayni `publish(payload, *,
          message_id, correlation_id, headers)` imzasini destekler. Mesaj
          kaybı garantisi outbox + retrier ile saglanir.
        outbox: broker dustugunde mesajlari kalici tutar; retrier thread bosaltir.
        secondary_publisher: OPSIYONEL ikinci yayin (legacy dual-publish modu;
          artik default kullanilmiyor). Birincil basari sonrasi BEST-EFFORT
          yayin yapilir; hata birincil akisi ETKILEMEZ, sadece counter+log.
        """
        self._broker = broker
        self._outbox = outbox
        self._secondary = secondary_publisher
        self._consecutive_failures = 0
        # Disk-full / outbox-full circuit breaker durumu (health endpoint
        # ve poller cycle bunu okur).
        self._lock = threading.Lock()
        self._outbox_full: bool = False
        self._outbox_full_since: float | None = None
        self._last_outbox_error: str | None = None
        # Secondary publisher icin ayri sayaclar (legacy dual-publish). Primary
        # broker davranisini etkilemez, sadece izleme.
        self._secondary_failures = 0
        self._secondary_successes = 0
        self._secondary_warn_thresholds = {1, 10, 100, 1000}

    @property
    def outbox_full(self) -> bool:
        with self._lock:
            return self._outbox_full

    @property
    def outbox_full_since(self) -> float | None:
        with self._lock:
            return self._outbox_full_since

    @property
    def last_outbox_error(self) -> str | None:
        with self._lock:
            return self._last_outbox_error

    def _set_outbox_full(self, error: str) -> None:
        with self._lock:
            if not self._outbox_full:
                self._outbox_full_since = time.time()
            self._outbox_full = True
            self._last_outbox_error = error[:500]

    def _clear_outbox_full(self) -> None:
        with self._lock:
            if self._outbox_full:
                logger.info("outbox_full_cleared — disk-full durumu sona erdi")
            self._outbox_full = False
            self._outbox_full_since = None
            self._last_outbox_error = None

    def publish(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        """Mesaji broker'a publish etmeyi dener; basarisizsa outbox'a yazar.

        Outbox dolu ise (disk-full senaryosu) `OutboxFullError` raise eder —
        caller (poller) bu istisnayi yakalayip cycle'i durdurmali. Sessiz
        veri kaybi yok.
        """
        try:
            self._broker.publish(
                payload,
                message_id=message_id,
                correlation_id=correlation_id,
                headers=headers,
            )
            if self._consecutive_failures:
                logger.info(
                    "resilient_publisher_recovered after_failures=%s",
                    self._consecutive_failures,
                )
                self._consecutive_failures = 0
            # Basarili publish → outbox-full durumunu da temizle (broker normal'e
            # dondu, retrier outbox'i bosaltacak)
            if self._outbox_full:
                self._clear_outbox_full()
            # Secondary publisher (legacy dual-publish; default kullanilmaz) —
            # best-effort. Hata primary akisi ETKILEMEZ; sadece sayac+log.
            self._publish_secondary_best_effort(
                payload,
                message_id=message_id,
                correlation_id=correlation_id,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            try:
                self._outbox.enqueue(
                    message_id=message_id,
                    correlation_id=correlation_id,
                    headers=headers,
                    payload=payload,
                    last_error=str(exc),
                )
            except OutboxFullError as full_exc:
                # Outbox max_pending'e ulasti — disk-full circuit breaker.
                # Caller'in poll cycle'i durdurmasi gerek; veri kaybi olmasin.
                self._set_outbox_full(str(full_exc))
                logger.error(
                    "outbox_full_circuit_breaker message_id=%s pending=%d limit=%d "
                    "publish_error=%s — POLLER DURDURULMALI, broker (NATS JetStream) "
                    "uzun suredir erisilemiyor olabilir",
                    message_id,
                    self._outbox.pending_count(),
                    self._outbox.max_pending,
                    exc,
                )
                # Cycle'in devam etmesini engellemek icin EXCEPTION YAY.
                # Eski davranis "sessiz drop" idi; bu artik bilincli olarak yok.
                raise full_exc
            except Exception as enq_exc:  # noqa: BLE001
                # Outbox SQLite write hatasi (disk IO, vb). Bu durumda da
                # circuit breaker'i tetikle ve raise et — sessiz drop yok.
                self._set_outbox_full(f"outbox_io_error: {enq_exc}")
                logger.error(
                    "outbox_enqueue_failed_after_publish_fail message_id=%s "
                    "publish_error=%s outbox_error=%s — POLLER DURDURULMALI",
                    message_id,
                    exc,
                    enq_exc,
                )
                raise enq_exc
            # Outbox enqueue basarili — sessiz fail edilmedi, retrier alir
            if self._consecutive_failures in (1, 5, 50, 500):
                logger.warning(
                    "publish_failed_outboxed message_id=%s error=%s consecutive=%s "
                    "(retrier arka planda yeniden deneyecek)",
                    message_id,
                    exc,
                    self._consecutive_failures,
                )

    def publish_batch(self, items: list[dict[str, Any]]) -> None:
        """Bir cihazin tum degisen sinyallerini TEK broker cagrisiyla yayinla.

        `items`: her biri {"payload", "message_id", "correlation_id", "headers"}.
        Broker `publish_batch` destekliyorsa (HTTP) tek POST; desteklemiyorsa
        (JetStream — mesaj-basina) tek tek `publish`'e duser.

        Basari -> outbox-full temizle. Hata -> TUM batch item'lari tek tek
        outbox'a yazilir (retrier mevcut satir-bazli mantikla bosaltir; sira
        ve at-least-once korunur). Outbox dolu -> OutboxFullError raise (breaker).
        """
        if not items:
            return
        batch_fn = getattr(self._broker, "publish_batch", None)
        if not callable(batch_fn):
            # Broker batch desteklemiyor -> tekli publish'e dus (mevcut davranis).
            for it in items:
                self.publish(
                    it["payload"],
                    message_id=it["message_id"],
                    correlation_id=it.get("correlation_id"),
                    headers=it.get("headers"),
                )
            return
        try:
            batch_fn(items)
            if self._consecutive_failures:
                logger.info("resilient_publisher_recovered after_failures=%s", self._consecutive_failures)
                self._consecutive_failures = 0
            if self._outbox_full:
                self._clear_outbox_full()
        except Exception as exc:  # noqa: BLE001
            # Batch POST basarisiz -> her item'i tek tek outbox'a yaz. Ilk
            # OutboxFullError'da breaker tetiklenir ve raise edilir (poller durur).
            self._consecutive_failures += 1
            for it in items:
                try:
                    self._outbox.enqueue(
                        message_id=it["message_id"],
                        correlation_id=it.get("correlation_id"),
                        headers=it.get("headers"),
                        payload=it["payload"],
                        last_error=str(exc),
                    )
                except OutboxFullError as full_exc:
                    self._set_outbox_full(str(full_exc))
                    logger.error(
                        "outbox_full_circuit_breaker (batch) pending=%d limit=%d error=%s",
                        self._outbox.pending_count(),
                        self._outbox.max_pending,
                        exc,
                    )
                    raise full_exc
                except Exception as enq_exc:  # noqa: BLE001
                    self._set_outbox_full(f"outbox_io_error: {enq_exc}")
                    logger.error("outbox_enqueue_failed_batch error=%s outbox_error=%s", exc, enq_exc)
                    raise enq_exc
            if self._consecutive_failures in (1, 5, 50, 500):
                logger.warning(
                    "publish_batch_failed_outboxed count=%d error=%s consecutive=%s",
                    len(items),
                    exc,
                    self._consecutive_failures,
                )

    def close(self) -> None:
        self._broker.close()
        if self._secondary is not None:
            try:
                self._secondary.close()
            except Exception:  # noqa: BLE001
                logger.debug("secondary_publisher_close_error", exc_info=True)

    # ---- Public outbox accessors (poller/health icin) ------------------------
    # Eski kod publisher._outbox'a private erisim yapiyordu ("None and X or -1"
    # antipattern dahil). Bu method'lar dis dunyaya kontrol edilebilir bir
    # API sunar.
    def pending_count(self) -> int:
        try:
            return int(self._outbox.pending_count())
        except Exception:  # noqa: BLE001
            return -1

    def dead_letter_count(self) -> int:
        try:
            return int(self._outbox.dead_letter_count())
        except Exception:  # noqa: BLE001
            return -1

    @property
    def outbox_max_pending(self) -> int:
        try:
            return int(self._outbox.max_pending)
        except Exception:  # noqa: BLE001
            return -1

    # Outbox retrier publish_fn icin kullanilan kanca: row -> broker.publish()
    def publish_outbox_row(self, row: dict[str, Any]) -> None:
        self._broker.publish(
            row["payload"],
            message_id=row["message_id"],
            correlation_id=row.get("correlation_id"),
            headers=row.get("headers"),
        )
        # Outbox'tan basarili gonderim → broker calisiyor demek; circuit
        # breaker'i da temizle.
        if self._outbox_full:
            self._clear_outbox_full()
        # Secondary publish: outbox'tan gec de olsa JetStream'e kopya gonderelim.
        # Nats-Msg-Id (message_id) dedup'i sayesinde tekrarli denemeler sorun
        # cikarmaz.
        self._publish_secondary_best_effort(
            row["payload"],
            message_id=row["message_id"],
            correlation_id=row.get("correlation_id"),
            headers=row.get("headers"),
        )

    # ------------------------------------------------------------------ ---
    def _publish_secondary_best_effort(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None,
        headers: dict[str, Any] | None,
    ) -> None:
        """Secondary publisher'a yayinla — basarisizliklari YUTAR.

        Bu fonksiyon PRIMARY broker akisindan TAMAMEN BAGIMSIZ. Hicbir
        kosulda exception yaymaz. Counter ve seyrek loglar tutulur (1, 10,
        100, 1000 ve sonra 1000'erli) ki sessiz olmasin.
        """
        if self._secondary is None:
            return
        try:
            self._secondary.publish(
                payload,
                message_id=message_id,
                correlation_id=correlation_id,
                headers=headers,
            )
            self._secondary_successes += 1
            self._secondary_failures = 0  # ust uste basari → counter sifirla
        except Exception as exc:  # noqa: BLE001
            self._secondary_failures += 1
            should_log = (
                self._secondary_failures in self._secondary_warn_thresholds
                or self._secondary_failures % 1000 == 0
            )
            if should_log:
                logger.warning(
                    "secondary_publisher_failed message_id=%s error=%s "
                    "consecutive=%s (RabbitMQ akisi devam ediyor)",
                    message_id,
                    exc,
                    self._secondary_failures,
                )

    @property
    def secondary_failures(self) -> int:
        return self._secondary_failures

    @property
    def secondary_successes(self) -> int:
        return self._secondary_successes
