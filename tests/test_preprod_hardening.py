"""Uretim oncesi sertlestirme paketi — deployment dogrulugu + izlenebilirlik.

Bu dosya BES maddeyi kilitliyor. Hicbiri runtime davranisini degistirmez;
hepsi "uretilen sey ile soylenen sey ayni mi" sorusunu olcer.

  1. INSTALL_MODE compose sablonundan EKSIKTI (sessiz `remote` fallback)
  2. gateway_source_sha eskiydi ve surdurulemez bir sey iddia ediyordu
  3. SECURITY.md HMAC'i hala "opsiyonel" anlatiyordu
  4. /operate docstring'i `command_id`yi "opsiyonel" diyordu
  5. uretim imaji her build'de farkli bagimlilik surumleri cekebiliyordu

OLCULEN KANITLAR (sahadan, salt-okunur)
---------------------------------------
* GW-001/GW-002 container env'inde `INSTALL_MODE=local` VAR — yani saha
  bugun etkilenmiyor. Bu kurulumlar appliance ajanindan geldi; hata bu
  repodaki SABLON yolunda (uzak kurulum) ve LATENT.
* Ayni GW-002 imajinda `charset-normalizer 3.5.0` / `idna 3.18` kuruluyken
  ayni kaynaktan bugun cozulen sonuc `3.5.1` / `3.19`. Ayni commit, farkli
  imaj — madde 5'in somut gerekcesi.
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
LOCK_YOLU = KOK / "requirements-lock-linux-py311.txt"

sys.path.insert(0, str(KOK / "scripts"))


# ==========================================================================
# 1. INSTALL_MODE
# ==========================================================================


def _compose_ham() -> str:
    return (KOK / "docker/compose.template.yml").read_text(encoding="utf-8")


def test_install_mode_compose_sablonunda_yer_tutucu_olarak_var():
    """SPESIFIK REGRESYON: bu anahtar compose'dan bir daha sessizce kaybolmasin.

    Sabit bir deger de KABUL EDILMEZ: `INSTALL_MODE: "remote"` yazmak
    yerel kurulumu yine yanlis moda sokardi (`.env.template` tam olarak
    bunu yapiyordu). Deger render zamani SECILMELI.
    """
    ham = _compose_ham()
    assert 'INSTALL_MODE: "{{INSTALL_MODE}}"' in ham, (
        "compose sablonunda INSTALL_MODE yer tutucusu YOK — sessiz `remote` fallback geri geldi"
    )


def test_install_mode_env_sablonunda_yer_tutucu_olarak_var():
    ham = (KOK / "docker/.env.template").read_text(encoding="utf-8")
    assert "INSTALL_MODE={{INSTALL_MODE}}" in ham
    assert "INSTALL_MODE=remote" not in ham, "sabit `remote` geri geldi"


def test_install_mode_sablon_basliginda_belgelendi():
    ham = _compose_ham()
    assert "{{INSTALL_MODE}}" in ham.split("services:")[0], "yer tutucu basligta belgelenmemis"
    assert "local | remote" in ham


@pytest.mark.parametrize("mod", ["local", "remote"])
def test_render_install_mode_dogru_deger_uretiyor(mod: str):
    """local -> local, remote -> remote. Uydurma varsayilan YOK."""
    import render_compose as rc

    ortak = {
        "code": "GW-009",
        "token": "t" * 40,
        "name": "Test",
        "backend_url": "https://backend.local/api/v1",
        "nats_url": "nats://nats:4222",
        "install_mode": mod,
    }
    compose = rc.render_compose(host_port=8021, **ortak)
    env = rc.render_env(**ortak)

    assert f'INSTALL_MODE: "{mod}"' in compose
    assert f"INSTALL_MODE={mod}" in env


@pytest.mark.parametrize("mod", ["local", "remote"])
def test_render_ciktisinda_dolmamis_yer_tutucu_kalmiyor(mod: str):
    """Sablona yer tutucu eklemek renderer'i sessizce bozabilirdi.

    `_render_text` doldurulmamis yer tutucuda `RenderError` atar; bu test
    iki cikti icin de tam kapanmayi dogrular.
    """
    import render_compose as rc

    desen = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")  # renderer ile AYNI desen
    ortak = {
        "code": "GW-009",
        "token": "t" * 40,
        "name": "Test",
        "backend_url": "https://backend.local/api/v1",
        "nats_url": "nats://nats:4222",
        "install_mode": mod,
    }
    assert not desen.findall(rc.render_compose(host_port=8021, **ortak))
    assert not desen.findall(rc.render_env(**ortak))


@pytest.mark.parametrize("kotu", ["", "hybrid", "LOCAL", "uzak", "true"])
def test_gecersiz_install_mode_reddedilir(kotu: str):
    """Sessiz varsayilan bu hatanin kaynagiydi; renderer da uydurmamali."""
    import render_compose as rc

    with pytest.raises(rc.RenderError):
        rc.render_compose(
            code="GW-009",
            token="t" * 40,
            name="Test",
            backend_url="https://backend.local/api/v1",
            nats_url="nats://nats:4222",
            host_port=8021,
            install_mode=kotu,
        )


def test_install_mode_cli_zorunlu_ve_varsayilansiz():
    """`--install-mode` verilmezse arg parser DUSMELI.

    Varsayilan konsaydi, operator hicbir sey yazmadan yanlis moda kurabilirdi
    — duzeltilen hatanin ta kendisi.
    """
    import render_compose as rc

    parser = rc._build_arg_parser()
    eylem = next(a for a in parser._actions if a.dest == "install_mode")
    assert eylem.required is True
    assert eylem.default is None
    assert tuple(eylem.choices) == ("local", "remote")


def test_install_mode_sozlesmede_zorunlu_ve_gerekcesi_var():
    assert "INSTALL_MODE" in SOZLESME["required_environment"]
    fark = SOZLESME["intentional_mode_differences"]["INSTALL_MODE"]
    assert {"local", "remote"} <= set(fark)
    assert fark["why"].strip()


# ==========================================================================
# 2. Release traceability
# ==========================================================================


def test_gateway_release_version_ile_ayni():
    assert SOZLESME["gateway_release"] == SURUM


def test_gateway_source_ref_version_ile_ayni():
    """`gateway_source_ref` yazim aninda BILINEBILIR — self-referential degil."""
    assert SOZLESME["gateway_source_ref"] == f"v{SURUM}"


def test_gateway_source_sha_formati_korunuyor():
    """Alan adi ve formati Grid'in parity semasi icin KORUNDU."""
    assert re.fullmatch(r"[0-9a-f]{40}", SOZLESME["gateway_source_sha"])


def test_kaynak_sha_ne_oldugunu_acikca_soyluyor():
    """Yanlis iddia birakma: bu deger 'bu commit' DEGIL, baseline'dir.

    Kaynak dosya kendi commit'ini tasiyamaz (dosyaya SHA yazmak yeni commit
    uretir ve SHA degisir). Onceki hali sessizce 'current commit' izlenimi
    veriyordu ve fiilen v1.11.0'da takili kalmisti.
    """
    anlam = SOZLESME["gateway_source_sha_semantics"]
    assert "self-referential" in anlam
    assert "GITHUB_SHA" in anlam
    assert SOZLESME["gateway_source_sha_baseline_ref"].startswith("v")


def test_release_workflow_gercek_github_sha_enjekte_ediyor():
    """Stale sabit SHA uretim artifact'ina SESSIZCE tasinamaz."""
    wf = (KOK / ".github/workflows/release-image.yml").read_text(encoding="utf-8")
    assert "gateway-deployment-contract.generated.json" in wf
    assert 'os.environ["GITHUB_SHA"]' in wf


def test_release_workflow_surum_tutarsizligini_yakaliyor():
    """`gateway_release` VERSION'dan kaydiysa release DUSMELI."""
    wf = (KOK / ".github/workflows/release-image.yml").read_text(encoding="utf-8")
    assert 'assert s["gateway_release"] == surum' in wf
    assert 'assert s["gateway_source_ref"] == f"v{surum}"' in wf


def test_imaj_gercek_revizyonu_etiketliyor():
    """Gercek izlenebilirlik imaj etiketinde: OCI revision = GITHUB_SHA."""
    wf = (KOK / ".github/workflows/release-image.yml").read_text(encoding="utf-8")
    assert "docker/metadata-action" in wf, "OCI etiketlerini ureten adim kaldirilmis"


# ==========================================================================
# 3. SECURITY.md — HMAC zorunlu
# ==========================================================================


def _security() -> str:
    return (KOK / "docs/SECURITY.md").read_text(encoding="utf-8")


def test_security_md_eski_opsiyonel_anlatimi_kalmadi():
    """REGRESYON: 'imza yoksa eskisi gibi devam' artik uretim varsayilani DEGIL."""
    m = _security()
    assert "opsiyonel HMAC" not in m
    assert "Header gelmezse (eski backend) gateway eskisi gibi devam eder" not in m


def test_security_md_zorunlu_hmac_anlatiyor():
    m = _security()
    assert "REQUIRE_BACKEND_RESPONSE_SIGNATURE=true" in m
    assert "fail-closed" in m.lower()
    for durum in ("Imza header'i YOK", "Imza bozuk", "Imza eslesmiyor"):
        assert durum in m, f"karar tablosunda `{durum}` yok"


def test_security_md_rollbackte_bile_bypass_olmadigini_soyluyor():
    """`false` yalnizca 'imza HIC YOK' durumunu tolere eder."""
    m = _security()
    assert "REQUIRE_BACKEND_RESPONSE_SIGNATURE=false" in m
    assert "bypass YOKTUR" in m


def test_security_md_imza_anahtari_ayrimini_dogru_anlatiyor():
    m = _security()
    assert "GATEWAY_COMMAND_DELIVERY_TOKEN" in m
    assert "GECIS DURUMU" in m and "F5A" in m, "F5A tamamlanmis gibi anlatilmamali"


# ==========================================================================
# 4. /operate dokumantasyonu
# ==========================================================================


def _operate_docstring() -> str:
    import inspect

    from dnp3_gateway import health_server

    kaynak = inspect.getsource(health_server)
    bas = kaynak.index("def _handle_operate")
    return kaynak[bas : kaynak.index('"""', kaynak.index('"""', bas) + 3)]


def test_operate_docstring_command_id_zorunlu_diyor():
    """REGRESYON: v1.11.2'den beri ZORUNLU; docstring 'OPSIYONEL' diyordu."""
    d = _operate_docstring()
    assert "OPSIYONEL ama ONERILEN" not in d
    assert "`command_id`: **ZORUNLU**" in d


def test_operate_docstring_created_at_ve_defteri_anlatiyor():
    d = _operate_docstring()
    assert "`created_at`: **ZORUNLU**" in d
    assert "COMMAND_MAX_AGE_SEC" in d and "COMMAND_CLOCK_SKEW_TOLERANCE_SEC" in d
    assert "CommandLedger ZORUNLU" in d
    assert "503" in d


def test_operate_docstring_zorunlu_opsiyonel_ayrimini_veriyor():
    d = _operate_docstring()
    assert "ZORUNLU : device_code, command, index, created_at, command_id" in d
    for alan in ("op_type", "count", "on_time_ms", "off_time_ms", "timeout_sec"):
        assert alan in d


def test_operate_docstring_command_id_strict_tamsayi_diyor():
    d = _operate_docstring()
    assert "STRICT tamsayi" in d
    assert "bool" in d.lower()


#: GECMIS ZAMAN isaretleri. "Eskiden opsiyoneldi" DOGRU bir tarihsel ifadedir
#: ve silinmemeli — kodun neden bugunku halinde oldugunu anlatir. Yasak olan,
#: SU ANKI sozlesmeymis gibi yazilmis "opsiyonel" iddiasidir.
_GECMIS_ISARETLERI = ("eskiden", "kullaniyordu", "atliyordu", "idi", "oncesinde", "artik")


def test_repo_genelinde_command_id_opsiyonel_iddiasi_kalmadi():
    """CHANGELOG haric: tarihsel kayitlar geriye donuk DEGISTIRILMEZ.

    Test SU ANKI iddiayi arar. `command_id: OPSIYONEL ama ONERILEN`
    (simdiki zaman) yakalanir; `OPSIYONEL kullaniyordu` (gecmis) yakalanmaz.
    """
    kotu = []
    for yol in list(KOK.glob("src/**/*.py")) + list(KOK.glob("docs/*.md")) + [KOK / "README.md"]:
        if not yol.is_file():
            continue
        for no, satir in enumerate(yol.read_text(encoding="utf-8").splitlines(), 1):
            d = satir.lower()
            if "command_id" not in d or not ("opsiyonel" in d or "optional" in d):
                continue
            if "zorunlu" in d or any(i in d for i in _GECMIS_ISARETLERI):
                continue
            kotu.append(f"{yol.relative_to(KOK)}:{no} -> {satir.strip()[:70]}")
    assert not kotu, f"`command_id` hala opsiyonel anlatiliyor: {kotu}"


# ==========================================================================
# 5. Bagimlilik reproducibility
# ==========================================================================


def _lock() -> str:
    return LOCK_YOLU.read_text(encoding="utf-8")


def test_lock_dosyasi_var_ve_exact_pinli():
    assert LOCK_YOLU.exists(), "uretim lock dosyasi yok"
    pinler = re.findall(r"^([A-Za-z0-9_.\-]+)==([^ \\\n]+)", _lock(), re.M)
    assert pinler, "lock icinde exact pin yok"
    # Aralik operatoru KALMAMALI — tek bir tanesi reproducibility'yi bozar.
    assert not re.search(r"^[A-Za-z0-9_.\-]+[><~]=", _lock(), re.M)


def test_lock_hash_iceriyor():
    """`--require-hashes` yalnizca surumu degil ICERIGI de sabitler."""
    assert "--hash=sha256:" in _lock()


def test_lock_runtime_bagimliliklarinin_hepsini_kapsiyor():
    ham = (KOK / "requirements.txt").read_text(encoding="utf-8")
    istenen = {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.\-]+)[><=~]", ham, re.M)}
    kilitli = {
        m.group(1).lower().replace("_", "-") for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==", _lock(), re.M)
    }
    eksik = {a.replace("_", "-") for a in istenen} - kilitli
    assert not eksik, f"lock disinda kalan runtime bagimliligi: {eksik}"


def test_dockerfile_lock_kullaniyor_ve_hash_dogruluyor():
    """REGRESYON: builder araliklara geri donerse imaj tekrar-uretilemez olur."""
    df = (KOK / "Dockerfile").read_text(encoding="utf-8")
    assert LOCK_YOLU.name in df
    assert "--require-hashes" in df
    assert "pip wheel --wheel-dir=/build/wheels -r /build/requirements.txt" not in df


def test_dockerfile_lock_python_surumunu_dogruluyor():
    """Lock py311 icin uretildi; imaj baska bir surume kayarsa SESSIZ kalmasin."""
    df = (KOK / "Dockerfile").read_text(encoding="utf-8")
    assert "sys.version_info[:2] == (3, 11)" in df


def test_yadnp3_pini_lock_disinda_ve_exact():
    """yadnp3 platform-bagimli wheel; lock'a KATILMADI ama exact pin KORUNDU."""
    dnp3 = (KOK / "requirements-dnp3.txt").read_text(encoding="utf-8")
    assert re.search(r"^yadnp3==3\.2\.1\.1", dnp3, re.M), "yadnp3 exact pin degismis"
    assert "yadnp3" not in _lock(), "yadnp3 lock'a girmis — platform kirilganligi"


def test_ci_imajin_lock_surumlerini_tasidigini_dogruluyor():
    ci = (KOK / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "Imaj kilitli bagimliliklari mi tasiyor" in ci
    assert LOCK_YOLU.name in ci


def test_pyproject_araliklari_korundu():
    """Kutuphane uyumlulugu aralikla kalir; SABIT surum yalnizca uretim imajinda."""
    pp = (KOK / "pyproject.toml").read_text(encoding="utf-8")
    assert "pydantic>=" in pp, "pyproject gereksiz yere exact pin'e cevrilmis"
