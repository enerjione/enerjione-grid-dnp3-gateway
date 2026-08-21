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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

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

# ---------------------------------------------------------------------------
# HORSTMANN DNP3 DEVICE PROFILE — CROB UYUMLULUK KISITI (P0)
# ---------------------------------------------------------------------------
#
# Resmi DNP V3.0 Device Profile Document (Dipl.-Ing. H. Horstmann GmbH,
# Smart Navigator 2.0 / Pole Master) Binary Output kabiliyetini soyle ilan
# eder:
#
#     Latch On          ALWAYS        Pulse On          NEVER
#     Latch Off         ALWAYS        Pulse Off         NEVER
#     Count > 1         NEVER         Queue             NEVER
#                                     Clear Queue       NEVER
#
# 1.15.0'a kadar validator `pulse_on`/`pulse_off`u ve `count` 1..255
# araligini KABUL EDIYORDU. Yani gateway, cihazin "ASLA desteklemiyorum"
# dedigi bir istegi TELE KOYABILIYORDU.
#
# Bu bir "cihaz nasilsa reddeder" durumu DEGILDIR:
#   * G12V1 icin reddin CommandStatus'u cihaza/firmware'e gore degisir
#     (NOT_SUPPORTED gelebilir, ama sessiz/kismi davranis da mumkundur);
#   * fiziksel bir cikisa `pulse` gondermek, latch bekleyen bir operatorun
#     niyetinden BASKA bir fiziksel sonuc uretebilir;
#   * `count > 1` ayni cikisi birden fazla kez surmek demektir.
#
# Dogru yer GATEWAY SINIRIDIR: uyumsuz istek TELE HIC CIKMAZ.
#
# Bu bir DARALTMADIR (fail-safe) — hicbir yetki/tazelik/idempotency katmani
# gevsetilmedi. Uretimdeki 26 komutun tamami zaten `latch_on / count=1`
# kullaniyor (bkz. docs/RUNBOOK.md), dolayisiyla saha davranisi DEGISMEZ.

#: Horstmann profilinin izin verdigi operasyon tipleri.
HORSTMANN_OP_TYPES = frozenset({"latch_on", "latch_off"})

#: Horstmann profilinde `Count > 1 = NEVER`. Tek gecerli deger 1'dir.
#: (`count = 0` ayrica reddedilir — bkz. COUNT_MIN gerekcesi.)
HORSTMANN_COUNT = 1


#: HAM (dogrulanmamis) CROB parametresi.
#:
#: Tipi bilincli olarak `int` DEGIL: cagiranin GONDERDIGI sey aynen tasinir.
#: `int(...)` ile normalize edilseydi `"1"`, `1.5` ve `True` sessizce 1 olurdu
#: ve asagidaki tip kontrolleri GERCEK sinirda hic calismazdi. Daraltma TEK
#: yerde yapilir: `validate_command_parameters`.
RawParameter = Any


def raw_command_parameter(payload: Mapping[str, Any], key: str, default: int) -> RawParameter:
    """Ham parametreyi getirir: alan YOKSA belgelenmis varsayilan, VARSA aynen.

    ALAN YOK ile ALAN VAR AMA GECERSIZ ayrimi F6'nin merkezinde:

        alan yok            -> belgelenmis varsayilan (uyumluluk)
        alan var, gecerli   -> aynen kullanilir
        alan var, gecersiz  -> AYNEN tasinir ve validator REDDEDER

    Bunun yerine yaygin iki kalip kullanilirsa niyet SESSIZCE degisir:

        int(payload.get(key, default))     # "1"/1.5/True -> 1
        payload.get(key, default) or default   # ACIK 0 -> default

    Ikincisi somut bir acikti: backend `count: 0` gonderdiginde `or 1` bunu
    1'e ceviriyordu. DNP3'te `count=0` "islemi SIFIR kez uygula" demektir —
    yani cagiran taraf "hicbir sey yapma" derken gateway "bir kez surdu".

    `None` da varsayilana DONMEZ: JSON `null` ACIK bir gonderimdir, alanin
    yoklugu degil. Reddedilmesi gerekir ki cagiran taraf ogrensin.
    """
    if key not in payload:
        return default
    return payload[key]


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
    normal = op_type.strip().lower()
    if normal not in SUPPORTED_OP_TYPES:
        return ParameterResult(
            ParameterReason.INVALID_OP_TYPE,
            f"desteklenmeyen op_type: {op_type!r}",
        )
    # HORSTMANN PROFIL KISITI: Pulse On/Off = NEVER.
    if normal not in HORSTMANN_OP_TYPES:
        return ParameterResult(
            ParameterReason.INVALID_OP_TYPE,
            (
                f"op_type={normal!r} Horstmann DNP3 Device Profile'inda DESTEKLENMIYOR "
                f"(Pulse On/Off = NEVER); izin verilen: {sorted(HORSTMANN_OP_TYPES)}. "
                "Kalici cikis icin `latch_on` / `latch_off` kullanin."
            ),
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
    # HORSTMANN PROFIL KISITI: Count > 1 = NEVER.
    if count != HORSTMANN_COUNT:
        return ParameterResult(
            ParameterReason.INVALID_COUNT,
            (
                f"count={count} Horstmann DNP3 Device Profile'inda DESTEKLENMIYOR "
                f"(Count > 1 = NEVER); tek gecerli deger {HORSTMANN_COUNT}. "
                "Ayni cikisi birden fazla kez surmek fiziksel olarak istenmeyen "
                "bir sonuc uretebilir."
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
