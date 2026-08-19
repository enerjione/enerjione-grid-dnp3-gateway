"""Cihaz oturum durumu kaydinin KALICILIGI — restart alarm firtinasi onlemi.

NEDEN ONEMLI
------------
Saha senaryosu: Smart Mode cihazlari normal sekilde uyuyor (modemleri kapali,
bir sonraki raporlarina kadar TCP oturumu yok). Gateway/container yeniden
baslarsa bellekteki "bu cihaz saglikli uyuyor" bilgisi kaybolur ve
cihazlarin TAMAMI comm_lost gorunur.

Bu dosya, o bilgiyi tasiyan dosyanin (a) dogru yazilip okundugunu,
(b) BOZUK/ESKI oldugunda gateway'i dusurmedigini ve uydurma karar
uretmedigini kilitler.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from dnp3_gateway.session_state_store import (
    MAX_RECORDS,
    MIN_WRITE_INTERVAL_SEC,
    STORE_VERSION,
    SessionStateRecord,
    SessionStateStore,
)


def _kayit(**kw) -> SessionStateRecord:
    ham = {
        "state": "smart_idle",
        "last_valid_contact_unix": time.time() - 60,
        "smart_idle_since_unix": time.time() - 30,
    }
    ham.update(kw)
    return SessionStateRecord(**ham)


# --------------------------------------------------------------------------
# Temel gidis-donus
# --------------------------------------------------------------------------


def test_yazilan_kayit_geri_okunur(tmp_path: Path) -> None:
    yol = tmp_path / "modes.json"
    s1 = SessionStateStore(yol)
    s1.record("MASTER-002", _kayit())
    assert s1.flush(force=True) is True

    s2 = SessionStateStore(yol)
    assert s2.load() == 1
    k = s2.get("MASTER-002")
    assert k is not None
    assert k.state == "smart_idle"
    assert k.last_valid_contact_unix is not None
    assert k.smart_idle_since_unix is not None


def test_dosya_versiyonlu_ve_atomik(tmp_path: Path) -> None:
    yol = tmp_path / "modes.json"
    s = SessionStateStore(yol)
    s.record("A", _kayit())
    s.flush(force=True)

    govde = json.loads(yol.read_text(encoding="utf-8"))
    assert govde["version"] == STORE_VERSION
    assert "A" in govde["devices"]
    # Gecici dosya birakilmamis olmali (atomik tmp + replace).
    assert not list(tmp_path.glob(".session-state-*")), "gecici dosya temizlenmemis"


def test_degismeyen_kayit_yazim_tetiklemez(tmp_path: Path) -> None:
    """600 cihazda her cycle disk yazmak gereksiz I/O olurdu."""
    yol = tmp_path / "modes.json"
    s = SessionStateStore(yol)
    k = _kayit()
    s.record("A", k)
    assert s.flush(force=True) is True
    s.record("A", k)  # AYNI kayit
    assert s.flush(force=True) is False, "degismeyen kayit icin disk yazilmis"


def test_yazim_hiz_sinirli(tmp_path: Path) -> None:
    yol = tmp_path / "modes.json"
    s = SessionStateStore(yol)
    s.record("A", _kayit())
    assert s.flush(force=True) is True
    s.record("A", _kayit(state="online"))
    # Hemen ardindan: hiz siniri nedeniyle yazilmaz...
    assert s.flush() is False
    assert MIN_WRITE_INTERVAL_SEC > 0
    # ...ama `force` (kapanis) siniri atlar; son durum KAYBOLMAZ.
    assert s.flush(force=True) is True


def test_bellek_ici_mod_diske_dokunmaz(tmp_path: Path) -> None:
    s = SessionStateStore(None)
    s.record("A", _kayit())
    assert s.flush(force=True) is False
    assert s.get("A") is not None
    assert not list(tmp_path.iterdir())


def test_silinen_cihazin_kaydi_dusulur(tmp_path: Path) -> None:
    s = SessionStateStore(tmp_path / "modes.json")
    s.record("A", _kayit())
    s.record("B", _kayit())
    assert s.forget({"A"}) == 1
    assert s.get("B") is None
    assert s.get("A") is not None


def test_kayit_sayisi_sinirli(tmp_path: Path) -> None:
    """Bozuk/kompromize bir config diski doldurmasin."""
    s = SessionStateStore(tmp_path / "modes.json")
    for i in range(MAX_RECORDS + 10):
        s.record(f"DEV-{i}", _kayit())
    assert len(s.snapshot()) == MAX_RECORDS


# --------------------------------------------------------------------------
# BOZUK / ESKI dosya — hicbiri gateway'i dusurmemeli
# --------------------------------------------------------------------------


def test_dosya_yoksa_sessizce_bos(tmp_path: Path) -> None:
    s = SessionStateStore(tmp_path / "yok.json")
    assert s.load() == 0
    assert s.snapshot() == {}


def test_bozuk_json_yok_sayilir(tmp_path: Path) -> None:
    yol = tmp_path / "modes.json"
    yol.write_text("{ bozuk", encoding="utf-8")
    s = SessionStateStore(yol)
    assert s.load() == 0, "bozuk dosyadan kayit sizmamali"
    assert s.snapshot() == {}


def test_surum_uyusmazligi_yok_sayilir(tmp_path: Path) -> None:
    """Yarim yamalak bir kayitla karar vermektense HIC karar vermemek dogru."""
    yol = tmp_path / "modes.json"
    yol.write_text(
        json.dumps({"version": STORE_VERSION + 99, "devices": {"A": {"operation_mode": "smart"}}}),
        encoding="utf-8",
    )
    s = SessionStateStore(yol)
    assert s.load() == 0


def test_tanimsiz_token_unknowna_dusurulur(tmp_path: Path) -> None:
    """Disaridan degistirilebilen bir dosya; tanimsiz token sessizce KABUL EDILEMEZ.

    Edilseydi asagidaki tum karsilastirmalar (mod == "smart" gibi) sessizce
    yanlis dala girerdi.
    """
    yol = tmp_path / "modes.json"
    yol.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "devices": {"A": {"state": "uydurma"}},
            }
        ),
        encoding="utf-8",
    )
    s = SessionStateStore(yol)
    assert s.load() == 1
    k = s.get("A")
    assert k.state == "lost", "tanimsiz token guvenli varsayilana dusmeli"


def test_sacma_zaman_damgalari_dusurulur(tmp_path: Path) -> None:
    """Gelecege ya da 2000 oncesine damgali kayitla uyku hesabi anlamsizdir."""
    yol = tmp_path / "modes.json"
    yol.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "devices": {
                    "GELECEK": {
                        "state": "smart_idle",
                        "last_valid_contact_unix": time.time() + 10 * 86400,
                        "smart_idle_since_unix": time.time() + 10 * 86400,
                    },
                    "TARIH_ONCESI": {
                        "state": "smart_idle",
                        "last_valid_contact_unix": 1.0,
                        "smart_idle_since_unix": 1.0,
                    },
                    "METIN": {"state": "smart_idle", "last_valid_contact_unix": "dun"},
                },
            }
        ),
        encoding="utf-8",
    )
    s = SessionStateStore(yol)
    s.load()
    for kod in ("GELECEK", "TARIH_ONCESI", "METIN"):
        k = s.get(kod)
        assert k is not None
        assert k.last_valid_contact_unix is None, f"{kod}: sacma damga kabul edilmis"
        assert k.smart_idle_since_unix is None


def test_devices_alani_bozuksa_yok_sayilir(tmp_path: Path) -> None:
    yol = tmp_path / "modes.json"
    yol.write_text(json.dumps({"version": STORE_VERSION, "devices": "liste degil"}), encoding="utf-8")
    assert SessionStateStore(yol).load() == 0


def test_bozuk_satir_digerlerini_dusurmez(tmp_path: Path) -> None:
    """Tek bir bozuk satir yuzunden TUM sahayi kaybetmek dogru olmazdi."""
    yol = tmp_path / "modes.json"
    yol.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "devices": {"IYI": {"state": "online"}, "BOZUK": ["liste"]},
            }
        ),
        encoding="utf-8",
    )
    s = SessionStateStore(yol)
    assert s.load() == 1
    assert s.get("IYI").state == "online"
    assert s.get("BOZUK") is None


def test_yazilamayan_yol_istisna_atmaz(tmp_path: Path) -> None:
    """Diske yazamamak telemetriyi DURDURMAZ; yalnizca uyku bilgisi kaybolur."""
    # Dosya yerine DIZIN: os.replace hedefi dizin oldugu icin yazim basarisiz.
    yol = tmp_path / "modes.json"
    yol.mkdir()
    s = SessionStateStore(yol)
    s.record("A", _kayit())
    assert s.flush(force=True) is False  # istisna ATMAZ
