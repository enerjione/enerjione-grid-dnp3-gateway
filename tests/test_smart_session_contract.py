"""GATEWAY 1.12.0 — Smart Session RESMI SOZLESMESI (GS01..GS14).

BU DOSYA NEDEN AYRI
-------------------
`test_smart_session_policy.py` DAVRANISI test eder (durum makinesi, uyku,
izolasyon). Bu dosya ise **Grid B5 entegrasyonunun birebir dayanacagi
SOZLESMEYI** kilitler: alan adlari, kabul edilen degerler, sinir degerleri,
gecersiz girdide ne olacagi ve health payload'inin tam sekli.

Grid backend bu degerleri TAHMIN ETMEYECEK — buradaki testler sozlesmenin
tek dogruluk kaynagidir. Bir madde degisirse CI kirilir ve sozlesme
degisikligi BILINCLI olur.

Numaralandirma gorev tanimindaki GS01..GS14 ile birebir eslesir.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import pytest

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend import DeviceConfig, GatewayConfigError
from dnp3_gateway.backend.config_client import (
    _SMART_SILENCE_MAX_SEC,
    _SMART_SILENCE_MIN_SEC,
    SESSION_POLICIES,
    _parse_optional_smart_silence,
    _parse_session_policy,
)
from dnp3_gateway.backend.health_header import build_payload
from dnp3_gateway.config import Settings
from dnp3_gateway.health_server import _device_health_snapshot

from .conftest import make_device


def _cihaz_item(**kw: Any) -> dict[str, Any]:
    """Backend `devices[]` satiri."""
    ham: dict[str, Any] = {"code": "D1", "ip_address": "10.0.0.10", "dnp3_address": 1}
    ham.update(kw)
    return ham


def _etkin_politika(**kw: Any) -> str:
    """Adapter'in UYGULAYACAGI politika (tek choke point)."""
    return mod.Yadnp3TelemetryReader._session_policy(replace(make_device("D1"), **kw))


# ==========================================================================
# GS01 — session_policy eksikse "continuous"
# ==========================================================================


def test_gs01_eksik_session_policy_continuous() -> None:
    assert _parse_session_policy(_cihaz_item()) == "continuous"
    assert _parse_session_policy(_cihaz_item(session_policy=None)) == "continuous"
    assert _parse_session_policy(_cihaz_item(session_policy="")) == "continuous"
    # DeviceConfig varsayilani da ayni.
    assert DeviceConfig(code="X", name="X", ip_address="1.1.1.1").session_policy == "continuous"
    assert _etkin_politika() == "continuous"


# ==========================================================================
# GS02 + GS03 — continuous her iki uc tipiyle gecerli
# ==========================================================================


def test_gs02_continuous_listening_kabul() -> None:
    assert _parse_session_policy(_cihaz_item(session_policy="continuous")) == "continuous"
    assert _etkin_politika(session_policy="continuous", ip_endpoint_type="listening") == "continuous"


def test_gs03_continuous_initiating_kabul() -> None:
    item = _cihaz_item(session_policy="continuous", ip_endpoint_type="initiating")
    assert _parse_session_policy(item) == "continuous"
    assert (
        _etkin_politika(session_policy="continuous", ip_endpoint_type="initiating", master_ip_port=20100)
        == "continuous"
    )


# ==========================================================================
# GS04 + GS05 — smart HER IKI uc tipiyle gecerli (1.14.0'da genisletildi)
# ==========================================================================


def test_gs04_smart_initiating_kabul() -> None:
    item = _cihaz_item(session_policy="smart", ip_endpoint_type="initiating")
    assert _parse_session_policy(item) == "smart"
    assert (
        _etkin_politika(session_policy="smart", ip_endpoint_type="initiating", master_ip_port=20100)
        == "smart"
    )


def test_gs05_smart_listening_artik_korunur(caplog: pytest.LogCaptureFixture) -> None:
    """1.14.0: `smart` + `listening` GECERLIDIR ve DUSURULMEZ.

    1.13.0 sozlesmesi bu kombinasyonu `continuous`a dusuruyor ve ERROR
    logluyordu. Gerekce "Smart Mode'da baglantiyi cihaz baslatir" idi; bu
    UC TIPI ile MODU birbirine karistiriyordu. Sabit IP'li bir Horstmann
    Smart modda calisir: modemini kapatir, gateway ona baglanamaz ve DOGRU
    davranis uykuyu KABUL ETMEKTIR (`smart_idle`), surekli SYN gondermek
    degil.

    Dusurmenin somut zarari: cihaz `continuous` kosturuldugunda gateway her
    tarama araliginda frame gonderir, 15sn'lik hareketsizlik sayaci HIC
    dolmaz ve modem HICBIR ZAMAN kapanmaz.
    """
    with caplog.at_level("ERROR"):
        etkin = _etkin_politika(session_policy="smart", ip_endpoint_type="listening")
    assert etkin == "smart", "smart+listening hala dusuruluyor"
    assert not [r for r in caplog.records if "session_policy_endpoint_mismatch" in r.getMessage()]


def test_gs05b_parse_asamasinda_uyari_yok(caplog: pytest.LogCaptureFixture) -> None:
    """Gecerli bir kombinasyon icin config asamasinda uyari URETILMEZ.

    Operatoru dogru bir kurulum icin ERROR ile uyarmak, gercek arizalari
    gizleyen gurultudur.
    """
    with caplog.at_level("ERROR"):
        deger = _parse_session_policy(_cihaz_item(session_policy="smart"))
    assert deger == "smart"
    assert not [r for r in caplog.records if "config_session_policy_endpoint_mismatch" in r.getMessage()]


# ==========================================================================
# GS06 — smart_max_silence_sec=None sozlesmesi
# ==========================================================================


def test_gs06_none_denetim_kapali() -> None:
    """None = sessizlik denetimi KAPALI; cihaz suresiz `smart_idle` kalabilir."""
    assert _parse_optional_smart_silence(_cihaz_item()) is None
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec=None)) is None
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec="")) is None
    assert DeviceConfig(code="X", name="X", ip_address="1.1.1.1").smart_max_silence_sec is None


def test_gs06b_gateway_varsayilani_kapali() -> None:
    """Gateway'de GOMULU bir sure YOKTUR; env yedegi de varsayilan olarak KAPALI."""
    alan = Settings.model_fields["dnp3_smart_max_silence_sec"]
    assert alan.default == 0, "gateway varsayilani 0 (kapali) olmali"

    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state()  # env yedegi verilmedi
    assert r._smart_max_silence_sec is None
    assert r._smart_silence_limit(make_device("D1")) is None


def test_gs06c_oncelik_cihaz_sonra_env() -> None:
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(smart_max_silence_sec=7200)
    assert r._smart_silence_limit(make_device("D1")) == 7200, "env yedegi kullanilmali"
    cihaz = replace(make_device("D1"), smart_max_silence_sec=900)
    assert r._smart_silence_limit(cihaz) == 900, "cihaz bazli deger env'i EZMELI"


# ==========================================================================
# GS07 — sayisal sinirlar
# ==========================================================================


def test_gs07_sinir_degerleri_pinlenir() -> None:
    """Grid bu sinirlari BIREBIR uygulayacak."""
    assert _SMART_SILENCE_MIN_SEC == 60
    assert _SMART_SILENCE_MAX_SEC == 2_592_000  # 30 gun


@pytest.mark.parametrize("deger", [60, 61, 3600, 93600, 2_591_999, 2_592_000])
def test_gs07b_gecerli_araliktaki_degerler_kabul(deger: int) -> None:
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec=deger)) == deger


@pytest.mark.parametrize("deger", [0, 1, 59, 2_592_001, -1, -3600])
def test_gs07c_arali_disi_degerler_reddedilir(deger: int) -> None:
    """Aralik disi -> None (denetim kapali) + WARNING. Config DUSMEZ.

    `0` ve NEGATIF degerler de buraya duser: "0 = kapali" gibi ikinci bir
    anlam TANIMLI DEGILDIR — kapali demek icin alan GONDERILMEZ (None).
    """
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec=deger)) is None


@pytest.mark.parametrize("deger", ["abc", "12.5.6", {}, [], object()])
def test_gs07d_bozuk_tip_reddedilir(deger: Any) -> None:
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec=deger)) is None


def test_gs07e_sayisal_metin_ve_float_daraltilir() -> None:
    """`int()` semantigi: sayisal metin kabul, float TABANA yuvarlanir."""
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec="3600")) == 3600
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec=3600.9)) == 3600
    # Daraltma sonrasi aralik disi kalirsa yine reddedilir.
    assert _parse_optional_smart_silence(_cihaz_item(smart_max_silence_sec=59.9)) is None


# ==========================================================================
# GS08 — gecersiz session_policy
# ==========================================================================


@pytest.mark.parametrize("deger", ["smrt", "SMART_MODE", "boost", "1", "true", "otomatik"])
def test_gs08_gecersiz_session_policy_configu_dusurur(deger: str) -> None:
    """SESSIZCE VARSAYILANA DUSMEK YASAK.

    `"smrt"` sessizce `continuous`a duserse, Smart moda alinmasi gereken bir
    cihaz periyodik taranmaya devam eder; modemi hicbir zaman kapanmaz ve
    bunu kimse fark etmez. Config'in reddedilmesi GUVENLI taraftir: gateway
    son iyi config'iyle calisir ve /health hatayi acikca raporlar.
    """
    with pytest.raises(GatewayConfigError) as hata:
        _parse_session_policy(_cihaz_item(session_policy=deger))
    assert "session_policy" in str(hata.value)


def test_gs08b_kabul_edilen_degerler_kumesi() -> None:
    assert SESSION_POLICIES == frozenset({"continuous", "smart", "auto"})


def test_gs08c_buyuk_harf_ve_bosluk_normalize() -> None:
    assert _parse_session_policy(_cihaz_item(session_policy="  Continuous ")) == "continuous"
    assert (
        _parse_session_policy(_cihaz_item(session_policy=" SMART ", ip_endpoint_type="initiating")) == "smart"
    )


# ==========================================================================
# GS09 — smart_idle ASLA lost'a esitlenmez
# ==========================================================================


def test_gs09_smart_idle_lost_degildir() -> None:
    """Kapali modem bir haberlesme arizasi DEGILDIR."""
    cache = mod._DeviceCache()
    cache.set_session_policy("smart")
    cache.set_connected(True)
    cache.begin_recovery()
    cache.set(30, 0, 42.0)  # gecerli DNP3 kaniti
    assert cache.state() == "online"

    cache.set_connected(False)  # cihaz modemini kapatti
    assert cache.state() == "smart_idle"
    assert cache.state() != "lost"
    assert cache.is_smart_idle() is True


def test_gs09b_kanitsiz_oturum_lost_uretir() -> None:
    """Salt TCP baglantisi kanit DEGILDIR — ariza `smart_idle`de saklanmaz."""
    cache = mod._DeviceCache()
    cache.set_session_policy("smart")
    cache.set_connected(True)
    cache.begin_recovery()
    cache.set_connected(False)  # hicbir DNP3 kaniti gelmedi
    assert cache.state() == "lost"
    assert cache.is_smart_idle() is False


def test_gs09c_continuous_kapanma_lost_uretir() -> None:
    cache = mod._DeviceCache()
    cache.set_session_policy("continuous")
    cache.set_connected(True)
    cache.begin_recovery()
    cache.set(30, 0, 42.0)
    cache.set_connected(False)
    assert cache.state() == "lost", "continuous davranisi DEGISMEMELI"


# ==========================================================================
# GS10 — health payload TAM sozlesmesi
# ==========================================================================


def _health(state: str, **kw: Any) -> dict[str, dict[str, Any]]:
    ham = {"state": state, "session_policy": kw.pop("session_policy", "continuous")}
    ham.update(kw)
    return {"D1": ham}


class _SahteReader:
    def __init__(self, per_device: dict[str, dict[str, Any]]) -> None:
        self._pd = per_device

    def device_health(self) -> dict[str, dict[str, Any]]:
        return self._pd


def test_gs10_smart_idle_lost_sayacina_girmez() -> None:
    """URETIM HATASI ONLEMI.

    Grid'in staleness watchdog'u `devices_lost > 0 && devices_online == 0`
    kosulunda gateway'in TUM cihazlarini offline yapabiliyor. Uyuyan bir
    Smart filosu `lost` sayilsaydi, tam da saglikli oldugu anda tum saha
    offline gorunurdu.
    """
    ozet = _device_health_snapshot(_SahteReader(_health("smart_idle", session_policy="smart")), 1)
    assert ozet["lost"] == 0, "smart_idle `lost` sayacina eklenmis — WATCHDOG BUG'I"
    assert ozet["smart_idle"] == 1
    assert ozet["online"] == 0
    assert ozet["recovering"] == 0
    assert ozet["unknown"] == 0
    assert ozet["total"] == 1


def test_gs10b_smart_idle_states_haritasina_girmez() -> None:
    """`devices.states` YALNIZCA sorunlu cihazlari tasir.

    `smart_idle` saglikli bir durumdur; buraya girseydi Grid onu taniyamayan
    bir "sorun durumu" olarak gorurdu.
    """
    govde = build_payload(
        status="ok",
        device_summary={"total": 1, "online": 0, "lost": 0, "smart_idle": 1},
        per_device=_health("smart_idle", session_policy="smart"),
    )
    assert "states" not in govde["devices"], "smart_idle sorun durumu olarak raporlanmis"
    assert govde["devices"]["smart_idle"] == 1
    assert govde["devices"]["lost"] == 0


def test_gs10c_gercek_kopma_hala_raporlanir() -> None:
    """Sessizlik penceresi asilan cihaz `lost` olur ve NORMAL sekilde raporlanir."""
    govde = build_payload(
        status="degraded",
        device_summary={"total": 1, "online": 0, "lost": 1, "smart_idle": 0},
        per_device=_health("lost", session_policy="smart"),
    )
    assert govde["devices"]["states"] == {"D1": "lost"}
    assert govde["devices"]["lost"] == 1


def test_gs10d_cihaz_basina_health_alanlari() -> None:
    """Grid/operator teshis icin gereken alanlar (sartname madde 7)."""
    ozet = _device_health_snapshot(
        _SahteReader(
            _health(
                "smart_idle",
                session_policy="smart",
                connected=False,
                reachable=False,
                last_frame_epoch=1_755_600_000.0,
                evidence_age_sec=None,
            )
        ),
        1,
    )
    assert ozet["session_policies"] == {"continuous": 0, "smart": 1, "auto": 0}
    # `smart_lost`: smart politikali ama GERCEKTEN kopuk cihazlar.
    assert ozet["smart_lost"] == 0


def test_gs10e_smart_politikada_lost_ayrica_sayilir() -> None:
    ozet = _device_health_snapshot(_SahteReader(_health("lost", session_policy="smart")), 1)
    assert ozet["lost"] == 1
    assert ozet["smart_lost"] == 1, "smart cihazin GERCEK kopmasi ayrica gorunmeli"
    assert ozet["smart_idle"] == 0


def test_gs10f_continuous_ozet_sayimlari_degismedi() -> None:
    """Mevcut health sozlesmesi KIRILMADI."""
    ozet = _device_health_snapshot(_SahteReader(_health("online")), 1)
    for anahtar in ("total", "online", "recovering", "lost", "unknown"):
        assert anahtar in ozet, f"mevcut sayac `{anahtar}` kaybolmus"
    assert ozet["online"] == 1
    assert ozet["lost"] == 0


# ==========================================================================
# GS11 — continuous davranisi degismedi
# ==========================================================================


def test_gs11_continuous_master_tarama_ve_acilis_polli_kurar() -> None:
    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    cagrilar: dict[str, Any] = {"class_scan": 0}

    class _NativeMaster:
        def AddClassScan(self, *a, **k):  # noqa: N802
            cagrilar["class_scan"] += 1
            return object()

        def Enable(self):  # noqa: N802
            pass

    class _Kanal:
        def AddMaster(self, ad, soe, app, cfg):  # noqa: N802, ARG002
            cagrilar["app"] = app
            return _NativeMaster()

    class _Manager:
        def AddTCPClient(self, *a, **k):  # noqa: N802
            return _Kanal()

    mm = mod._ManagedMaster(
        _Manager(),
        device=make_device("RTU-1"),
        local_address=1,
        tcp_port=20000,
        scan_interval_sec=5,
        baseline_interval_sec=30,
        session_policy="continuous",
    )
    assert cagrilar["class_scan"] == 2, "event + integrity taramasi KURULMALI"
    assert cagrilar["app"].AssignClassDuringStartup() is True
    assert mm.session_policy == "continuous"


# ==========================================================================
# GS12 + GS13 — geriye donuk uyum
# ==========================================================================


@dataclass(frozen=True)
class DeviceConfigV1114:
    """1.11.4 `DeviceConfig` alan kumesi — BIREBIR (60a9b64 baseline).

    Smart alanlari YOKTUR. Amac: yeni backend'in gonderdigi ek alanlarin eski
    gateway'i DUSURMEDIGINI kilitlemek.
    """

    code: str
    name: str
    ip_address: str
    dnp3_address: int = 1
    dnp3_tcp_port: int | None = None
    master_address: int | None = None
    ip_endpoint_type: str = "listening"
    master_ip_port: int | None = None
    poll_interval_sec: int = 2
    timeout_ms: int = 3000
    retry_count: int = 2
    signal_profile: str = "default"


def test_gs12_eski_gateway_yeni_alanlari_yok_sayar() -> None:
    """1.11.4 parser davranisi: BILINMEYEN ALANLAR YOK SAYILIR.

    `state.load_from_cache` ve backend parser'i alanlari
    `DeviceConfig.__dataclass_fields__`e gore SUZER. Yeni backend
    `session_policy` / `smart_max_silence_sec` gonderdiginde 1.11.4:
      * parse etmeyi SURDURUR (exception YOK),
      * bilinmeyen alanlari YOK SAYAR,
      * mevcut `continuous` davranisini korur (o surumde tek davranis budur).
    """
    yeni_backend_satiri = {
        "code": "D1",
        "name": "D1",
        "ip_address": "10.0.0.10",
        "dnp3_address": 4,
        "ip_endpoint_type": "initiating",
        "master_ip_port": 20100,
        # --- 1.12.0 ile eklenen alanlar ---
        "session_policy": "smart",
        "smart_max_silence_sec": 93600,
        # --- ileride eklenebilecek, henuz bilinmeyen alan ---
        "gelecekteki_alan": {"x": 1},
    }
    alanlar = {f.name for f in fields(DeviceConfigV1114)}
    eski = DeviceConfigV1114(**{k: v for k, v in yeni_backend_satiri.items() if k in alanlar})

    assert eski.code == "D1"
    assert eski.ip_endpoint_type == "initiating"
    assert eski.master_ip_port == 20100
    assert not hasattr(eski, "session_policy"), "1.11.4'te bu alan OLMAMALI"
    assert not hasattr(eski, "smart_max_silence_sec")


def test_gs12b_yeni_gateway_eski_configu_okur() -> None:
    """Smart alanlari OLMAYAN eski bir config bugunku gateway'de sorunsuz."""
    eski_satir = {"code": "D1", "ip_address": "10.0.0.10", "dnp3_address": 4}
    assert _parse_session_policy(eski_satir) == "continuous"
    assert _parse_optional_smart_silence(eski_satir) is None


def test_gs13_bilinmeyen_alanlar_tolere_edilir() -> None:
    """Disk onbellegi yolu: tanimadigi alanlar SESSIZCE suzulur, patlamaz."""
    ham = {
        "code": "D1",
        "name": "D1",
        "ip_address": "10.0.0.10",
        "session_policy": "smart",
        "smart_max_silence_sec": 93600,
        "gelecekteki_alan": 123,
        "baska_bilinmeyen": None,
    }
    d = DeviceConfig(**{k: v for k, v in ham.items() if k in DeviceConfig.__dataclass_fields__})
    assert d.session_policy == "smart"
    assert d.smart_max_silence_sec == 93600
    assert not hasattr(d, "gelecekteki_alan")


def test_gs13b_backend_payloadinda_bilinmeyen_alan_configu_dusurmez() -> None:
    """Uctan uca: tanimadigimiz bir alan config parse'ini KIRMAZ."""
    from dnp3_gateway.backend.config_client import _parse_gateway_config

    cfg = _parse_gateway_config(
        {
            "config_version": "v1",
            "devices": [
                _cihaz_item(
                    session_policy="smart",
                    ip_endpoint_type="initiating",
                    master_ip_port=20100,
                    smart_max_silence_sec=93600,
                    gelecekteki_alan={"derin": [1, 2]},
                )
            ],
            "signals": [],
            "gelecekteki_ust_alan": "x",
        },
        default_gateway_code="GW-001",
    )
    assert cfg.devices[0].session_policy == "smart"
    assert cfg.devices[0].smart_max_silence_sec == 93600


# ==========================================================================
# GS14 — komut duzlemi DEGISMEDI
# ==========================================================================


def test_gs14_komut_duzlemi_imzalari_degismedi() -> None:
    """Smart Session komut duzlemine DOKUNMADI.

    Yeni komut kuyrugu, wake-up komutu ya da replay cercevesi EKLENMEDI.
    """
    import inspect

    from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader

    imza = inspect.signature(Yadnp3TelemetryReader.operate_device)
    assert set(imza.parameters) == {
        "self",
        "device",
        "index",
        "op_type",
        "count",
        "on_time_ms",
        "off_time_ms",
        "timeout_sec",
        "mode",
    }, "operate_device imzasi degismis — komut duzlemi sozlesmesi kirildi"

    crob = inspect.signature(mod._ManagedMaster.operate_crob)
    assert set(crob.parameters) == {
        "self",
        "index",
        "op_type",
        "count",
        "on_time_ms",
        "off_time_ms",
        "timeout_sec",
        "mode",
    }


def test_gs14b_uyuyan_cihaza_fiziksel_komut_gonderilmez() -> None:
    """Uyuyan cihaz icin CROB = 0, SELECT = 0, OPERATE = 0.

    Sahte basari URETILMEZ, olmayan baglanti uzerinden komut DENENMEZ ve
    komut BEKLETILMEZ (kuyruk yok — bilincli).
    """
    cagrilar: dict[str, int] = {"direct": 0, "sbo": 0, "crob_build": 0}

    class _NativeMaster:
        def DirectOperate(self, *a, **k):  # noqa: N802
            cagrilar["direct"] += 1

        def SelectAndOperate(self, *a, **k):  # noqa: N802
            cagrilar["sbo"] += 1

    class _MM:
        """`_ManagedMaster` yuzeyi — uyuyan (smart_idle) cihaz."""

        def __init__(self) -> None:
            self.device = make_device("SN2-1")
            self.cache = mod._DeviceCache()
            self.cache.set_session_policy("smart")
            self._master = _NativeMaster()
            self.last_command_at = 0.0

        def ulasilabilir(self) -> bool:
            return False  # modem kapali

        def kanit_yasi(self) -> float:
            return -1.0

    mm = _MM()
    sonuc = mod._ManagedMaster.operate_crob(mm, index=7, op_type="latch_on")

    assert sonuc["ok"] is False
    assert sonuc["status"] == "offline", "sahte basari ya da baska bir durum uretilmis"
    assert cagrilar == {"direct": 0, "sbo": 0, "crob_build": 0}, (
        "uyuyan cihaza FIZIKSEL DNP3 komutu gonderildi"
    )


def test_gs14c_komut_kuyrugu_eklenmedi() -> None:
    """Bu surumde komut kuyruklama/replay BILINCLI olarak YOK."""
    from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader

    yasakli = ("queue_command", "pending_command", "replay", "wake_and_operate", "deferred_command")
    for ad in dir(Yadnp3TelemetryReader):
        for y in yasakli:
            assert y not in ad.lower(), f"beklenmeyen komut kuyrugu yuzeyi: {ad}"


# ==========================================================================
# Deployment contract — dosya ile kod ayni seyi soylemeli
# ==========================================================================


def test_deployment_contract_smart_session_alanlari_kodla_uyumlu() -> None:
    """Sozlesme dosyasi Grid'in okudugu yerdir; koddan SAPAMAZ."""
    import json

    kok = Path(__file__).resolve().parents[1]
    sozlesme = json.loads((kok / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))
    smart = sozlesme["smart_session"]

    assert smart["supported"] is True
    assert sorted(smart["session_policy_values"]) == sorted(SESSION_POLICIES)
    assert smart["device_config_fields"] == [
        "session_policy",
        "smart_max_silence_sec",
        "dial_in_interval_min",
        "smart_listen_reconnect_max_sec",
    ]
    assert smart["smart_max_silence_min"] == _SMART_SILENCE_MIN_SEC
    assert smart["smart_max_silence_max"] == _SMART_SILENCE_MAX_SEC
    assert smart["smart_idle_health_state"] == "smart_idle"
    assert smart["smart_idle_counted_as_lost"] is False
    assert smart["unknown_device_config_fields"] == "ignored"

    # 1.14.0: uc tipi kisiti KALDIRILDI. Anahtarin KENDISI de gitmeli —
    # dosyada kalirsa Grid onu hala gecerli bir kural sanip frontend'de
    # `initiating` zorlamasini surdururdu.
    assert "smart_requires_endpoint_type" not in smart
    assert "auto_requires_endpoint_type" not in smart


def test_deployment_contract_1_14_0_alanlari() -> None:
    """Dial-In / sonda / listening sozlesmesi dosyada TAM olmali."""
    import json

    kok = Path(__file__).resolve().parents[1]
    sozlesme = json.loads((kok / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))
    smart = sozlesme["smart_session"]

    assert smart["dial_in_interval_min_range"] == "60..1440"
    assert smart["dial_in_interval_min_default"] is None
    assert smart["smart_listen_reconnect_max_sec_range"] == "5..600"

    # EN KRITIK SOZLESME MADDELERI — bunlar bozulursa saha davranisi
    # sessizce tersine doner.
    assert smart["late_is_state"] is False
    assert smart["late_counted_as_lost"] is False
    assert smart["late_in_total"] is False
    assert smart["active_probes_affect_state"] is False

    # --- Tanilama sozlesmesi (PR #32 review madde 1 + 2) ---------------
    # Tanilama DNP3 okuma yolunu BLOKLAYAMAZ ve cihazin DNP3 portuna ham
    # soket ACILMAZ. Ikisi de sessizce geri gelebilecek turden hatalar.
    assert smart["diagnostics_block_read_path"] is False
    assert smart["raw_tcp_probe_removed"] is True
    assert smart["active_probes"] == ["icmp_probe"], "TCP sondasi sozlesmeye geri gelmis"
    assert smart["tcp_probe_status_source"] == "yadnp3_channel_state"
    assert smart["tcp_probe_status_values"] == ["open", "connecting", "unknown"]
    assert "bounded_async_executor" in smart["diagnostics_execution"]
    assert smart["diagnostics_probe_trigger"] == "device_late_only"

    # --- Kapanis semantigi (PR #32 final review) -----------------------
    # Tanilama iscileri master/kanal yikimindan ONCE ve TAMAMEN durur.
    # Bu bir siralama tercihi degil, DOGRULUK SARTIDIR.
    assert smart["diagnostics_shutdown_before_master_teardown"] is True
    assert smart["diagnostics_shutdown_semantics"].startswith("best_effort_cancel_then_join")
    # Isciler cikmazsa yikim DEVAM ETMEZ. "Logla ve devam et", ihlal edilen
    # degismezi bir log satirina indirger ve use-after-free yarisini KABUL
    # ederdi.
    assert smart["diagnostics_shutdown_failure_behavior"] == "fail_closed_abort_teardown"
    assert smart["diagnostics_counters"] == [
        "dropped_total",
        "cancelled_total",
        "completed_total",
    ]

    # --- Saha kabulu trafik beklentisi (review madde 3) ----------------
    # "0 paket" olcutu `listening` icin YANLISTIR: uykuda SYN denemeleri
    # BEKLENIR. Sifir olmasi gereken sey DNP3 UYGULAMA YUKUDUR.
    assert smart["sleep_traffic_pass_criterion"] == "dnp3_application_payload_packets == 0"

    # --- Tam tamsayi ayristirmasi (review madde 4) ---------------------
    assert smart["optional_int_parsing"].startswith("exact_integer")

    assert "late" in smart["health_device_counters"]
    for alan in (
        "dial_in_interval_min",
        "next_expected_report_epoch",
        "report_overdue_sec",
        "report_late",
        "ip_probe_status",
        "tcp_probe_status",
        "last_probe_epoch",
    ):
        assert alan in smart["health_device_fields_added_1_14_0"]

    assert sozlesme["gateway_release"] == (kok / "VERSION").read_text(encoding="utf-8").strip()


def test_health_ciktisi_sozlesmedeki_1_14_0_alanlarini_tasir() -> None:
    """Sozlesme dosyasi ile GERCEK `/health` ciktisi ayni seyi soylemeli.

    Dosyaya alan yazip kodda uretmemek, Grid tarafinda sessizce `None`
    okunan bir alan demektir — en pahali entegrasyon hatasi turu.
    """
    import json

    kok = Path(__file__).resolve().parents[1]
    sozlesme = json.loads((kok / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))

    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state()
    r._scan_interval_sec = 5
    r._baseline_interval_sec = 30
    r._default_dnp3_tcp_port = 20000

    # Taklit `_OturumDurumu`dan tureyen TEK KAYNAKtan gelir; elle alan
    # kopyalamak bu testi gercekten olmayan bir yuzeye karsi yesil yapardi.
    from .test_smart_session_policy import SahteMaster

    mm = SahteMaster(make_device("D1"), session_policy="smart")
    r._masters["D1"] = mm
    saglik = r.device_health()["D1"]

    for alan in sozlesme["smart_session"]["health_device_fields_added_1_14_0"]:
        assert alan in saglik, f"sozlesmede var, /health ciktisinda YOK: {alan}"
