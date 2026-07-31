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


_MIGRATIONS: list[Migration] = [
    (1, "initial command_ledger schema", _migration_001_initial),
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
