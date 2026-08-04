"""Class 1/2/3 event scan araligi poll araligindan AYRI ayarlanabilmeli.

SAHADA OLCULDU (401 cihaz, 2026-08-04)
--------------------------------------
`scan_interval_sec` dogrudan `default_poll_interval_sec`e baglanmisti. Ama
bu iki sure FARKLI isler:

  * poll interval -> gateway'in cache'ten okuyup YAYINLAMA turu
  * scan interval -> cihaza "yeni olayin var mi" diye SORMA sikligi

scan=1s ile 401 cihaz saniyede **401 DNP3 istegi** uretiyordu. Her istek bir
TCP round-trip + cerceve cozumleme + C++->Python callback zinciri demek; bu
yuzden gateway CPU'su cihaz sayisiyla dogru orantili artiyordu (mesaj
sayisiyla DEGIL — JSON serilestirme olculdu, toplam yalnizca %2.3 idi).

Cihazlar unsolicited modda calisir (disableUnsolOnStartup=False), yani
degisiklikleri kendiliginden gonderir; scan yalnizca yedek mekanizmadir.

Varsayilan 0 = eski davranis; mevcut kurulumlar etkilenmez.
"""

from __future__ import annotations

import inspect

from dnp3_gateway.adapters import factory
from dnp3_gateway.config import Settings


def _ayar(**kw) -> Settings:
    temel = {
        "gateway_code": "GW-T",
        "gateway_token": "t" * 32,
        "backend_api_url": "http://127.0.0.1",
    }
    temel.update(kw)
    return Settings(**temel)


def _etkin_scan(s: Settings) -> int:
    """factory'nin kullandigi ifadenin birebir ayni si."""
    return s.dnp3_event_scan_interval_sec or s.default_poll_interval_sec


def test_varsayilan_eski_davranisi_korur() -> None:
    """Ayar verilmezse scan = poll interval (geriye uyum)."""
    s = _ayar(default_poll_interval_sec=1)
    assert s.dnp3_event_scan_interval_sec == 0
    assert _etkin_scan(s) == 1


def test_ayar_verilince_poll_aralikindan_bagimsizlasir() -> None:
    """Asil kazanc: yayin turu 1sn kalirken DNP3 sorgusu seyrelir."""
    s = _ayar(default_poll_interval_sec=1, dnp3_event_scan_interval_sec=5)
    assert _etkin_scan(s) == 5
    # Yayin turu DEGISMEMELI
    assert s.default_poll_interval_sec == 1


def test_makul_araliga_zorlanir() -> None:
    """Yanlis girilmis deger sessizce kabul edilmemeli."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _ayar(dnp3_event_scan_interval_sec=-1)
    with pytest.raises(ValidationError):
        _ayar(dnp3_event_scan_interval_sec=99999)


def test_factory_ayri_ayari_kullanir() -> None:
    """REGRESYON: factory poll interval'i gecirmeye devam ederse ayar olur da ise yaramaz."""
    kaynak = inspect.getsource(factory.build_adapter)
    assert "dnp3_event_scan_interval_sec" in kaynak, (
        "factory yeni ayari okumuyor — scan hala poll interval'e bagli kalir"
    )
    assert "scan_interval_sec=event_scan_sec" in kaynak, "factory hesaplanan degeri adapter'a gecirmiyor"


def test_io_thread_sayisi_disariya_acik() -> None:
    """Log'da 'auto' yerine GERCEK sayi yazilabilmeli (operator dogrulayabilsin)."""
    import pytest

    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader

    r = Yadnp3TelemetryReader(local_address=1, default_dnp3_tcp_port=20000, device_count_hint=500)
    try:
        assert r.io_thread_count == 20
    finally:
        r.close()


def test_log_satiri_gercek_degerleri_yazar() -> None:
    """`manager_threads=auto` ve `scan=poll` yaniltiyordu; ikisi de duzeltildi."""
    kaynak = inspect.getsource(factory.build_adapter)
    assert "io_threads=%s" in kaynak
    assert "okuyucu.io_thread_count" in kaynak, "log hala gercek thread sayisini yazmiyor"
