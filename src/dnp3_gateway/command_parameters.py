"""Fiziksel DNP3 komut parametrelerinin dogrulanmasi (F6).

NEDEN VAR
---------
F1/F2 komutun DOGRU NOKTAYA gittigini, F3 komutun TAZE oldugunu garanti
ediyor. Ama noktayi ve tazeligi dogrulanmis bir komutun PARAMETRELERI
(`op_type`, `count`, `on_time_ms`, `off_time_ms`) fiziksel cagriya
denetlenmeden ulasiyordu.

Tek koruma `operate_crob` icindeki su satirlardi:

    crob.count      = int(count)
    crob.onTimeMS   = int(on_time_ms)
    crob.offTimeMS  = int(off_time_ms)

Bu KASITLI bir dogrulama degil, iki kazadan ibaretti:

  1. `int(...)` SESSIZCE DONUSTURUYORDU. `"1"`, `1.5` ve `True` degerleri
     hicbir uyari vermeden 1'e cevriliyordu. Python'da `bool` bir `int` alt
     sinifi oldugu icin `count=True` tip kontrolunden de gecerdi.
  2. Aralik disi degerler yalnizca pybind11'in tip kontrolu sayesinde
     reddediliyordu (`TypeError`), o da CROB kurulumu sirasinda — yani DNP3
     oturumu ZATEN acildiktan sonra. Sonuc "CROB olusturulamadi" gibi
     genel bir hataydi; operator icin sebep gorunmuyordu.

Ayrica gecersiz parametre hicbir yerde TERMINAL bir sonuc uretmiyordu.

BU MODUL SAF
------------
Ag, cihaz, oturum ve state bilmez; exception atmaz. Karar cagiran tarafta
terminal bir komut sonucuna cevrilir — F1/F2 (`command_authorization`) ve
F3 (`command_freshness`) ile ayni desen.

SINIRLAR OLCULEREK BULUNDU, UYDURULMADI
---------------------------------------
Degerler `opendnp3.ControlRelayOutputBlock` alanlarinin GERCEK tiplerinden
turetildi (yadnp3 3.2.1.1 ile olculdu):

    count      uint8   -> 0..255
    onTimeMS   uint32  -> 0..4294967295
    offTimeMS  uint32  -> 0..4294967295

Bu araliklarin disindaki her deger kutuphane tarafindan zaten reddedilir;
burada YALNIZCA daha erken, daha acik ve daha aciklanabilir hale getirilir.

`count = 0` AYRICA REDDEDILIR: DNP3'te gecerli bir kodlamadir ama "islemi
SIFIR kez uygula" demektir. Cihaz bunu SUCCESS ile yanitlar ve HICBIR SEY
YAPMAZ — operator komutu basarili gorur, saha degismez. Sessiz basarisizlik,
gorunur reddedilmeden daha kotudur.

ZAMANLAMA ALANLARI LATCH'TE ANLAMSIZDIR
---------------------------------------
LATCH_ON/LATCH_OFF'ta `on/off time` cihaz tarafindan yok sayilir (sahadaki
Horstmann SN2 Device Profile PULSE desteklemiyor; uretimdeki tum komutlar
`latch_on`). Yine de deger REDDEDILMEZ: `/operate` ucunun bugunku
varsayilanlari 100/100 ve bunlari kirmak F6'nin isi degil. Yalnizca teknik
araliga uyup uymadigina bakilir.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: `opendnp3.ControlRelayOutputBlock.count` alani uint8.
COUNT_MIN = 1
COUNT_MAX = 255

#: `onTimeMS` / `offTimeMS` alanlari uint32.
TIME_MS_MIN = 0
TIME_MS_MAX = 2**32 - 1

#: Desteklenen operasyon tipleri — `Yadnp3TelemetryReader._op_map` ile AYNI
#: kume. Burada tekrar taniniyor ki dogrulama DNP3 oturumu acilmadan ONCE
#: yapilabilsin; adapter'daki kontrol savunma derinligi olarak kalir.
SUPPORTED_OP_TYPES = frozenset({"latch_on", "latch_off", "pulse_on", "pulse_off"})


class ParameterReason(str, Enum):
    """Karar. Deger, backend'e bildirilen `status` metnidir (<=40 karakter)."""

    VALID = "valid"
    INVALID_OP_TYPE = "invalid_op_type"
    INVALID_COUNT = "invalid_count"
    INVALID_TIMING = "invalid_timing"


@dataclass(frozen=True)
class ParameterResult:
    reason: ParameterReason
    detail: str

    @property
    def valid(self) -> bool:
        return self.reason is ParameterReason.VALID

    @property
    def status(self) -> str:
        return self.reason.value


def _tam_sayi_mi(deger: object) -> bool:
    """Gercek `int` mi — `bool` KABUL EDILMEZ.

    Python'da `isinstance(True, int)` True'dur. `count=True` sessizce 1 olarak
    islenirdi; tip kontrolu yapiyormus gibi gorunen bir kod bunu kacirir.
    """
    return isinstance(deger, int) and not isinstance(deger, bool)


def validate_command_parameters(
    *,
    op_type: object,
    count: object,
    on_time_ms: object,
    off_time_ms: object,
) -> ParameterResult:
    """CROB parametrelerini dogrular. Yan etkisi yoktur.

    Tip donusumu YAPMAZ: `"1"`, `1.5` ve `True` reddedilir. Sessiz coercion,
    backend'in gonderdigi seyle cihaza gidenin ayrismasi demektir.
    """
    if not isinstance(op_type, str):
        return ParameterResult(
            ParameterReason.INVALID_OP_TYPE,
            f"op_type metin olmali, {type(op_type).__name__} geldi",
        )
    # `.lower()` MEVCUT davranistir (bkz. `_op_map` cagrisi) ve korunuyor;
    # yeni bir alias/fuzzy esleme EKLENMEDI.
    if op_type.strip().lower() not in SUPPORTED_OP_TYPES:
        return ParameterResult(
            ParameterReason.INVALID_OP_TYPE,
            f"desteklenmeyen op_type: {op_type!r}",
        )

    if not _tam_sayi_mi(count):
        return ParameterResult(
            ParameterReason.INVALID_COUNT,
            f"count tam sayi olmali (bool degil), {type(count).__name__} geldi",
        )
    if not (COUNT_MIN <= count <= COUNT_MAX):
        return ParameterResult(
            ParameterReason.INVALID_COUNT,
            (
                f"count araliginda degil: {count} (izin verilen {COUNT_MIN}..{COUNT_MAX}); "
                "0 'islemi sifir kez uygula' demektir ve cihaz hicbir sey yapmadan "
                "SUCCESS doner"
            ),
        )

    for ad, deger in (("on_time_ms", on_time_ms), ("off_time_ms", off_time_ms)):
        if not _tam_sayi_mi(deger):
            return ParameterResult(
                ParameterReason.INVALID_TIMING,
                f"{ad} tam sayi olmali (bool degil), {type(deger).__name__} geldi",
            )
        if not (TIME_MS_MIN <= deger <= TIME_MS_MAX):
            return ParameterResult(
                ParameterReason.INVALID_TIMING,
                f"{ad} araliginda degil: {deger} (izin verilen {TIME_MS_MIN}..{TIME_MS_MAX})",
            )

    return ParameterResult(ParameterReason.VALID, "valid")
