"""Kopan cihaz KENDILIGINDEN geri gelmeli — manuel refresh gerekmemeli.

SAHADA GOZLENEN
---------------
Operator: "bazen gercek cihazla haberlesme gidiyor, manuel refresh
attigimda geri geliyor."

Sebep koddaydi: otomatik integrity poll yalnizca IKI durumda tetikleniyordu
  1. `online -> recovering` (stale-edge), TEK SEFER
  2. `lost -> recovering`, ama YALNIZCA veri zaten geliyorsa (`not stale`)

Yani "cihaz lost + link acik gorunuyor + veri gelmiyor" durumunda gateway
cihaza HICBIR SEY SORMUYORDU; ya TCP'nin kopup yeniden acilmasini (OnOpen)
ya da verinin kendiliginden gelmesini pasif bekliyordu. Kilidi kiran tek
sey operatorun elle tetikledigi `/refresh-all` idi.

Bu senaryo 4G/GSM hatlarda kural: modem RRC idle'a duser, TCP soketi
FIN/RST uretmeden yari-acik kalir, opendnp3 OnClose gormez, outstation da
unsolicited veri gondermeyi keser. Iki taraf da sessizdir ve kimse ilk
hamleyi yapmaz. Saha 4G uzerinden calisacagi icin sistemin kendini
toparlamasi sart.

BU DOSYA NEYI KILITLER
  * lost + stale durumunda gateway PERIYODIK olarak kendi kendine
    integrity poll gonderir (manuel refresh'in yaptiginin aynisi),
  * yoklama sonuc vermezse N denemeden sonra TCP oturumu ZORLA yeniden
    kurulur (yari-acik sokete poll gondermek ise yaramaz),
  * cihaz dondugunde sayaclar sifirlanir,
  * yoklamalar log'u bogmaz; yalnizca durum degisimi olay uretir.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from tests.conftest import make_device, make_signal


class SahteCache:
    """`_DeviceCache` yerine gecen, durumu testin kontrol ettigi taklit."""

    def __init__(self, *, state: str, connected: bool, stale: bool) -> None:
        self._state = state
        self._connected = connected
        self._stale = stale
        self.integrity_istekleri = 0

    # --- adapter'in kullandigi yuzey ---
    def is_connected(self) -> bool:
        return self._connected

    def state(self) -> str:
        return self._state

    def last_update_at(self) -> float:
        # stale=True -> cok eski; stale=False -> su an
        return 0.0 if self._stale else time.monotonic()

    def begin_recovery(self) -> None:
        self._state = "recovering"

    def recovery_age(self) -> float:
        return 0.0

    def fail_recovery(self) -> None:
        self._state = "lost"

    def begin_stale_announce(self) -> tuple[bool, object]:
        return True, None

    def consume_recovery_publish(self) -> bool:
        return False

    def mark_all_dirty(self) -> int:
        return 0

    def reset_stale_announce(self) -> None:
        pass

    def peek_if_dirty(self, group: int, index: int) -> Any:
        # Bu testlerde deger yayini ilgi alani disinda; hicbir nokta "kirli"
        # degil, yani okuma dongusu bos liste uretir.
        return None


class SahteMaster:
    """`_ManagedMaster` taklidi: integrity poll ve shutdown sayilir."""

    def __init__(self, cache: SahteCache, device: Any = None) -> None:
        self.cache = cache
        self.device = device
        self.connection_fingerprint: tuple = ()
        self.poll_sayisi = 0
        self.shutdown_sayisi = 0

    def request_integrity_poll(self) -> bool:
        self.poll_sayisi += 1
        self.cache.integrity_istekleri += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_sayisi += 1


@pytest.fixture
def okuyucu(monkeypatch: pytest.MonkeyPatch):
    """Gercek native master kurmadan `Yadnp3TelemetryReader` ornegi."""
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    # __init__'i atliyoruz (native manager kurar); testin dokundugu alanlari
    # elle hazirliyoruz.
    import threading

    r._lock = threading.RLock()
    r._masters = {}
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._local_address = 1
    r._default_dnp3_tcp_port = 20000
    r._time_sync = "lan"
    r._manager = None
    r._lost_probe_lock = threading.Lock()
    r._lost_probe = {}
    r._lost_probe_total = 0
    r._forced_relink_total = 0
    return r


def _oku(okuyucu, mm: SahteMaster, device, signals, *, monkeypatch):
    monkeypatch.setattr(okuyucu, "_ensure_master", lambda d: mm)
    return okuyucu.read_device(device=device, signals=signals)


# ------------------------------------------------------------------ boşluk
def test_lost_ve_veri_gelmiyorken_gateway_kendisi_sorgular(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """ASIL DUZELTME: lost + stale durumunda periyodik integrity poll.

    Duzeltmeden once bu sayac 0'da kaliyordu ve cihaz ancak operator
    manuel refresh atinca geri geliyordu.
    """
    device = make_device(code="d1")
    signals = [make_signal()]
    cache = SahteCache(state="lost", connected=True, stale=True)
    mm = SahteMaster(cache)
    okuyucu._masters[device.code] = mm

    _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    assert mm.poll_sayisi >= 1, (
        "lost + stale durumunda gateway cihaza HICBIR SEY sormuyor; kilit ancak manuel refresh ile kiriliyor"
    )


def test_yoklama_araligindan_once_tekrar_sorgulanmaz(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """Her poll cycle'inda sorgu atmak 4G'de gereksiz trafik uretir."""
    device = make_device(code="d1")
    signals = [make_signal()]
    mm = SahteMaster(SahteCache(state="lost", connected=True, stale=True))
    okuyucu._masters[device.code] = mm

    for _ in range(5):
        _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    assert mm.poll_sayisi == 1, f"aralik korunmali, {mm.poll_sayisi} sorgu gitti"


def test_yoklama_sonuc_vermezse_link_zorla_yeniden_kurulur(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """Yari-acik sokete integrity poll ise yaramaz — oturum yenilenmeli.

    4G modem RRC idle'a dustugunde TCP soketi FIN/RST uretmeden olur;
    opendnp3 OnClose gormez ve poll'lar bosluga gider. Tek cikis yolu
    kanali kapatip yeniden kurmaktir.
    """
    device = make_device(code="d1")
    signals = [make_signal()]
    mm = SahteMaster(SahteCache(state="lost", connected=True, stale=True))
    okuyucu._masters[device.code] = mm

    # Yoklama araligini sifirlayarak ard arda deneme yaptir.
    monkeypatch.setattr(mod, "_LOST_PROBE_INTERVAL_SEC", 0.0)
    for _ in range(mod._LOST_RELINK_AFTER_PROBES + 1):
        _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    assert mm.shutdown_sayisi == 1, "N basarisiz yoklamadan sonra oturum yenilenmeli"
    assert device.code not in okuyucu._masters, (
        "master sozlukten dusurulmeli ki _ensure_master yeniden kursun"
    )


def test_cihaz_donunce_sayaclar_sifirlanir(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bir sonraki kopmada relink esigi bastan baslamali."""
    device = make_device(code="d1")
    signals = [make_signal()]
    cache = SahteCache(state="lost", connected=True, stale=True)
    mm = SahteMaster(cache, device)
    okuyucu._masters[device.code] = mm

    monkeypatch.setattr(mod, "_LOST_PROBE_INTERVAL_SEC", 0.0)
    _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)
    _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)
    assert okuyucu._lost_probe_durumu(mm)["deneme"] >= 2

    # Cihaz geri geldi: link acik + veri taze.
    cache._stale = False
    cache._state = "lost"  # relink yolu online'a tasiyacak
    _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    assert okuyucu._lost_probe_durumu(mm)["deneme"] == 0, "toparlanmada sayac sifirlanmali"


def test_online_cihaz_yoklanmaz(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saglikli cihaza fazladan integrity poll gitmemeli (500 cihaz olcegi)."""
    device = make_device(code="d1")
    signals = [make_signal()]
    mm = SahteMaster(SahteCache(state="online", connected=True, stale=False))
    okuyucu._masters[device.code] = mm

    _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    assert mm.poll_sayisi == 0


def test_baglanti_yokken_poll_denenmez_ama_sayac_isler(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP hic kurulmadiysa integrity poll anlamsiz; relink de gereksiz.

    Kanal zaten kendi retry'ini yapiyor (SYN gonderiyor); araya girmek
    baglanti kurulmasini geciktirir.
    """
    device = make_device(code="d1")
    signals = [make_signal()]
    mm = SahteMaster(SahteCache(state="lost", connected=False, stale=True))
    okuyucu._masters[device.code] = mm

    monkeypatch.setattr(mod, "_LOST_PROBE_INTERVAL_SEC", 0.0)
    for _ in range(mod._LOST_RELINK_AFTER_PROBES + 2):
        _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    assert mm.poll_sayisi == 0, "link kapaliyken integrity poll gonderilmemeli"
    assert mm.shutdown_sayisi == 0, "kanal zaten yeniden baglanmayi deniyor"


def test_yoklama_sayaclari_raporlanir(okuyucu, monkeypatch: pytest.MonkeyPatch) -> None:
    """/health "acaba deniyor mu" sorusunu tahminle degil sayacla cevaplamali."""
    device = make_device(code="d1")
    signals = [make_signal()]
    mm = SahteMaster(SahteCache(state="lost", connected=True, stale=True))
    okuyucu._masters[device.code] = mm

    monkeypatch.setattr(mod, "_LOST_PROBE_INTERVAL_SEC", 0.0)
    for _ in range(mod._LOST_RELINK_AFTER_PROBES + 1):
        _oku(okuyucu, mm, device, signals, monkeypatch=monkeypatch)

    stat: dict[str, Any] = okuyucu.recovery_stats()
    assert stat["lost_probe_total"] >= 1
    assert stat["forced_relink_total"] == 1
