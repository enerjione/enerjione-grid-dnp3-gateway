"""PR #30 review duzeltmeleri — B1..B4 sozlesme kilitleri.

Bu dosya, review'da yakalanan DORT dar hatanin geri gelmesini engeller.
Her biri sessiz bir uretim hatasiydi:

  B1  env sessizlik esigi `1..59` araligini SESSIZCE kabul ediyordu — 30
      saniyelik bir esik NORMAL bir Smart uykusunu bile kopuk ilan eder.
  B2  `smart_lost` sayaci uretiliyordu ama WIRE PAYLOAD'a tasinmiyordu;
      sozlesme onu ilan ettigi icin Grid hicbir zaman gelmeyecek bir alani
      beklerdi.
  B3  disk yazimi basarisiz olunca `_kirli` bayragi KAYBOLUYORDU: tek bir
      gecici disk hatasi kaydi kalici olarak dusururdu.
  B4  "null = disabled" ifadesi runtime ile CELISIYORDU (gercekte env
      yedegine dusuluyor).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend.config_client import _SMART_SILENCE_MAX_SEC, _SMART_SILENCE_MIN_SEC
from dnp3_gateway.backend.health_header import build_payload
from dnp3_gateway.config import Settings
from dnp3_gateway.health_server import _device_health_snapshot
from dnp3_gateway.session_state_store import SessionStateRecord, SessionStateStore

from .conftest import make_device

_KOK = Path(__file__).resolve().parents[1]


def _smart_sozlesme() -> dict[str, Any]:
    return json.loads((_KOK / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))[
        "smart_session"
    ]


class _SahteReader:
    def __init__(self, per_device: dict[str, dict[str, Any]]) -> None:
        self._pd = per_device

    def device_health(self) -> dict[str, dict[str, Any]]:
        return self._pd


# ==========================================================================
# B1 — env sessizlik esigi FAIL-CLOSED
# ==========================================================================


@pytest.mark.parametrize("deger", [0, 60, 61, 93600, 2_592_000])
def test_b1_env_gecerli_degerler_kabul(deger: int) -> None:
    """Kabul edilen kume: `0` (kapali) VEYA `60..2592000`."""
    s = Settings(dnp3_smart_max_silence_sec=deger, gateway_code="G", gateway_token="t")
    assert s.dnp3_smart_max_silence_sec == deger


@pytest.mark.parametrize("deger", [1, 2, 30, 59, 2_592_001, 10_000_000])
def test_b1_env_gecersiz_degerler_bootta_reddedilir(deger: int) -> None:
    """Gecersiz her deger BOOT'TA reddedilir (fail-closed)."""
    with pytest.raises(ValidationError):
        Settings(dnp3_smart_max_silence_sec=deger, gateway_code="G", gateway_token="t")


@pytest.mark.parametrize("deger", [1, 2, 30, 59])
def test_b1_yasak_bant_acik_mesajla_reddedilir(deger: int) -> None:
    """`1..59` SESSIZCE KABUL EDILEMEZ.

    `ge=0` tek basina bu araligi GECIRIRDI (pydantic'in `le=` kisiti yalnizca
    ust siniri korur). 30 saniyelik bir esik NORMAL bir Smart uykusunu bile
    kopuk ilan eder ve operator bunu ancak sahada fark ederdi; bu yuzden
    hata mesaji ayarin adini ve kabul edilen kumeyi ACIKCA soyler.
    """
    with pytest.raises(ValidationError) as hata:
        Settings(dnp3_smart_max_silence_sec=deger, gateway_code="G", gateway_token="t")
    mesaj = str(hata.value)
    assert "DNP3_SMART_MAX_SILENCE_SEC" in mesaj
    assert "0" in mesaj and "60" in mesaj


def test_b1_env_ve_cihaz_sinirlari_ayni() -> None:
    """Iki yol AYNI sinirlari uygulamali.

    Farkli olsalardi ayni deger bir yoldan gecip digerinden reddedilirdi.
    """
    from dnp3_gateway.config import _SMART_SILENCE_ENV_MAX, _SMART_SILENCE_ENV_MIN

    assert _SMART_SILENCE_ENV_MIN == _SMART_SILENCE_MIN_SEC == 60
    assert _SMART_SILENCE_ENV_MAX == _SMART_SILENCE_MAX_SEC == 2_592_000


# ==========================================================================
# B2 — smart_lost WIRE parity
# ==========================================================================


def test_b2_smart_lost_wire_payloada_tasinir() -> None:
    """`smart_lost` health_server'da uretiliyordu ama TELE GITMIYORDU."""
    govde = build_payload(
        status="degraded",
        device_summary={"total": 10, "online": 7, "lost": 2, "smart_idle": 1, "smart_lost": 2},
        per_device=None,
    )
    assert govde["devices"]["smart_lost"] == 2


def test_b2_smart_idle_states_disinda_kalmaya_devam_eder() -> None:
    """B2 duzeltmesi `smart_idle` davranisini BOZMAMALI."""
    govde = build_payload(
        status="ok",
        device_summary={"total": 2, "online": 0, "lost": 1, "smart_idle": 1, "smart_lost": 1},
        per_device={
            "IDLE-1": {"state": "smart_idle", "session_policy": "smart"},
            "KOPUK-1": {"state": "lost", "session_policy": "smart"},
        },
    )
    # Uyuyan cihaz sorun listesine GIRMEZ; gercekten kopuk olan GIRER.
    assert govde["devices"]["states"] == {"KOPUK-1": "lost"}
    assert govde["devices"]["smart_idle"] == 1
    assert govde["devices"]["smart_lost"] == 1


def test_b2_sozlesme_ile_wire_payload_paritesi() -> None:
    """Sozlesmedeki HER sayac gercekten tele gidebiliyor olmali.

    Sozlesme bir sayaci ilan edip runtime onu dusuruyorsa Grid hicbir zaman
    gelmeyecek bir alani bekler — bu PR'da yakalanan hata tam olarak buydu.
    """
    ilan_edilen = _smart_sozlesme()["health_device_counters"]
    ozet = {ad: i + 1 for i, ad in enumerate(ilan_edilen)}
    govde = build_payload(status="ok", device_summary=ozet, per_device=None)

    eksik = [ad for ad in ilan_edilen if ad not in govde["devices"]]
    assert not eksik, f"sozlesmede ilan edilen ama wire payload'a TASINMAYAN sayaclar: {eksik}"
    for ad in ilan_edilen:
        assert govde["devices"][ad] == ozet[ad], f"{ad} degeri bozuldu"


def test_b2_health_server_sozlesmedeki_sayaclari_uretir() -> None:
    """Diger yon: runtime'in urettigi ozet sozlesmeyi karsilamali."""
    ilan_edilen = set(_smart_sozlesme()["health_device_counters"])
    ozet = _device_health_snapshot(
        _SahteReader(
            {
                "A": {"state": "smart_idle", "session_policy": "smart"},
                "B": {"state": "lost", "session_policy": "smart"},
                "C": {"state": "online", "session_policy": "continuous"},
            }
        ),
        3,
    )
    eksik = [ad for ad in ilan_edilen if ad not in ozet]
    assert not eksik, f"health_server ozeti sozlesmedeki sayaclari uretmiyor: {eksik}"
    assert ozet["smart_idle"] == 1
    assert ozet["smart_lost"] == 1
    assert ozet["lost"] == 1
    assert ozet["online"] == 1


# ==========================================================================
# B3 — kalicilik yeniden deneme dayanikliligi
# ==========================================================================


def test_b3_basarisiz_yazim_sonrasi_kayit_kaybolmaz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk hatasi kaydi KALICI olarak dusurmemeli.

    Eski akis: snapshot al -> `_kirli=False` -> diske yaz. Yazim patlarsa
    bayrak temizlenmis olurdu ve o kayit BIR DAHA HIC yazilmazdi; restart'ta
    uyuyan filo sahte comm_lost uretirdi — yani bu dosyanin onlemek icin var
    oldugu seyin ta kendisi.
    """
    yol = tmp_path / "session_state.json"
    store = SessionStateStore(yol)
    kayit = SessionStateRecord(
        state="smart_idle",
        last_valid_contact_unix=1_755_600_000.0,
        smart_idle_since_unix=1_755_600_010.0,
    )
    store.record("SN2-1", kayit)

    # --- 1. flush: os.replace KONTROLLU olarak patlar ---
    import os as _os

    cagri = {"n": 0}
    gercek = _os.replace

    def _patlayan_replace(src, dst):
        cagri["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_os, "replace", _patlayan_replace)
    assert store.flush(force=True) is False, "basarisiz yazim True donmus"
    assert cagri["n"] == 1
    assert not yol.exists(), "dosya yazilmis gorunuyor"

    # --- YENI record YOK; yalnizca yeniden deneniyor ---
    monkeypatch.setattr(_os, "replace", gercek)
    assert store.flush(force=True) is True, "yeniden deneme yazmadi — kayit KAYBOLDU"

    # --- Dosyada kayit GERCEKTEN var ---
    yeniden = SessionStateStore(yol)
    assert yeniden.load() == 1
    geri = yeniden.get("SN2-1")
    assert geri is not None
    assert geri.state == "smart_idle"
    assert geri.smart_idle_since_unix == kayit.smart_idle_since_unix


def test_b3_basarisiz_yazim_istisna_sizdirmaz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetri HICBIR kosulda crash etmemeli."""
    import os as _os

    store = SessionStateStore(tmp_path / "s.json")
    store.record("A", SessionStateRecord(state="smart_idle"))

    def _patla(src, dst):
        raise RuntimeError("beklenmedik")

    monkeypatch.setattr(_os, "replace", _patla)
    assert store.flush(force=True) is False  # istisna DISARI SIZMAZ


def test_b3_araya_giren_record_bozulmaz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Yazim sirasinda gelen YENI kayit kaybolmamali."""
    import os as _os

    yol = tmp_path / "s.json"
    store = SessionStateStore(yol)
    store.record("A", SessionStateRecord(state="smart_idle"))

    def _patla(src, dst):
        # Yazim suruyorken baska bir thread yeni kayit yaziyor.
        store.record("B", SessionStateRecord(state="online"))
        raise OSError(28, "disk dolu")

    monkeypatch.setattr(_os, "replace", _patla)
    assert store.flush(force=True) is False
    monkeypatch.undo()

    assert store.flush(force=True) is True
    yeniden = SessionStateStore(yol)
    yeniden.load()
    assert yeniden.get("A") is not None, "ilk kayit kayboldu"
    assert yeniden.get("B") is not None, "yazim sirasinda gelen kayit kayboldu"


def test_b3_basarili_yazimdan_sonra_tekrar_yazilmaz(tmp_path: Path) -> None:
    """Kirli degilken flush no-op kalmali (gereksiz I/O yok)."""
    store = SessionStateStore(tmp_path / "s.json")
    store.record("A", SessionStateRecord(state="smart_idle"))
    assert store.flush(force=True) is True
    assert store.flush(force=True) is False


# ==========================================================================
# B4 — kanonik cozum sirasi
# ==========================================================================


def _reader(**kw: Any) -> Any:
    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state(**kw)
    return r


def test_b4_gecerli_cihaz_degeri_kazanir() -> None:
    r = _reader(smart_max_silence_sec=7200)
    assert r._smart_silence_limit(replace(make_device("D1"), smart_max_silence_sec=900)) == 900


def test_b4_null_cihaz_degeri_env_e_duser() -> None:
    """`null` = "cihaz seviyesinde ezme yok" — "kapali" DEMEK DEGIL."""
    r = _reader(smart_max_silence_sec=7200)
    assert r._smart_silence_limit(make_device("D1")) == 7200


def test_b4_env_yoksa_kapali() -> None:
    assert _reader()._smart_silence_limit(make_device("D1")) is None


@pytest.mark.parametrize("gecersiz", [0, -1, 30, 59, 2_592_001, "abc", object()])
def test_b4_gecersiz_cihaz_degeri_env_e_duser(gecersiz: Any) -> None:
    """GECERSIZ cihaz degeri: ezme YOK SAYILIR, env yedegine dusulur.

    ARALIK KONTROLU ADAPTER'DA DA SART: `DeviceConfig` DISK ONBELLEGINDEN de
    gelebilir ve o yol backend parser'ini ATLAR. Aksi halde onbellekten gelen
    30 saniyelik bir esik NORMAL uyuyan her cihazi kopuk ilan ederdi.
    """
    r = _reader(smart_max_silence_sec=7200)
    cihaz = replace(make_device("D1"), smart_max_silence_sec=gecersiz)
    assert r._smart_silence_limit(cihaz) == 7200, "gecersiz cihaz degeri env'i ezmis"


@pytest.mark.parametrize("gecersiz", [0, -1, 30, 2_592_001, "abc"])
def test_b4_gecersiz_cihaz_degeri_env_de_yoksa_kapali(gecersiz: Any) -> None:
    r = _reader()
    cihaz = replace(make_device("D1"), smart_max_silence_sec=gecersiz)
    assert r._smart_silence_limit(cihaz) is None


def test_b4_sozlesme_cozum_sirasini_acikca_yaziyor() -> None:
    """Grid sirayi sozlesmeden okuyabilmeli — koddan tahmin ETMEMELI."""
    smart = _smart_sozlesme()
    assert smart["smart_max_silence_resolution_order"] == [
        "valid_device_value(60..2592000)",
        "env:DNP3_SMART_MAX_SILENCE_SEC(0=disabled,60..2592000)",
        "disabled",
    ]
    assert smart["env_smart_max_silence_accepted"] == "0 | 60..2592000"
    assert "fail_closed" in smart["env_smart_max_silence_invalid_behavior"]
    assert "device_override_ignored" in smart["smart_max_silence_invalid_behavior"]
    # CELISKILI "null = disabled" ifadesi KALMAMALI.
    assert "ezme yok" in smart["smart_max_silence_default_semantic"].lower()


def test_b4_dokumanlar_ayni_ifadeyi_kullaniyor() -> None:
    """Sozlesme / CHANGELOG / docs / .env.example AYNI seyi soylemeli.

    Farkli yerlerde farkli anlatilan bir sozlesme, Grid tarafinda tahmin
    demektir — bu gorevin onlemek istedigi sey tam olarak budur.
    """
    for yol in (
        "CHANGELOG.md",
        "docs/HORSTMANN_SMART_MODE.md",
        "docs/BACKEND_TODO.md",
        ".env.example",
    ):
        metin = (_KOK / yol).read_text(encoding="utf-8")
        assert "ezme yok" in metin.lower(), f"{yol} kanonik ifadeyi kullanmiyor"
