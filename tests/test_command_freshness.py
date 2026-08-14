"""F3A — komut tazeligi (TTL) ve rolling-upgrade uyumu.

Kapatilan uretim riski
----------------------
Komut zincirinde hicbir katmanda YAS kavrami yoktu. Backend'in `/pending`
sorgusunda yas filtresi yok (yalnizca `status='pending'`), gateway de gelen
komutu kac saat once uretildigine bakmadan calistiriyordu.

Somut senaryo: backend'de komut olusturuluyor, gateway 30 dakika kapali
kaliyor (bakim/deploy/elektrik), acildiginda komut hala `pending` ve OLDUGU
GIBI calisiyor. Kuyrukta bekleyen `master.firmware_update` ya da
`master.software_reset` icin bu kabul edilemez.

GECIS DURUMU
------------
Backend `created_at`'i HENUZ gondermiyor. Bu yuzden `COMMAND_REQUIRE_TIMESTAMP`
varsayilani `false` ve damgasiz komut eski davranisla calisir. Asagidaki
testler HEM bugunku davranisin bozulmadigini HEM de damga geldiginde TTL'nin
bypass edilemedigini kilitler.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dnp3_gateway.backend import DeviceConfig, PendingCommand
from dnp3_gateway.command_freshness import (
    FreshnessReason,
    parse_command_timestamp,
    validate_command_freshness,
)
from dnp3_gateway.main import _execute_pending_commands
from dnp3_gateway.state import GatewayState

from .conftest import make_gateway_config, make_signal

_TTL = 120.0
_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta_sec: float) -> str:
    """`_NOW`'a gore kaydirilmis timezone-aware ISO damga."""
    return (_NOW - timedelta(seconds=delta_sec)).isoformat()


def _sebep(created_at: str | None, *, require: bool = False) -> FreshnessReason:
    return validate_command_freshness(
        created_at, now=_NOW, max_age_sec=_TTL, require_timestamp=require
    ).reason


# --------------------------------------------------------------------------
# A/B. damga YOK — rolling upgrade
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bos", [None, "", "   "])
def test_damga_yoksa_gecis_bayragi_ile_izin(bos) -> None:
    """A: bugunku backend `created_at` gondermiyor -> komut CALISMALI."""
    sonuc = validate_command_freshness(bos, now=_NOW, max_age_sec=_TTL, require_timestamp=False)
    assert sonuc.fresh
    assert sonuc.legacy_allowed is True, "gecis izni gorunur olmali (cagiran taraf loglar)"


@pytest.mark.parametrize("bos", [None, "", "   "])
def test_damga_yoksa_require_acikken_reddedilir(bos) -> None:
    """B: bayrak acikken damgasiz komut fail-closed."""
    sonuc = validate_command_freshness(bos, now=_NOW, max_age_sec=_TTL, require_timestamp=True)
    assert sonuc.reason is FreshnessReason.TIMESTAMP_MISSING
    assert sonuc.legacy_allowed is False


# --------------------------------------------------------------------------
# C-G. yas sinirlari
# --------------------------------------------------------------------------


def test_taze_komut_gecer() -> None:
    assert _sebep(_iso(1)) is FreshnessReason.FRESH


def test_tam_sinir_taze_sayilir() -> None:
    """D: age == max_age KAPSAYICI sinirdir (`>` ile karsilastirilir)."""
    assert _sebep(_iso(_TTL)) is FreshnessReason.FRESH


def test_sinirin_hemen_ustu_expired() -> None:
    """E: TTL + epsilon -> expired."""
    assert _sebep(_iso(_TTL + 0.001)) is FreshnessReason.EXPIRED
    assert _sebep(_iso(_TTL + 1)) is FreshnessReason.EXPIRED


@pytest.mark.parametrize(
    ("yas", "aciklama"),
    [
        (30 * 60, "F: 30 dakika — gateway bakim penceresi senaryosu"),
        (7 * 24 * 3600, "G: 7 gun — unutulmus kuyruk kaydi"),
    ],
)
def test_cok_eski_komut_expired(yas: float, aciklama: str) -> None:
    sonuc = validate_command_freshness(_iso(yas), now=_NOW, max_age_sec=_TTL, require_timestamp=False)
    assert sonuc.reason is FreshnessReason.EXPIRED, aciklama
    assert sonuc.age_sec == pytest.approx(yas, abs=1.0)


def test_bayrak_acikken_de_kapaliyken_de_ayni_ttl() -> None:
    """TTL bypass EDILEMEZ: damga geldiginde bayragin degeri sonucu degistirmez."""
    for require in (False, True):
        assert _sebep(_iso(_TTL + 60), require=require) is FreshnessReason.EXPIRED
        assert _sebep(_iso(5), require=require) is FreshnessReason.FRESH


# --------------------------------------------------------------------------
# H-J. gelecekteki damga
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ileri", [1, 5, 30, 59.9])
def test_kucuk_gelecek_sapmasi_kabul(ileri: float) -> None:
    """H/I: makul saat sapmasi mesru komutu kesmemeli; yas 0 sayilir."""
    sonuc = validate_command_freshness(_iso(-ileri), now=_NOW, max_age_sec=_TTL, require_timestamp=False)
    assert sonuc.fresh
    assert sonuc.age_sec == 0.0


def test_tolerans_siniri_60_saniye() -> None:
    assert _sebep(_iso(-60)) is FreshnessReason.FRESH
    assert _sebep(_iso(-60.001)) is FreshnessReason.TIMESTAMP_FUTURE


@pytest.mark.parametrize("ileri", [61, 3600, 365 * 24 * 3600])
def test_absurt_gelecek_damga_fail_closed(ileri: float) -> None:
    """J: kabul edilseydi komut OLUMSUZ olurdu — yasi hicbir zaman TTL'yi asmaz."""
    assert _sebep(_iso(-ileri)) is FreshnessReason.TIMESTAMP_FUTURE


# --------------------------------------------------------------------------
# K/L. bozuk damga
# --------------------------------------------------------------------------


def test_timezone_suz_damga_reddedilir() -> None:
    """K: naive degeri UTC varsaymak, yerel saatte uretilmis damgayi saatlerce
    kaydirir ve TTL'yi anlamsiz kilar."""
    assert _sebep("2026-08-14T12:00:00") is FreshnessReason.TIMESTAMP_INVALID
    assert _sebep("2026-08-14 12:00:00") is FreshnessReason.TIMESTAMP_INVALID


@pytest.mark.parametrize(
    "bozuk",
    ["dun", "2026-13-45T99:99:99+00:00", "1786710000", "{}", "2026-08-14T12:00:00+99:00"],
)
def test_bozuk_damga_fail_closed_ve_patlamaz(bozuk: str) -> None:
    """L: process CRASH ETMEZ, kontrollu red uretir."""
    assert _sebep(bozuk) is FreshnessReason.TIMESTAMP_INVALID


def test_bozuk_damga_gecis_bayragindan_etkilenmez() -> None:
    """Deger GONDERILDI ama okunamadi — bu gecis durumu degil, bozuk veridir."""
    assert _sebep("dun", require=False) is FreshnessReason.TIMESTAMP_INVALID
    assert _sebep("dun", require=True) is FreshnessReason.TIMESTAMP_INVALID


def test_z_soneki_desteklenir() -> None:
    """Python 3.10 `fromisoformat`i 'Z' anlamaz; sozlesme ikisini de kapsar."""
    assert parse_command_timestamp("2026-08-14T12:00:00Z") is not None
    assert parse_command_timestamp("2026-08-14T12:00:00+00:00") is not None
    assert parse_command_timestamp("2026-08-14T12:00:00") is None  # naive


# --------------------------------------------------------------------------
# pull yolu entegrasyonu
# --------------------------------------------------------------------------

_KATALOG = [
    make_signal("master.firmware_update", data_type="binary_output", object_group=10, index=2),
    make_signal("master.reset_all_fcis", data_type="binary_output", object_group=10, index=7),
]


class _SayanReader:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def operate_device(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "status": "ok", "control": kwargs.get("op_type")}


def _state() -> GatewayState:
    device = DeviceConfig(code="SN2_0", name="SN2_0", ip_address="10.0.0.9", dnp3_address=4)
    st = GatewayState()
    st.update(make_gateway_config(devices=[device], signals=list(_KATALOG)))
    return st


def _komut(**kw: Any) -> PendingCommand:
    v: dict[str, Any] = {
        "id": 1,
        "device_code": "SN2_0",
        "command": "reset_all_fcis",
        "dnp3_index": 7,
    }
    v.update(kw)
    return PendingCommand(**v)


def _calistir(cmd: PendingCommand, **kw: Any):
    reader = _SayanReader()
    sonuclar = _execute_pending_commands(
        reader, _state(), [cmd], gateway_code="GW-002", max_age_sec=_TTL, **kw
    )
    return reader, sonuclar


def test_pull_damgasiz_komut_bugunku_gibi_calisir() -> None:
    """A (entegrasyon): MEVCUT BACKEND DAVRANISI DEGISMEDI."""
    reader, sonuclar = _calistir(_komut())
    assert len(reader.calls) == 1
    assert sonuclar[0]["ok"] is True


def test_pull_damgasiz_komut_require_acikken_calismaz() -> None:
    reader, sonuclar = _calistir(_komut(id=2), require_timestamp=True)
    assert reader.calls == []
    assert sonuclar[0]["status"] == "command_timestamp_missing"
    assert sonuclar[0]["ok"] is False


def test_pull_taze_damga_calisir() -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
    reader, sonuclar = _calistir(_komut(id=3, created_at=ts))
    assert len(reader.calls) == 1
    assert sonuclar[0]["ok"] is True


def test_pull_expired_komut_operate_cagirmaz() -> None:
    """E (entegrasyon): CROB GONDERILMEZ ve sessizce dusmez."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    reader, sonuclar = _calistir(_komut(id=4, created_at=ts))
    assert reader.calls == [], "expired komut icin CROB GONDERILDI"
    assert len(sonuclar) == 1
    assert sonuclar[0]["id"] == 4
    assert sonuclar[0]["ok"] is False
    assert sonuclar[0]["status"] == "expired"
    assert sonuclar[0]["error"]


def test_expired_sonucu_ledger_bicimindedir() -> None:
    """O: sonuc, ledger/backend teslim yolunun bekledigi sekli tasimali."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _reader, sonuclar = _calistir(_komut(id=5, created_at=ts))
    kayit = sonuclar[0]
    assert set(kayit) >= {"id", "ok", "status", "error"}
    assert isinstance(kayit["id"], int)
    # backend `result_status` String(40) — tasmamali
    assert len(kayit["status"]) <= 40


# --------------------------------------------------------------------------
# M/N. F1/F2 ile precedence
# --------------------------------------------------------------------------


def test_expired_yanlis_index_expired_kazanir() -> None:
    """M: tazelik istegin ICERIGINDEN bagimsizdir; once o degerlendirilir."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    # index 2 = firmware_update, komut reset_all_fcis -> normalde mismatch
    reader, sonuclar = _calistir(_komut(id=6, dnp3_index=2, created_at=ts))
    assert reader.calls == []
    assert sonuclar[0]["status"] == "expired"


def test_taze_yanlis_index_f2_kazanir() -> None:
    """N: F1/F2 korumasi tazelik eklendikten sonra da yerinde."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    reader, sonuclar = _calistir(_komut(id=7, dnp3_index=2, created_at=ts))
    assert reader.calls == []
    assert sonuclar[0]["status"] == "command_index_mismatch"


def test_expired_komut_missing_command_dan_oncelikli() -> None:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    reader, sonuclar = _calistir(_komut(id=8, command="", created_at=ts))
    assert reader.calls == []
    assert sonuclar[0]["status"] == "expired"


def test_bilinmeyen_cihaz_tazelikten_oncelikli() -> None:
    """`device_not_found` en ustte kalir — cihaz olmadan katalog cozulemez."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    reader, sonuclar = _calistir(_komut(id=9, device_code="YOK", created_at=ts))
    assert reader.calls == []
    assert sonuclar[0]["status"] == "device_not_found"


# --------------------------------------------------------------------------
# parser sozlesmesi
# --------------------------------------------------------------------------


def test_pending_parser_created_at_i_tasir() -> None:
    """Alan gelirse tasinir, gelmezse None — eski payload aynen calisir."""
    from dnp3_gateway.backend.config_client import _optional_command_timestamp

    assert _optional_command_timestamp("2026-08-14T12:00:00+00:00") == "2026-08-14T12:00:00+00:00"
    assert _optional_command_timestamp(None) is None
    assert _optional_command_timestamp("   ") is None
    # Bozuk deger BURADA reddedilmez; ham tasinir ki terminal sonuc uretilebilsin
    assert _optional_command_timestamp("dun") == "dun"
