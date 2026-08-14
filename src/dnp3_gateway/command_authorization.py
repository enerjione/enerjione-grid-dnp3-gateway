"""Fiziksel DNP3 komutlari icin YEREL yetkilendirme (F1 + F2).

NEDEN VAR
---------
Gateway, backend'den gelen `dnp3_index`'i HICBIR yerde dogrulamadan CROB'a
ceviriyordu. Cihazlarin konfigure binary output listesi elde OLDUGU HALDE
yetkilendirmede kullanilmiyordu; `PendingCommand.command` alani ise parse
edilip atiliyordu (repoda tek bir okuyucusu yoktu).

Bunun neyi mumkun kildigi somuttur — gercek katalogdan:

    index  7  -> master.reset_all_fcis      (zararsiz, sahada kullanilan komut)
    index  2  -> master.firmware_update
    index 22  -> master.modem_firmware_ota
    index 23  -> master.software_reset

Backend'de bir hata, yanlis bir arayuz esleme ya da kompromize bir config
token'i "FCI sifirla" niyetiyle index 2 gonderdiginde gateway bunu oldugu gibi
iletir ve saha cihazina firmware guncellemesi baslatir. Cihaz hicbir hata
dondurmez; istenmeyen seyi YAPAR.

SOZLESME (kanitlandi, uydurulmadi)
----------------------------------
Backend ham index KABUL ETMIYOR; index'i kendi katalogundan turetiyor
(`device_command_service.resolve_command_index`):

    SignalCatalog.key == f"master.{slug}"
      AND data_type == "binary_output"
      AND is_active
      AND model == <cihaz modeli>

`PendingCommand.command` bu `slug`'in ta kendisidir. Yani asagidaki kontrol
backend'in KENDI sorgusunun aynasidir — yeni bir sozlesme talebi degil,
bagimsiz bir ikinci dogrulamadir (defense-in-depth). Uretimdeki 22 komut
kaydinin 22'sinde de gecerlidir.

NORMALIZASYON YOK — BILINCLI
----------------------------
Buyuk/kucuk harf esitleme, trim, label esleme, alias ve benzeri gevsek
eslestirmeler UYGULANMAZ. Gevsek eslestirme fail-OPEN yonunde calisir:
`reset_tamper` ile `reset_tamper_alarm`'i ayni sayan bir kural, tam da
onlemeye calistigimiz "yanlis noktaya surme" hatasini uretir. Backend tam
eslesme yapiyor; gateway ondan gevsek olursa kontrol anlamini yitirir.

MODEL IZOLASYONU
----------------
Yetkilendirme cihazin KENDI sinyal setine karsi yapilmalidir
(`state.signals_for(device)`), global bir index allowlist'ine karsi DEGIL.
Ayni slug modeller arasinda farkli index'e dusebilir; olculdu:
`master.boost_mode` SN 2.0'da index 26, Pole Master Kit'te index 30.
Global bir liste ikisini de kabul eder ve komutu yanlis noktaya gonderir.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from dnp3_gateway.backend import SignalConfig

#: Backend'in slug -> katalog anahtari cevriminde kullandigi SABIT onek.
#: `signal.source` ile URETILMEZ: backend `master.` onekini hardcode ettigi
#: icin satellite kaynakli bir output'u zaten hic cozemez. Burada daha genis
#: davranmak iki tarafin sozlesmesini ayristirirdi.
COMMAND_KEY_PREFIX = "master."

#: Yalnizca bu data_type komut hedefi olabilir. G1 binary INPUT index'ine
#: yazma denemesi bu filtre sayesinde katalogda karsilik bulamaz.
BINARY_OUTPUT_DATA_TYPE = "binary_output"


class AuthorizationReason(str, Enum):
    """Yetkilendirme sonucu. Deger, backend'e bildirilen `status` metnidir."""

    AUTHORIZED = "authorized"
    #: Istenen index cihazin konfigure binary output listesinde yok.
    INDEX_NOT_AUTHORIZED = "index_not_authorized"
    #: Index gecerli ama komut NIYETI o noktayi gostermiyor (F2).
    COMMAND_INDEX_MISMATCH = "command_index_mismatch"
    #: Cihazin sinyal seti cozumlenemedi ya da hic output tasimiyor.
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    #: Komut slug'i bos/eksik — F2 dogrulanamaz, bu yuzden reddedilir.
    COMMAND_MISSING = "command_missing"


@dataclass(frozen=True)
class AuthorizationResult:
    """Yetkilendirme karari.

    Exception KULLANILMIYOR: bu normal bir dogrulama akisidir ve her sonuc
    (kabul de red de) backend'e kalici bir komut sonucu olarak bildirilir.
    """

    reason: AuthorizationReason
    detail: str
    #: Yetkili ise komutun cozumlendigi katalog noktasi (log/denetim icin).
    signal: SignalConfig | None = None

    @property
    def authorized(self) -> bool:
        return self.reason is AuthorizationReason.AUTHORIZED

    @property
    def status(self) -> str:
        """Backend `device_commands.result_status` icin kisa metin."""
        return self.reason.value


def _output_signals(signals: Iterable[SignalConfig]) -> list[SignalConfig]:
    return [s for s in signals if s.data_type == BINARY_OUTPUT_DATA_TYPE]


def authorize_output_command(
    signals: Sequence[SignalConfig] | None,
    *,
    dnp3_index: int,
    command: str | None,
) -> AuthorizationResult:
    """Komutu cihazin KENDI katalogu ile dogrular. Yan etkisi yoktur.

    `signals`: bu CIHAZA cozumlenmis sinyal seti (`state.signals_for(device)`).
    `dnp3_index`: backend'in istedigi DNP3 index'i.
    `command`: backend komut slug'i (`master.` oneki OLMADAN).

    Sira bilincli: komut slug'i once dogrulanir ki eksik slug ile F2
    YAPISAL OLARAK atlanamasin.
    """
    outputs = _output_signals(signals or ())
    if not outputs:
        return AuthorizationResult(
            AuthorizationReason.CATALOG_UNAVAILABLE,
            "cihazin sinyal setinde binary output yok ya da set cozumlenemedi",
        )

    # Bos / yalnizca bosluk iceren slug REDDEDILIR. Uretim sozlesmesinde bu
    # alan her zaman dolu (olculdu: 22/22). Bosken index-only kontrole dusmek
    # F2 icin bir bypass kapisi acardi.
    if not command or not command.strip():
        return AuthorizationResult(
            AuthorizationReason.COMMAND_MISSING,
            "komut slug'i bos; niyet dogrulanamadigi icin komut reddedildi",
        )

    hedef = next((s for s in outputs if s.dnp3_index == dnp3_index), None)
    if hedef is None:
        return AuthorizationResult(
            AuthorizationReason.INDEX_NOT_AUTHORIZED,
            f"index {dnp3_index} cihazin konfigure binary output listesinde yok",
        )

    # LITERAL karsilastirma — normalizasyon yok (bkz. modul docstring).
    beklenen_key = f"{COMMAND_KEY_PREFIX}{command}"
    if hedef.key != beklenen_key:
        return AuthorizationResult(
            AuthorizationReason.COMMAND_INDEX_MISMATCH,
            (
                f"komut {command!r} ile index {dnp3_index} ayni noktayi gostermiyor; "
                f"index bu cihazda {hedef.key!r} noktasina ait"
            ),
        )

    return AuthorizationResult(AuthorizationReason.AUTHORIZED, "yetkili", signal=hedef)
