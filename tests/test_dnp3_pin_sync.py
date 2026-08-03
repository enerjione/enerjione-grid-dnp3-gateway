"""DNP3 native surum PIN'i tek kaynakta kalsin.

NEDEN BEKCI GEREK
-----------------
`yadnp3==X.Y.Z` pini bir donem UC ayri yerde tekrarlaniyordu: `Dockerfile`
(Docker/Linux sahasi), `scripts/install.ps1` (Windows sahasi) ve CI. Bu, bir
native protokol kutuphanesi icin sessiz ve pahali bir tuzak:

  * Birini yukseltip digerini unutmak, CI'in test ettigi surumle sahanin
    calistirdigi surumun AYRISMASI demek.
  * Ayrisma derleme hatasi vermez; yalnizca DNP3 davranisinda — yani
    musteri sahasinda — gorunur.

Uc taraf da artik `requirements-dnp3.txt` okuyor. Bu test, birinin kolaylik
olsun diye tekrar sabit surum yazmasini engeller.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

KOK = Path(__file__).resolve().parents[1]
PIN_DOSYASI = KOK / "requirements-dnp3.txt"

# "yadnp3" hemen ardindan bir surum belirteci (==, >=, ~=) gelen her satir.
# Pin dosyasinin KENDISI haric hicbir yerde bulunmamali.
SABIT_SURUM = re.compile(r"yadnp3\s*[=><~!]=")

# Pini paylasmasi gereken dosyalar.
TUKETICILER = (
    Path("Dockerfile"),
    Path("scripts/install.ps1"),
    Path(".github/workflows/ci.yml"),
)


def test_pin_dosyasi_tek_bir_surum_tanimlar() -> None:
    satirlar = [
        s.strip()
        for s in PIN_DOSYASI.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.strip().startswith("#")
    ]
    assert len(satirlar) == 1, f"pin dosyasinda tam olarak bir gereksinim olmali: {satirlar}"
    assert SABIT_SURUM.search(satirlar[0]), f"surum sabitlenmemis: {satirlar[0]!r}"


@pytest.mark.parametrize("goreli", TUKETICILER, ids=lambda p: str(p))
def test_tuketiciler_pini_tekrarlamaz(goreli: Path) -> None:
    dosya = KOK / goreli
    assert dosya.exists(), f"beklenen dosya yok: {goreli}"
    icerik = dosya.read_text(encoding="utf-8")

    tekrar = [
        satir.strip()
        for satir in icerik.splitlines()
        # Yorum satirlari serbest — aciklamada surum anmak sorun degil.
        if SABIT_SURUM.search(satir) and not satir.strip().startswith(("#", "//"))
    ]
    assert not tekrar, (
        f"{goreli} icinde sabit yadnp3 surumu var: {tekrar}. "
        f"Pin yalnizca requirements-dnp3.txt'te durmali; buradan "
        f"`-r requirements-dnp3.txt` ile kurun."
    )


@pytest.mark.parametrize("goreli", TUKETICILER, ids=lambda p: str(p))
def test_tuketiciler_pin_dosyasini_kullanir(goreli: Path) -> None:
    icerik = (KOK / goreli).read_text(encoding="utf-8")
    assert "requirements-dnp3.txt" in icerik, (
        f"{goreli} paylasilan pin dosyasini kurmuyor — yadnp3 orada hic "
        f"kurulmuyorsa DNP3 modu calismaz, sabit surum yaziliyorsa ayrisir."
    )
