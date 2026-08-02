"""DNP3 kalite bayraklari, master yeniden kurulumu, budama ve kaynak denetimi.

Kapatilan uretim riskleri:

1. **Kalite bayraklari tamamen yok sayiliyordu** — her deger `quality="good"`
   yayinlaniyordu. Outstation CT referansini kaybedip noktayi
   `value=0.0, flags=ONLINE|REFERENCE_ERR` raporladiginda SCADA hat akimini
   0 A kabul ediyordu.

2. **Cihaz IP'si degisince master yeniden kurulmuyordu** — `_ensure_master`
   yalnizca `device.code` ile anahtarliyordu. Saha ekibi RTU IP'sini
   degistirdiginde gateway eski IP'ye baglanmaya devam ediyor; eski IP baska
   cihaza atanmissa O CIHAZDAN okuyup degerleri ESKI device_code ile
   yayinliyordu.

3. **Kalici 'lost' kilidi** — grace dolduktan sonra TCP hic kopmazsa cihaz
   DNP3'te saglikli konussa bile gateway sonsuza kadar comm_lost yayinliyordu.

4. **Sinirsiz buyume** — `_seen_command_ids`, command_ledger, dead_letter.

5. **Disk ve saat hic olculmuyordu.**
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dnp3_gateway.adapters.dnp3_yadnp3_master import (
    _DeviceCache,
    _double_bit_to_float,
    map_dnp3_quality,
)
from dnp3_gateway.backend import PendingCommand
from dnp3_gateway.messaging.command_ledger import CommandLedger
from dnp3_gateway.messaging.outbox import Outbox
from dnp3_gateway.resource_guard import ClockGuard, DiskGuard
from dnp3_gateway.state import MAX_SEEN_COMMAND_IDS, GatewayState

_G = 30


# --------------------------------------------------------------------------
# kalite bayraklari
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "beklenen"),
    [
        (0x01, "good"),  # ONLINE
        (None, "good"),  # bayrak yok -> geriye uyum
        (0x00, "invalid"),  # ONLINE YOK -> deger gecersiz
        (0x03, "restart"),  # ONLINE|RESTART
        (0x05, "comm_lost"),  # ONLINE|COMM_LOST
        (0x11, "forced"),  # ONLINE|LOCAL_FORCED
        (0x09, "forced"),  # ONLINE|REMOTE_FORCED
        (0x21, "invalid"),  # ONLINE|OVER_RANGE
        (0x41, "invalid"),  # ONLINE|REFERENCE_ERR
    ],
)
def test_kalite_bayragi_eslemesi(flags: int | None, beklenen: str) -> None:
    assert map_dnp3_quality(flags) == beklenen


def test_referans_hatasi_good_olarak_gecmez() -> None:
    """REGRESYON: CT referansi kayipken 0.0 degeri 'good' yayinlaniyordu."""
    assert map_dnp3_quality(0x01 | 0x40) != "good"


def test_comm_lost_restart_ten_oncelikli() -> None:
    assert map_dnp3_quality(0x07) == "comm_lost"


def test_cache_bayraklari_tasir() -> None:
    c = _DeviceCache()
    c.set(_G, 1, 5.0, flags=0x41)
    taken = c.peek_if_dirty(_G, 1)
    assert taken is not None
    raw, _text, flags, _version, _dt, _tq = taken
    assert raw == 5.0
    assert flags == 0x41


def test_sadece_bayrak_degisimi_de_yayin_tetikler() -> None:
    """Deger ayni kalsa da nokta 'gecerli'den 'REFERENCE_ERR'e gectiyse SCADA gormeli."""
    c = _DeviceCache()
    c.set(_G, 1, 5.0, flags=0x01)
    _r, _t, _f, v, _dt, _tq = c.peek_if_dirty(_G, 1)
    c.commit_published([(_G, 1, v)])
    assert c.peek_if_dirty(_G, 1) is None

    c.set(_G, 1, 5.0, flags=0x41)  # ayni deger, BOZUK kalite
    assert c.peek_if_dirty(_G, 1) is not None


def test_double_bit_donusumu() -> None:
    class _E:
        def __init__(self, v: int) -> None:
            self.value = v

    assert _double_bit_to_float(_E(0)) == 0.0  # INTERMEDIATE
    assert _double_bit_to_float(_E(2)) == 2.0  # DETERMINED_ON
    assert _double_bit_to_float("bozuk") == 0.0


# --------------------------------------------------------------------------
# master imzasi (baglanti parametresi degisimi)
# --------------------------------------------------------------------------


def test_baglanti_imzasi_ip_degisince_farklidir() -> None:
    """REGRESYON: IP degisince master yeniden kurulmuyor, eski IP'ye baglaniyordu."""
    from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader
    from dnp3_gateway.backend import DeviceConfig

    fp = Yadnp3TelemetryReader._connection_fingerprint

    class _R:
        _default_dnp3_tcp_port = 20000
        _local_address = 1
        _resolve_tcp_port = staticmethod(Yadnp3TelemetryReader._resolve_tcp_port)
        _resolve_local_address = staticmethod(Yadnp3TelemetryReader._resolve_local_address)

    r = _R()
    eski = DeviceConfig(code="D1", name="d", ip_address="10.20.5.11", dnp3_address=4)
    yeni_ip = DeviceConfig(code="D1", name="d", ip_address="10.20.5.19", dnp3_address=4)
    yeni_addr = DeviceConfig(code="D1", name="d", ip_address="10.20.5.11", dnp3_address=7)
    yeni_mod = DeviceConfig(
        code="D1",
        name="d",
        ip_address="10.20.5.11",
        dnp3_address=4,
        ip_endpoint_type="initiating",
        master_ip_port=20100,
    )

    assert fp(r, eski) == fp(r, eski)
    assert fp(r, eski) != fp(r, yeni_ip), "IP degisimi imzayi degistirmeli"
    assert fp(r, eski) != fp(r, yeni_addr), "DNP3 adres degisimi imzayi degistirmeli"
    assert fp(r, eski) != fp(r, yeni_mod), "endpoint tipi degisimi imzayi degistirmeli"


# --------------------------------------------------------------------------
# budama
# --------------------------------------------------------------------------


def test_seen_command_ids_sinirli_buyur() -> None:
    """REGRESYON: set hic budanmiyordu; 1 yilda ~31M id = GB'larca RAM."""
    st = GatewayState()

    class _Poll:
        commands = [
            PendingCommand(id=i, device_code="D1", command="x", dnp3_index=1)
            for i in range(MAX_SEEN_COMMAND_IDS + 500)
        ]
        config_nonce = 0
        refresh_nonce = 0

    st.apply_pending_poll(_Poll())
    assert len(st._seen_command_ids) <= MAX_SEEN_COMMAND_IDS
    # Kuyruk yine de TUM komutlari tasimali (dedup penceresi kuyruga engel degil)
    assert len(st.take_pending_commands()) == MAX_SEEN_COMMAND_IDS + 500


def test_ayni_komut_iki_kez_kuyruga_girmez() -> None:
    st = GatewayState()
    cmd = PendingCommand(id=7, device_code="D1", command="x", dnp3_index=1)

    class _Poll:
        commands = [cmd]
        config_nonce = 0
        refresh_nonce = 0

    st.apply_pending_poll(_Poll())
    st.apply_pending_poll(_Poll())
    assert len(st.take_pending_commands()) == 1


def test_ledger_prune_teslim_edilmisleri_siler(tmp_path: Path) -> None:
    led = CommandLedger(tmp_path / "l.db")
    try:
        led.start_dispatch(1)
        led.record_result({"id": 1, "ok": True, "status": "ok"})
        led.mark_delivered(1)
        led.start_dispatch(2)
        led.record_result({"id": 2, "ok": True, "status": "ok"})  # teslim EDILMEDI

        # Cok yeni -> hicbiri silinmez
        assert led.prune(retain_days=90) == 0
        # retain_days=0 -> her sey 'eski' sayilir; yalnizca DELIVERED olan silinir.
        # (Windows saat cozunurlugu icin kisa bekleme; bkz. dead_letter testi.)
        time.sleep(0.05)
        assert led.prune(retain_days=0) == 1
        assert led.pending_result_count() == 1, "teslim edilmemis sonuc KORUNMALI"
    finally:
        led.close()


def test_dead_letter_prune(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        for i in range(5):
            rid = ob.enqueue(message_id=f"m{i}", correlation_id=None, headers=None, payload={"v": i})
            ob.move_to_dead_letter(rid, "kalici hata")
        assert ob.dead_letter_count() == 5
        assert ob.prune_dead_letter(retain_days=30) == 0  # hepsi taze
        # Windows'ta time.time() cozunurlugu ~15.6ms; satirlar ayni tick'e
        # dusebiliyor. Kisa bir bekleme ile cutoff'un TUM satirlardan sonra
        # oldugundan emin oluyoruz (testi zamanlamaya duyarsiz kilar).
        time.sleep(0.05)
        assert ob.prune_dead_letter(retain_days=0) == 5
        assert ob.dead_letter_count() == 0
    finally:
        ob.close()


def test_dead_letter_adet_siniri(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        for i in range(20):
            rid = ob.enqueue(message_id=f"m{i}", correlation_id=None, headers=None, payload={"v": i})
            ob.move_to_dead_letter(rid, "hata")
        # max_rows alt siniri 1000; 20 satir icin silme olmamali
        assert ob.prune_dead_letter(retain_days=30, max_rows=5) == 0
        assert ob.dead_letter_count() == 20
    finally:
        ob.close()


# --------------------------------------------------------------------------
# kaynak denetimi
# --------------------------------------------------------------------------


def test_disk_guard_olcum_yapar(tmp_path: Path) -> None:
    g = DiskGuard(tmp_path)
    snap = g.check()
    assert snap["level"] in ("ok", "low", "critical")
    assert snap["free_bytes"] is not None and snap["free_bytes"] > 0
    assert g.snapshot()["level"] == snap["level"]


def test_disk_guard_olmayan_yol_patlamaz(tmp_path: Path) -> None:
    g = DiskGuard(tmp_path / "yok" / "boyle" / "bir" / "yol")
    snap = g.check()  # exception ATMAMALI
    assert "level" in snap


def test_clock_guard_sapma_hesaplar() -> None:
    g = ClockGuard()
    assert g.snapshot()["skew_sec"] is None
    assert g.is_safe_for_time_sync is True  # olcum yoksa eski davranis

    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    # Backend saati 5 dakika GERIDE -> gateway ileri sapmis
    server = datetime.now(timezone.utc) - timedelta(minutes=5)
    g.observe_http_date(format_datetime(server))
    snap = g.snapshot()
    assert snap["skew_sec"] is not None
    assert snap["skew_sec"] > 250
    assert g.is_safe_for_time_sync is False, "buyuk sapmada yanlis saati 300 cihaza YAZMAMALIYIZ"


def test_clock_guard_kucuk_sapma_guvenli() -> None:
    from datetime import datetime, timezone
    from email.utils import format_datetime

    g = ClockGuard()
    g.observe_http_date(format_datetime(datetime.now(timezone.utc)))
    assert g.is_safe_for_time_sync is True
    assert abs(g.snapshot()["skew_sec"]) < 5


def test_clock_guard_bozuk_baslik_yok_sayilir() -> None:
    g = ClockGuard()
    for bad in (None, "", "bu bir tarih degil", "Mon, 32 Xxx 2026"):
        g.observe_http_date(bad)
    assert g.snapshot()["skew_sec"] is None


# --------------------------------------------------------------------------
# production validator: mock yasak
# --------------------------------------------------------------------------


def test_production_mock_modu_reddeder(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESYON: prod'da mock unutulursa uydurma telemetri SCADA'ya akiyordu."""
    from dnp3_gateway.config import Settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "mock")
    monkeypatch.setenv("GATEWAY_TOKEN", "A" * 48)
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    with pytest.raises(Exception, match="mock"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_dnp3_modu_kabul_eder(monkeypatch: pytest.MonkeyPatch) -> None:
    from dnp3_gateway.config import Settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")
    monkeypatch.setenv("GATEWAY_TOKEN", "A" * 48)
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.is_dnp3_mode is True
    # Yeni ayarlarin varsayilanlari
    assert s.dnp3_time_sync == "lan"
    assert s.dnp3_publish_quality_flags is False  # backend hazir olunca acilacak


# --------------------------------------------------------------------------
# saat sicramasi: bayatlik karari monotonic saatle verilmeli
# --------------------------------------------------------------------------


def test_saat_sicramasi_cihazi_bayat_yapmaz(monkeypatch) -> None:
    """REGRESYON: bayatlik duvar saatiyle olculuyordu.

    Sahada iki sik senaryo var: (a) RTC pili bos bir endustriyel PC aciliyor,
    NTP birkac dakika sonra saati AYLARCA ileri aliyor; (b) rutin bir NTP
    duzeltmesi saati birkac dakika sicratiyor.

    Duvar saatiyle olcumde `now - last_update` bir anda esigi asar ve gateway
    O ANDA HABERLESEN 300 cihazin TAMAMINI comm_lost ilan eder. SCADA'da
    toplu "haberlesme yok" dalgasi, ardindan toplu recovery yayini olusur;
    hicbir cihazda gercek bir sorun yokken. Monotonic saat NTP'den etkilenmez.
    """
    c = _DeviceCache()
    c.set(30, 1, 5.0)

    monotonic_damga = c.last_update_at()
    duvar_damga = c.last_update_wall()
    assert monotonic_damga > 0.0
    assert duvar_damga > 0.0

    # Duvar saati 1 yil ileri sicradi (NTP duzeltmesi).
    gercek_time = time.time
    monkeypatch.setattr(time, "time", lambda: gercek_time() + 365 * 86400)

    # Bayatlik karari ETKILENMEMELI: monotonic damga yerinde duruyor.
    assert time.monotonic() - c.last_update_at() < 5.0, "bayatlik hesabi duvar saatinden etkilenmis"


def test_recovery_grace_saat_sicramasindan_etkilenmez(monkeypatch) -> None:
    """Grace period bir SUREDIR; saat sicramasi onu bitirmemeli.

    Duvar saatiyle olculdugunde ileri bir sicrama, cihaz daha ilk frame'ini
    gondermeden `recovery_age()`'i esigin ustune tasir ve gateway cihazi
    hemen 'lost'a dusururdu — recovery penceresi hic isletilmemis olurdu.
    """
    c = _DeviceCache()
    c.begin_recovery()
    assert c.state() == "recovering"
    assert c.recovery_age() < 1.0

    gercek_time = time.time
    monkeypatch.setattr(time, "time", lambda: gercek_time() + 3600)

    assert c.recovery_age() < 5.0, "grace period duvar saati sicramasiyla tuketilmis"
