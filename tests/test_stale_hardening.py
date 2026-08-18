"""Stale dokumantasyon + uretim imaj pinning kilitleri (B, C, D).

Buradaki testlerin hepsi ayni sinifta bir hataya karsi: **kod ilerledi,
metin geride kaldi.** Ikisi de "dogru" gorunur ve ikisi de kod okunmadan
fark edilmez; sahada yanlis karar aldiran sey ise metindir.

  B — F5 rollout TAMAMLANDI. Sozlesme, SECURITY.md ve .env.example hala
      "backend F5A sahaya cikmadi" diyordu ve `GATEWAY_COMMAND_DELIVERY_TOKEN`
      icin "PROVISION EDILMEZ" talimati veriyordu. Bu talimat artik yanlis
      ve dogru yapilandirmayi ENGELLIYORDU.

  C — `render_compose.py` `--image` verilmezse `:latest` uretiyordu. Uretim
      compose'unun `:latest`e bagli olmasi iki sey demek: dosyaya bakarak
      "hangi surum kurulu" CEVAPLANAMAZ ve siradan bir `docker compose pull`
      gateway'i sessizce baska bir surume gecirir.

  D — v1.11.3 traceability mekanizmasi (self-referential SHA tuzagina
      girmeden) KORUNUYOR.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

KOK = Path(__file__).resolve().parents[1]
SOZLESME = json.loads((KOK / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))
SURUM = (KOK / "VERSION").read_text(encoding="utf-8").strip()

sys.path.insert(0, str(KOK / "scripts"))

#: Kod ile metnin ayristigi noktalari isaretleyen STALE ifadeler.
#: CHANGELOG haric — tarihsel kayitlar geriye donuk degistirilmez.
_STALE_F5 = (
    "F5A tamamlanmadi",
    "backend F5A sahaya cikmadi",
    "optional_until_backend_f5a",
    "F5A SAHAYA CIKMADAN",
    "F5A sahaya cikana kadar",
    "F5A tamamlanana kadar",
)

_METIN_DOSYALARI = [
    KOK / "docs/SECURITY.md",
    KOK / ".env.example",
    KOK / "docker/gateway-deployment-contract.json",
    KOK / "docker/.env.template",
    KOK / "docker/compose.template.yml",
]


# ==========================================================================
# B — F5 dokumantasyon / contract drift
# ==========================================================================


@pytest.mark.parametrize("yol", _METIN_DOSYALARI, ids=lambda p: p.name)
def test_stale_f5_ifadeleri_kalmadi(yol: Path):
    """REGRESYON: F5 tamamlandi; "henuz cikmadi" metni geri gelmemeli."""
    ham = yol.read_text(encoding="utf-8")
    bulunan = [s for s in _STALE_F5 if s.lower() in ham.lower()]
    assert not bulunan, f"{yol.name} hala stale F5 ifadesi tasiyor: {bulunan}"


def test_src_icinde_stale_f5_ifadesi_kalmadi():
    kotu = []
    for yol in KOK.glob("src/**/*.py"):
        ham = yol.read_text(encoding="utf-8")
        for s in _STALE_F5:
            if s.lower() in ham.lower():
                kotu.append(f"{yol.relative_to(KOK).as_posix()}: {s}")
    assert not kotu, f"kaynak kodda stale F5 ifadesi: {kotu}"


def test_contract_f5_kredensiyalini_tamamlanmis_olarak_tarif_ediyor():
    alan = SOZLESME["transition_environment"]["GATEWAY_COMMAND_DELIVERY_TOKEN"]
    assert alan["status"] == "available"
    assert "F5 TAMAMLANDI" in alan["rollout"]
    # Yanlis talimat kaldirildi: artik "provision edilmez" DEMIYOR.
    assert "ordering_constraint" not in alan


def test_contract_kati_ayrimi_ve_geriye_donuk_uyumu_ayri_anlatiyor():
    """Ikisi karisirsa operator "bos birakmak da ayni" saniir."""
    alan = SOZLESME["transition_environment"]["GATEWAY_COMMAND_DELIVERY_TOKEN"]
    assert "GATEWAY_TOKEN'a geri DUSULMEZ" in alan["strict_separation"]
    assert "geriye donuk uyumluluk" in alan["backward_compatibility"].lower()
    assert "onerilen yapilandirma degildir" in alan["backward_compatibility"].lower()


def test_contract_production_guard_korundu():
    """F5 runtime politikasi DEGISMEDI — yalnizca metin duzeltildi."""
    alan = SOZLESME["transition_environment"]["GATEWAY_COMMAND_DELIVERY_TOKEN"]
    assert "GATEWAY_TOKEN" in alan["production_guard"]
    assert "GATEWAY_COMMAND_TOKEN" in alan["production_guard"]


def test_security_md_f5_tamamlandi_diyor():
    m = (KOK / "docs/SECURITY.md").read_text(encoding="utf-8")
    assert "F5 TAMAMLANDI" in m
    assert "onerilen" in m.lower()
    assert "Geriye donuk uyumluluk" in m


# ==========================================================================
# C — standalone image pinning
# ==========================================================================


def _render(**kw):
    import render_compose as rc

    varsayilan = {
        "code": "GW-009",
        "token": "t" * 40,
        "name": "Test",
        "backend_url": "https://backend.local/api/v1",
        "nats_url": "nats://nats:4222",
        "host_port": 8021,
        "install_mode": "remote",
    }
    varsayilan.update(kw)
    return rc.render_compose(**varsayilan)


def _imaj_satiri(compose: str) -> str:
    for satir in compose.splitlines():
        if satir.strip().startswith("image:"):
            return satir.split("image:", 1)[1].strip()
    raise AssertionError("compose ciktisinda image satiri yok")


def test_uretim_compose_ciktisi_latest_e_baglanmiyor():
    """REGRESYON: uretim ciktisi sessizce `:latest`e donmemeli."""
    imaj = _imaj_satiri(_render())
    assert not imaj.endswith(":latest"), f"uretim compose `:latest` kullaniyor: {imaj}"


def test_uretim_compose_ciktisi_version_ile_pinli():
    imaj = _imaj_satiri(_render())
    assert imaj.endswith(f":{SURUM}"), f"imaj VERSION ile pinli degil: {imaj}"
    assert re.fullmatch(r"[\w.\-/]+:\d+\.\d+\.\d+", imaj), imaj


def test_acik_image_parametresi_hala_calisiyor():
    """Digest ile daha kati pin isteyen kurulumlar engellenmemeli."""
    digest = "ghcr.io/enerjione/enerjione-grid-dnp3-gateway@sha256:" + "a" * 64
    assert _imaj_satiri(_render(image=digest)) == digest


def test_default_image_fonksiyonu_version_okuyor():
    import render_compose as rc

    assert rc.default_image().endswith(f":{SURUM}")
    assert rc.IMAGE_REPO in rc.default_image()


def test_release_workflow_latest_yayinlamaya_devam_ediyor():
    """C maddesi TUKETIM tarafini degistirdi; YAYIN politikasina dokunmadi."""
    wf = (KOK / ".github/workflows/release-image.yml").read_text(encoding="utf-8")
    assert "type=raw,value=latest" in wf, ":latest yayin politikasi kaldirilmis"
    assert "type=semver,pattern={{version}}" in wf, "semver etiketi bozulmus"


def test_docs_ornekleri_yeni_davranisa_uygun():
    d = (KOK / "docs/DOCKER.md").read_text(encoding="utf-8")
    assert "--install-mode" in d, "CLI ornegi zorunlu bayragi gostermiyor"
    assert "`:latest`e **baglanmaz**" in d


# ==========================================================================
# D — traceability korundu
# ==========================================================================


def test_traceability_alanlari_version_ile_hizali():
    assert SOZLESME["gateway_release"] == SURUM
    assert SOZLESME["gateway_source_ref"] == f"v{SURUM}"


def test_kaynak_sha_hala_baseline_semantiginde():
    """Self-referential tuzaga girilmedi: kaynak dosya kendi commit'ini iddia etmiyor."""
    assert re.fullmatch(r"[0-9a-f]{40}", SOZLESME["gateway_source_sha"])
    assert "self-referential" in SOZLESME["gateway_source_sha_semantics"]
    assert SOZLESME["gateway_source_sha_baseline_ref"].startswith("v")


def test_release_workflow_dogrulamalari_korundu():
    wf = (KOK / ".github/workflows/release-image.yml").read_text(encoding="utf-8")
    assert 'assert s["gateway_release"] == surum' in wf
    assert 'assert s["gateway_source_ref"] == f"v{surum}"' in wf
    assert 'os.environ["GITHUB_SHA"]' in wf


def test_dependency_lock_mekanizmasi_korundu():
    """E: v1.11.3 lock mekanizmasi bozulmadi."""
    df = (KOK / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in df
    assert "requirements-lock-linux-py311.txt" in df
    dnp3 = (KOK / "requirements-dnp3.txt").read_text(encoding="utf-8")
    assert re.search(r"^yadnp3==3\.2\.1\.1", dnp3, re.M)
