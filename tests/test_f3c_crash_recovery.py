"""F3C — GERCEK PROSES cokme testleri (mock degil).

NEDEN GERCEK PROSES
-------------------
F3C'nin tek iddiasi su: ayni backend `command_id` icin fiziksel DNP3 komutu
EN FAZLA BIR KEZ uretilir; proses cokmesi, restart, kira yeniden sunumu ve
mukerrer HTTP teslimi bunu DEGISTIRMEZ.

Bu iddia bellek-ici bir sahte defterle KANITLANAMAZ: onemli olan tam olarak
SQLite dosyasinin diskte ne zaman kalici hale geldigidir. `synchronous=FULL`
fsync'i, WAL'in kurtarilmasi ve `INSERT OR IGNORE`un restart sonrasi davranisi
ancak gercek bir dosya + gercek bir proses oldurulerek gozlemlenebilir.

Bu yuzden burada:
  * cocuk proses `sys.executable` ile GERCEKTEN calistirilir
  * `Popen.kill()` ile OLDURULUR (temiz kapanma YOK)
  * defter dosyasi disk uzerinde kalir ve ikinci proses onu acar
  * fiziksel komut sayaci AYRI bir dosyaya yazilir; sayim proses olumunden
    etkilenmez

WINDOWS
-------
`signal.SIGKILL` Windows'ta YOKTUR ve CI matrisinde windows-latest var.
`Popen.kill()` her iki platformda da zorla sonlandirir (POSIX'te SIGKILL,
Windows'ta TerminateProcess) — bu yuzden bilincli olarak o kullaniliyor.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")

#: Cocugun "hazirim / su noktaya geldim" demesi icin azami bekleme.
_ISARET_TIMEOUT = 60.0


def _bekle(yol: Path, timeout: float = _ISARET_TIMEOUT) -> bool:
    """Isaret dosyasi olusana kadar bekler. Sabit `sleep` YOK — kosula bakar."""
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        if yol.exists():
            return True
        time.sleep(0.01)
    return False


def _calistir(kod: str, **bicim: object) -> subprocess.CompletedProcess:
    """Cocugu calistirip BITMESINI bekler."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(kod).format(src=repr(SRC), **bicim)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _oldur(kod: str, isaret_yolu: Path, **bicim: object) -> subprocess.Popen:
    """Cocugu baslatir, `isaret` dosyasini gorunce ZORLA oldurur.

    Isaret, cocugun tam olarak istenen noktaya geldigini soyler; zamanlama
    varsayimi YOK.

    `isaret_yolu` bilincli olarak `bicim` sozlugundeki `isaret` anahtarindan
    AYRI adlandirildi: ikisi ayni ad olsaydi `**bicim` ile cakisirdi.
    """
    p = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(kod).format(src=repr(SRC), **bicim)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _bekle(isaret_yolu), (
            f"cocuk isaret dosyasini yazmadi: {isaret_yolu}\n"
            f"stdout={p.stdout.read() if p.stdout else ''}\n"
            f"stderr={p.stderr.read() if p.stderr else ''}"
        )
        p.kill()  # POSIX: SIGKILL, Windows: TerminateProcess
        p.wait(timeout=30)
    finally:
        if p.poll() is None:  # pragma: no cover
            p.kill()
    return p


def _sayac(yol: Path) -> int:
    """Fiziksel komut sayisi = sayac dosyasindaki satir sayisi."""
    if not yol.exists():
        return 0
    return len([s for s in yol.read_text(encoding="utf-8").splitlines() if s.strip()])


# ---------------------------------------------------------------------------
# Cocuk proses gövdeleri
# ---------------------------------------------------------------------------
#
# ORTAK KURULUM: gercek `CommandLedger`, gercek `_execute_pending_commands`,
# gercek `GatewayState`. YALNIZCA DNP3 katmani sahte — fiziksel komutu
# saymak icin. Yani test, uretim kod yolunun kendisini surer.

_ORTAK = '''
    import sys, json
    sys.path.insert(0, {src})
    from pathlib import Path
    from datetime import datetime, timedelta, timezone

    from dnp3_gateway.backend.config_client import PendingCommand
    from dnp3_gateway.messaging.command_ledger import CommandLedger
    from dnp3_gateway.state import GatewayState
    from dnp3_gateway.backend import DeviceConfig, GatewayConfig, SignalConfig
    from dnp3_gateway.main import _execute_pending_commands

    DEFTER = Path({defter})
    SAYAC = Path({sayac})
    ISARET = Path({isaret})

    class Sayan:
        """CROB yerine sayac dosyasina satir yazar (proses olse de kalir)."""
        def operate_device(self, **kw):
            with SAYAC.open("a", encoding="utf-8") as f:
                f.write(json.dumps({{"id": kw.get("index")}}) + "\\n")
                f.flush()
                import os
                os.fsync(f.fileno())
            return {{"ok": True, "status": "ok"}}

    def durum():
        st = GatewayState()
        st.update(GatewayConfig(
            gateway_code="GW-001", gateway_name="T", batch_interval_sec=5,
            max_devices=10, is_active=True, config_version="v1",
            devices=[DeviceConfig(code="DEV-1", name="DEV-1", ip_address="10.0.0.5",
                                  dnp3_address=1, poll_interval_sec=5)],
            signals=[SignalConfig(key="master.fault_reset", label="r", unit=None,
                                  source="master", dnp3_class="Class 1",
                                  data_type="binary_output", dnp3_object_group=10,
                                  dnp3_index=3, scale=1.0, offset=0.0,
                                  supports_alarm=False)],
        ))
        return st

    def komut(cid=1, token="JETON-1"):
        return PendingCommand(
            id=cid, device_code="DEV-1", command="fault_reset", dnp3_index=3,
            created_at=datetime.now(timezone.utc).isoformat(),
            delivery_token=token,
        )
'''


@pytest.fixture()
def ortam(tmp_path: Path):
    return {
        "defter": repr(str(tmp_path / "command_ledger_GW-001.db")),
        "sayac": repr(str(tmp_path / "execute_count.log")),
        "isaret": repr(str(tmp_path / "isaret")),
        "_sayac_yolu": tmp_path / "execute_count.log",
        "_isaret_yolu": tmp_path / "isaret",
        "_defter_yolu": tmp_path / "command_ledger_GW-001.db",
    }


# ===========================================================================
# C01 — defter yazilmadan cokme
# ===========================================================================


def test_c01_defter_yazilmadan_cokme_execute_0(ortam) -> None:
    """Defter yazimindan ONCE olen proses fiziksel komut URETMEMIS olmali.

    Bu, B penceresinin ta kendisi: gateway yaniti bellege aldi ama dayanikli
    deftere yazmadan oldu.
    """
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    # Defter yazimindan ONCE isaret ver ve bekle -> tam bu noktada oldurulecek.
    ISARET.write_text("hazir", encoding="utf-8")
    import time
    while True:
        time.sleep(0.05)
    """
    )
    _oldur(kod, ortam["_isaret_yolu"], **{k: ortam[k] for k in ("defter", "sayac", "isaret")})

    assert _sayac(ortam["_sayac_yolu"]) == 0, "defter yazilmadan CROB gonderilmis"

    # Restart: defterde kayit YOK -> komut YENI sayilir -> TAM 1 kez calisir.
    kod2 = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    kayitli, _ = led.kayitli_jeton(cmd.id)
    assert not kayitli, "defterde beklenmedik kayit"
    assert led.start_dispatch(cmd.id, cmd.delivery_token)
    _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                              max_age_sec=120.0)
    led.close()
    print("TAMAM")
    """
    )
    ort = _calistir(kod2, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert "TAMAM" in ort.stdout, f"stderr={ort.stderr}"
    assert _sayac(ortam["_sayac_yolu"]) == 1


# ===========================================================================
# C02 — defter COMMIT edildi, execute ONCESI cokme
# ===========================================================================


def test_c02_defter_sonrasi_cokme_ikinci_execute_yok(ortam) -> None:
    """SPEC §24'un EN KRITIK senaryosu.

    defter kalici
      -> SIGKILL
      -> restart
      -> AYNI command_id yeniden sunuluyor
      -> ikinci CROB = 0
    """
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    assert led.start_dispatch(cmd.id, cmd.delivery_token) is True   # KALICI
    ISARET.write_text("defter-yazildi", encoding="utf-8")
    import time
    while True:
        time.sleep(0.05)
    """
    )
    _oldur(kod, ortam["_isaret_yolu"], **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert _sayac(ortam["_sayac_yolu"]) == 0

    # Restart + backend komutu YENIDEN sunuyor (kira suresi doldu).
    kod2 = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    kayitli, jeton = led.kayitli_jeton(cmd.id)
    assert kayitli is True, "dayanikli kayit KAYBOLDU"
    # Uretim yolu: kayit varsa CALISTIRMA YOK, yalnizca ACK kurtarmasi.
    if not led.start_dispatch(cmd.id, cmd.delivery_token):
        led.ack_yeniden_kuyrukla(cmd.id)
    else:
        _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                                  max_age_sec=120.0)
    print("ACK_BEKLEYEN=%d" % led.pending_ack_count())
    led.close()
    print("TAMAM")
    """
    )
    ort = _calistir(kod2, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert "TAMAM" in ort.stdout, f"stderr={ort.stderr}"
    assert _sayac(ortam["_sayac_yolu"]) == 0, (
        "DAYANIKLI KAYIT VARKEN IKINCI KEZ CROB GONDERILDI — F3C'nin en temel guvenlik degismezi kirildi"
    )
    assert "ACK_BEKLEYEN=1" in ort.stdout, "ACK yeniden kuyruklanmadi"


# ===========================================================================
# C03 — execute yapildi, sonuc yazilmadan cokme
# ===========================================================================


def test_c03_execute_sonrasi_cokme_toplam_1_ve_unknown(ortam) -> None:
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    assert led.start_dispatch(cmd.id, cmd.delivery_token) is True
    _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                              max_age_sec=120.0)
    # SONUC YAZILMADAN oldurulecek.
    ISARET.write_text("execute-bitti", encoding="utf-8")
    import time
    while True:
        time.sleep(0.05)
    """
    )
    _oldur(kod, ortam["_isaret_yolu"], **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert _sayac(ortam["_sayac_yolu"]) == 1

    kod2 = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    kurtarilan = led.recover_unknown_results()
    print("KURTARILAN=%s" % json.dumps([r["status"] for r in kurtarilan]))
    cmd = komut()
    if led.start_dispatch(cmd.id, cmd.delivery_token):
        _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                                  max_age_sec=120.0)
    led.close()
    print("TAMAM")
    """
    )
    ort = _calistir(kod2, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert "TAMAM" in ort.stdout, f"stderr={ort.stderr}"
    assert '"unknown"' in ort.stdout, "yarim kalan komut unknown bildirilmedi"
    assert _sayac(ortam["_sayac_yolu"]) == 1, "restart sonrasi CROB TEKRARLANDI"


# ===========================================================================
# C04 — ACK bekliyorken cokme
# ===========================================================================


def test_c04_ack_bekleyen_cokme_restart_sonrasi_ack_devam(ortam) -> None:
    """ACK RAM'de olsaydi SIGKILL ile kaybolur ve backend komutu hicbir zaman
    `sent` goremezdi — yani F3C'nin kapattigi pencere ACK tarafindan yeniden
    acilirdi."""
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    assert led.start_dispatch(cmd.id, cmd.delivery_token) is True
    assert led.pending_ack_count() == 1
    ISARET.write_text("ack-bekliyor", encoding="utf-8")
    import time
    while True:
        time.sleep(0.05)
    """
    )
    _oldur(kod, ortam["_isaret_yolu"], **{k: ortam[k] for k in ("defter", "sayac", "isaret")})

    kod2 = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    bekleyen = led.pending_acks()
    print("ACK=%s" % json.dumps(bekleyen))
    cmd = komut()
    if led.start_dispatch(cmd.id, cmd.delivery_token):
        _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                                  max_age_sec=120.0)
    led.close()
    print("TAMAM")
    """
    )
    ort = _calistir(kod2, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert "TAMAM" in ort.stdout, f"stderr={ort.stderr}"
    assert '"command_id": 1' in ort.stdout and '"delivery_token": "JETON-1"' in ort.stdout, (
        f"ACK restart sonrasi kayboldu: {ort.stdout}"
    )
    assert _sayac(ortam["_sayac_yolu"]) == 0


# ===========================================================================
# C05 — sonuc kalici, teslim oncesi cokme
# ===========================================================================


def test_c05_sonuc_kalici_restart_sonrasi_yeniden_teslim(ortam) -> None:
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    led.start_dispatch(cmd.id, cmd.delivery_token)
    led.record_result({{"id": cmd.id, "ok": True, "status": "ok"}})
    ISARET.write_text("sonuc-yazildi", encoding="utf-8")
    import time
    while True:
        time.sleep(0.05)
    """
    )
    _oldur(kod, ortam["_isaret_yolu"], **{k: ortam[k] for k in ("defter", "sayac", "isaret")})

    kod2 = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    print("SONUC=%d" % led.pending_result_count())
    print("ACK=%d" % led.pending_ack_count())
    led.close()
    print("TAMAM")
    """
    )
    ort = _calistir(kod2, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    assert "TAMAM" in ort.stdout, f"stderr={ort.stderr}"
    assert "SONUC=1" in ort.stdout, "kalici sonuc restart sonrasi kayboldu"
    assert "ACK=1" in ort.stdout, "ACK kuyrugu sonuctan etkilenmis"


# ===========================================================================
# C06 — GERCEK poll dongusunde "once deftere yaz, sonra calistir" SIRASI
# ===========================================================================


def test_c06_gercek_dongude_defter_execute_sirasi(ortam) -> None:
    """`_run_command_poll` icindeki SIRA: dayanikli yazim CROB'dan ONCE.

    BU TEST NEDEN VAR
    -----------------
    Diger cokme testleri `start_dispatch` ve `_execute_pending_commands`i
    cocuk kodda ELLE cagiriyor; bu yuzden URETIM DONGUSUNDEKI sirayi
    dogrulamiyorlar. Mutasyon sinamasi bunu ortaya cikardi: sira bilerek
    bozuldugunda (once calistir, sonra deftere yaz) tum paket YESIL kaliyordu.

    Burada gercek `_run_command_poll` surulur ve proses tam CROB aninda SERT
    olduruleur (`os._exit`, temizlik yok):

      DOGRU SIRA  : defter COMMIT -> CROB -> olum -> restart -> kayit VAR
                    -> ikinci CROB YOK  -> toplam 1
      BOZUK SIRA  : CROB -> olum (defter BOS) -> restart -> komut YENI sanilir
                    -> ikinci CROB      -> toplam 2
    """
    bicim = {k: ortam[k] for k in ("defter", "sayac", "isaret")}

    tur = (
        _ORTAK
        + '''
    import threading
    from dnp3_gateway.main import _run_command_poll
    from dnp3_gateway.backend.config_client import PendingPoll

    OLDUR = {oldur}

    class Patlayan(Sayan):
        """CROB anini simule eder; istenirse prosesi SERT oldurur."""
        def operate_device(self, **kw):
            sonuc = super().operate_device(**kw)
            if OLDUR:
                import os
                os._exit(9)   # temiz kapanma YOK - defter ne yazildiysa o kalir
            return sonuc

    class Istemci:
        gateway_code = "GW-001"
        def fetch_pending_commands(self):
            return PendingPoll(commands=(komut(),))
        def report_delivery_acks(self, acks):
            return {{int(a["command_id"]) for a in acks}}
        def report_command_results(self, results):
            return None

    led = CommandLedger(DEFTER)
    dur = threading.Event()
    uyan = threading.Event()

    # Tek tur kossun diye: ilk turdan sonra durdur.
    def _tek_tur():
        import time as _t
        _t.sleep(2.0)
        dur.set()
    threading.Thread(target=_tek_tur, daemon=True).start()

    _run_command_poll(
        client=Istemci(), reader=Patlayan(), state=durum(), ledger=led,
        stop_event=dur, poll_sec=1, config_wake=uyan,
        max_age_sec=120.0, require_timestamp=True,
    )
    print("KAYIT=%s" % (led.kayitli_jeton(1)[0],))
    led.close()
    print("TAMAM")
    '''
    )

    # 1) Ilk tur: CROB aninda SERT olum.
    ilk = _calistir(tur, oldur="True", **bicim)
    assert _sayac(ortam["_sayac_yolu"]) == 1, (
        f"ilk turda tam 1 CROB bekleniyordu; stdout={ilk.stdout} stderr={ilk.stderr}"
    )

    # 2) Restart: ayni komut YENIDEN sunuluyor, bu kez olum yok.
    ikinci = _calistir(tur, oldur="False", **bicim)
    assert "TAMAM" in ikinci.stdout, f"stderr={ikinci.stderr}"
    assert "KAYIT=True" in ikinci.stdout, "dayanikli kayit bulunamadi"

    assert _sayac(ortam["_sayac_yolu"]) == 1, (
        f"TOPLAM FIZIKSEL KOMUT {_sayac(ortam['_sayac_yolu'])} — 1 olmaliydi. "
        "Dayanikli yazim CROB'dan SONRA yapiliyorsa, cokme aninda defter bos "
        "kalir ve restart sonrasi komut YENIDEN calistirilir."
    )


# ===========================================================================
# G03 / G04 / G30 — kimlik dayanikliligi ve toplam tekrar guvenligi
# ===========================================================================


def test_g03_proses_restart_kimligi_degistirmez(ortam) -> None:
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    print("EPOCH=%s" % led.epoch)
    led.close()
    """
    )
    a = _calistir(kod, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    b = _calistir(kod, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    e1 = [s for s in a.stdout.splitlines() if s.startswith("EPOCH=")][0]
    e2 = [s for s in b.stdout.splitlines() if s.startswith("EPOCH=")][0]
    assert e1 == e2, f"proses restart kimligi degistirdi: {e1} != {e2}"


def test_g04_dosya_yerinde_kaldikca_kimlik_sabit(ortam, tmp_path: Path) -> None:
    """Container recreate benzetimi: SURec degisir, VOLUME (dosya) ayni kalir.

    Gercek container testi CI'da docker gerektirir; burada dayanikliligin
    KAYNAGI olan sey — ayni dosyanin ayni kimligi vermesi — kanitlaniyor.
    """
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    print("EPOCH=%s" % led.epoch)
    print("ACK=%d" % led.pending_ack_count())
    led.close()
    """
    )
    ilk = _calistir(
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    led.start_dispatch(1, "JETON-1")
    print("EPOCH=%s" % led.epoch)
    led.close()
    """,
        **{k: ortam[k] for k in ("defter", "sayac", "isaret")},
    )
    e1 = [s for s in ilk.stdout.splitlines() if s.startswith("EPOCH=")][0]

    sonra = _calistir(kod, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    e2 = [s for s in sonra.stdout.splitlines() if s.startswith("EPOCH=")][0]

    assert e1 == e2, "ayni dosya farkli kimlik verdi"
    assert "ACK=1" in sonra.stdout, "bekleyen ACK yeni proseste kayboldu"


def test_g05_dosya_silinince_kimlik_degisir(ortam) -> None:
    kod = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    print("EPOCH=%s" % led.epoch)
    led.close()
    """
    )
    a = _calistir(kod, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})
    for ek in ("", "-wal", "-shm"):
        p = Path(str(ortam["_defter_yolu"]) + ek)
        if p.exists():
            p.unlink()
    b = _calistir(kod, **{k: ortam[k] for k in ("defter", "sayac", "isaret")})

    e1 = [s for s in a.stdout.splitlines() if s.startswith("EPOCH=")][0]
    e2 = [s for s in b.stdout.splitlines() if s.startswith("EPOCH=")][0]
    assert e1 != e2, "defter silindi ama kimlik AYNI kaldi — backend farki goremez"


def test_g30_uc_restart_boyunca_toplam_execute_1(ortam) -> None:
    """Komut defalarca yeniden sunulsa ve proses defalarca olse bile
    FIZIKSEL KOMUT TOPLAMI 1'i asmamali."""
    ilk = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    if led.start_dispatch(cmd.id, cmd.delivery_token):
        _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                                  max_age_sec=120.0)
    led.close()
    print("TAMAM")
    """
    )
    tekrar = (
        _ORTAK
        + """
    led = CommandLedger(DEFTER)
    cmd = komut()
    kayitli, _ = led.kayitli_jeton(cmd.id)
    if kayitli:
        led.ack_yeniden_kuyrukla(cmd.id)
    elif led.start_dispatch(cmd.id, cmd.delivery_token):
        _execute_pending_commands(Sayan(), durum(), [cmd], gateway_code="GW-001",
                                  max_age_sec=120.0)
    led.close()
    print("TAMAM")
    """
    )
    bicim = {k: ortam[k] for k in ("defter", "sayac", "isaret")}
    assert "TAMAM" in _calistir(ilk, **bicim).stdout
    for _ in range(3):
        ort = _calistir(tekrar, **bicim)
        assert "TAMAM" in ort.stdout, f"stderr={ort.stderr}"

    assert _sayac(ortam["_sayac_yolu"]) == 1, (
        f"toplam fiziksel komut {_sayac(ortam['_sayac_yolu'])} — 1 olmaliydi"
    )
