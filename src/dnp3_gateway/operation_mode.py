"""Horstmann MASTER `Operation Mode` -> oturum politikasi (G-SMART-02).

KAPSAM BILEREK DAR
------------------
Bu modul TEK bir soruyu cevaplar: "bu cihazin hucresel iletisimcisi su an
Smart mi Boost mu?" Genel bir metadata/rol cercevesi DEGILDIR; yalnizca
`session_policy="auto"` icin gereken en kucuk mekanizmadir.

DEGER MI, BAYRAK MI? — EN KRITIK AYRIM
--------------------------------------
Smart Navigator 2.0 dokumantasyonu Master Operation Mode icin sunu yazar:

    0x01 = Boost Mode
    0x81 = Smart Mode

Bu degerler noktanin DEGERI DEGIL, tam DNP3 **bayrak okteti**dir. DNP3'te
Group 1 bayrak byte'inin 7. biti (0x80) noktanin DURUM (STATE) bitidir —
yani DEGERIN KENDISI:

    0x01 = ONLINE, STATE=0  -> binary deger FALSE
    0x81 = ONLINE, STATE=1  -> binary deger TRUE

Kutuphane ile DOGRULANDI (yadnp3 3.2.1.1):

    opendnp3.Binary(False, Flags(0x01)) -> .value=False, flags=0x01
    opendnp3.Binary(True,  Flags(0x01)) -> .value=True,  flags=0x81

yadnp3 SOE handler'i bize DEGERI verir (`it.value.value` -> bool) ve bayrak
byte'ini AYRI tasir. Dolayisiyla adapter cache'inde saklanan sayisal deger:

    1.0 -> SMART      (0x81'in STATE biti)
    0.0 -> BOOST      (0x01)

Naif "1 = Boost" varsayimi TERSINE cevirirdi ve Smart bir cihaz surekli
taranarak modemini hicbir zaman kapatamazdi. `tests/test_operation_mode.py`
bu turetmeyi bayrak oktetlerinden ADIM ADIM pinler.

MASTER / POLEMASTER — TEK OTORITE
---------------------------------
Hucresel oturum Master (ya da Pole Master) cihazina aittir. Satellite
uniteler gateway acisindan bagimsiz bir DNP3 baglantisi DEGILDIR; verileri
Master uzerinden gelen siradan telemetridir ve politika kararina KATILMAZ.

`Boost Mode Enabled` (G1 idx 63) KONFIGURASYONDUR — "bu cihazda boost
acilabilir mi". Calisma anindaki gercek durum DEGILDIR ve mod kaynagi
olarak KULLANILMAZ.

INDEX SABITLENMEZ
-----------------
`Operation Mode` SN 2.0'da G1 index 15, Pole Master profilinde BASKA bir
index'tir. Bu yuzden kodda sabit bir index karsilastirmasi YOKTUR; sinyal
cihazin KENDI katalogundan semantik kimlikle (kaynak + anahtar) bulunur.
`tests/test_auto_session_policy.py` sabit-index dalinin geri gelmesini
statik olarak engeller.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mod tokenleri
# ---------------------------------------------------------------------------

#: Mod HENUZ gozlenmedi. Hata degil, bilgi eksikligi.
MODE_UNKNOWN = "unknown"
MODE_SMART = "smart"
MODE_BOOST = "boost"

MODES: frozenset[str] = frozenset({MODE_UNKNOWN, MODE_SMART, MODE_BOOST})


# ---------------------------------------------------------------------------
# Bayrak okteti -> deger turetmesi (dokumantasyondan)
# ---------------------------------------------------------------------------

#: Smart Navigator 2.0 dokumantasyonundaki HAM BAYRAK OKTETLERI.
#: Bunlar deger degil, tam DNP3 Group 1 bayrak byte'idir.
HORSTMANN_FLAGS_BOOST = 0x01
HORSTMANN_FLAGS_SMART = 0x81

#: DNP3 Group 1 bayrak byte'inda DURUM (STATE) biti = noktanin DEGERI.
DNP3_BINARY_STATE_BIT = 0x80


def _state_bit(flags: int) -> int:
    """Bayrak oktetinden noktanin binary degerini cikarir (0/1)."""
    return 1 if flags & DNP3_BINARY_STATE_BIT else 0


#: Dokumandan TURETILEN deger eslemesi — elle yazilmadi.
#: 0x81 -> 1 (SMART), 0x01 -> 0 (BOOST).
SMART_RAW_VALUE = _state_bit(HORSTMANN_FLAGS_SMART)
BOOST_RAW_VALUE = _state_bit(HORSTMANN_FLAGS_BOOST)


def normalize_operation_mode(raw: Any, *, smart_raw_value: int = SMART_RAW_VALUE) -> str:
    """Cache'teki ham binary deger -> `smart` / `boost` / `unknown`.

    Beklenmeyen deger (None, NaN, 2.0) SESSIZCE bir moda ZORLANMAZ:
    `unknown` doner ve cagiran taraf muhafazakar davranir. Uydurma bir mod,
    cihazi yanlislikla susturabilir ya da modemini acik tutabilirdi.
    """
    if raw is None:
        return MODE_UNKNOWN
    try:
        deger = int(round(float(raw)))
    except (TypeError, ValueError):
        return MODE_UNKNOWN
    if deger not in (0, 1):
        return MODE_UNKNOWN
    return MODE_SMART if deger == int(smart_raw_value) else MODE_BOOST


# ---------------------------------------------------------------------------
# Sinyal kimligi
# ---------------------------------------------------------------------------

#: Hucresel iletisimciyi gosteren `source` degerleri. Saha katalogunda hem
#: SN 2.0 hem Pole Master Kit `source="master"` kullanir; digerleri baska
#: kataloglara karsi tolerans.
_MASTER_SOURCES: frozenset[str] = frozenset({"master", "polemaster", "pole_master", "pole master", "pm"})

#: Satellite kaynaklari — politika kararindan KESIN olarak dislanir.
_SATELLITE_PREFIXES: tuple[str, ...] = ("sat", "satellite")

#: Anahtarin son bileseni TAM OLARAK bu olmali. `boost_mode_enabled`
#: (yetenek) ve `boost_mode` (komut noktasi) bu esitligi SAGLAMAZ.
_OPERATION_MODE_KEY = "operation_mode"

#: Binary INPUT: `Operation Mode` bir DURUM noktasidir (G1), komut noktasi
#: (G10) degil. Bu filtre `master.boost_mode` (G10) noktasinin yanlislikla
#: mod kaynagi sanilmasini imkansiz kilar.
_BINARY_INPUT_GROUP = 1
_BINARY_DATA_TYPE = "binary"


def _norm(deger: Any) -> str:
    return str(deger or "").strip().lower()


def is_satellite_source(source: Any) -> bool:
    s = _norm(source)
    return bool(s) and s.startswith(_SATELLITE_PREFIXES)


def is_master_source(source: Any) -> bool:
    s = _norm(source)
    return bool(s) and not is_satellite_source(s) and s in _MASTER_SOURCES


def resolve_master_operation_mode_signal(device: Any, signals: Any) -> Any | None:
    """Hucresel MASTER'in `Operation Mode` sinyali; yoksa/belirsizse None.

    None donmesi HATA DEGILDIR: Horstmann olmayan bir cihaz ya da mod
    noktasi tanimlanmamis bir katalog. Cagiran taraf `unknown` ile
    muhafazakar davranir.

    Eslesme kurallari (HEPSI saglanmali):
      1. binary INPUT (G1) — komut noktasi degil,
      2. anahtarin son bileseni tam olarak `operation_mode`,
      3. kaynak hucresel master — Satellite KESIN reddedilir.
    """
    adaylar = []
    for s in signals or ():
        if _norm(getattr(s, "data_type", None)) != _BINARY_DATA_TYPE:
            continue
        try:
            if int(getattr(s, "dnp3_object_group", 0) or 0) != _BINARY_INPUT_GROUP:
                continue
        except (TypeError, ValueError):
            continue
        if getattr(s, "dnp3_index", None) is None:
            continue
        kaynak = getattr(s, "source", None)
        if is_satellite_source(kaynak):
            continue
        if _norm(getattr(s, "key", None)).rsplit(".", 1)[-1] != _OPERATION_MODE_KEY:
            continue
        if not is_master_source(kaynak):
            continue
        adaylar.append(s)

    if not adaylar:
        return None
    if len(adaylar) > 1:
        # BELIRSIZ -> UNKNOWN. Birini secmek, yanlis secildiginde cihazi
        # sessizce susturabilir ya da modemini acik tutabilirdi.
        if _uyari_ver(f"ambiguous:{getattr(device, 'code', '?')}"):
            logger.warning(
                "operation_mode_signal_ambiguous device=%s adaylar=%s — MASTER "
                "noktasi belirsiz; mod UNKNOWN kabul edildi",
                getattr(device, "code", "?"),
                [getattr(s, "key", "?") for s in adaylar],
            )
        return None
    return adaylar[0]


# ---------------------------------------------------------------------------
# Uyari tekrar bastirici (cihaz basina TEK satir)
# ---------------------------------------------------------------------------
_uyari_lock = threading.Lock()
_uyarilan: set[str] = set()


def _uyari_ver(anahtar: str) -> bool:
    with _uyari_lock:
        if anahtar in _uyarilan:
            return False
        _uyarilan.add(anahtar)
        return True


def reset_warning_state() -> None:
    """Test yardimcisi."""
    with _uyari_lock:
        _uyarilan.clear()
