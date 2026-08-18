"""ClockGuard soguk acilis fail-safe (A) — DNP3 saat yazma kapisi.

KAPATILAN ACIK
--------------
`is_safe_for_time_sync` hic olcum yapilmadiysa `True` donuyordu; gerekcesi
"backend'e hic ulasilamamis olabilir; eski davranisi bozmayalim" idi. Bu,
korumanin en cok gerektigi ani tam olarak acikta birakiyordu:

    host acilir       -> RTC/NTP henuz duzelmemis
    backend ULASILMAZ -> Date gozlemi YOK -> guard "guvenli" der
    onbellekli config -> DNP3 ayaga kalkar
    DNP3_TIME_SYNC=lan -> DOGRULANMAMIS host saati outstation'lara YAZILIR

Yani "olcemedim" durumu en riskliyken en gevsek karari veriyordu. Yanlis
saati 300 cihaza yazmak, hic yazmamaktan cok daha kotudur: cihazin kendi
olay tamponu da bozulur ve baska bir master'in okudugu damgalar da yanlis
olur.

KAPSAM SINIRI — BU TESTLERIN ASIL DERDI
---------------------------------------
Fail-safe YALNIZCA saat yazmayi kapatmali. Boot, onbellekli config, DNP3
polling, telemetri, NATS/outbox, health ve komut akisi (F1-F7) calismaya
DEVAM ETMELI. Asagida hem kapinin kapandigi hem de baska hicbir seyin
kapanmadigi olculuyor.

Gercek cihaz YOK: saat yazma noktasi (`Now()`) sahte bir opendnp3 zaman
tipiyle kosturuluyor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import pytest

from dnp3_gateway.resource_guard import CLOCK_UNSAFE_SKEW_SEC, ClockGuard


def _date(offset_sec: float = 0.0) -> str:
    """Backend `Date` basligi; pozitif offset = backend ILERIDE."""
    return format_datetime(datetime.now(timezone.utc) + timedelta(seconds=offset_sec))


# ==========================================================================
# 1-6. ClockGuard karar tablosu
# ==========================================================================


def test_1_hic_gozlem_yoksa_saat_yazma_engelli():
    """SOGUK ACILIS: olcum yoksa fail-safe."""
    g = ClockGuard()
    assert g.clock_state == "unknown"
    assert g.is_safe_for_time_sync is False


def test_2_gecerli_date_kucuk_sapma_izinli():
    g = ClockGuard()
    g.observe_http_date(_date())
    assert g.clock_state == "safe"
    assert g.is_safe_for_time_sync is True


def test_3_gecerli_date_guvensiz_sapma_engelli():
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.clock_state == "unsafe"
    assert g.is_safe_for_time_sync is False


def test_4_guvensizden_guvenliye_toparlanma():
    """Saat duzelirse kapi yeniden ACILMALI — kalici kilit degil."""
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.is_safe_for_time_sync is False

    g.observe_http_date(_date())
    assert g.clock_state == "safe"
    assert g.is_safe_for_time_sync is True


@pytest.mark.parametrize("eksik", [None, "", "   "])
def test_5_date_yoksa_unknown_kalir(eksik: Any):
    """Header hic gelmedi -> "olctum ve guvenli" DEMEK DEGIL."""
    g = ClockGuard()
    g.observe_http_date(eksik)
    assert g.clock_state == "unknown"
    assert g.is_safe_for_time_sync is False


@pytest.mark.parametrize("bozuk", ["bu bir tarih degil", "Mon, 32 Xxx 2026", "1755500000", "-"])
def test_6_bozuk_date_unknown_kalir(bozuk: str):
    g = ClockGuard()
    g.observe_http_date(bozuk)
    assert g.clock_state == "unknown"
    assert g.is_safe_for_time_sync is False


def test_bozuk_date_onceki_gecerli_olcumu_silmez():
    """Gecerli bir olcumden sonra bozuk bir header gelirse geri gidilmemeli."""
    g = ClockGuard()
    g.observe_http_date(_date())
    assert g.is_safe_for_time_sync is True

    g.observe_http_date("bu bir tarih degil")
    assert g.clock_state == "safe", "bozuk header gecerli olcumu sildi"


def test_snapshot_unknown_ile_unsafe_i_ayirt_ediyor():
    """Operator "neden yazilmiyor" sorusunu tek bakista cevaplayabilmeli."""
    g = ClockGuard()
    assert g.snapshot()["clock_state"] == "unknown"
    assert g.snapshot()["safe_for_time_sync"] is False
    assert g.snapshot()["skew_sec"] is None

    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.snapshot()["clock_state"] == "unsafe"
    assert g.snapshot()["skew_sec"] is not None


# ==========================================================================
# 7-8. GERCEK SAAT YAZMA NOKTASI (`Now()`)
# ==========================================================================
#
# Asagisi ClockGuard'i degil, ADAPTER'in saat yazma kapisini olcer: guard
# dogru karar verse bile `Now()` onu yok sayarsa saha yine yanlis saat alir.
# Nitekim bu metod bir donem guard'i kontrol edip yalnizca DEBUG log basiyor,
# sonra YINE DE `time.time()` donduruyordu.

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 kurulu degil")

from dnp3_gateway.adapters import dnp3_yadnp3_master as ym  # noqa: E402


@pytest.fixture
def guard_bagli():
    """Adapter'in modul-duzeyi guard referansini test suresince degistirir."""
    onceki = ym._clock_guard_ref.get("guard")
    try:
        yield ym.set_clock_guard
    finally:
        ym._clock_guard_ref["guard"] = onceki


def _master_app() -> Any:
    """GERCEK `_make_master_app` fabrikasi.

    `cache` yalnizca IIN/olay yollarinda kullaniliyor; `Now()` ona hic
    dokunmuyor. Yine de GERCEK `_DeviceCache` veriliyor ki fabrika
    imzasindaki bir degisiklik burada gorunsun.
    """
    return ym._make_master_app(ym._DeviceCache(), "SN2_0")


def _now_degeri(app: Any) -> Any:
    return app.Now()


def _gecerli_mi(dnptime: Any) -> bool:
    """opendnp3 DNPTime gecerli mi (yazilacak mi)?"""
    for alan in ("is_valid", "isValid", "valid"):
        if hasattr(dnptime, alan):
            v = getattr(dnptime, alan)
            return bool(v() if callable(v) else v)
    # Bazi binding surumlerinde gecersizlik epoch 0 ile ifade edilir.
    for alan in ("value", "msSinceEpoch"):
        if hasattr(dnptime, alan):
            v = getattr(dnptime, alan)
            return int(v() if callable(v) else v) != 0
    raise AssertionError(f"DNPTime gecerlilik alani bulunamadi: {dir(dnptime)}")


def test_7_soguk_acilista_saat_yazilmiyor(guard_bagli):
    """Backend erisilemez + onbellekli config -> DNP3 saat yazma = 0."""
    guard_bagli(ClockGuard())  # hic gozlem yok = soguk acilis
    assert not _gecerli_mi(_now_degeri(_master_app())), "dogrulanmamis host saati outstation'a YAZILIYOR"


def test_8_backend_gelince_saat_yazma_kapisi_aciliyor(guard_bagli):
    """Ilk gecerli Date gozleminden sonra normal davranis geri gelir."""
    g = ClockGuard()
    guard_bagli(g)
    app = _master_app()
    assert not _gecerli_mi(_now_degeri(app))  # once: unknown

    g.observe_http_date(_date())  # backend geldi
    assert _gecerli_mi(_now_degeri(app)), "saat guvenli oldugu halde kapi kapali kaldi"


def test_guvensiz_sapmada_da_saat_yazilmiyor(guard_bagli):
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    guard_bagli(g)
    assert not _gecerli_mi(_now_degeri(_master_app()))


# ==========================================================================
# KAPSAM SINIRI: fail-safe BASKA HICBIR SEYI kapatmiyor
# ==========================================================================


def test_saat_kapisi_yalnizca_saat_yazmayi_etkiliyor():
    """`is_safe_for_time_sync` TEK bir yerde tuketiliyor: `Now()`.

    Biri ilerde bu bayragi polling/telemetri/komut yoluna baglarsa, soguk
    acilista gateway'in YARISI kapanir. Bu test o regresyonu yakalar.
    """
    import pathlib

    kok = pathlib.Path(ym.__file__).parent.parent
    kullanim = []
    for yol in kok.rglob("*.py"):
        for no, satir in enumerate(yol.read_text(encoding="utf-8").splitlines(), 1):
            if "is_safe_for_time_sync" in satir and "def " not in satir:
                kullanim.append(f"{yol.relative_to(kok).as_posix()}:{no}")

    # resource_guard.py (snapshot) + adapter (Now kapisi) disinda kullanim OLMAMALI.
    beklenmeyen = [
        k for k in kullanim if not k.startswith(("resource_guard.py", "adapters/dnp3_yadnp3_master.py"))
    ]
    assert not beklenmeyen, f"saat bayragi baska yollara sizmis: {beklenmeyen}"
