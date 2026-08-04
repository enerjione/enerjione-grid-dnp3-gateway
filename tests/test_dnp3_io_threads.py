"""DNP3 IO thread sayisi cihaz olcegine gore belirlenmeli.

SAHADA OLCULDU (2026-08-04)
---------------------------
`Yadnp3TelemetryReader.__init__` icindeki yorum su heuristigi TARIF EDIYORDU:

    manager_threads=0 -> max(4, ceil(device_count_hint / 25))

...ama UYGULAMIYORDU; "device_count_hint constructor'da bilinmiyor" denip
sabit 4 aliniyordu. 400 cihazli sahada sonuc **100 cihaz/thread** oldu.

Her cihazin TCP I/O'su ve scheduler isi bu dort thread'e diziliyor. Sikisma
hicbir sayacta gorunmez: cihaz "online" kalir, yalnizca DNP3 yaniti gecikir
ve veri bayatlar — yani en zor teshis edilen ariza tiplerinden biri.

Ipucu artik `MAX_PARALLEL_DEVICES`ten geliyor (operatorun olcek beklentisi);
reader boot'ta, config gelmeden kuruldugu icin gercek cihaz sayisi bilinemez.
"""

from __future__ import annotations

import pytest

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader  # noqa: E402


def _reader(**kwargs) -> Yadnp3TelemetryReader:
    r = Yadnp3TelemetryReader(local_address=1, default_dnp3_tcp_port=20000, **kwargs)
    return r


@pytest.mark.parametrize(
    ("ipucu", "beklenen"),
    [
        (0, 4),  # ipucu yok -> guvenli taban
        (50, 4),  # kucuk kurulum -> taban
        (100, 4),  # 100/25 = 4
        (300, 12),
        (400, 16),
        (500, 20),  # hedef olcek
        (1000, 32),  # tavan
        (5000, 32),  # tavan asilmaz
    ],
)
def test_thread_sayisi_olcekle_artar(ipucu: int, beklenen: int) -> None:
    r = _reader(device_count_hint=ipucu)
    try:
        assert r._manager_threads == beklenen, (
            f"{ipucu} cihaz ipucunda {r._manager_threads} thread secildi, {beklenen} bekleniyordu"
        )
    finally:
        r.close()


def test_400_cihazda_artik_sabit_4_degil() -> None:
    """REGRESYON: sahadaki tam senaryo — 400 cihaz, sabit 4 thread."""
    r = _reader(device_count_hint=400)
    try:
        assert r._manager_threads > 4, (
            "400 cihazda hala sabit 4 IO thread seciliyor (100 cihaz/thread) — heuristik uygulanmamis"
        )
        assert r._manager_threads == 16
    finally:
        r.close()


def test_acik_ayar_ipucunu_ezer() -> None:
    """`DNP3_MANAGER_THREADS` set edilmisse operator kararina saygi duyulur."""
    r = _reader(manager_threads=7, device_count_hint=500)
    try:
        assert r._manager_threads == 7
    finally:
        r.close()


def test_acik_ayar_makul_araliga_sikistirilir() -> None:
    """Yanlis girilmis dev bir deger sistemi bogmamali."""
    r = _reader(manager_threads=9999, device_count_hint=0)
    try:
        assert r._manager_threads == 64
    finally:
        r.close()


def test_factory_olcek_ipucunu_gecirir() -> None:
    """Zincir dogrulamasi: ayardan adapter'a ipucu gercekten akiyor mu?

    `device_count_hint` factory'de baglanmazsa heuristik sessizce devre disi
    kalir ve sahada yine sabit 4 thread kosar.
    """
    import inspect

    from dnp3_gateway.adapters import factory

    kaynak = inspect.getsource(factory.build_adapter)
    assert "device_count_hint" in kaynak, (
        "factory adapter'a olcek ipucu gecirmiyor — heuristik devre disi kalir"
    )
    assert "max_parallel_devices" in kaynak
