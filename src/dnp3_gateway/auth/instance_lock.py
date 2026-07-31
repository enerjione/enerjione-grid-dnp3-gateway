"""Multi-instance lock — ayni GATEWAY_CODE iki proseste calismasin.

Sorun: Iki proses ayni `GATEWAY_CODE` + `GATEWAY_STATE_DIR` ile basladiginda:
  * instance_id dosyasi paylasilir (loglarda karisir)
  * outbox SQLite dosyasi iki process'ten yazilir → "database is locked"
    veya corrupt + duplicate publish riski
  * Backend `last_seen` iki node arasinda alip durur, config refresh karisir

Cozum: Baslangicta state dizininde gateway koduna ozel `*.lock` dosyasi
ac, exclusive lock dene; alinmazsa SystemExit ile sessizce reddet.

Cross-platform:
  * Windows: `msvcrt.locking(LK_NBLCK)` — non-blocking, IOError = busy
  * POSIX:   `fcntl.flock(LOCK_EX | LOCK_NB)` — OSError = busy

Lock dosyasi acik kaldigi surece tutulur; proses cikinca OS otomatik
serbest birakir (crash dahil — Windows handle cleanup, POSIX fd close).

WINDOWS BYTE-RANGE TUZAGI (2026-07 duzeltmesi)
----------------------------------------------
`msvcrt.locking()` POSIX `flock`'tan farkli olarak TUM DOSYAYI degil,
dosyanin O ANKI KONUMUNDAN itibaren N byte'lik bir ARALIGI kilitler.

Eski kod dosyayi `"a+b"` (append) ile aciyordu; CPython append modunda
dosya konumunu acilista EOF'a alir. Ilk proses bos dosyada [0,1) araligini
kilitliyor, ardindan ~10 byte'lik `pid=NNNN` metadata'sini yaziyordu.
Ikinci proses ayni dosyayi actiginda konum EOF=10 oluyor ve [10,11)
araligini kilitliyordu — FARKLI aralik, dolayisiyla CAKISMA YOK ve lock
BASARILI donuyordu. Yani Windows'ta multi-instance korumasi hic calismiyordu.

Sonuclari (uretim hedefi Windows Server oldugu icin hepsi gercek):
  * Ayni outbox_<CODE>.db ve command_ledger_<CODE>.db'ye iki proses yazar
    -> "database is locked" / WAL bozulmasi
  * Iki gateway ayni /pending komutunu ceker; CommandLedger PROSES-YEREL
    oldugu icin dedup calismaz -> AYNI CROB cihaza IKI KEZ gonderilir
    (kesici acma komutu tekrarlanir)
  * Ayni LOG_FILE_PATH'e iki RotatingFileHandler yazar -> rotation bozulur

Duzeltme iki parcali:
  1. Kilit HER ZAMAN sabit 0 ofsetinden alinir (`seek(0)` + `"r+b"` modu;
     append semantigi tamamen birakildi).
  2. Metadata (pid) AYRI bir `.info` dosyasina yazilir. Boylece lock
     dosyasinin boyutu hic degismez ve kilitli aralik ile yazma islemi
     birbirine hic dokunmaz.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import IO


def acquire_instance_lock(*, state_dir: str, gateway_code: str) -> IO[bytes]:
    """Exclusive lock al; alinamazsa SystemExit.

    Donen file handle programin omru boyunca acik tutulmalidir (caller
    referansi tutmali). Cikinca otomatik release olur.
    """
    base = Path(state_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"GATEWAY_STATE_DIR olusturulamadi: {base!s} hata={exc}"
        ) from exc

    safe_code = re.sub(r"[^\w-]+", "_", gateway_code.strip())[:50] or "gw"
    lock_path = base / f"instance_{safe_code}.lock"

    # "r+b" (O_RDWR|O_CREAT): append semantigi YOK, konum 0'da baslar.
    # Bu kritik — bkz. modul docstring'indeki Windows byte-range tuzagi.
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+b")
    except OSError as exc:
        raise SystemExit(
            f"Lock dosyasi acilamadi: {lock_path!s} hata={exc}"
        ) from exc

    locked = _try_lock_exclusive(fh)
    if not locked:
        holder = _read_lock_holder(lock_path)
        fh.close()
        raise SystemExit(
            f"Ayni GATEWAY_CODE={gateway_code!r} icin baska bir proses zaten "
            f"calisiyor{holder} (lock: {lock_path!s}). Multi-instance icin farkli "
            f"GATEWAY_CODE + farkli GATEWAY_STATE_DIR + farkli WORKER_HEALTH_PORT "
            f"kullanin."
        )

    # Metadata AYRI dosyaya: lock dosyasinin boyutu hic degismemeli, aksi
    # halde ikinci prosesin baslangic konumu kayar (eski hatanin kok nedeni).
    _write_lock_holder(lock_path)
    return fh


def _info_path(lock_path: Path) -> Path:
    return lock_path.with_suffix(lock_path.suffix + ".info")


def _write_lock_holder(lock_path: Path) -> None:
    """Kilidi tutan prosesin pid'ini yan dosyaya yazar (yalniz teshis amacli)."""
    try:
        _info_path(lock_path).write_text(
            f"pid={os.getpid()}\n", encoding="utf-8"
        )
    except OSError:
        # Lock zaten alindi; metadata yazilamasa da calismaya devam.
        pass


def _read_lock_holder(lock_path: Path) -> str:
    """Hata mesajina eklenecek ' (pid=NNNN)' parcasi; okunamazsa bos string."""
    try:
        text = _info_path(lock_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return f" [{text.splitlines()[0]}]"


def _try_lock_exclusive(fh: IO[bytes]) -> bool:
    """Platforma gore non-blocking exclusive lock; True = alindi.

    Windows'ta kilit SABIT 0 ofsetinden alinir. `msvcrt.locking` konum-bagimli
    byte-range kilidi kullandigi icin bu seek ZORUNLUDUR; olmazsa iki proses
    farkli araliklari kilitler ve ikisi de basarili olur.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            # LK_NBLCK = non-blocking exclusive; [0,1) araligi kilitlenir.
            # Bu byte'a ASLA yazilmaz (metadata ayri .info dosyasinda).
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            # flock tum dosyayi kilitler; konumdan bagimsizdir.
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
