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
#: edilir; DAHA fazlasi reddedilir. Cagiran taraf `clock_skew_tolerance_sec`
#: ile ezer (bkz. `COMMAND_CLOCK_SKEW_TOLERANCE_SEC`).
#:
#: 60 -> 5 DUSURULDU (F3C): sahada olculen backend-gateway saat farki ~67 ms
#: iken 60 sn, olcumun ~900 kati bir pencereydi ve saati ileri kaymis ya da
#: damgasi bozulmus bir komutun gercek yasini gizleyebilirdi.
_FUTURE_TOLERANCE_SEC = 5.0


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
    #: Backend'in bildirdigi MUTLAK son kullanma ani (`delivery_not_after`)
    #: gecmis. Kira yenilenmis olsa bile bu ani asan komut CALISTIRILMAZ.
    DELIVERY_DEADLINE_PASSED = "delivery_deadline_passed"
    #: Teslim ustverisi bozuk (ornegin `delivery_not_after` ayristirilamiyor).
    #: Fail-closed: ustverisini anlamadigimiz komutu cihaza gondermeyiz.
    DELIVERY_METADATA_INVALID = "delivery_metadata_invalid"


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
    # `object`: `/operate` HAM JSON degerini gecirir (bkz. F6 felsefesi).
    # `str | None` yazmak, tip kontrolu yapilmis izlenimi verirdi.
    created_at: object,
    *,
    now: datetime,
    max_age_sec: float,
    require_timestamp: bool,
    clock_skew_tolerance_sec: float = _FUTURE_TOLERANCE_SEC,
    delivery_not_after: str | None = None,
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
        # `created_at` METIN OLMAYABILIR: `/operate` govdesi ham JSON tasir,
        # yani sayi/liste/sozluk gelebilir. Metin olmayan ama None de olmayan
        # her deger "GONDERILDI ama okunamadi" sayilir -> fail-closed.
        # (Eskiden asagidaki `.strip()` bu durumda AttributeError atiyordu.)
        if created_at is not None and not isinstance(created_at, str):
            return FreshnessResult(
                FreshnessReason.TIMESTAMP_INVALID,
                f"created_at metin olmali, {type(created_at).__name__} geldi",
            )
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
        # SINIR DETERMINISTIK: `created_at <= now + tolerans` KABUL.
        # Karsilastirma `gelecek > tolerans` seklinde, yani TAM toleransta olan
        # damga hala kabul edilir.
        if gelecek > clock_skew_tolerance_sec:
            # Tolerans disinda gelecek bir damga saat sapmasiyla aciklanamaz.
            # Kabul etmek komutu OLUMSUZ kilardi: yasi hicbir zaman TTL'yi
            # asmaz ve sonsuza kadar "taze" gorunur.
            #
            # SAAT GERI ADIMI da buraya duser: NTP sistem saatini geri alirsa
            # mesru bir komut GELECEKTE gorunur ve reddedilir. BILINCLI TAKAS —
            # yanlislikla reddetmek, bayat bir fiziksel komutu calistirmaktan
            # iyidir; operator durumu kontrol edip yeni komut verebilir.
            return FreshnessResult(
                FreshnessReason.TIMESTAMP_FUTURE,
                f"created_at {gelecek:.3f}s GELECEKTE (tolerans {clock_skew_tolerance_sec:.3f}s)",
                age_sec=yas,
            )
        # Makul saat sapmasi: yas 0 kabul edilir.
        return FreshnessResult(
            FreshnessReason.FRESH,
            f"created_at {gelecek:.3f}s gelecekte (tolerans icinde)",
            age_sec=0.0,
        )

    if yas > max_age_sec:
        return FreshnessResult(
            FreshnessReason.EXPIRED,
            f"komut {yas:.0f}s once olusturuldu; azami yas {max_age_sec:.0f}s",
            age_sec=yas,
        )

    # --- MUTLAK SON KULLANMA ANI (F3C) -----------------------------------
    #
    # Backend'in turettigi `delivery_not_after` DEGISMEZDIR ve kira yenilense
    # bile OTELENMEZ. Yerel TTL'den AYRI olarak uygulanir cunku `max_age_sec`
    # gateway tarafinda YAPILANDIRILABILIR: daha genis ayarlanirsa backend'in
    # kapattigi pencere burada acik kalirdi. Savunma derinligi.
    #
    # Yerel TTL'DEN SONRA degerlendiriliyor: ikisi de gecmisse "cok eski" demek
    # `expired`dir ve o daha anlasilir bir sonuctur.
    if delivery_not_after is not None:
        ham_sinir = str(delivery_not_after).strip()
        if ham_sinir:
            sinir = parse_command_timestamp(ham_sinir)
            if sinir is None:
                # Ustverisini ANLAMADIGIMIZ komutu cihaza gondermeyiz.
                return FreshnessResult(
                    FreshnessReason.DELIVERY_METADATA_INVALID,
                    "delivery_not_after ayristirilamadi ya da timezone tasimiyor",
                    age_sec=yas,
                )
            # SINIR: `now <= delivery_not_after` KABUL.
            if now > sinir:
                return FreshnessResult(
                    FreshnessReason.DELIVERY_DEADLINE_PASSED,
                    f"teslim son kullanma ani gecti ({sinir.isoformat()}); "
                    "kira yenilense bile fiziksel komut calistirilmaz",
                    age_sec=yas,
                )

    return FreshnessResult(FreshnessReason.FRESH, "taze", age_sec=yas)
