"""Multi-instance lock regresyon testleri.

REGRESYON: Windows'ta lock TAMAMEN ETKISIZDI. `msvcrt.locking()` dosyanin
o anki konumundan itibaren bir byte ARALIGINI kilitler; eski kod dosyayi
`"a+b"` (append -> konum EOF) ile acip ardindan ~10 byte metadata yaziyordu.
Ikinci proses konum 10'da [10,11) araligini kilitliyor, cakisma olmuyor ve
ayni GATEWAY_CODE ile ikinci gateway calisabiliyordu.

Somut sonuc: iki proses ayni outbox/ledger SQLite dosyasina yazar ve
CommandLedger proses-yerel oldugu icin AYNI CROB cihaza IKI KEZ gonderilir.

Bu testler hem ayni-proses hem AYRI-PROSES (gercek senaryo) durumunu kapsar.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dnp3_gateway.auth.instance_lock import acquire_instance_lock


def test_ilk_instance_lock_alir(tmp_path: Path) -> None:
    fh = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    assert fh is not None
    fh.close()


def test_ikinci_instance_ayni_kod_ile_reddedilir(tmp_path: Path) -> None:
    """Ayni proseste bile ikinci lock denemesi reddedilmeli."""
    fh = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
        assert "GW-001" in str(exc.value)
    finally:
        fh.close()


def test_farkli_gateway_code_ayni_dizinde_calisir(tmp_path: Path) -> None:
    a = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    b = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-002")
    try:
        assert a is not None and b is not None
    finally:
        a.close()
        b.close()


def test_lock_dosyasinin_boyutu_degismez(tmp_path: Path) -> None:
    """Kok neden korumasi: metadata lock dosyasina YAZILMAMALI.

    Lock dosyasi buyurse Windows'ta ikinci prosesin baslangic konumu kayar
    ve byte-range cakismasi olusmaz (eski hatanin ta kendisi).
    """
    fh = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    try:
        lock_file = tmp_path / "instance_GW-001.lock"
        assert lock_file.exists()
        assert lock_file.stat().st_size == 0, (
            "lock dosyasina icerik yazilmis — ikinci prosesin lock ofseti kayar"
        )
        # pid metadata'si ayri dosyada olmali
        info = tmp_path / "instance_GW-001.lock.info"
        assert info.exists()
        assert "pid=" in info.read_text(encoding="utf-8")
    finally:
        fh.close()


def test_lock_serbest_birakilinca_yeniden_alinabilir(tmp_path: Path) -> None:
    fh = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    fh.close()
    fh2 = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    fh2.close()


_CHILD = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {src!r})
    from dnp3_gateway.auth.instance_lock import acquire_instance_lock
    try:
        fh = acquire_instance_lock(state_dir={state_dir!r}, gateway_code="GW-001")
    except SystemExit:
        print("REDDEDILDI")
    else:
        print("LOCK_ALINDI")
        fh.close()
    """
)


def test_ayri_proses_reddedilir(tmp_path: Path) -> None:
    """GERCEK SENARYO: NSSM servisi calisirken operator elle ikinci kopya baslatir.

    Bu, eski kodun Windows'ta gecirdigi tam senaryodur.
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    fh = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    try:
        out = subprocess.run(
            [sys.executable, "-c", _CHILD.format(src=src, state_dir=str(tmp_path))],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "REDDEDILDI" in out.stdout, (
            f"ikinci PROSES lock alabildi — multi-instance korumasi etkisiz.\n"
            f"stdout={out.stdout!r} stderr={out.stderr!r}"
        )
    finally:
        fh.close()


def test_ayri_proses_lock_serbest_kalinca_alabilir(tmp_path: Path) -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    fh = acquire_instance_lock(state_dir=str(tmp_path), gateway_code="GW-001")
    fh.close()
    out = subprocess.run(
        [sys.executable, "-c", _CHILD.format(src=src, state_dir=str(tmp_path))],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "LOCK_ALINDI" in out.stdout, f"stdout={out.stdout!r} stderr={out.stderr!r}"
