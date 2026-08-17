"""Backend yanitlarinin AUTHENTICITY dogrulamasi (F4B).

NEDEN VAR
---------
`GET /config` ve `GET /pending` yanitlari `X-Config-Signature` basliginda
HMAC-SHA256 imza tasiyor. Gateway bu imzayi "baslik VARSA dogrula" seklinde
kontrol ediyordu; yani baslik DUSURULDUGUNDE payload sorgusuz kabul
ediliyordu. Bu fail-OPEN'di ve iki ucun tasidigi sey onemsiz degil:

  /config   -> cihaz listesi, IP/adres ve BINARY OUTPUT KATALOGU, yani
               gateway'deki F1/F2 yetkilendirmesinin GIRDISI
  /pending  -> FIZIKSEL KOMUT niyeti: command, dnp3_index, created_at,
               delivery_token

Saha gateway'leri backend'e duz HTTP ile baglaniyor (olculdu). Yani bu iki
uc icin imza TEK authenticity kontrolu. Basligi dusurebilen bir saldirgan
kataloğu degistirip F1/F2'yi etkisiz kilabilir ya da dogrudan komut enjekte
edebilirdi.

SOZLESME (tel bicimi DEGISMEDI)
-------------------------------
  govde  : yanitin HAM byte'lari
  alg    : HMAC-SHA256
  anahtar: gateway token
  baslik : X-Config-Signature
  bicim  : 64 karakter kucuk harf hex

`require` BAYRAGININ SINIRI
---------------------------
`REQUIRE_BACKEND_RESPONSE_SIGNATURE=false` YALNIZCA imzanin HIC GELMEDIGI
duruma izin verir (imza gondermeyen eski bir backend'e kontrollu rollback).
Baslik GELDIYSE her kosulda dogrulanir — bayrak gecersiz bir imzayi ASLA
bypass etmez. F3'teki `COMMAND_REQUIRE_TIMESTAMP` ile ayni guvenlik modeli:
bayrak mevcut kontrolu kapatmaz, yalnizca eksik legacy alana gecici izin
verir.

Bu modul SAF: exception atmaz, ag/parse/state bilmez. Karar cagiran tarafta
`GatewayConfigError`e cevrilir.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import Enum

#: Backend'in urettigi bicim. Once bu kaliba bakiyoruz ki `compare_digest`e
#: yalnizca ASCII hex ulassin — aksi halde bozuk/non-ASCII bir baslik
#: `TypeError` atar ve tek bir kotu yanit TUM komut kanalini dusururdu.
_HEX64 = re.compile(r"[0-9a-f]{64}")


class SignatureReason(str, Enum):
    """Dogrulama karari."""

    VALID = "valid"
    #: Baslik hic gelmedi ve `require` KAPALI — kontrollu rollback izni.
    LEGACY_ALLOWED = "legacy_allowed"
    #: Baslik hic gelmedi ve `require` ACIK.
    MISSING = "signature_missing"
    #: Baslik var ama bicim disi (kisa/uzun/hex disi/non-ASCII).
    MALFORMED = "signature_malformed"
    #: Bicim dogru ama deger tutmuyor — MITM/kompromize.
    MISMATCH = "signature_mismatch"


@dataclass(frozen=True)
class SignatureResult:
    reason: SignatureReason
    detail: str

    @property
    def accepted(self) -> bool:
        """Yanit islenmeye devam edebilir mi."""
        return self.reason in (SignatureReason.VALID, SignatureReason.LEGACY_ALLOWED)

    @property
    def legacy_allowed(self) -> bool:
        """Imza YOKTU ve rollback bayragi sayesinde kabul edildi.

        Cagiran taraf bunu GORUNUR bicimde loglar; sessiz kalmasi, gecici
        olmasi gereken override'in kalicilasmasi demek olurdu.
        """
        return self.reason is SignatureReason.LEGACY_ALLOWED


def verify_backend_response_signature(
    *,
    body: bytes,
    header_value: str | None,
    token: str,
    require: bool,
    context: str,
) -> SignatureResult:
    """Yanit govdesinin imzasini dogrular. Yan etkisi yoktur.

    `body`    : yanitin HAM byte'lari (parse edilmemis).
    `header_value` : `X-Config-Signature` degeri (None/bos olabilir).
    `token`   : HMAC anahtari (gateway token'i).
    `require` : imza YOKSA reddedilsin mi.
    `context` : "config" | "pending" — yalnizca mesaj metni icin.
    """
    ham = (header_value or "").strip()
    if not ham:
        if require:
            return SignatureResult(
                SignatureReason.MISSING,
                f"{context} response signature missing",
            )
        return SignatureResult(
            SignatureReason.LEGACY_ALLOWED,
            f"{context} response signature missing; "
            "REQUIRE_BACKEND_RESPONSE_SIGNATURE=false override'i ile kabul edildi",
        )

    aday = ham.lower()
    if not _HEX64.fullmatch(aday):
        # Bicim disi deger `require`den BAGIMSIZ reddedilir: gonderilmis ama
        # okunamayan bir imza "eksik" degil, BOZUKtur.
        return SignatureResult(
            SignatureReason.MALFORMED,
            f"{context} response signature malformed (64 karakter kucuk harf hex bekleniyor)",
        )

    beklenen = hmac.new(str(token or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(aday, beklenen):
        return SignatureResult(
            SignatureReason.MISMATCH,
            f"{context} response signature mismatch — MITM/kompromize olabilir, payload reddedildi",
        )

    return SignatureResult(SignatureReason.VALID, "valid")
