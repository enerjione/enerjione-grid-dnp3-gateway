"""SQLite surumleme / butunluk / karantina testleri.

Kapatilan iki uretim riski:

1. **Yukseltme = saha genelinde crash-loop.** Sema `CREATE TABLE IF NOT EXISTS`
   ile kuruluyordu; yeni bir surum kolon eklerse sahadaki MEVCUT .db dosyasi
   eski semada kalir ve ilk sorgu `no such column` atar. Bu hata korumasiz
   yollardan prosesi oldurur ve `restart: unless-stopped` ile tum saha ayni
   anda crash-loop'a girer.

2. **Bozuk dosya = kalici boot arizasi.** Elektrik kesintisi sonrasi
   "database disk image is malformed" -> yapicida raise -> gateway her
   acilista patlar, health server hic acilmaz.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dnp3_gateway.messaging.command_ledger import CommandLedger
from dnp3_gateway.messaging.outbox import Outbox
from dnp3_gateway.messaging.sqlite_support import (
    SqliteIntegrityError,
    apply_migrations,
    check_integrity,
    open_versioned_db,
    quarantined_files,
)


def _v(conn: sqlite3.Connection) -> int:
    (n,) = conn.execute("PRAGMA user_version").fetchone()
    return int(n)


# --------------------------------------------------------------------------
# migration runner
# --------------------------------------------------------------------------


def test_migrationlar_sirayla_uygulanir(tmp_path: Path) -> None:
    calls: list[int] = []

    def m1(c: sqlite3.Connection) -> None:
        calls.append(1)
        c.execute("CREATE TABLE t (a INTEGER)")

    def m2(c: sqlite3.Connection) -> None:
        calls.append(2)
        c.execute("ALTER TABLE t ADD COLUMN b INTEGER DEFAULT 0")

    migrations = [(1, "ilk", m1), (2, "ikinci", m2)]
    conn = open_versioned_db(tmp_path / "x.db", migrations=migrations, label="t")
    assert calls == [1, 2]
    assert _v(conn) == 2
    conn.close()


def test_migrationlar_idempotent_ikinci_acilista_calismaz(tmp_path: Path) -> None:
    calls: list[int] = []

    def m1(c: sqlite3.Connection) -> None:
        calls.append(1)
        c.execute("CREATE TABLE t (a INTEGER)")

    migrations = [(1, "ilk", m1)]
    db = tmp_path / "x.db"
    open_versioned_db(db, migrations=migrations, label="t").close()
    open_versioned_db(db, migrations=migrations, label="t").close()
    assert calls == [1], "migration ikinci acilista tekrar calisti"


def test_eski_semadan_yukseltme(tmp_path: Path) -> None:
    """GERCEK SENARYO: sahadaki v1 dosyasi, yeni surum v2 kolonunu ekler."""

    def m1(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE t (a INTEGER)")

    def m2(c: sqlite3.Connection) -> None:
        c.execute("ALTER TABLE t ADD COLUMN b INTEGER NOT NULL DEFAULT 0")

    db = tmp_path / "x.db"
    # Saha: v1 ile calisiyor, veri var
    conn = open_versioned_db(db, migrations=[(1, "ilk", m1)], label="t")
    conn.execute("INSERT INTO t (a) VALUES (42)")
    conn.commit()
    conn.close()

    # Yeni surum yuklendi: v2 migration'i var
    conn = open_versioned_db(db, migrations=[(1, "ilk", m1), (2, "ikinci", m2)], label="t")
    assert _v(conn) == 2
    # Mevcut veri KORUNDU
    assert conn.execute("SELECT a, b FROM t").fetchone() == (42, 0)
    conn.close()


def test_migration_hatasi_geri_alinir(tmp_path: Path) -> None:
    def m1(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE t (a INTEGER)")

    def m2_bozuk(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE u (x INTEGER)")
        raise RuntimeError("migration patladi")

    db = tmp_path / "x.db"
    conn = open_versioned_db(db, migrations=[(1, "ilk", m1)], label="t")
    conn.close()

    with pytest.raises(RuntimeError):
        open_versioned_db(db, migrations=[(1, "ilk", m1), (2, "bozuk", m2_bozuk)], label="t")

    # Surum ilerlememeli ve yarim tablo kalmamali
    conn = sqlite3.connect(str(db))
    assert _v(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "u" not in tables
    conn.close()


def test_koddan_yeni_sema_reddedilir(tmp_path: Path) -> None:
    """Downgrade koruma: DB koddan yeniyse sessizce devam etmek yerine hata ver."""

    def m1(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE t (a INTEGER)")

    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db))
    with pytest.raises(SqliteIntegrityError):
        apply_migrations(conn, [(1, "ilk", m1)], label="t")
    conn.close()


# --------------------------------------------------------------------------
# butunluk + karantina
# --------------------------------------------------------------------------


def test_saglikli_db_quick_check_gecer(tmp_path: Path) -> None:
    conn = open_versioned_db(tmp_path / "x.db", migrations=[], label="t")
    assert check_integrity(conn) is None
    conn.close()


def test_bozuk_db_karantinaya_alinir_ve_calismaya_devam_eder(tmp_path: Path) -> None:
    """REGRESYON: bozuk dosya eskiden yapicida raise edip kalici boot arizasi yapardi."""

    def m1(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE t (a INTEGER)")

    db = tmp_path / "x.db"
    conn = open_versioned_db(db, migrations=[(1, "ilk", m1)], label="t")
    conn.execute("INSERT INTO t (a) VALUES (1)")
    conn.commit()
    conn.close()

    # Dosyayi bozalim: gecerli SQLite basligi ama govdesi cop
    raw = bytearray(db.read_bytes())
    for i in range(100, min(len(raw), 4096)):
        raw[i] = 0xFF
    db.write_bytes(bytes(raw))

    before = len(quarantined_files())
    # Patlamamali: bozuk dosya karantinaya alinip TEMIZ DB ile devam etmeli
    conn = open_versioned_db(db, migrations=[(1, "ilk", m1)], label="t")
    assert check_integrity(conn) is None
    assert _v(conn) == 1
    conn.close()

    assert len(quarantined_files()) == before + 1
    assert list(tmp_path.glob("x.db.corrupt.*")), "bozuk dosya forensic icin saklanmali"


def test_sqlite_olmayan_dosya_da_karantinaya_alinir(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    db.write_bytes(b"bu bir sqlite dosyasi degil, tamamen cop icerik" * 20)
    conn = open_versioned_db(db, migrations=[], label="t")
    assert check_integrity(conn) is None
    conn.close()
    assert list(tmp_path.glob("x.db.corrupt.*"))


# --------------------------------------------------------------------------
# gercek siniflar
# --------------------------------------------------------------------------


def test_outbox_semasi_surumlenir(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "outbox.db")
    conn = sqlite3.connect(str(tmp_path / "outbox.db"))
    assert _v(conn) == 2, "outbox v2 (next_attempt_at) olmali"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert "next_attempt_at" in cols
    conn.close()
    ob.close()


def test_outbox_v1_dosyasi_v2ye_yukseltilir(tmp_path: Path) -> None:
    """Sahadaki 0.4.x outbox dosyasi kolon eklenerek yukseltilebilmeli."""
    db = tmp_path / "outbox.db"
    # 0.4.x semasini elle kur (v1, next_attempt_at YOK)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            correlation_id TEXT,
            headers TEXT,
            payload TEXT NOT NULL,
            enqueued_at REAL NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE TABLE outbox_dead_letter (
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
        """
    )
    conn.execute(
        "INSERT INTO outbox (message_id, payload, enqueued_at) VALUES ('m1', '{}', 1.0)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    ob = Outbox(db)
    try:
        # Birikmis mesaj KORUNDU (silinmedi/karantinaya alinmadi)
        assert ob.pending_count() == 1
        rows = ob.fetch_batch(10)
        assert rows[0]["message_id"] == "m1"
    finally:
        ob.close()

    conn = sqlite3.connect(str(db))
    assert _v(conn) == 2
    assert "next_attempt_at" in {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    conn.close()


def test_command_ledger_semasi_surumlenir(tmp_path: Path) -> None:
    led = CommandLedger(tmp_path / "ledger.db")
    led.close()
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    assert _v(conn) == 1
    conn.close()


def test_bozuk_outbox_gateway_i_bloke_etmez(tmp_path: Path) -> None:
    """En kritigi: bozuk outbox artik gateway'i acilista oldurmemeli."""
    db = tmp_path / "outbox.db"
    ob = Outbox(db)
    ob.enqueue(message_id="m1", correlation_id=None, headers=None, payload={"v": 1})
    ob.close()

    raw = bytearray(db.read_bytes())
    for i in range(100, min(len(raw), 4096)):
        raw[i] = 0xFF
    db.write_bytes(bytes(raw))

    ob2 = Outbox(db)  # patlamamali
    try:
        assert ob2.pending_count() == 0  # veri kayip ama gateway ayakta
        ob2.enqueue(message_id="m2", correlation_id=None, headers=None, payload={"v": 2})
        assert ob2.pending_count() == 1
    finally:
        ob2.close()
