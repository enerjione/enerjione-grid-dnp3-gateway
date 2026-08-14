"""Fiziksel DNP3 komutlari icin TAZELIK dogrulamasi.

NEDEN VAR
---------
Komut zincirinde hicbir katmanda YAS kavrami yoktu: `PendingCommand` zaman
alani tasimiyor, backend'in `/pending` sorgusunda yas filtresi yok ve gateway
gelen her komutu — kac saat once uretildigine bakmadan — calistiriyordu.

Somut senaryo: backend'de bir komut olusturuluyor, ardindan gateway 30 dakika
kapali kaliyor (bakim, deploy, elektrik). Gateway acildiginda o komut hala
`pending` durumda bekliyor ve OLDUGU GIBI calistiriliyor. Operator o komutu
cok once vazgecilmis sayiyor olabilir; kuyrukta bekleyen `master.firmware_update`
ya da `master.software_reset` gibi bir nokta icin bu kabul edilemez.

SOZLESME
--------
Backend F3B ve sonrasi `/pending` komutlarinda timezone-aware UTC
`created_at` tasir; bayat komutu zaten teslim ETMEZ (`failed` +
`result_status='expired'`). Gateway ayni kontrolu BAGIMSIZ yapar: teslimat
ile fiziksel gonderim arasinda gecen sure yalnizca burada bilinir.

  * `created_at` VARSA  -> TTL her zaman uygulanir, bypass edilemez.
  * `created_at` YOKSA  -> `require_timestamp` bayragi karar verir.
      true (VARSAYILAN) -> fail-closed
      false             -> legacy izin + gorunur uyari

`require_timestamp` TTL'yi kapatan bir feature flag DEGILDIR. `false`
YALNIZCA gecici rollback / `created_at` gondermeyen eski bir backend'e
karsi kontrollu uyumluluk icindir; normal dagitimda kullanilmaz.

SAAT
----
Karsilastirma gateway'in kendi UTC saatiyle yapilir. Bilincli olarak kucuk
tutuldu: backend'in HTTP `Date` basligindan skew-bagisik yas hesabi (iki
degerin de backend saatinden gelmesi) daha dogru olurdu ama config client'in
yanit yolunu degistirmeyi gerektirir; ayri bir madde olarak duruyor.
Buradaki gelecek-toleransi (bkz. `_FUTURE_TOLERANCE_SEC`) makul bir skew'i
zaten sogurur. Dagitim kabulunde backend<->gateway sapmasi OLCULMELIDIR.

Gateway KENDI alis zamanini "uretim zamani" olarak KULLANAMAZ — alis zamani
her zaman "simdi"dir ve tam da yakalanmak istenen bayat komutu gorunmez
kilardi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

#: Bu kadar GELECEKTEKI damga normal saat sapmasi sayilir ve yas 0 kabul
#: edilir. Gateway ile backend saatleri arasinda birkac saniyelik fark
#: olagandir; bunu "gecersiz" saymak mesru komutlari keserdi.
_FUTURE_TOLERANCE_SEC = 60.0


class FreshnessReason(str, Enum):
    """Tazelik karari. Deger, backend'e bildirilen `status` metnidir.

    Backend `result_status` alani `String(40)` ve serbest metindir; DB'de
    enum/CHECK kisiti yoktur, dolayisiyla bu degerler ek bir backend
    degisikligi olmadan akar.
    """

    FRESH = "fresh"
    #: Damga var ve komut TTL'yi asmis.
    EXPIRED = "expired"
    #: Damga hic yok ve `require_timestamp` acik.
    TIMESTAMP_MISSING = "command_timestamp_missing"
    #: Damga var ama ayristirilamiyor ya da timezone tasimiyor.
    TIMESTAMP_INVALID = "command_timestamp_invalid"
    #: Damga tolerans disinda GELECEKTE — bozuk/supheli deger.
    TIMESTAMP_FUTURE = "command_timestamp_future"


@dataclass(frozen=True)
class FreshnessResult:
    """Tazelik karari. Exception KULLANILMAZ — bu normal bir dogrulama akisi
    ve her sonuc (kabul de red de) backend'e kalici komut sonucu olur."""

    reason: FreshnessReason
    detail: str
    #: Hesaplanabildiyse komutun yasi (saniye). Log/denetim icin.
    age_sec: float | None = None
    #: Damga YOKTU ve rollback override'i (`require_timestamp=False`) ile
    #: izin verildi. Cagiran taraf bunu gorunur bicimde loglar; sessiz
    #: kalmasi, gecici olmasi gereken override'in kalicilasmasi demek olurdu.
    legacy_allowed: bool = False

    @property
    def fresh(self) -> bool:
        return self.reason is FreshnessReason.FRESH

    @property
    def status(self) -> str:
        return self.reason.value


def parse_command_timestamp(raw: str | None) -> datetime | None:
    """ISO-8601 damgayi timezone-AWARE datetime'a cevirir; olmazsa None.

    Ham metin `PendingCommand` icinde tasinir, parse EDILMEDEN. Boylece bozuk
    bir damga parse dongusunde komutu SESSIZCE DUSURMEZ (o durumda backend
    sonucu hicbir zaman ogrenemezdi); bunun yerine burada reddedilir ve
    terminal bir sonuc uretilir.

    Timezone TASIMAYAN damga kabul EDILMEZ: naive bir degeri UTC varsaymak,
    yerel saatte uretilmis bir damgayi saatlerce kaydirir ve TTL'yi anlamsiz
    kilar.
    """
    if raw is None:
        return None
    metin = str(raw).strip()
    if not metin:
        return None
    # Python 3.10'un `fromisoformat`i 'Z' son ekini anlamaz (3.11+ anlar).
    # Backend Pydantic ile '+00:00' uretiyor ama sozlesme her ikisini de
    # kapsasin.
    if metin.endswith(("Z", "z")):
        metin = metin[:-1] + "+00:00"
    try:
        deger = datetime.fromisoformat(metin)
    except (TypeError, ValueError):
        return None
    if deger.tzinfo is None or deger.utcoffset() is None:
        return None
    return deger


def validate_command_freshness(
    created_at: str | None,
    *,
    now: datetime,
    max_age_sec: float,
    require_timestamp: bool,
) -> FreshnessResult:
    """Komutun hala calistirilacak kadar TAZE olup olmadigini soyler.

    `created_at`: backend'in bildirdigi olusturma damgasi (ham ISO metin).
    `now`: karsilastirma ani (timezone-aware).
    `max_age_sec`: TTL.
    `require_timestamp`: damga yoksa reddedilsin mi (varsayilan True;
        False yalnizca gecici rollback icindir).

    Yan etkisi yoktur.
    """
    damga = parse_command_timestamp(created_at)

    if damga is None:
        ham = (created_at or "").strip()
        if ham:
            # Deger GONDERILDI ama okunamadi (bozuk ya da timezone'suz).
            # Bu bir gecis durumu DEGIL, bozuk veridir — bayraktan bagimsiz
            # olarak fail-closed.
            return FreshnessResult(
                FreshnessReason.TIMESTAMP_INVALID,
                "created_at ayristirilamadi ya da timezone tasimiyor; timezone-aware ISO-8601 bekleniyor",
            )
        if require_timestamp:
            return FreshnessResult(
                FreshnessReason.TIMESTAMP_MISSING,
                "komut zaman damgasi yok ve COMMAND_REQUIRE_TIMESTAMP acik",
            )
        # ROLLBACK OVERRIDE: operator bilincli olarak `false` vermis
        # (orn. `created_at` gondermeyen eski bir backend'e donuldu).
        # Komut eski davranisla calisir ama bu sessiz kalmaz.
        return FreshnessResult(
            FreshnessReason.FRESH,
            "komut zaman damgasi yok; COMMAND_REQUIRE_TIMESTAMP=false override'i ile izin verildi",
            legacy_allowed=True,
        )

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    yas = (now - damga).total_seconds()

    if yas < 0:
        gelecek = -yas
        if gelecek > _FUTURE_TOLERANCE_SEC:
            # Tolerans disinda gelecek bir damga saat sapmasiyla aciklanamaz.
            # Kabul etmek komutu OLUMSUZ kilardi: yasi hicbir zaman TTL'yi
            # asmaz ve sonsuza kadar "taze" gorunur.
            return FreshnessResult(
                FreshnessReason.TIMESTAMP_FUTURE,
                f"created_at {gelecek:.0f}s GELECEKTE (tolerans {_FUTURE_TOLERANCE_SEC:.0f}s)",
                age_sec=yas,
            )
        # Makul saat sapmasi: yas 0 kabul edilir.
        return FreshnessResult(
            FreshnessReason.FRESH,
            f"created_at {gelecek:.1f}s gelecekte (tolerans icinde)",
            age_sec=0.0,
        )

    if yas > max_age_sec:
        return FreshnessResult(
            FreshnessReason.EXPIRED,
            f"komut {yas:.0f}s once olusturuldu; azami yas {max_age_sec:.0f}s",
            age_sec=yas,
        )

    return FreshnessResult(FreshnessReason.FRESH, "taze", age_sec=yas)
