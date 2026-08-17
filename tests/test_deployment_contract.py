"""Gateway kendi uretim sablonlarini KENDI sozlesmesine karsi dogrular.

Sozlesmenin sahibi bu repo'dur (docker/gateway-deployment-contract.json).
Grid tam kopyasini vendor eder; ama sahibinin kendi ciktilari sozlesmeden
kaymissa vendor edilen sey de yanlis olur -- bu yuzden kontrol BURADA baslar.

OLCULEN INTRA-REPO DRIFT (v1.10.0)
----------------------------------
Bu dosya yazilmadan once ayni repo'nun iki uretim sablonu birbirini
tutmuyordu:

    compose.template.yml : POLL=1  MAX_PARALLEL=500 baseline=60 scan YOK
    .env.template        : POLL=5  MAX_PARALLEL=50  baseline=60 scan YOK
                           + legacy DNP3_RESPONSE_TIMEOUT_SEC/READ_STRATEGY

`scan` ikisinde de eksikti ve bu, config.py varsayilani 0 oldugu icin
`scan = poll` demek: poll=1 ile saniyede cihaz basina 1 DNP3 istegi
(401 cihazda olculdu, 2026-08-04).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

KOK = Path(__file__).resolve().parents[1]
SOZLESME = json.loads((KOK / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))
VARSAYILANLAR: dict[str, str] = SOZLESME["environment_defaults"]
YASAKLI: dict[str, str] = SOZLESME["forbidden_environment"]
RUNTIME: dict = SOZLESME["docker_runtime"]

COMPOSE_YOLU = KOK / "docker/compose.template.yml"
ENV_YOLU = KOK / "docker/.env.template"


def _compose_veri() -> dict:
    """Sablonu parse eder.

    Yer tutucular ONCE doldurulur: `image: {{IMAGE}}` gibi TIRNAKSIZ bir
    alan YAML'da akis-eslemesi (flow mapping) baslangici sayilir ve parse
    patlar. Degerin kendisi onemsiz; sinanan sey yapinin ve sabitlerin
    dogrulugu.
    """
    ham = COMPOSE_YOLU.read_text(encoding="utf-8")
    ham = re.sub(r"\{\{[A-Z_]+\}\}", "yer-tutucu", ham)
    return yaml.safe_load(ham)


def _compose_env() -> dict[str, str]:
    env = _compose_veri()["services"]["gateway"]["environment"]
    return {k: str(v) for k, v in env.items()}


def _compose_servis() -> dict:
    return _compose_veri()["services"]["gateway"]


def _env_dosyasi() -> dict[str, str]:
    out: dict[str, str] = {}
    for satir in ENV_YOLU.read_text(encoding="utf-8").splitlines():
        satir = satir.strip()
        if not satir or satir.startswith("#") or "=" not in satir:
            continue
        k, v = satir.split("=", 1)
        out[k] = v
    return out


KAYNAKLAR = {"compose.template.yml": _compose_env, ".env.template": _env_dosyasi}


@pytest.mark.parametrize("kaynak", sorted(KAYNAKLAR))
@pytest.mark.parametrize("anahtar", sorted(VARSAYILANLAR))
def test_T02_T03_sablonlar_sozlesme_degerini_tasiyor(kaynak: str, anahtar: str):
    env = KAYNAKLAR[kaynak]()
    beklenen = VARSAYILANLAR[anahtar]
    assert anahtar in env, (
        f"`{kaynak}` icinde `{anahtar}` yok. Eksik olmasi kod varsayilanina "
        f"dusmek demektir (sozlesme: {beklenen!r})."
    )
    assert env[anahtar] == beklenen, (
        f"`{kaynak}`: {anahtar} = {env[anahtar]!r}, sozlesme {beklenen!r} diyor"
    )


@pytest.mark.parametrize("kaynak", sorted(KAYNAKLAR))
def test_yasakli_legacy_anahtarlar_yok(kaynak: str):
    env = KAYNAKLAR[kaynak]()
    for anahtar, neden in YASAKLI.items():
        assert anahtar not in env, f"`{kaynak}` yasakli `{anahtar}` uretiyor. {neden}"


def test_scan_araligi_EXPLICIT():
    """Sozlesmenin en kolay gozden kacan maddesi.

    `dnp3_event_scan_interval_sec` varsayilani 0'dir ve factory
    `scan or poll` yazar; yani ayarin YOKLUGU sessizce scan=poll uretir.
    Bu test o sessizligi yakalar.
    """
    for kaynak, fn in KAYNAKLAR.items():
        assert "DNP3_EVENT_SCAN_INTERVAL_SEC" in fn(), (
            f"`{kaynak}` scan araligini explicit vermiyor -- scan poll'a duser"
        )


def test_docker_runtime_sozlesmeye_uyuyor():
    svc = _compose_servis()
    assert str(svc["stop_grace_period"]) == RUNTIME["stop_grace_period"]
    assert svc["restart"] == RUNTIME["restart"]
    nofile = svc["ulimits"]["nofile"]
    assert int(nofile["soft"]) == RUNTIME["ulimits"]["nofile"]["soft"]
    assert int(nofile["hard"]) == RUNTIME["ulimits"]["nofile"]["hard"]
    assert svc["logging"]["driver"] == RUNTIME["logging"]["driver"]
    assert any(
        str(v).endswith(RUNTIME["state_volume_mount"]) for v in svc.get("volumes", [])
    ), "state volume yok"


def test_sozlesme_bu_surumu_tarif_ediyor():
    surum = (KOK / "VERSION").read_text(encoding="utf-8").strip()
    assert SOZLESME["gateway_release"] == surum, (
        f"sozlesme {SOZLESME['gateway_release']} diyor ama VERSION {surum}. "
        "Surum cikarirken sozlesme de guncellenmeli."
    )
    assert re.fullmatch(r"[0-9a-f]{40}", SOZLESME["gateway_source_sha"])


def test_zorunlu_alanlar_sablonlarda_yer_tutucu_olarak_var():
    """Sozlesme 'her kurulum bunlari SAGLAMALI' diyor; sablon bos birakmamali."""
    env = _env_dosyasi()
    compose = _compose_env()
    for anahtar in SOZLESME["required_environment"]:
        assert anahtar in env or anahtar in compose, (
            f"zorunlu `{anahtar}` hicbir sablonda yok"
        )
