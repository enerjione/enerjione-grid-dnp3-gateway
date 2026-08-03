"""Saha kurulum scriptleri Windows PowerShell 5.1'de PARSE EDILEBILMELI.

GERCEK ARIZA
------------
`scripts/install.ps1` BOM'suz UTF-8 kaydedilmisti ve icinde em-dash (`—`,
U+2014) geciyordu. Windows Server'da varsayilan kabuk **Windows PowerShell
5.1**'dir ve BOM'suz dosyalari UTF-8 degil ANSI (cp1252) olarak okur:

    U+2014  ->  UTF-8: E2 80 94  ->  cp1252: â € "
                                              ^^^ 0x94 = U+201D

PowerShell akilli tirnaklari (U+201D) GERCEK tirnak sayar. Sonuc: string
erken kapanir, ardindan gelen `}` ve `)` yerini sasirir ve **script hic
calismaz** — dokumante edilen kurulum yolu stok bir Windows Server'da
tamamen kirilmisti. PowerShell 7 (UTF-8 varsayilan) bunu maskeliyordu.

Hata tipi ozellikle sinsi: yalnizca bir YORUM satirindaki tire yuzunden
scriptin TAMAMI patlar ve hata mesaji tireyi degil, cok asagidaki bir
suslu parantezi gosterir.

KURAL
-----
`scripts/*.ps1` yalnizca ASCII icermeli. Alternatif (dosyalara BOM eklemek)
daha kirilgandir: BOM sessizce kaybolabilir, ASCII kurali ise burada
dogrulanabilir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT_DIZINI = Path(__file__).resolve().parents[1] / "scripts"
SCRIPTLER = sorted(SCRIPT_DIZINI.glob("*.ps1"))

# cp1252'de PowerShell'in tirnak/karakter olarak yanlis yorumlayacagi tipik
# "akilli" noktalama. Hepsi ASCII karsiligiyla yazilmali.
TEHLIKELI = {
    "—": "em dash — ASCII '-' kullanin",
    "–": "en dash — ASCII '-' kullanin",
    "‘": "sol tek tirnak — ASCII ' kullanin",
    "’": "sag tek tirnak — ASCII ' kullanin",
    "“": 'sol cift tirnak — ASCII " kullanin',
    "”": 'sag cift tirnak — ASCII " kullanin',
    "﻿": "gomulu BOM karakteri — silin",
    " ": "kirilmaz bosluk — normal bosluk kullanin",
}


def test_en_az_bir_script_var() -> None:
    """Glob bosa duserse testler sessizce 'gecer' — bunu engelle."""
    assert SCRIPTLER, f"{SCRIPT_DIZINI} altinda .ps1 bulunamadi"


@pytest.mark.parametrize("script", SCRIPTLER, ids=lambda p: p.name)
def test_script_sadece_ascii(script: Path) -> None:
    ham = script.read_bytes()
    metin = ham.decode("utf-8")

    hatalar: list[str] = []
    for satir_no, satir in enumerate(metin.splitlines(), 1):
        for sutun, karakter in enumerate(satir, 1):
            if ord(karakter) < 128:
                continue
            aciklama = TEHLIKELI.get(karakter, f"ASCII-disi karakter U+{ord(karakter):04X}")
            hatalar.append(f"  satir {satir_no}, sutun {sutun}: {karakter!r} — {aciklama}")

    assert not hatalar, (
        f"{script.name} ASCII-disi karakter iceriyor. Windows PowerShell 5.1 "
        f"BOM'suz dosyalari cp1252 okur ve bunlar scripti PARSE EDILEMEZ hale "
        f"getirebilir (yorum satirindaki tek bir tire tum scripti bozar):\n" + "\n".join(hatalar)
    )


@pytest.mark.parametrize("script", SCRIPTLER, ids=lambda p: p.name)
def test_script_cp1252_olarak_da_ayni_okunur(script: Path) -> None:
    """Asil garanti: dosya hangi kodlamayla okunursa okunsun AYNI metin.

    ASCII-only ise UTF-8 ve cp1252 cozumlemeleri birebir esittir; yani
    PowerShell 5.1 ile 7 ayni scripti gorur. Bu test, kurala uyuldugunu
    karakter listesinden bagimsiz olarak dogrular.
    """
    ham = script.read_bytes()
    assert ham.decode("utf-8") == ham.decode("cp1252"), (
        f"{script.name} kodlamaya gore FARKLI okunuyor — PowerShell 5.1 (cp1252) "
        f"ile PowerShell 7 (UTF-8) ayni scripti gormeyecek."
    )
