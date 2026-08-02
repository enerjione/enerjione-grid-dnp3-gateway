"""DNP3 fiziksel komutlar için kalıcı, tekrar-göndermez command journal."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from dnp3_gateway.messaging.sqlite_support import Migration, open_versioned_db


def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """v1 — baslangic semasi (0.4.x ile ayni; mevcut dosyalar uyumlu)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS command_ledger (
            command_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            received_at REAL NOT NULL,
            dispatch_started_at REAL,
            completed_at REAL,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_command_ledger_delivery
            ON command_ledger (delivery_state, completed_at);
        """
    )


def _migration_002_delivery_failures(conn: sqlite3.Connection) -> None:
    """v2 — sonuc teslimindeki KALICI hata sayaci.

    Eskiden teslim hatasi tipsizdi: kalici bir 4xx (token rotasyonu, sema
    degisikligi, silinmis command_id) kuyrugu SONSUZA KADAR bloke ediyordu.
    Bu sayac sayesinde kalici hata birkac denemeden sonra dead-letter'a
    tasinir ve kuyruk ilerler.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(command_ledger)").fetchall()}
    if "delivery_failures" not in cols:
        conn.execute("ALTER TABLE command_ledger ADD COLUMN delivery_failures INTEGER NOT NULL DEFAULT 0")


_MIGRATIONS: list[Migration] = [
    (1, "initial command_ledger schema", _migration_001_initial),
    (2, "command_ledger.delivery_failures (kalici teslim hatasi sayaci)", _migration_002_delivery_failures),
]


class CommandLedger:
    """Command intent/result kaydını fsync ile saklar.

    ``start_dispatch`` başarılı dönmeden DNP3'e CROB gönderilmez. Aynı ID
    restart sonrası yeniden gelirse ``False`` döner; fiziksel komut tekrar
    edilmez. `dispatching` kayıtları açılışta ``unknown`` sonuca çevrilir.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # synchronous=FULL: bu dosya FIZIKSEL komutlarin tek yerel kaydidir.
        # Bir CROB'un gonderilip gonderilmedigi bilgisi elektrik kesintisinde
        # bile kaybolmamali (idempotency + denetim izi).
        # open_versioned_db: quick_check ile bozuk dosya karantinaya alinir,
        # user_version ile sema surumlenir, POSIX'te chmod 600 uygulanir.
        self._conn = open_versioned_db(
            self.db_path,
            migrations=_MIGRATIONS,
            label=f"command_ledger[{self.db_path.name}]",
            synchronous="FULL",
            wal_autocheckpoint=1000,
        )

    def start_dispatch(self, command_id: int) -> bool:
        """Dispatch intent'i kalıcı yazılırsa True döner."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO command_ledger "
                "(command_id, state, received_at, dispatch_started_at) VALUES (?, 'dispatching', ?, ?)",
                (int(command_id), time.time(), time.time()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def record_result(self, result: dict[str, Any]) -> None:
        """Terminal sonucu kalıcı kaydeder; result delivery ayrıca yürür."""
        command_id = int(result["id"])
        with self._lock:
            self._conn.execute(
                "UPDATE command_ledger SET state = 'completed', completed_at = ?, result_json = ?, "
                "delivery_state = 'pending', delivery_error = NULL WHERE command_id = ?",
                (time.time(), json.dumps(result, ensure_ascii=False), command_id),
            )
            self._conn.commit()

    def recover_unknown_results(self) -> list[dict[str, Any]]:
        """Önceki process'in sonucunu yazamadığı dispatch'leri unknown yapar."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT command_id FROM command_ledger WHERE state = 'dispatching' ORDER BY command_id"
            ).fetchall()
            now = time.time()
            results = [
                {
                    "id": int(row[0]),
                    "ok": False,
                    "status": "unknown",
                    "error": "gateway restarted while DNP3 command outcome was unknown; command was not replayed",
                }
                for row in rows
            ]
            for result in results:
                self._conn.execute(
                    "UPDATE command_ledger SET state = 'completed', completed_at = ?, result_json = ?, "
                    "delivery_state = 'pending', delivery_error = NULL WHERE command_id = ?",
                    (now, json.dumps(result, ensure_ascii=False), result["id"]),
                )
            self._conn.commit()
            return results

    def pending_results(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result_json FROM command_ledger "
                "WHERE state = 'completed' AND delivery_state = 'pending' ORDER BY completed_at"
            ).fetchall()
        return [json.loads(row[0]) for row in rows if row[0]]

    def mark_delivered(self, command_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE command_ledger SET delivery_state = 'delivered', delivery_error = NULL WHERE command_id = ?",
                (int(command_id),),
            )
            self._conn.commit()

    def bump_delivery_failure(self, command_id: int) -> int:
        """KALICI teslim hatasi sayacini artirir ve yeni degeri doner."""
        with self._lock:
            self._conn.execute(
                "UPDATE command_ledger SET delivery_failures = delivery_failures + 1 WHERE command_id = ?",
                (int(command_id),),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT delivery_failures FROM command_ledger WHERE command_id = ?",
                (int(command_id),),
            ).fetchone()
        return int(row[0]) if row else 0

    def delivery_dead_letter_count(self) -> int:
        """Teslim edilemeyip dead-letter'a alinan sonuc sayisi (/health icin)."""
        with self._lock:
            (n,) = self._conn.execute(
                "SELECT COUNT(*) FROM command_ledger WHERE delivery_state = 'dead_letter'"
            ).fetchone()
        return int(n)

    def mark_delivery_dead_letter(self, command_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE command_ledger SET delivery_state = 'dead_letter', delivery_error = ? WHERE command_id = ?",
                (error[:2000], int(command_id)),
            )
            self._conn.commit()

    def known_command_ids(self) -> set[int]:
        with self._lock:
            rows = self._conn.execute("SELECT command_id FROM command_ledger").fetchall()
        return {int(row[0]) for row in rows}

    def pending_result_count(self) -> int:
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM command_ledger WHERE state = 'completed' AND delivery_state = 'pending'"
            ).fetchone()
        return int(count)

    def dead_letter_count(self) -> int:
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM command_ledger WHERE delivery_state = 'dead_letter'"
            ).fetchone()
        return int(count)

    def prune(self, *, retain_days: float = 90.0) -> int:
        """Teslim edilmis eski komut kayitlarini siler; silinen sayiyi doner.

        Tablo hic budanmiyordu. Bir backend otomasyonu saniyede 1 komut
        uretirse yilda ~31M satir birikir; saha PC'sinde bu yuzlerce MB kalici
        disk demektir ve `pending_results` sorgusunu de yavaslatir.

        YALNIZCA `delivery_state='delivered'` satirlar silinir — teslim
        edilmemis sonuclar ve dead_letter kayitlari (denetim izi) KORUNUR.
        """
        # Alt sinir burada DEGIL cagri yerinde (config `ge=1`) uygulanir;
        # fonksiyonun kendisi test edilebilir kalsin.
        cutoff = time.time() - max(0.0, float(retain_days)) * 86400.0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM command_ledger "
                "WHERE delivery_state = 'delivered' AND completed_at IS NOT NULL "
                "AND completed_at < ?",
                (cutoff,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
