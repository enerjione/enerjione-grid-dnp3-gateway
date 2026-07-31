"""Yerlesik cihaz profilleri — model bazli DNP3 adres haritalari.

NE ISE YARAR
------------
Bir DNP3 modelinin (object_group, index, scale, offset) haritasi cihaz
FIRMWARE'ININ ozelligidir: her kurulumda aynidir, musteriye gore degismez.
Protokol surucusu de burada oldugu icin haritanin dogal yeri gateway'dir.
Yerlesik harita sayesinde bilinen bir model, backend katalogu bos olsa bile
dogru yoklanir.

OTORITE — BACKEND KAZANIR
-------------------------
Bu haritalar YEDEKTIR, otorite DEGIL. Backend bir model icin sinyal
gonderiyorsa O kullanilir. Gerekce: kurulumcu sahada yanlis bir DNP3
index'ini arayuzden duzeltebilmeli. Yerlesik harita kazansaydi, tek bir adres
hatasi icin yeni gateway imaji cikarmak gerekirdi — dakikalik bir mudahale
surum cikarma isine donerdi.

Oncelik (bkz. state.signals_for):
    1. backend profili (dolu)      -> kazanir
    2. yerlesik profil             -> backend bos/yok ise
    3. duz `signals` listesi       -> profil kavrami hic yoksa (eski backend)

SAPMA TEHLIKELI DEGIL
---------------------
Harita iki yerde duruyor (backend seed + burasi) ama backend kazandigi icin
sapma sessiz yanlis veri uretmez: sahada hangi degerin kullanildigi her zaman
backend katalogudur. Yerlesik kopya yalnizca katalog susturuldugunda devreye
girer.

YENI MODEL EKLEME
-----------------
`<model_kodu>.json` dosyasi ekle. Dosya adi backend'deki `devices.model`
degeriyle BIREBIR ayni olmali (bkz. backend app/data/device_models.py) —
eslesme o kod uzerinden yapilir.
"""

from __future__ import annotations

import json
import logging
from functools import cache
from pathlib import Path

from dnp3_gateway.backend import SignalConfig

logger = logging.getLogger(__name__)

_PROFILE_DIR = Path(__file__).resolve().parent

# SignalConfig'in tanidigi alanlar. Backend seed'inde ayrica iec104_* /
# modbus_* / mqtt_* / description / display_order bulunur; onlar SCADA cikisi
# ve ekran duzeniyle ilgilidir, DNP3 okumasiyla degil — buraya tasinmaz.
_ALANLAR = (
    "key",
    "label",
    "unit",
    "source",
    "dnp3_class",
    "data_type",
    "dnp3_object_group",
    "dnp3_index",
    "scale",
    "offset",
    "supports_alarm",
)


@cache
def builtin_profile(model: str) -> tuple[SignalConfig, ...]:
    """Modelin yerlesik sinyal seti. Bilinmiyorsa BOS demet.

    Onbellekli: dosya her poll cycle'inda degil, surec omrunde bir kez okunur.
    """
    anahtar = (model or "").strip()
    if not anahtar or "/" in anahtar or "\\" in anahtar or anahtar.startswith("."):
        # Path traversal'a kapali: model adi backend'den gelen bir string.
        return ()
    yol = _PROFILE_DIR / f"{anahtar}.json"
    if not yol.is_file():
        return ()
    try:
        satirlar = json.loads(yol.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "builtin_profile_load_failed model=%s error=%s — yerlesik profil yok sayildi",
            anahtar,
            exc,
        )
        return ()
    if not isinstance(satirlar, list):
        return ()

    sinyaller: list[SignalConfig] = []
    for satir in satirlar:
        if not isinstance(satir, dict) or not satir.get("key"):
            continue
        try:
            sinyaller.append(SignalConfig(**{alan: satir.get(alan) for alan in _ALANLAR}))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "builtin_profile_signal_invalid model=%s key=%r error=%s",
                anahtar,
                satir.get("key"),
                exc,
            )
    return tuple(sinyaller)


def known_models() -> tuple[str, ...]:
    """Yerlesik profili olan model kodlari."""
    return tuple(sorted(p.stem for p in _PROFILE_DIR.glob("*.json")))
