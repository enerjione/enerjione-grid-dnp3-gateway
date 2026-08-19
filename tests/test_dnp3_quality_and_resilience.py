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
# kalite bayraklari — TIPE GORE (object group)
# --------------------------------------------------------------------------
#
# DNP3'te bayrak byte'inin 5/6/7. bitleri object group'a gore FARKLI anlam
# tasir. Esleme bunu yapmadiginda iki somut saha hatasi uretiyordu:
#
#   * G3 double-bit'te 0x40/0x80 KESICI POZISYONUDUR. Tip-kor esleme
#     DETERMINED_OFF'u (flags=0x41) "REFERENCE_ERR" sanip her ACIK kesiciyi
#     `invalid` yayinlardi.
#   * G1/G10/G20/G21'de 0x20-0x40 sirasiyla CHATTER_FILTER / RESERVED /
#     ROLLOVER / DISCONTINUITY'dir; hicbiri "bu deger guvenilmez" demez.
#
# Asagidaki grup sabitleri ve bit degerleri `test_bayrak_tablosu_*` ile
# GERCEK opendnp3 enum'larina karsi pinlenmistir.

_G1_BINARY = 1
_G3_DOUBLE_BIT = 3
_G10_BINARY_OUT = 10
_G20_COUNTER = 20
_G21_FROZEN_COUNTER = 21
_G30_ANALOG = 30
_G40_ANALOG_OUT = 40
_G110_STRING = 110

_ONLINE = 0x01
_RESTART = 0x02
_COMM_LOST = 0x04
_REMOTE_FORCED = 0x08
_LOCAL_FORCED = 0x10
_BIT5 = 0x20  # CHATTER_FILTER | RESERVED1 | ROLLOVER | OVERRANGE
_BIT6 = 0x40  # RESERVED | STATE1 | DISCONTINUITY | REFERENCE_ERR
_BIT7 = 0x80  # STATE | STATE2 | RESERVED

#: Kalite bayragi TASIYAN tum gruplar — ortak bitler hepsinde ayni anlamda.
_KALITE_GRUPLARI = (
    _G1_BINARY,
    _G3_DOUBLE_BIT,
    _G10_BINARY_OUT,
    _G20_COUNTER,
    _G21_FROZEN_COUNTER,
    _G30_ANALOG,
    _G40_ANALOG_OUT,
)


# ---- ortak bitler: TUM gruplarda ayni sonucu vermeli ----------------------


@pytest.mark.parametrize("group", _KALITE_GRUPLARI)
@pytest.mark.parametrize(
    ("flags", "beklenen"),
    [
        (_ONLINE, "good"),
        (0x00, "invalid"),  # ONLINE YOK -> deger gecersiz
        (_ONLINE | _RESTART, "restart"),
        (_ONLINE | _COMM_LOST, "comm_lost"),
        (_ONLINE | _LOCAL_FORCED, "forced"),
        (_ONLINE | _REMOTE_FORCED, "forced"),
    ],
)
def test_ortak_bitler_tum_gruplarda_ayni(group: int, flags: int, beklenen: str) -> None:
    assert map_dnp3_quality(flags, group) == beklenen


# ---- analog gruplar: bit5/bit6 GERCEKTEN deger butunlugu ------------------


@pytest.mark.parametrize("group", [_G30_ANALOG, _G40_ANALOG_OUT])
@pytest.mark.parametrize("bit", [_BIT5, _BIT6])
def test_analog_overrange_ve_reference_err_invalid(group: int, bit: int) -> None:
    """G30/G40'ta 0x20=OVERRANGE, 0x40=REFERENCE_ERR — olcume guvenilmez."""
    assert map_dnp3_quality(_ONLINE | bit, group) == "invalid"


def test_referans_hatasi_good_olarak_gecmez() -> None:
    """REGRESYON: CT referansi kayipken 0.0 degeri 'good' yayinlaniyordu."""
    assert map_dnp3_quality(_ONLINE | _BIT6, _G30_ANALOG) != "good"


def test_analog_reserved_biti_kaliteyi_bozmaz() -> None:
    """G30'da 0x80 RESERVED — set gelse bile kaliteyi etkilememeli."""
    assert map_dnp3_quality(_ONLINE | _BIT7, _G30_ANALOG) == "good"


# ---- G3 double-bit: 0x40/0x80 POZISYON, kalite DEGIL ---------------------


@pytest.mark.parametrize(
    ("flags", "aciklama"),
    [
        (_ONLINE | _BIT6, "DETERMINED_OFF (kesici ACIK)"),
        (_ONLINE | _BIT7, "DETERMINED_ON (kesici KAPALI)"),
        (_ONLINE | _BIT6 | _BIT7, "INDETERMINATE (pozisyon belirsiz)"),
    ],
)
def test_double_bit_durum_bitleri_invalid_uretmez(flags: int, aciklama: str) -> None:
    """REGRESYON: tip-kor esleme her ACIK kesiciyi `invalid` yayinlardi.

    G3'te 0x40=STATE1, 0x80=STATE2 kesici pozisyonudur. Kutuphane ile
    dogrulandi: DETERMINED_OFF -> 0x41, DETERMINED_ON -> 0x81,
    INDETERMINATE -> 0xC1. Bunlar REFERENCE_ERR/RESERVED DEGILDIR.
    """
    assert map_dnp3_quality(flags, _G3_DOUBLE_BIT) == "good", aciklama


def test_double_bit_indeterminate_kalite_degil_deger_tasir() -> None:
    """INDETERMINATE bir KALITE sorunu degil, bir POZISYON bilgisidir.

    Kesici "acik mi kapali mi bilinmiyor" durumunu bildiriyor; nokta online ve
    dogru rapor veriyor. `invalid` yapmak olcumu alarm degerlendirmesinden
    dusurur ve operator tam da gormesi gereken "pozisyon belirsiz" isaretini
    KAYBEDER. Anlam `value` alaninda tasinir (3.0 = INDETERMINATE).
    """
    assert map_dnp3_quality(_ONLINE | _BIT6 | _BIT7, _G3_DOUBLE_BIT) == "good"

    class _E:
        def __init__(self, v: int) -> None:
            self.value = v

    assert _double_bit_to_float(_E(0)) == 0.0  # INTERMEDIATE
    assert _double_bit_to_float(_E(1)) == 1.0  # DETERMINED_OFF
    assert _double_bit_to_float(_E(2)) == 2.0  # DETERMINED_ON
    assert _double_bit_to_float(_E(3)) == 3.0  # INDETERMINATE


def test_double_bit_chatter_filter_invalid_uretmez() -> None:
    """G3'te 0x20 CHATTER_FILTER — nokta titriyor ama deger gecersiz degil."""
    assert map_dnp3_quality(_ONLINE | _BIT5, _G3_DOUBLE_BIT) == "good"


# ---- G1 binary: 0x20 CHATTER_FILTER, 0x40 RESERVED, 0x80 STATE -----------


def test_binary_chatter_filter_reference_err_sanilmaz() -> None:
    """G1'de 0x20 OVERRANGE DEGIL, CHATTER_FILTER'dir."""
    assert map_dnp3_quality(_ONLINE | _BIT5, _G1_BINARY) == "good"


def test_binary_durum_biti_kaliteyi_bozmaz() -> None:
    """G1'de 0x80 STATE — kapali bir kontak (deger=1) `good` kalmali."""
    assert map_dnp3_quality(_ONLINE | _BIT7, _G1_BINARY) == "good"


def test_binary_reserved_biti_reference_err_sanilmaz() -> None:
    """G1'de 0x40 RESERVED'dir; REFERENCE_ERR olarak decode EDILMEMELI."""
    assert map_dnp3_quality(_ONLINE | _BIT6, _G1_BINARY) == "good"


# ---- G10 binary output status: 0x20/0x40 RESERVED, 0x80 STATE ------------


@pytest.mark.parametrize("bit", [_BIT5, _BIT6, _BIT7])
def test_binary_output_reserved_ve_state_bitleri_kaliteyi_bozmaz(bit: int) -> None:
    """G10'da 0x20/0x40 RESERVED1/RESERVED2, 0x80 STATE — analog gibi okunmamali."""
    assert map_dnp3_quality(_ONLINE | bit, _G10_BINARY_OUT) == "good"


# ---- G20/G21 counter: 0x20 ROLLOVER, 0x40 DISCONTINUITY ------------------


@pytest.mark.parametrize("group", [_G20_COUNTER, _G21_FROZEN_COUNTER])
@pytest.mark.parametrize("bit", [_BIT5, _BIT6])
def test_counter_rollover_ve_discontinuity_analog_gibi_decode_edilmez(group: int, bit: int) -> None:
    """G20/G21'de 0x20=ROLLOVER, 0x40=DISCONTINUITY.

    Ikisi de sayacin O ANKI degerinin yanlis oldugunu SOYLEMEZ; yalnizca
    onceki degerle FARK almanin gecersiz oldugunu bildirir. `invalid`
    yapmak, 32-bit bir sayacin her normal rollover'inda olcumu alarm
    degerlendirmesinden dusururdu. Bilgi ham `dnp3_flags` byte'inda korunur.
    """
    assert map_dnp3_quality(_ONLINE | bit, group) == "good"


# ---- oncelik sirasi ------------------------------------------------------


def test_comm_lost_restart_ten_oncelikli() -> None:
    assert map_dnp3_quality(_ONLINE | _RESTART | _COMM_LOST, _G30_ANALOG) == "comm_lost"


def test_comm_lost_overrange_dan_oncelikli() -> None:
    """`comm_lost` backend'de CIHAZ seviyesine kilitli TEK kalitedir.

    Altina cekilirse (orn. `invalid` one alinsa) gercekten olu bir cihaz
    "online ama bir noktasi bozuk" gorunur — en kritik sinyal kaybolur.
    """
    assert map_dnp3_quality(_ONLINE | _COMM_LOST | _BIT5, _G30_ANALOG) == "comm_lost"


# ---- bayrak YOKKEN: tipe gore fail-safe ----------------------------------


@pytest.mark.parametrize("group", _KALITE_GRUPLARI)
def test_kalite_tasiyan_grupta_bayrak_yoksa_fail_safe_invalid(group: int) -> None:
    """Bayragi okunamamis bir olcum `good` SAYILMAZ.

    Bu tipler kalite byte'i TASIMAK ZORUNDA; bayrak yoksa bu bir binding
    anomalisidir. `good` demek, tam da kapatmaya calistigimiz desteksiz
    "bu deger saglam" iddiasi olurdu.
    """
    assert map_dnp3_quality(None, group) == "invalid"


def test_g110_string_bayraksiz_normaldir() -> None:
    """REGRESYON RISKI: G110 OctetString kalite byte'i TASIMAZ.

    `cache.set(_OBJECT_GROUP_STRING, ...)` bayrak vermeden yazar. Kor bir
    "flags is None -> invalid" kurali, bayrak yayina acildigi anda seri no /
    IMEI / firmware / IP noktalarinin TAMAMINI `invalid` yapardi.
    """
    assert map_dnp3_quality(None, _G110_STRING) == "good"


def test_g110_bayrak_gelse_bile_kalite_uretmez() -> None:
    """G110 icin bayrak yorumlanmaz — tip kalite byte'i tanimlamaz."""
    assert map_dnp3_quality(0x00, _G110_STRING) == "good"
    assert map_dnp3_quality(_ONLINE | _BIT6, _G110_STRING) == "good"


def test_bilinmeyen_grupta_bit5_bit6_invalid_uretmez() -> None:
    """Bilinmeyen grupta 5/6. bitlerin anlami bilinmez — kalite UYDURULMAZ.

    Ortak bitler yine degerlendirilir.
    """
    bilinmeyen = 99
    assert map_dnp3_quality(_ONLINE | _BIT5 | _BIT6, bilinmeyen) == "good"
    assert map_dnp3_quality(_ONLINE | _COMM_LOST, bilinmeyen) == "comm_lost"
    assert map_dnp3_quality(0x00, bilinmeyen) == "invalid"


def test_object_group_zorunlu_parametredir() -> None:
    """Tip-kor cagri SESSIZCE yanlis kalite uretirdi; imza bunu engellemeli."""
    with pytest.raises(TypeError):
        map_dnp3_quality(0x41)  # type: ignore[call-arg]


# ---- bit tablosu gercek kutuphaneye karsi pinlenir ------------------------


def test_bayrak_tablosu_kutuphaneyle_uyumlu() -> None:
    """Yukaridaki bit anlamlari GERCEK opendnp3 enum'lariyla dogrulanir.

    Bu test olmadan tablo bir yorum satirindan ibaret kalirdi; yadnp3
    yukseltmesi bit anlamlarini degistirirse burada kirilir.
    """
    opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 kurulu degil")

    beklenen = {
        "BinaryQuality": ("CHATTER_FILTER", "RESERVED", "STATE"),
        "DoubleBitBinaryQuality": ("CHATTER_FILTER", "STATE1", "STATE2"),
        "BinaryOutputStatusQuality": ("RESERVED1", "RESERVED2", "STATE"),
        "CounterQuality": ("ROLLOVER", "DISCONTINUITY", "RESERVED"),
        "FrozenCounterQuality": ("ROLLOVER", "DISCONTINUITY", "RESERVED"),
        "AnalogQuality": ("OVERRANGE", "REFERENCE_ERR", "RESERVED"),
        "AnalogOutputStatusQuality": ("OVERRANGE", "REFERENCE_ERR", "RESERVED"),
    }
    for enum_adi, (b5, b6, b7) in beklenen.items():
        enum = getattr(opendnp3, enum_adi)
        uyeler = enum.__members__
        # Ortak bitler her tipte AYNI olmali.
        assert uyeler["ONLINE"].value == _ONLINE
        assert uyeler["RESTART"].value == _RESTART
        assert uyeler["COMM_LOST"].value == _COMM_LOST
        assert uyeler["REMOTE_FORCED"].value == _REMOTE_FORCED
        assert uyeler["LOCAL_FORCED"].value == _LOCAL_FORCED
        # Tipe gore degisen bitler.
        assert uyeler[b5].value == _BIT5, f"{enum_adi}.{b5}"
        assert uyeler[b6].value == _BIT6, f"{enum_adi}.{b6}"
        assert uyeler[b7].value == _BIT7, f"{enum_adi}.{b7}"


def test_double_bit_durum_bitleri_kutuphaneyle_dogrulanir() -> None:
    """G3 pozisyonlarinin GERCEK bayrak byte'lari — testin dayanagi budur."""
    opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 kurulu degil")

    beklenen = {
        "INTERMEDIATE": _ONLINE,
        "DETERMINED_OFF": _ONLINE | _BIT6,
        "DETERMINED_ON": _ONLINE | _BIT7,
        "INDETERMINATE": _ONLINE | _BIT6 | _BIT7,
    }
    for ad, bayrak in beklenen.items():
        olcum = opendnp3.DoubleBitBinary(opendnp3.DoubleBit.__members__[ad])
        gercek = int(getattr(olcum.flags, "value", olcum.flags))
        assert gercek == bayrak, f"DoubleBit.{ad} bayragi degismis: {gercek:#04x}"
        # Ve hicbiri `invalid` uretmemeli.
        assert map_dnp3_quality(gercek, _G3_DOUBLE_BIT) == "good", ad


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
        # Oturum politikasi da imzanin parcasi (G-SMART-01): degisirse
        # master'in yeniden kurulmasi GEREKIR.
        _session_policy = staticmethod(Yadnp3TelemetryReader._session_policy)

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
    # SOGUK ACILIS FAIL-SAFE: olcum yoksa saat YAZILMAZ. Eskiden True idi ve
    # korumayi tam da en cok gerektigi anda (host acilisi, backend erisilemez)
    # devre disi birakiyordu. Ayrintili testler: test_clock_cold_boot.py
    assert g.is_safe_for_time_sync is False

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
