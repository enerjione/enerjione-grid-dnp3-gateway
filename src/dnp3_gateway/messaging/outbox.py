r"""SQLite tabanli persistent outbox: broker'a gonderilemeyen mesajlari kaybetmez.

Mimari (broker = NATS JetStream 0.4.x'te):
  poller -> publish() try -> broker
                          \-> fail/exception -> outbox.enqueue(mesaj) -> SQLite
  background OutboxRetrier thread -> outbox.dequeue_batch()
                                  -> publisher.publish()
                                  -> basarili: outbox.delete(id), basarisiz: retry_count++
                                  -> retry_count > MAX -> dead-letter tablosuna tasi
                                  -> JetStreamNotReadyError -> retry_count ARTMAZ
                                     (transient; broker yokken sessiz dead-letter
                                     migrasyonu engellenmis)

Garantiler:
  - Process restart'a dayanikli: SQLite diskte; baslangicta queue okunur.
  - At-least-once delivery: ayni mesaj iki kere gidebilir; tag-engine idempotent
    (`message_id` bazli) tasarlanmistir, yan etki yok.
  - At-most-once kayip yok: publish exception olsa bile mesaj outbox'ta.
  - Poison message koruma: bir mesaj MAX kez denenip basarisiz olursa
    dead_letter tablosuna tasinir; ana kuyruk bos kalmaz.
  - Disk dolma koruma: pending_count > THRESHOLD ise enqueue raise eder
    (ResilientPublisher disk-full circuit breaker icin kullanir).

Goldman: ayri SQLite dosyasi her gateway icin (`GATEWAY_STATE_DIR/outbox_<CODE>.db`).
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dnp3_gateway.messaging.sqlite_support import Migration, open_versioned_db

logger = logging.getLogger(__name__)

# Bir mesaj kac kere retry edilirse dead-letter tablosuna tasinir.
# 100 retry × 60sn (max backoff) = ~100 dakika; bundan sonra mesajin
# transit-only sorununu asmasi cok zayif ihtimal — kalici poison.
DEFAULT_MAX_RETRIES = 100

# Outbox doldugu zaman enqueue() OutboxFullError raise eder. Tipik production
# 100 cihaz × saniyede ~50 mesaj. Broker (NATS) down 1 saat boyunca = 180,000
# mesaj birikebilir; bu sinir asilirsa publisher disk-full davranisina gecer.
DEFAULT_MAX_PENDING = 500_000

# Exponential backoff parametreleri (OutboxRetrier).
# Basarisiz cycle sonrasi: 1s -> 1.5s -> 2.25s -> ... cap 60s.
DEFAULT_MIN_BACKOFF_SEC = 1.0
DEFAULT_MAX_BACKOFF_SEC = 60.0
DEFAULT_BACKOFF_MULTIPLIER = 1.5
DEFAULT_BACKOFF_JITTER = 0.2  # ±%20 jitter (thundering herd onlemi)


class OutboxFullError(RuntimeError):
    """Outbox doldu (>= max_pending). Publisher bunu disk-full circuit
    breaker'i tetiklemek icin yakalar."""


def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """v1 — baslangic semasi (0.4.x ile ayni; mevcut dosyalar uyumlu)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            correlation_id TEXT,
            headers TEXT,
            payload TEXT NOT NULL,
            enqueued_at REAL NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_outbox_enqueued_at ON outbox(enqueued_at);

        -- Poison message'lar burada (retry_count > MAX olmus olanlar). Manuel
        -- inceleme icin saklanir. Retention v2'de eklendi.
        CREATE TABLE IF NOT EXISTS outbox_dead_letter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            correlation_id TEXT,
            headers TEXT,
            payload TEXT NOT NULL,
            enqueued_at REAL NOT NULL,
            moved_at REAL NOT NULL,
            retry_count INTEGER NOT NULL,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_dead_letter_moved_at
            ON outbox_dead_letter(moved_at);
        """
    )


def _migration_002_next_attempt_at(conn: sqlite3.Connection) -> None:
    """v2 — satir-bazli backoff icin `next_attempt_at`.

    Eskiden retrier TUM kuyrugu tek bir global backoff ile yonetiyordu ve
    kalici hatali tek bir satir kuyrugun basini sonsuza kadar tikayabiliyordu
    (head-of-line blocking). Satir bazli `next_attempt_at` ile hatali satir
    ertelenir, arkasindaki telemetri akmaya devam eder.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()}
    if "next_attempt_at" not in cols:
        conn.execute(
            "ALTER TABLE outbox ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_outbox_next_attempt ON outbox(next_attempt_at, id)"
    )


_MIGRATIONS: list[Migration] = [
    (1, "initial outbox + dead_letter schema", _migration_001_initial),
    (2, "outbox.next_attempt_at (satir-bazli backoff)", _migration_002_next_attempt_at),
]


class Outbox:
    """Thread-safe SQLite tabanli persistent kuyruk."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._max_pending = max(1000, int(max_pending))
        # Surumlenmis + butunlugu dogrulanmis baglanti:
        #   * PRAGMA quick_check — bozuk dosya karantinaya alinir, gateway
        #     kalici boot arizasina girmez (eskiden yapicida raise ediyordu).
        #   * PRAGMA user_version tabanli migration — yeni kolon eklemek
        #     sahadaki mevcut .db dosyalarini crash-loop'a sokmaz.
        # POSIX chmod 600 open_versioned_db icinde uygulanir (telemetri
        # payload'u + cihaz IP'leri diger kullanicilara kapali kalsin).
        self._conn: sqlite3.Connection | None = open_versioned_db(
            self.db_path,
            migrations=_MIGRATIONS,
            label=f"outbox[{self.db_path.name}]",
            synchronous="NORMAL",
            wal_autocheckpoint=1000,
        )
        # Estimate counter: enqueue'da SELECT COUNT(*) yapmak yerine in-memory
        # sayac tutariz. Init'te bir kez tam COUNT, sonra +/- delta.
        self._pending_estimate: int = 0
        try:
            with self._lock:
                c = self._cached_connection()
                (n,) = c.execute("SELECT COUNT(*) FROM outbox").fetchone()
                self._pending_estimate = int(n)
        except sqlite3.Error:
            self._pending_estimate = 0

    def _cached_connection(self) -> sqlite3.Connection:
        """Persistent connection — caller'in self._lock altinda olmasi gerekir."""
        if self._conn is None:
            self._conn = self._open_connection()
        return self._conn

    def _open_connection(self) -> sqlite3.Connection:
        """Yeniden baglanti (close sonrasi). Migration + quick_check dahil."""
        return open_versioned_db(
            self.db_path,
            migrations=_MIGRATIONS,
            label=f"outbox[{self.db_path.name}]",
            synchronous="NORMAL",
            wal_autocheckpoint=1000,
        )

    # Geriye uyumluluk: eski testler veya kullanim _connect()'i context
    # manager olarak cagiriyor olabilir.
    def _connect(self) -> sqlite3.Connection:
        return self._open_connection()

    def _init_db(self) -> None:
        """Geriye uyumluluk no-op — sema artik open_versioned_db ile kurulur."""
        return None

    @property
    def max_pending(self) -> int:
        return self._max_pending

    def enqueue(
        self,
        *,
        message_id: str,
        correlation_id: str | None,
        headers: dict[str, Any] | None,
        payload: dict[str, Any],
        last_error: str | None = None,
    ) -> int:
        """Outbox'a mesaj ekler. Outbox dolu ise OutboxFullError raise eder.

        Performans: pending limit kontrolu in-memory `_pending_estimate` ile
        yapilir; her enqueue'da SELECT COUNT(*) tarama YOK. 500K satirlik
        outbox'ta bile O(1).

        Disk dolma korumasi: max_pending'i asarsa publisher caller'i disk-full
        circuit breaker'i tetikler (poll cycle'i durdurur, /health UNHEALTHY).
        """
        with self._lock:
            if self._pending_estimate >= self._max_pending:
                raise OutboxFullError(
                    f"outbox dolu (pending~={self._pending_estimate}, "
                    f"limit={self._max_pending}); broker (NATS JetStream) uzun "
                    "suredir erisilemiyor olabilir"
                )
            c = self._cached_connection()
            cur = c.execute(
                "INSERT INTO outbox (message_id, correlation_id, headers, payload, enqueued_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    correlation_id,
                    json.dumps(headers, ensure_ascii=False) if headers else None,
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                    last_error,
                ),
            )
            c.commit()
            self._pending_estimate += 1
            return int(cur.lastrowid or 0)

    def fetch_batch(self, limit: int = 200) -> list[dict[str, Any]]:
        """En eski mesajlardan limit kadarini doner."""
        with self._lock:
            c = self._cached_connection()
            rows = c.execute(
                "SELECT id, message_id, correlation_id, headers, payload, retry_count, enqueued_at, last_error "
                "FROM outbox ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "message_id": r[1],
                "correlation_id": r[2],
                "headers": json.loads(r[3]) if r[3] else None,
                "payload": json.loads(r[4]),
                "retry_count": r[5],
                "enqueued_at": r[6],
                "last_error": r[7],
            }
            for r in rows
        ]

    def delete(self, row_id: int) -> None:
        with self._lock:
            c = self._cached_connection()
            cur = c.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
            c.commit()
            if cur.rowcount > 0:
                self._pending_estimate = max(0, self._pending_estimate - cur.rowcount)

    def mark_retry(self, row_id: int, error: str) -> None:
        # Hata mesaji 2KB'a kadar saklanir — full traceback genelde yeterli.
        with self._lock:
            c = self._cached_connection()
            c.execute(
                "UPDATE outbox SET retry_count = retry_count + 1, last_error = ? WHERE id = ?",
                (error[:2000], row_id),
            )
            c.commit()

    def move_to_dead_letter(self, row_id: int, error: str) -> bool:
        """Bir mesaj MAX kez denenip hala basarisiz oldugunda dead-letter
        tablosuna tasir. Ana kuyruktan silinir, alt-kuyrukta forensic icin
        saklanir. Returns True if moved.
        """
        with self._lock:
            c = self._cached_connection()
            row = c.execute(
                "SELECT message_id, correlation_id, headers, payload, enqueued_at, retry_count "
                "FROM outbox WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                return False
            c.execute(
                "INSERT INTO outbox_dead_letter "
                "(message_id, correlation_id, headers, payload, enqueued_at, moved_at, retry_count, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    time.time(),
                    row[5],
                    error[:2000],
                ),
            )
            cur = c.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
            c.commit()
            if cur.rowcount > 0:
                self._pending_estimate = max(0, self._pending_estimate - cur.rowcount)
            return True

    def pending_count(self) -> int:
        """Yaklasik pending mesaj sayisi. Estimate counter ile O(1).

        Tam tarama icin `pending_count_exact()` kullan; ama bu hot-path'te
        500K satirda yavas (~100ms+)."""
        with self._lock:
            return self._pending_estimate

    def pending_count_exact(self) -> int:
        """Gercek COUNT(*) — debug/observability icin. Production hot-path'te
        kullanma; `pending_count()`'in O(1) estimate'i yeterli."""
        with self._lock:
            c = self._cached_connection()
            (n,) = c.execute("SELECT COUNT(*) FROM outbox").fetchone()
            return int(n)

    def dead_letter_count(self) -> int:
        with self._lock:
            c = self._cached_connection()
            (n,) = c.execute("SELECT COUNT(*) FROM outbox_dead_letter").fetchone()
            return int(n)

    def close(self) -> None:
        """Persistent connection'i temizle. Test/teardown icin."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None


class OutboxRetrier:
    """Arka thread: outbox'taki mesajlari periyodik olarak yeniden gondermeye calisir.

    Davranis:
      * `publish_fn` basarili donerse mesaj outbox'tan silinir.
      * `publish_fn` raise ederse retry_count++ ve **exponential backoff**
        ile bekleyip tekrar dener (1s -> 1.5s -> 2.25s -> ... cap 60s, ±%20
        jitter). Boylece broker uzun sure down olunca log/CPU spam olmaz.
      * Bir mesaj `max_retries` kez basarisiz olursa dead-letter tablosuna
        tasinir (poison message korumasi).
      * Basarili gonderim sonrasi backoff sifirlanir (broker geri geldi).
    """

    def __init__(
        self,
        outbox: Outbox,
        publish_fn: Callable[[dict[str, Any]], None],
        *,
        poll_interval_sec: float = 2.0,
        batch_size: int = 100,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_backoff_sec: float = DEFAULT_MIN_BACKOFF_SEC,
        max_backoff_sec: float = DEFAULT_MAX_BACKOFF_SEC,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
    ) -> None:
        self._outbox = outbox
        self._publish_fn = publish_fn
        self._poll_interval = max(0.5, float(poll_interval_sec))
        self._batch_size = max(1, int(batch_size))
        self._max_retries = max(1, int(max_retries))
        self._min_backoff = max(0.1, float(min_backoff_sec))
        self._max_backoff = max(self._min_backoff, float(max_backoff_sec))
        self._backoff_multiplier = max(1.05, float(backoff_multiplier))
        self._current_backoff: float = self._min_backoff
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="outbox-retrier", daemon=True)
        self._thread.start()
        logger.info(
            "outbox_retrier_started db=%s interval_sec=%s batch=%s max_retries=%s "
            "backoff_min=%ss backoff_max=%ss",
            self._outbox.db_path,
            self._poll_interval,
            self._batch_size,
            self._max_retries,
            self._min_backoff,
            self._max_backoff,
        )

    def stop(self, timeout_sec: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout_sec)
        self._thread = None

    def _next_backoff(self) -> float:
        """Sonraki bekleme suresini hesaplar (multiplier + jitter)."""
        b = self._current_backoff
        # Jitter: ±%20, "thundering herd" onlemi (multiple gateway ayni anda
        # broker'a yuklenmesin)
        jitter_factor = 1.0 + random.uniform(-DEFAULT_BACKOFF_JITTER, DEFAULT_BACKOFF_JITTER)
        # Sonraki interval'i artir (cap'la)
        self._current_backoff = min(self._max_backoff, b * self._backoff_multiplier)
        return max(0.1, b * jitter_factor)

    def _reset_backoff(self) -> None:
        self._current_backoff = self._min_backoff

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self._outbox.fetch_batch(self._batch_size)
            except Exception as exc:  # noqa: BLE001
                logger.warning("outbox_fetch_failed error=%s", exc)
                self._stop.wait(self._poll_interval)
                continue
            if not rows:
                # Bos kuyruk = saglikli durum; backoff'i sifirla, normal interval'a don.
                self._reset_backoff()
                self._stop.wait(self._poll_interval)
                continue
            sent = 0
            failed_in_batch = False
            broker_not_ready = False
            for row in rows:
                if self._stop.is_set():
                    break
                try:
                    self._publish_fn(row)
                except Exception as exc:  # noqa: BLE001
                    err_str = str(exc)
                    # "Not ready" TRANSIENT hata: broker baglantisi yok demek;
                    # bu mesajin icerigiyle ilgili degil. retry_count artirma
                    # (aksi takdirde 100dk outage'da 500K mesaj sessizce
                    # dead-letter'a dusurulebilir). Sadece batch'i kir,
                    # backoff'a gec — baglanti gelince bir sonraki cycle'da
                    # ayni mesaj yeniden denenecek.
                    is_not_ready = (
                        type(exc).__name__ == "JetStreamNotReadyError"
                        or "not ready" in err_str.lower()
                    )
                    if is_not_ready:
                        broker_not_ready = True
                        failed_in_batch = True
                        break
                    next_retry = row["retry_count"] + 1
                    if next_retry >= self._max_retries:
                        # Poison message — dead-letter tablosuna tasi, ana
                        # kuyrugu bloke etmesin
                        moved = self._outbox.move_to_dead_letter(row["id"], err_str)
                        if moved:
                            logger.error(
                                "outbox_dead_letter id=%s message_id=%s retries=%s "
                                "last_error=%s — mesaj poison kabul edildi, "
                                "dead-letter tablosuna tasindi",
                                row["id"],
                                row["message_id"],
                                next_retry,
                                err_str[:200],
                            )
                        # Basarisizlik sayilmaz; cunku mesaj artik dead-letter'da
                        # ve sonraki mesajlar publish edilmeli — break etmiyoruz.
                        continue
                    self._outbox.mark_retry(row["id"], err_str)
                    logger.debug(
                        "outbox_retry_failed id=%s retry=%s/%s error=%s",
                        row["id"],
                        next_retry,
                        self._max_retries,
                        exc,
                    )
                    # Broker hala dusukse alttaki mesajlar da fail eder;
                    # batch'i kir, backoff'a gec
                    failed_in_batch = True
                    break
                self._outbox.delete(row["id"])
                sent += 1
            if broker_not_ready:
                # Sadece "outage" durumunu seyrek logla — log spam'i onle
                if self._current_backoff <= self._min_backoff * 1.5:
                    logger.warning(
                        "outbox_retrier_broker_not_ready pending=%d — "
                        "baglanti bekleniyor, retry_count artirilmiyor",
                        len(rows),
                    )
            if sent:
                logger.info("outbox_drained sent=%s remaining_in_batch=%s", sent, len(rows) - sent)
            if failed_in_batch:
                # Broker hala saglik degil — exponential backoff
                wait = self._next_backoff()
                logger.warning(
                    "outbox_backoff sent=%s wait=%.2fs current_cap=%.0fs",
                    sent,
                    wait,
                    self._current_backoff,
                )
                self._stop.wait(wait)
            else:
                # Tum batch basarili — broker saglikli, normal interval
                self._reset_backoff()
                self._stop.wait(self._poll_interval)
