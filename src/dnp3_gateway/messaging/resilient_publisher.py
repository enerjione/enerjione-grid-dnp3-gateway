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

# Outbox-full breaker histerezisi (low-water mark). Breaker acildiktan sonra
# poller ancak pending sayisi limitin bu oranina DUSTUGUNDE devam eder.
# Tek basarili gonderimde acmak, dolu bir outbox'ta "ac-kapa" dongusu yaratiyor
# ve her turda okunan olcumler bosa gidiyordu.
_OUTBOX_RESUME_RATIO = 0.80


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
        # Sayaclar KILIT ALTINDA: 100 poll worker'i es zamanli publish cagirir.
        # Kilitsizken `+= 1` kayip artislarla ilerliyor, "1/5/50/500" esikli
        # uyari loglari atlanabiliyordu — yani surekli hata veren bir broker
        # loglarda neredeyse sessiz kaliyordu.
        self._counter_lock = threading.Lock()
        self._consecutive_failures = 0
        # Son publish_batch cagrisinda mesajlar broker'a mi gitti (False) yoksa
        # outbox'a mi dustu (True)? Poller metrik dogrulugu icin okur:
        # outbox'a dusen mesajlar "published" sayilmamali.
        self._last_batch_outboxed = False
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
    def last_batch_outboxed(self) -> bool:
        """Son publish_batch broker yerine outbox'a mi dustu?"""
        return self._last_batch_outboxed

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
        """Breaker'i kapat — ANCAK HISTEREZIS ile.

        ESKI DAVRANIS — KAYIP DONGUSU:
        Tek bir basarili gonderim breaker'i temizliyordu. Outbox 500.000
        limitindeyken retrier bir satiri gonderip breaker'i sifirliyor, poller
        hemen 300 cihazi birden okumaya basliyor, ilk batch tekrar limite
        carpiyor ve breaker yeniden aciliyordu. Bu dongu dakikada onlarca kez
        tekrarliyor, her turunda okunan degerler bosa gidiyordu.

        Artik breaker yalnizca outbox gercekten NEFES ALDIGINDA kapanir:
        pending, limitin `_RESUME_RATIO`'su altina inmeli (low-water mark).
        """
        with self._lock:
            if not self._outbox_full:
                return
        try:
            pending = int(self._outbox.pending_count())
            limit = int(self._outbox.max_pending)
        except Exception:  # noqa: BLE001
            pending, limit = 0, 0
        resume_at = int(limit * _OUTBOX_RESUME_RATIO) if limit > 0 else 0
        if limit > 0 and pending > resume_at:
            # Henuz yeterince bosalmadi; breaker acik kalsin.
            return
        with self._lock:
            if not self._outbox_full:
                return
            self._outbox_full = False
            self._outbox_full_since = None
            self._last_outbox_error = None
        logger.info(
            "outbox_full_cleared pending=%d resume_threshold=%d limit=%d — "
            "poller yeniden calisabilir",
            pending,
            resume_at,
            limit,
        )

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
            with self._counter_lock:
                had_failures = self._consecutive_failures
                self._consecutive_failures = 0
            if had_failures:
                logger.info(
                    "resilient_publisher_recovered after_failures=%s", had_failures
                )
            # Basarili publish → outbox-full durumunu da temizlemeyi dene
            # (histerezis esigi saglanmissa gercekten kapanir).
            if self.outbox_full:
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
            with self._counter_lock:
                self._consecutive_failures += 1
                consecutive = self._consecutive_failures
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
            if consecutive in (1, 5, 50, 500) or consecutive % 1000 == 0:
                logger.warning(
                    "publish_failed_outboxed message_id=%s error=%s consecutive=%s "
                    "(retrier arka planda yeniden deneyecek)",
                    message_id,
                    exc,
                    consecutive,
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
            self._last_batch_outboxed = False
            with self._counter_lock:
                had_failures = self._consecutive_failures
                self._consecutive_failures = 0
            if had_failures:
                logger.info("resilient_publisher_recovered after_failures=%s", had_failures)
            if self.outbox_full:
                self._clear_outbox_full()
        except Exception as exc:  # noqa: BLE001
            # Batch POST basarisiz -> her item'i tek tek outbox'a yaz.
            #
            # ESKI DAVRANIS — SESSIZ VERI KAYBI:
            # Ilk OutboxFullError'da dongu HEMEN raise ediyordu; kalan item'lar
            # ne broker'a ne diske gidiyordu ve hicbir yerde sayilmiyordu.
            # Ustelik o degerlerin cache'teki dirty bayragi ZATEN temizlenmis
            # oldugu icin kayip telafi edilemiyordu.
            #
            # Artik: (a) dirty bayragi yayin onaylanana kadar temizlenmiyor
            # (bkz. poller._commit_published), (b) hata durumunda kalan item'lar
            # yine de best-effort enqueue ediliyor, (c) gercekten diske
            # yazilamayanlarin SAYISI acikca ERROR loglaniyor — sessiz degil.
            with self._counter_lock:
                self._consecutive_failures += 1
                consecutive = self._consecutive_failures
            first_fatal: Exception | None = None
            enqueued = 0
            dropped = 0
            for it in items:
                try:
                    self._outbox.enqueue(
                        message_id=it["message_id"],
                        correlation_id=it.get("correlation_id"),
                        headers=it.get("headers"),
                        payload=it["payload"],
                        last_error=str(exc),
                    )
                    enqueued += 1
                except (OutboxFullError, Exception) as enq_exc:  # noqa: BLE001
                    dropped += 1
                    if first_fatal is None:
                        first_fatal = enq_exc
                        if isinstance(enq_exc, OutboxFullError):
                            self._set_outbox_full(str(enq_exc))
                        else:
                            self._set_outbox_full(f"outbox_io_error: {enq_exc}")
            if first_fatal is not None:
                logger.error(
                    "outbox_enqueue_failed_batch enqueued=%d dropped=%d pending=%d "
                    "limit=%d publish_error=%s outbox_error=%s — POLLER DURDURULUYOR; "
                    "kalicilastirilamayan olcumler cihaz cache'inde 'degismis' olarak "
                    "kalir ve broker geri gelince yeniden yayinlanir",
                    enqueued,
                    dropped,
                    self._outbox.pending_count(),
                    self._outbox.max_pending,
                    exc,
                    first_fatal,
                )
                raise first_fatal from exc
            # Mesajlar broker'a degil outbox'a dustu — poller bunu "published"
            # saymamali (metrik yalani onlemi).
            self._last_batch_outboxed = True
            if consecutive in (1, 5, 50, 500) or consecutive % 1000 == 0:
                logger.warning(
                    "publish_batch_failed_outboxed count=%d error=%s consecutive=%s",
                    len(items),
                    exc,
                    consecutive,
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
        # Outbox'tan basarili gonderim → broker calisiyor demek; breaker'i
        # kapatmayi dene (histerezis esigi saglaninca gercekten kapanir).
        if self.outbox_full:
            self._clear_outbox_full()
        self._after_outbox_send()
        # Secondary publish: outbox'tan gec de olsa JetStream'e kopya gonderelim.
        # Nats-Msg-Id (message_id) dedup'i sayesinde tekrarli denemeler sorun
        # cikarmaz.
        self._publish_secondary_best_effort(
            row["payload"],
            message_id=row["message_id"],
            correlation_id=row.get("correlation_id"),
            headers=row.get("headers"),
        )

    def publish_outbox_rows(self, rows: list[dict[str, Any]]) -> None:
        """Outbox satirlarini TOPLU gonder (broker destekliyorsa tek POST).

        Drenaj hizinin ana kaldiraci: eskiden retrier her satiri ayri POST +
        ayri DELETE/commit ile gonderiyordu. LAN RTT 20ms ile bu, 200'luk bir
        batch'te ~4 saniye + 200 fsync demekti; her batch sonrasi da 2 saniye
        uyunuyordu (~32 mesaj/sn). 500.000 mesajlik bir birikim saatler suruyor,
        bu sirada uretim devam ettigi icin net drenaj sifira yaklasiyordu.

        Broker toplu gonderimi desteklemiyorsa AttributeError yerine acik bir
        hata veririz; retrier tekil yola duser.
        """
        batch_fn = getattr(self._broker, "publish_batch", None)
        if not callable(batch_fn):
            raise NotImplementedError("broker publish_batch desteklemiyor")
        if not rows:
            return
        batch_fn(
            [
                {
                    "payload": r["payload"],
                    "message_id": r["message_id"],
                    "correlation_id": r.get("correlation_id"),
                    "headers": r.get("headers"),
                }
                for r in rows
            ]
        )
        if self.outbox_full:
            self._clear_outbox_full()
        self._after_outbox_send()

    def _after_outbox_send(self) -> None:
        """Outbox'tan basarili gonderim sonrasi ortak kanca (alt siniflar icin)."""
        return None

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
