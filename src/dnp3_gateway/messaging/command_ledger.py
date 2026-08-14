"""DNP3 fiziksel komutlar için kalıcı, tekrar-göndermez command journal."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dnp3_gateway.messaging.sqlite_support import Migration, open_versioned_db

logger = logging.getLogger(__name__)


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


def _migration_003_delivery_ack(conn: sqlite3.Connection) -> None:
    """v3 — F3C: teslim jetonu, DAYANIKLI teslim-ACK'i ve defter kimligi.

    KAPATILAN ARIZA
    Backend `/pending` yanitini uretirken komutu `sent` isaretliyordu. Yanit ag
    uzerinde kaybolursa (A) ya da gateway onu okuyup `start_dispatch`ten ONCE
    olurse (B), komut backend'de sonsuza kadar `sent` kalir, cihaza HIC gitmez
    ve kayip SESSIZ olurdu. Artik teslim, gateway kalici olarak kabul ettigini
    BILDIRINCE (ACK) tamamlanmis sayilir.

    `ack_state`:
      none      teslim protokolu disi (eski backend; ACK beklenmiyor)
      pending   deftere YAZILDI, backend'e henuz bildirilmedi
      acked     backend bildirimi onayladi

    ACK NEDEN DEFTERDE (RAM'de degil)
    ACK ancak `start_dispatch` COMMIT'inden SONRA uretilebilir; bellekte
    tutulsaydi SIGKILL ile kaybolur ve backend komutu hicbir zaman `sent`
    goremezdi — yani F3C'nin kapattigi pencere ACK tarafindan yeniden acilirdi.

    `ledger_meta.epoch` — DEFTER KIMLIGI
    Bu defter silinir/sifirlanirsa tekrar-onleme kaniti kaybolur: backend daha
    once teslim ettigi bir komutu yeniden sunarsa gateway onu YENI sanip CROB'u
    TEKRARLAR. Epoch bunu gorunur kilar — defter yeniden yaratildiginda deger
    degisir, backend kiraladigi epoch ile eslesmedigini gorup komutu otomatik
    teslim ETMEZ.

    Zaman damgasi bu isi yapamazdi: bellekte tutuluyor, restart'ta kayboluyor
    ve saat geri alinirsa iki farkli defter ayni damgayi tasiyabiliyordu.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(command_ledger)").fetchall()}
    if "delivery_token" not in cols:
        conn.execute("ALTER TABLE command_ledger ADD COLUMN delivery_token TEXT")
    if "ack_state" not in cols:
        # Mevcut satirlar 'none': eski backend bu komutlar icin ACK BEKLEMIYOR.
        # 'pending' vermek, yukseltme aninda gecmisteki TUM komutlar icin
        # backend'e anlamsiz ACK yagdirirdi.
        conn.execute("ALTER TABLE command_ledger ADD COLUMN ack_state TEXT NOT NULL DEFAULT 'none'")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_command_ledger_ack ON command_ledger (ack_state)")
    conn.execute("CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


_MIGRATIONS: list[Migration] = [
    (1, "initial command_ledger schema", _migration_001_initial),
    (2, "command_ledger.delivery_failures (kalici teslim hatasi sayaci)", _migration_002_delivery_failures),
    (3, "command_ledger delivery_token/ack_state + ledger_meta.epoch (F3C)", _migration_003_delivery_ack),
]

#: `ledger_meta` icindeki defter kimligi anahtari.
_EPOCH_KEY = "epoch"


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
        # Bozuk dosya karantinaya alindiysa: defter SIFIRLANDI.
        #
        # Bu, outbox'in sifirlanmasiyla AYNI SEY DEGILDIR. Outbox'ta kaybedilen
        # birikmis telemetridir (gorunur, telafi edilebilir). Burada kaybedilen
        # FIZIKSEL KOMUT gecmisidir; iki somut sonucu var:
        #   1. `recover_unknown_results` bos doner — onceki proses yarim birakmis
        #      bir CROB'un sonucunu backend'e ASLA bildiremez, backend o komutu
        #      sonsuza kadar "bekliyor" gorur.
        #   2. Tekrar-onleme garantisi kalkar — backend bekleyen bir komutu
        #      yeniden gonderirse gateway onu YENI sanip CROB'u tekrarlar.
        # Ikisi de sessiz olurdu; bu yuzden ayrica raporlaniyor.
        self.journal_reset_at: float | None = None
        self.journal_reset_path: str | None = None

        def _karantina(yol: Path, sebep: str) -> None:
            self.journal_reset_at = time.time()
            self.journal_reset_path = str(yol)

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
            on_quarantine=_karantina,
        )
        # Defter kimligi migration'lardan SONRA yuklenir (tablo o zaman var).
        self._epoch = self._epoch_yukle()

        if self.journal_reset_at is not None:
            logger.error(
                "command_ledger_reset path=%s karantina=%s — KOMUT DEFTERI BOZUKTU ve "
                "sifirlandi. Bu prosesten ONCE gonderilmis komutlarin sonuclari "
                "backend'e bildirilemez ve o komutlar icin TEKRAR-ONLEME GARANTISI "
                "YOKTUR (backend bekleyen bir komutu yeniden gonderirse CROB "
                "tekrarlanabilir). Karantina dosyasini inceleyin ve bekleyen "
                "komutlari operator ile dogrulayin.",
                self.db_path,
                self.journal_reset_path,
            )

    def status_snapshot(self) -> dict[str, Any]:
        """Health/observability icin ozet. Hata durumunda bile govde doner."""
        snapshot: dict[str, Any] = {
            "journal_reset": self.journal_reset_at is not None,
            "journal_reset_at": self.journal_reset_at,
            "journal_reset_path": self.journal_reset_path,
            # Defter kimligi: bu degerin DEGISMESI, tekrar-onleme kanitinin
            # kaybolmasi demektir. Operatorun gorebilmesi gerekir.
            "ledger_epoch": self._epoch,
        }
        try:
            with self._lock:
                (pending,) = self._conn.execute(
                    "SELECT COUNT(*) FROM command_ledger WHERE delivery_state = 'pending'"
                ).fetchone()
                (dead,) = self._conn.execute(
                    "SELECT COUNT(*) FROM command_ledger WHERE delivery_state = 'dead_letter'"
                ).fetchone()
            snapshot["result_pending"] = int(pending)
            snapshot["result_dead_letter"] = int(dead)
        except sqlite3.Error:
            logger.debug("command_ledger_status_failed", exc_info=True)
        return snapshot

    def _epoch_yukle(self) -> str:
        """Defter kimligini okur; yoksa URETIR ve KALICI yazar.

        Ayni dosya yasadigi surece (proses restart, container restart,
        container recreate + ayni named volume) DEGISMEZ. Dosya silinir ya da
        karantinaya alinirsa yeni bir defterle birlikte YENI kimlik dogar —
        tam olarak backend'in ogrenmesi gereken sey budur.

        `INSERT OR IGNORE` + geri okuma: iki surec ayni anda acsa bile tek bir
        deger kazanir ve ikisi de ayni degeri okur.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ledger_meta (key, value) VALUES (?, ?)",
                (_EPOCH_KEY, str(uuid.uuid4())),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT value FROM ledger_meta WHERE key = ?", (_EPOCH_KEY,)).fetchone()
        return str(row[0])

    @property
    def epoch(self) -> str:
        """Bu defterin DAYANIKLI kimligi (UUID)."""
        return self._epoch

    def start_dispatch(self, command_id: int, delivery_token: str | None = None) -> bool:
        """Dispatch intent'i kalıcı yazılırsa True döner.

        `delivery_token` verilirse (F3C teslim protokolu) kayit ACK BEKLIYOR
        durumunda acilir. ACK ancak bu COMMIT tamamlandiktan SONRA uretilebilir;
        sozlesmenin can alici noktasi budur.

        JETON OPAKTIR: cozulmez, normalize edilmez, loglanmaz. Yalnizca komutla
        iliskilendirilip backend'e aynen geri verilir.
        """
        jeton = None if delivery_token is None else str(delivery_token)
        ack = "pending" if jeton else "none"
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO command_ledger "
                "(command_id, state, received_at, dispatch_started_at, delivery_token, ack_state) "
                "VALUES (?, 'dispatching', ?, ?, ?, ?)",
                (int(command_id), time.time(), time.time(), jeton, ack),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def kayitli_jeton(self, command_id: int) -> tuple[bool, str | None]:
        """(kayit_var_mi, kayitli_jeton) doner.

        Cagiran taraf mukerrer teslimi bununla ayirt eder: kayit VARSA fiziksel
        komut TEKRARLANMAZ, yalnizca ACK/sonuc kurtarmasi yapilir.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT delivery_token FROM command_ledger WHERE command_id = ?",
                (int(command_id),),
            ).fetchone()
        if row is None:
            return (False, None)
        return (True, None if row[0] is None else str(row[0]))

    def ack_yeniden_kuyrukla(self, command_id: int) -> bool:
        """Mukerrer teslimde ACK'i yeniden kuyruga alir. True = kuyruklandi.

        NEDEN GEREKLI: backend komutu yeniden sundugu icin ACK'in ona ULASMADIGI
        anlasilir. Kayit zaten defterde oldugundan `start_dispatch` False doner
        ve eski kodda komut sessizce ATLANIRDI — yani backend hicbir zaman
        `sent` goremez ve komut kira/deneme tukenene kadar yeniden sunulurdu.

        `acked` durumunu geri almaz: backend zaten onaylamissa yeniden bildirmek
        gereksiz trafik olurdu. Yalnizca jetonu OLAN kayitlar icin anlamlidir.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE command_ledger SET ack_state = 'pending' "
                "WHERE command_id = ? AND delivery_token IS NOT NULL AND ack_state != 'pending'",
                (int(command_id),),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) == 1

    def pending_acks(self) -> list[dict[str, Any]]:
        """Backend'e bildirilmeyi bekleyen teslim onaylari (FIFO)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT command_id, delivery_token FROM command_ledger "
                "WHERE ack_state = 'pending' AND delivery_token IS NOT NULL "
                "ORDER BY received_at, command_id"
            ).fetchall()
        return [{"command_id": int(r[0]), "delivery_token": str(r[1])} for r in rows]

    def mark_ack_delivered(self, command_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE command_ledger SET ack_state = 'acked' WHERE command_id = ?",
                (int(command_id),),
            )
            self._conn.commit()

    def pending_ack_count(self) -> int:
        with self._lock:
            (n,) = self._conn.execute(
                "SELECT COUNT(*) FROM command_ledger WHERE ack_state = 'pending'"
            ).fetchone()
        return int(n)

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
