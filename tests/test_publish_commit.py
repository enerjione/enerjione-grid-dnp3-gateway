"""Yayin-onayli (commit-after-publish) akis testleri — kalici veri kaybi korumasi.

Kapatilan uc kalici kayip:

1. **TOCTOU yarisi.** `read_device` degeri okuyup dirty bayragini YAYINDAN ONCE
   temizliyordu. Okuma ile temizleme arasinda DNP3 IO thread'i yeni bir olcum
   yazarsa, poller ESKI degeri yayinlayip YENI degerin bayragini siliyordu.
   Deger bir daha DEGISENE kadar yayinlanmiyordu; periyodik integrity poll de
   ayni degeri yazdigi icin "degismedi" sayilip durumu duzeltmiyordu.
   Somut sonuc: acilan bir kesici SCADA'da saatlerce kapali gorunuyordu.

2. **Yayin basarisiz olsa bile bayrak temizdi.** Outbox dolarsa mesaj ne
   broker'a ne diske gidiyordu; deger "yayinlanmis" sayildigi icin telafi
   edilemiyordu.

3. **refresh-all no-op idi.** Delta-only yayinin TEK telafi mekanizmasi,
   degeri degismeyen sinyaller icin hicbir mesaj uretmiyordu.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dnp3_gateway.adapters.base import SignalReading, TelemetryReader
from dnp3_gateway.adapters.dnp3_yadnp3_master import _DeviceCache
from dnp3_gateway.backend import DeviceConfig, SignalConfig
from dnp3_gateway.messaging import OutboxFullError
from dnp3_gateway.poller import poll_device

_G = 30  # analog input


def _signal(key: str, index: int) -> SignalConfig:
    return SignalConfig(
        key=key,
        label=key,
        unit=None,
        source="master",
        dnp3_class="Class 1",
        data_type="analog",
        dnp3_object_group=_G,
        dnp3_index=index,
        scale=1.0,
        offset=0.0,
        supports_alarm=False,
    )


def _device(code: str = "DEV-1") -> DeviceConfig:
    return DeviceConfig(code=code, name=code, ip_address="192.168.10.5", dnp3_address=1)


# --------------------------------------------------------------------------
# _DeviceCache: atomik okuma + surumlu onay
# --------------------------------------------------------------------------


def test_peek_if_dirty_bayragi_temizlemez() -> None:
    c = _DeviceCache()
    c.set(_G, 1, 5.0)
    assert c.peek_if_dirty(_G, 1) is not None
    # Ikinci peek hala dirty gormeli — okuma bayragi tuketmez
    assert c.peek_if_dirty(_G, 1) is not None
    assert c.dirty_count() == 1


def test_commit_published_bayragi_temizler() -> None:
    c = _DeviceCache()
    c.set(_G, 1, 5.0)
    raw, _s, version = c.peek_if_dirty(_G, 1)
    assert raw == 5.0
    assert c.commit_published([(_G, 1, version)]) == 1
    assert c.peek_if_dirty(_G, 1) is None
    assert c.dirty_count() == 0


def test_okuma_ile_onay_arasinda_gelen_deger_kaybolmaz() -> None:
    """REGRESYON (TOCTOU): eski kod bu senaryoda yeni degeri kalici kaybediyordu."""
    c = _DeviceCache()
    c.set(_G, 1, 0.0)                       # kesici KAPALI
    raw, _s, version = c.peek_if_dirty(_G, 1)
    assert raw == 0.0

    # Tam bu sirada cihaz ACILIYOR — DNP3 IO thread'i yeni degeri yaziyor
    c.set(_G, 1, 1.0)

    # Poller eski degeri (0.0) yayinladi ve onaylamaya calisiyor
    cleared = c.commit_published([(_G, 1, version)])
    assert cleared == 0, "surum degismisken bayrak temizlenmemeli"

    # Yeni deger HALA yayin bekliyor
    taken = c.peek_if_dirty(_G, 1)
    assert taken is not None
    assert taken[0] == 1.0, "kesicinin ACIK degeri bir sonraki cycle'da yayinlanmali"


def test_ayni_deger_tekrar_yazilirsa_surum_ilerlemez() -> None:
    """Class 0 baseline scan ayni degeri yazdiginda gereksiz yayin olmamali."""
    c = _DeviceCache()
    c.set(_G, 1, 5.0)
    _r, _s, v1 = c.peek_if_dirty(_G, 1)
    c.commit_published([(_G, 1, v1)])
    c.set(_G, 1, 5.0)  # ayni deger
    assert c.peek_if_dirty(_G, 1) is None


def test_mark_all_dirty_surumleri_ilerletir() -> None:
    """Ucus halindeki bir onay, recovery yeniden-yayinini iptal etmemeli."""
    c = _DeviceCache()
    c.set(_G, 1, 5.0)
    _r, _s, version = c.peek_if_dirty(_G, 1)

    # refresh-all / recovery: her sey yeniden yayinlanacak
    assert c.mark_all_dirty() == 1

    # Onceki cycle'in gec kalan onayi gelirse bayragi SILMEMELI
    assert c.commit_published([(_G, 1, version)]) == 0
    assert c.peek_if_dirty(_G, 1) is not None


# --------------------------------------------------------------------------
# poller: onay yalnizca kalicilastiktan sonra
# --------------------------------------------------------------------------


@dataclass
class _FakeReader(TelemetryReader):
    """read_device sabit okuma doner; commit_published cagrilarini kaydeder."""

    readings: list[SignalReading]
    committed: list[list[SignalReading]]

    def read_device(self, *, device, signals):  # noqa: ARG002
        return list(self.readings)

    def operate_device(self, **_kwargs):  # noqa: ANN003
        return {"ok": False, "status": "unsupported"}

    def commit_published(self, *, device, readings):  # noqa: ARG002
        self.committed.append(list(readings))
        return len(readings)


class _OkPublisher:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def publish_batch(self, items):
        self.batches.append(list(items))


class _OutboxFullPublisher:
    def publish_batch(self, items):  # noqa: ARG002
        raise OutboxFullError("outbox dolu")


def _readings() -> list[SignalReading]:
    return [
        SignalReading(
            signal_key="a",
            source="master",
            data_type="analog",
            raw_value=1.0,
            scaled_value=1.0,
            quality="good",
            read_token=(_G, 1, 7),
        )
    ]


def test_basarili_yayin_onaylanir() -> None:
    reader = _FakeReader(readings=_readings(), committed=[])
    pub = _OkPublisher()
    n = poll_device(
        gateway_code="GW-001",
        device=_device(),
        signals=[_signal("a", 1)],
        reader=reader,
        publisher=pub,
    )
    assert n == 1
    assert len(pub.batches) == 1
    assert len(reader.committed) == 1, "kalicilasan yayin onaylanmali"


def test_outbox_dolu_ise_onay_verilmez() -> None:
    """REGRESYON: eskiden deger yayinlanmadan 'yayinlandi' sayiliyordu."""
    reader = _FakeReader(readings=_readings(), committed=[])
    with pytest.raises(OutboxFullError):
        poll_device(
            gateway_code="GW-001",
            device=_device(),
            signals=[_signal("a", 1)],
            reader=reader,
            publisher=_OutboxFullPublisher(),
        )
    assert reader.committed == [], (
        "kalicilastirilamayan olcum onaylanmamali; aksi halde deger kalici kaybolur"
    )


def test_no_change_okumalar_yayinlanmaz_ve_onaylanmaz() -> None:
    reader = _FakeReader(
        readings=[
            SignalReading(
                signal_key="a",
                source="master",
                data_type="analog",
                raw_value=0.0,
                scaled_value=0.0,
                quality="no_change",
            )
        ],
        committed=[],
    )
    pub = _OkPublisher()
    assert poll_device(
        gateway_code="GW-001",
        device=_device(),
        signals=[_signal("a", 1)],
        reader=reader,
        publisher=pub,
    ) == 0
    assert pub.batches == []
    assert reader.committed == []
