"""B3 — cihaz modeline gore sinyal seti.

SORUN
-----
Poller TEK bir global sinyal listesi kullaniyordu (`state.signals()`) ve onu
TUM cihazlara ayni sekilde uyguluyordu. Cihaz modeli tek oldugu surece bu
TESADUFEN dogru calisir.

Ikinci bir DNP3 modeli eklendigi anda bozulur: ayni (object_group, index)
cifti iki modelde FARKLI buyuklugu gosterir. Gateway hangi cihaz icin hangi
seti kullanacagini bilmedigi icin okudugu degeri YANLIS `signal_key` ile
yayinlar. Hata SESSIZDIR — telemetri akar, deger makul gorunur, ama backend'de
esik alarmi baska bir buyuklugun uzerinden calisir.

Ustelik `DeviceConfig.signal_profile` alani ZATEN vardi ve dokumantasyonu
"backend bu profile gore filtreler" diyordu; ne backend filtreliyordu ne de
gateway alani kullaniyordu.

KAPSAM: bu gateway yalnizca DNP3 konusur. Baska protokoller (or. Modbus
konusan Smart Navigator 1.0) AYRI bir gateway ile calisir. Yani buradaki
"profil" protokol ayrimi degil, ayni protokol icindeki MODEL ayrimidir.
"""

from __future__ import annotations

import json

from dnp3_gateway.backend import DeviceConfig, GatewayConfig, SignalConfig
from dnp3_gateway.backend.config_client import _parse_gateway_config
from dnp3_gateway.adapters import TelemetryReader
from dnp3_gateway.poller import run_poll_cycle
from dnp3_gateway.state import GatewayState

from .conftest import make_device, make_gateway_config, make_signal

MODEL_A = "horstmann_sn_2_0"
MODEL_B = "acme_rtu_9000"   # ayni protokol (DNP3), BASKA model


def _cihaz(code: str, profil: str) -> DeviceConfig:
    d = make_device(code)
    return DeviceConfig(**{**d.__dict__, "signal_profile": profil})


def _config(*, devices, signals, by_profile) -> GatewayConfig:
    temel = make_gateway_config(devices=devices, signals=signals)
    return GatewayConfig(**{**temel.__dict__, "signals_by_profile": by_profile})


# ------------------------------------------------------- cihaz basina secim


def test_her_cihaz_KENDI_modelinin_setini_alir():
    """B3'un ozu."""
    a_sinyal = make_signal("master.current", object_group=30, index=0)
    b_sinyal = make_signal("acme.oil_temp", object_group=30, index=0)  # AYNI adres

    state = GatewayState()
    state.update(
        _config(
            devices=[_cihaz("DEV-A", MODEL_A), _cihaz("DEV-B", MODEL_B)],
            signals=[a_sinyal, b_sinyal],
            by_profile={MODEL_A: [a_sinyal], MODEL_B: [b_sinyal]},
        )
    )

    a_keys = [s.key for s in state.signals_for(_cihaz("DEV-A", MODEL_A))]
    b_keys = [s.key for s in state.signals_for(_cihaz("DEV-B", MODEL_B))]
    assert a_keys == ["master.current"]
    assert b_keys == ["acme.oil_temp"]


def test_ESKI_backend_duz_listeye_duser():
    """`signals_by_profile` alani yoksa davranis AYNEN korunur."""
    sinyal = make_signal("master.current")
    state = GatewayState()
    state.update(make_gateway_config(devices=[_cihaz("DEV-A", MODEL_A)], signals=[sinyal]))

    assert [s.key for s in state.signals_for(_cihaz("DEV-A", MODEL_A))] == ["master.current"]


def test_BOS_profil_duz_listeye_DUSMEZ():
    """Kasitli tasarim karari.

    Backend bos liste gonderdiginde "bu model icin sinyal tanimli degil" diyor.
    Duz listeye dusmek KOMSU DNP3 modelinin adreslerini yoklamak olurdu:
    makul gorunen ama baska bir buyukluge ait degerler yanlis anahtarla
    yayinlanir. Sessiz yanlis veri, gorunur eksik veriden daha kotudur.
    """
    a_sinyal = make_signal("master.current")
    state = GatewayState()
    state.update(
        _config(
            devices=[_cihaz("DEV-A", MODEL_A), _cihaz("DEV-B", MODEL_B)],
            signals=[a_sinyal],
            by_profile={MODEL_A: [a_sinyal], MODEL_B: []},
        )
    )

    assert state.signals_for(_cihaz("DEV-B", MODEL_B)) == [], (
        "bos profil duz listeye dusmus — komsu modelin adresleri yoklanir"
    )


def test_BILINMEYEN_profil_duz_listeye_duser():
    """Anahtar hic yoksa (surum uyumsuzlugu) cihaz KARANLIGA dusmemeli.

    Bos liste ile bu durum FARKLIDIR: orada backend bilerek "sinyal yok"
    diyor; burada anahtari hic gormedik, yani bilgi eksik.
    """
    a_sinyal = make_signal("master.current")
    state = GatewayState()
    state.update(
        _config(
            devices=[_cihaz("DEV-A", MODEL_A)],
            signals=[a_sinyal],
            by_profile={MODEL_A: [a_sinyal]},
        )
    )
    yabanci = _cihaz("DEV-Z", "hic_bilinmeyen")
    assert [s.key for s in state.signals_for(yabanci)] == ["master.current"]


# --------------------------------------------------- gercek poll davranisi


def test_poll_cycle_her_cihaza_KENDI_setini_verir():
    """En onemli test: davranisin ta kendisi.

    Yukaridaki testler secimi dogruluyor; bu test poller'in o secimi gercekten
    KULLANDIGINI dogrular. Poller eskiden tek global listeyi tum cihazlara
    veriyordu — bu test o regresyonu kirmizi yapar.
    """
    a_sinyal = make_signal("master.current", object_group=30, index=0)
    b_sinyal = make_signal("acme.oil_temp", object_group=30, index=0)

    state = GatewayState()
    state.update(
        _config(
            devices=[_cihaz("DEV-A", MODEL_A), _cihaz("DEV-B", MODEL_B)],
            signals=[a_sinyal, b_sinyal],
            by_profile={MODEL_A: [a_sinyal], MODEL_B: [b_sinyal]},
        )
    )

    gorulen: dict[str, list[str]] = {}

    class _KaydedenReader(TelemetryReader):
        def read_device(self, *, device, signals):  # type: ignore[no-untyped-def]
            gorulen[device.code] = [s.key for s in signals]
            return []

        def operate_device(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"ok": True}

    class _Publisher:
        def publish_batch(self, items):  # type: ignore[no-untyped-def]
            pass

        def publish(self, payload, **kwargs):  # type: ignore[no-untyped-def]
            pass

        def close(self) -> None:
            pass

    run_poll_cycle(
        gateway_code="GW-001",
        state=state,
        reader=_KaydedenReader(),
        publisher=_Publisher(),
        now_monotonic=1000.0,
        max_parallel=1,
    )

    assert gorulen == {
        "DEV-A": ["master.current"],
        "DEV-B": ["acme.oil_temp"],
    }, f"cihazlar yanlis sinyal setiyle yoklandi: {gorulen}"


# --------------------------------------------------------- config parse


def test_config_yanitindan_profiller_AYRISTIRILIR():
    payload = {
        "gateway_code": "GW-001",
        "gateway_name": "Test",
        "config_version": "v1",
        "is_active": True,
        "devices": [],
        "signals": [],
        "signals_by_profile": {
            MODEL_A: [
                {
                    "key": "master.current",
                    "label": "Akim",
                    "data_type": "analog",
                    "dnp3_object_group": 30,
                    "dnp3_index": 0,
                    "scale": 1.0,
                    "offset": 0.0,
                }
            ],
            MODEL_B: [],
        },
    }
    cfg = _parse_gateway_config(payload, default_gateway_code="GW-001")
    assert [s.key for s in cfg.signals_by_profile[MODEL_A]] == ["master.current"]
    assert cfg.signals_by_profile[MODEL_B] == [], "bos profil yutulmus"


def test_profil_sinyalleri_AYNI_dogrulamadan_gecer():
    """inf/nan gibi bozuk degerler duz listede reddediliyorsa profilde de."""
    payload = {
        "gateway_code": "GW-001",
        "config_version": "v1",
        "is_active": True,
        "devices": [],
        "signals": [],
        "signals_by_profile": {
            MODEL_A: [
                {
                    "key": "master.current",
                    "data_type": "analog",
                    "dnp3_object_group": 30,
                    "dnp3_index": 0,
                    "scale": "Infinity",   # reddedilmeli
                    "offset": "nan",       # reddedilmeli
                }
            ]
        },
    }
    cfg = _parse_gateway_config(payload, default_gateway_code="GW-001")
    sinyal = cfg.signals_by_profile[MODEL_A][0]
    assert sinyal.scale == 1.0
    assert sinyal.offset == 0.0


def test_bozuk_signals_by_profile_config_i_DUSURMEZ():
    payload = {
        "gateway_code": "GW-001",
        "config_version": "v1",
        "is_active": True,
        "devices": [],
        "signals": [{"key": "master.current", "data_type": "analog"}],
        "signals_by_profile": "bu bir sozluk degil",
    }
    cfg = _parse_gateway_config(payload, default_gateway_code="GW-001")
    assert cfg.signals_by_profile == {}
    assert [s.key for s in cfg.signals] == ["master.current"]


# ------------------------------------------------------------ disk cache


def test_disk_cache_profilleri_KORUR(tmp_path):
    """Backend erisilemezken restart eden gateway profilleri kaybetmemeli.

    Kaybetseydi tum cihazlari duz listeyle yoklardi — cok modelli sahada tam
    olarak B3'un onledigi sessiz yanlis veri, hem de en kotu anda.
    """
    a_sinyal = make_signal("master.current")
    b_sinyal = make_signal("acme.oil_temp")
    cache = tmp_path / "config-cache.json"

    yazan = GatewayState(cache_path=cache)
    yazan.update(
        _config(
            devices=[_cihaz("DEV-A", MODEL_A), _cihaz("DEV-B", MODEL_B)],
            signals=[a_sinyal, b_sinyal],
            by_profile={MODEL_A: [a_sinyal], MODEL_B: [b_sinyal]},
        )
    )
    assert cache.exists(), "cache yazilmadi"
    assert "signals_by_profile" in json.loads(cache.read_text(encoding="utf-8"))

    okuyan = GatewayState(cache_path=cache)
    assert okuyan.load_from_cache() is True
    assert [s.key for s in okuyan.signals_for(_cihaz("DEV-B", MODEL_B))] == ["acme.oil_temp"]


def test_ESKI_cache_dosyasi_okunabilir(tmp_path):
    """Alan icermeyen eski cache -> duz listeye dusulur, patlamaz."""
    cache = tmp_path / "config-cache.json"
    cache.write_text(
        json.dumps(
            {
                "gateway_code": "GW-001",
                "config_version": "v1",
                "is_active": True,
                "devices": [],
                "signals": [
                    {
                        "key": "master.current",
                        "label": "Akim",
                        "unit": None,
                        "source": "master",
                        "dnp3_class": "Class 1",
                        "data_type": "analog",
                        "dnp3_object_group": 30,
                        "dnp3_index": 0,
                        "scale": 1.0,
                        "offset": 0.0,
                        "supports_alarm": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state = GatewayState(cache_path=cache)
    assert state.load_from_cache() is True
    assert [s.key for s in state.signals_for(_cihaz("DEV-A", MODEL_A))] == ["master.current"]


def test_yalnizca_profilde_sinyal_varsa_cycle_CALISIR():
    """Duz liste bos, profil dolu — cycle erken cikmamali.

    `has_any_signals` yalnizca duz listeye bakiyor olsaydi, profil bazli
    config'te gateway hicbir cihazi yoklamadan doner ve saha KARANLIK kalirdi.
    """
    a_sinyal = make_signal("master.current")
    state = GatewayState()
    state.update(
        _config(
            devices=[_cihaz("DEV-A", MODEL_A)],
            signals=[],                       # DUZ LISTE BOS
            by_profile={MODEL_A: [a_sinyal]},
        )
    )
    assert state.has_any_signals() is True
    assert [s.key for s in state.signals_for(_cihaz("DEV-A", MODEL_A))] == ["master.current"]
