"""SQLite semasi icin surumleme, butunluk kontrolu ve bozuk-dosya karantinasi.

NEDEN GEREKLI
-------------
Outbox ve CommandLedger semalari `CREATE TABLE IF NOT EXISTS` ile kuruluyordu.
Bunun iki somut sonucu vardi:

1. **Yukseltme = saha genelinde crash-loop.** Yeni bir surum outbox tablosuna
   kolon eklerse (orn. retrier backoff icin `next_attempt_at`), sahadaki
   MEVCUT `.db` dosyasinda eski sema durur. `CREATE TABLE IF NOT EXISTS`
   hicbir sey yapmaz, ilk sorgu `sqlite3.OperationalError: no such column`
   atar. Bu hata korumasiz yollardan ana dongude yakalanmayip prosesi
   oldurur; `restart: unless-stopped` ile TUM SAHA ayni anda crash-loop'a
   girer. Rollback prosedurü de yoktur.

2. **Bozuk dosya = kalici boot arizasi.** Ani elektrik kesintisi +
   `synchronous=NORMAL` ile dosya "database disk image is malformed" verir.
   Yapicida raise edildigi icin gateway her acilista patlar, health server
   hic acilmaz, log'da yapilandirilmamis bir traceback kalir. Operatorun tek
   cikisi dosyayi elle silmektir — ki bu, icindeki tum birikmis telemetriyi
   imha etmek demektir.

COZUM
-----
* `PRAGMA user_version` tabanli sirali migration runner (0 -> 1 -> 2 ...).
  Her migration idempotent bir fonksiyondur ve tek transaction'da uygulanir.
* Acilista `PRAGMA quick_check` — bozuksa dosya `*.corrupt.<ts>` olarak
  KARANTINAYA alinir ve temiz bir DB ile devam edilir. Veri kaybi olur ama
  gateway ayakta kalir ve durum `/health` uzerinden gorunur; sessiz crash-loop
  yerine gorulebilir, sinirli bir kayip.
* Karantina olaylari `quarantined_files` listesinde toplanir; health server
  bunu `outbox_db_quarantined` issue'suna cevirir.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Migration = (hedef_surum, aciklama, uygulayici). Uygulayici acik bir
# transaction icinde cagrilir; hata firlatirsa transaction geri alinir.
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

# Karantinaya alinan dosyalar (health endpoint'i raporlasin diye).
_QUARANTINED: list[str] = []


def quarantined_files() -> list[str]:
    """Bu proses omrunde karantinaya alinmis DB dosyalari."""
    return list(_QUARANTINED)


def _record_quarantine(path: Path) -> None:
    _QUARANTINED.append(str(path))


class SqliteIntegrityError(RuntimeError):
    """DB bozuk ve karantinaya da alinamadi."""


def check_integrity(conn: sqlite3.Connection) -> str | None:
    """`PRAGMA quick_check` — saglikliysa None, degilse hata metni.

    `integrity_check` yerine `quick_check`: buyuk outbox dosyalarinda (500K
    satir) tam kontrol saniyeler surer ve her boot'u geciktirir; quick_check
    sayfa-seviyesi bozulmalari pratikte yakalar.
    """
    try:
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
    except sqlite3.DatabaseError as exc:
        return f"quick_check calistirilamadi: {exc}"
    if not row:
        return "quick_check bos sonuc dondu"
    result = str(row[0]).strip().lower()
    return None if result == "ok" else str(row[0])


def quarantine_corrupt_db(db_path: Path, reason: str) -> Path | None:
    """Bozuk DB'yi `<ad>.corrupt.<unix_ts>` olarak yeniden adlandirir.

    WAL/SHM yan dosyalari da tasinir; aksi halde yeni DB eski WAL'i miras alip
    tekrar bozulur. Yeniden adlandirma basarisiz olursa None doner (caller
    karar verir).
    """
    stamp = int(time.time())
    target = db_path.with_name(f"{db_path.name}.corrupt.{stamp}")
    try:
        if db_path.exists():
            shutil.move(str(db_path), str(target))
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.exists():
                try:
                    shutil.move(str(side), str(target) + suffix)
                except OSError:
                    try:
                        side.unlink()
                    except OSError:
                        pass
    except OSError as exc:
        logger.error(
            "sqlite_quarantine_failed path=%s error=%s — dosya elle temizlenmeli",
            db_path,
            exc,
        )
        return None

    _record_quarantine(target)
    logger.error(
        "sqlite_db_quarantined path=%s -> %s reason=%s — bozuk veritabani "
        "karantinaya alindi, TEMIZ bir DB ile devam ediliyor. Icindeki "
        "birikmis kayitlar KAYIP. Dosya forensic icin saklandi.",
        db_path,
        target,
        reason,
    )
    return target


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: list[Migration],
    *,
    label: str,
) -> int:
    """`PRAGMA user_version` tabanli sirali migration uygulayicisi.

    Yalnizca mevcut surumden BUYUK hedefli migration'lar calisir; her biri
    kendi transaction'inda uygulanir ve basarili olunca user_version yazilir.
    Donen deger: uygulanan migration sayisi.
    """
    (current,) = conn.execute("PRAGMA user_version").fetchone()
    current = int(current or 0)
    target = max((v for v, _d, _f in migrations), default=0)

    if current > target:
        # DB, koddan DAHA YENI bir surumden geliyor (downgrade / rollback).
        # Sessizce devam etmek sema uyumsuzlugu demek; acikca uyar.
        logger.error(
            "sqlite_schema_newer_than_code db=%s db_version=%d code_version=%d — "
            "bu veri dosyasi daha yeni bir gateway surumu tarafindan olusturulmus. "
            "Gateway'i yukseltin veya dosyayi tasiyip temiz baslatin.",
            label,
            current,
            target,
        )
        raise SqliteIntegrityError(
            f"{label}: DB semasi (v{current}) koddan (v{target}) yeni"
        )

    applied = 0
    for version, description, migrate in sorted(migrations, key=lambda m: m[0]):
        if version <= current:
            continue
        try:
            conn.execute("BEGIN")
            migrate(conn)
            # PRAGMA parametrelenmiyor; version int oldugu icin guvenli.
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception(
                "sqlite_migration_failed db=%s version=%d desc=%s",
                label,
                version,
                description,
            )
            raise
        applied += 1
        logger.info(
            "sqlite_migration_applied db=%s version=%d desc=%s",
            label,
            version,
            description,
        )
    return applied


def open_versioned_db(
    db_path: Path,
    *,
    migrations: list[Migration],
    label: str,
    synchronous: str = "NORMAL",
    wal_autocheckpoint: int = 1000,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """Butunlugu dogrulanmis, semasi guncel bir SQLite baglantisi acar.

    Akis:
      1. Baglan, PRAGMA'lari uygula.
      2. `quick_check` — bozuksa dosyayi karantinaya al ve TEMIZ DB ile
         yeniden dene (bir kez). Boylece bozuk dosya kalici boot arizasina
         donusmez.
      3. Migration'lari uygula (`PRAGMA user_version`).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect() -> sqlite3.Connection:
        """Baglan + PRAGMA'lari uygula.

        ONEMLI: PRAGMA'lar bozuk dosyada hata verir. Baglantiyi burada
        kapatmazsak Windows'ta dosya handle'i acik kalir ve karantina icin
        yapilan `shutil.move` "WinError 32: file in use" ile basarisiz olur —
        yani bozuk dosya yine kalici boot arizasina donusur.
        """
        conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=timeout)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA synchronous={synchronous}")
            conn.execute(f"PRAGMA wal_autocheckpoint={int(wal_autocheckpoint)}")
        except BaseException:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            raise
        return conn

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        problem = check_integrity(conn)
    except sqlite3.DatabaseError as exc:
        # Dosya o kadar bozuk ki baglanti/PRAGMA bile kurulamiyor.
        problem = f"baglanti kurulamadi: {exc}"
        conn = None

    if problem is not None:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            conn = None
        if quarantine_corrupt_db(db_path, problem) is None:
            raise SqliteIntegrityError(
                f"{label}: DB bozuk ({problem}) ve karantinaya alinamadi: {db_path}"
            )
        conn = _connect()
        residual = check_integrity(conn)
        if residual is not None:
            conn.close()
            raise SqliteIntegrityError(
                f"{label}: karantina sonrasi temiz DB de bozuk: {residual}"
            )

    # POSIX'te telemetri payload'u + cihaz IP'leri diger kullanicilara kapali.
    if os.name == "posix":
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    apply_migrations(conn, migrations, label=label)
    return conn
