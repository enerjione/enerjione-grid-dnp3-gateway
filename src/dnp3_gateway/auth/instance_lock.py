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

    # 'a+b' modu: dosya yoksa olusturur, varsa append (write izni gerek;
    # lock baska bir proseste ise open() yine de gecer, asil engel locking
    # cagrisinda).
    try:
        fh = open(lock_path, "a+b")
    except OSError as exc:
        raise SystemExit(
            f"Lock dosyasi acilamadi: {lock_path!s} hata={exc}"
        ) from exc

    locked = _try_lock_exclusive(fh)
    if not locked:
        fh.close()
        raise SystemExit(
            f"Ayni GATEWAY_CODE={gateway_code!r} icin baska bir proses zaten "
            f"calisiyor (lock: {lock_path!s}). Multi-instance icin farkli "
            f"GATEWAY_CODE + farkli GATEWAY_STATE_DIR + farkli WORKER_HEALTH_PORT "
            f"kullanin."
        )

    # PID + zaman damgasi yaz (debug icin); lock zaten alindi, icerik
    # kritik degil ama operator dosyaya bakinca hangi proses tuttugunu gorur.
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()}\n".encode("utf-8"))
        fh.flush()
    except OSError:
        # Lock alindi; metadata yazilamasa da sorun degil.
        pass

    return fh


def _try_lock_exclusive(fh: IO[bytes]) -> bool:
    """Platforma gore non-blocking exclusive lock; True = alindi."""
    if sys.platform == "win32":
        import msvcrt

        try:
            # LK_NBLCK = non-blocking exclusive; lock 1 byte yeter,
            # tum dosya icin OS exclusive isaretler.
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
