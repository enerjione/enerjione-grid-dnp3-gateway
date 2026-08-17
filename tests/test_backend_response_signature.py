"""Backend yanit imzasi FAIL-CLOSED (F4B).

KAPATILAN ACIK
--------------
Gateway `X-Config-Signature`'i "baslik VARSA dogrula" seklinde kontrol
ediyordu; baslik DUSURULDUGUNDE payload sorgusuz kabul ediliyordu. Bu iki
ucun tasidigi sey onemsiz degil:

  /config   -> cihaz listesi ve BINARY OUTPUT KATALOGU, yani gateway'deki
               F1/F2 yetkilendirmesinin GIRDISI
  /pending  -> FIZIKSEL KOMUT niyeti: command, dnp3_index, created_at,
               delivery_token

Saha gateway'leri backend'e duz HTTP ile baglaniyor (olculdu). Yani imza bu
iki uc icin TEK authenticity kontrolu; basligi dusurebilen bir saldirgan
katalogu degistirip F1/F2'yi etkisiz kilabilir ya da komut enjekte edebilirdi.

BU DOSYANIN KILITLEDIGI EN ONEMLI IKI SEY
-----------------------------------------
1. `REQUIRE_BACKEND_RESPONSE_SIGNATURE=false` rollback modu GECERSIZ bir
   imzayi bypass ETMEZ — yalnizca imzanin HIC gelmedigi duruma izin verir.
2. Dogrulanmamis bir `/config` yaniti `_last_config`/`_last_etag` onbellegine
   GIRMEZ; aksi halde 304 zinciri zehirlenir ve dogrulanmamis katalog
   kalicilasirdi.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient
from dnp3_gateway.backend.config_client import GatewayConfigError
from dnp3_gateway.backend.response_signature import (
    SignatureReason,
    verify_backend_response_signature,
)

TOKEN = "tok"


def _imza(body: bytes, token: str = TOKEN) -> str:
    return hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _sonuc(header, *, body: bytes = b"{}", require: bool = True, token: str = TOKEN):
    return verify_backend_response_signature(
        body=body, header_value=header, token=token, require=require, context="config"
    )


# ==========================================================================
# 1-12. Saf helper
# ==========================================================================


def test_gecerli_imza_kabul() -> None:
    assert _sonuc(_imza(b"{}")).reason is SignatureReason.VALID


def test_gecersiz_imza_reddedilir() -> None:
    assert _sonuc("a" * 64).reason is SignatureReason.MISMATCH


def test_eksik_imza_strict_modda_reddedilir() -> None:
    assert _sonuc(None).reason is SignatureReason.MISSING
    assert _sonuc("").reason is SignatureReason.MISSING


def test_eksik_imza_rollback_modunda_kabul() -> None:
    sonuc = _sonuc(None, require=False)
    assert sonuc.reason is SignatureReason.LEGACY_ALLOWED
    assert sonuc.accepted and sonuc.legacy_allowed


def test_kritik_rollback_modu_gecersiz_imzayi_bypass_etmez() -> None:
    """5: bayrak yalnizca EKSIK imzaya izin verir; GELEN imza her kosulda dogrulanir."""
    assert _sonuc("a" * 64, require=False).reason is SignatureReason.MISMATCH
    assert _sonuc("kisa", require=False).reason is SignatureReason.MALFORMED
    assert not _sonuc("a" * 64, require=False).accepted


def test_yalnizca_bosluk_eksik_sayilir() -> None:
    """6: whitespace-only baslik 'eksik' semantigi tasir."""
    assert _sonuc("   ").reason is SignatureReason.MISSING
    assert _sonuc("\t\n", require=False).reason is SignatureReason.LEGACY_ALLOWED


@pytest.mark.parametrize(
    ("deger", "aciklama"),
    [
        ("a" * 63, "7: cok kisa"),
        ("a" * 65, "8: cok uzun"),
        ("z" * 64, "9: hex disi"),
        ("g" * 64, "9: hex disi (g)"),
        ("!" * 64, "9: noktalama"),
        ("ç" * 64, "10: non-ASCII"),
        ("½" * 64, "10: non-ASCII"),
        ("0x" + "a" * 62, "9: prefiks"),
        (json.dumps({"sig": "x"}), "10: JSON"),
    ],
)
def test_bozuk_imza_kontrollu_reddedilir(deger: str, aciklama: str) -> None:
    """7-10: hicbiri crash uretmemeli — `compare_digest` non-ASCII'de TypeError atar."""
    assert _sonuc(deger).reason is SignatureReason.MALFORMED, aciklama


def test_buyuk_harf_hex_kabul_edilir() -> None:
    """Bicim normalize edilir; backend kucuk harf uretir ama tolere ediyoruz."""
    assert _sonuc(_imza(b"{}").upper()).reason is SignatureReason.VALID


def test_govde_tek_byte_degisince_reddedilir() -> None:
    """11: imza TAM govde byte'lari uzerinden."""
    imza = _imza(b'{"a":1}')
    assert _sonuc(imza, body=b'{"a":1}').reason is SignatureReason.VALID
    assert _sonuc(imza, body=b'{"a":2}').reason is SignatureReason.MISMATCH


def test_baska_token_ile_dogrulanmaz() -> None:
    """12: token rotasyonu — eski anahtarla uretilmis imza gecmez."""
    assert _sonuc(_imza(b"{}", token="eski"), token="yeni").reason is SignatureReason.MISMATCH


def test_bos_token_crash_uretmez() -> None:
    assert _sonuc(_imza(b"{}"), token="").reason is SignatureReason.MISMATCH


# ==========================================================================
# HTTP client kosumu
# ==========================================================================


class _Resp:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        imza: str | None = "__auto__",
        etag: str | None = None,
        token: str = TOKEN,
    ) -> None:
        self.status_code = status_code
        body = json.dumps(payload)
        self.text = body
        self.content = body.encode("utf-8")
        self.headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.content)),
        }
        if etag:
            self.headers["ETag"] = etag
        if imza == "__auto__":
            self.headers["X-Config-Signature"] = _imza(self.content, token)
        elif imza is not None:
            self.headers["X-Config-Signature"] = imza
        self.kapatildi = False

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int = 65536, decode_unicode: bool = False):  # noqa: ARG002
        if self.content:
            yield self.content

    def close(self) -> None:
        self.kapatildi = True


class _Session:
    """Sirayla verilen yanitlari doner; gonderilen header'lari kaydeder."""

    def __init__(self, *yanitlar: _Resp) -> None:
        self._yanitlar = list(yanitlar)
        self.istek_headerlari: list[dict[str, str]] = []

    def get(self, url: str, **kw: Any) -> _Resp:  # noqa: ARG002
        self.istek_headerlari.append(dict(kw.get("headers") or {}))
        return self._yanitlar.pop(0) if len(self._yanitlar) > 1 else self._yanitlar[0]


def _identity() -> GatewayIdentity:
    return GatewayIdentity(
        gateway_code="GW-001",
        token=TOKEN,
        instance_id="test",
        app_version="0.0.0-test",
        app_environment="development",
    )


def _client(session: _Session, *, require: bool = True) -> BackendConfigClient:
    return BackendConfigClient(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=session,  # type: ignore[arg-type]
        require_response_signature=require,
    )


_CONFIG = {
    "gateway_code": "GW-001",
    "gateway_name": "T",
    "batch_interval_sec": 5,
    "max_devices": 10,
    "is_active": True,
    "config_version": "v1",
    "devices": [],
    "signals": [],
}
_PENDING = {"commands": [], "config_nonce": 7, "refresh_nonce": 3, "is_active": True}


# ==========================================================================
# 13-21. /config
# ==========================================================================


def test_config_imzali_200_parse_edilir() -> None:
    c = _client(_Session(_Resp(_CONFIG)))
    assert c.fetch_config().config_version == "v1"


def test_config_eksik_imza_strict_reddedilir() -> None:
    c = _client(_Session(_Resp(_CONFIG, imza=None)))
    with pytest.raises(GatewayConfigError, match="signature missing"):
        c.fetch_config()


def test_config_gecersiz_imza_reddedilir() -> None:
    c = _client(_Session(_Resp(_CONFIG, imza="b" * 64)))
    with pytest.raises(GatewayConfigError, match="signature mismatch"):
        c.fetch_config()


def test_config_eksik_imza_rollback_modunda_kabul(caplog) -> None:
    caplog.set_level("WARNING")
    c = _client(_Session(_Resp(_CONFIG, imza=None)), require=False)
    assert c.fetch_config().config_version == "v1"
    assert "backend_response_signature_missing_legacy_allowed" in caplog.text
    assert TOKEN not in caplog.text or "token" not in caplog.text.lower()


def test_config_gecersiz_imza_rollback_modunda_da_reddedilir() -> None:
    """Rollback bayragi authenticity'yi kapatmaz."""
    c = _client(_Session(_Resp(_CONFIG, imza="c" * 64)), require=False)
    with pytest.raises(GatewayConfigError, match="signature mismatch"):
        c.fetch_config()


@pytest.mark.parametrize("imza", [None, "d" * 64, "bozuk"])
def test_kritik_dogrulanmamis_config_onbellege_girmez(imza) -> None:
    """17: reddedilen yanit `_last_config`/`_last_etag`i DEGISTIRMEZ."""
    c = _client(_Session(_Resp(_CONFIG, imza=imza, etag='"v1"')))
    with pytest.raises(GatewayConfigError):
        c.fetch_config()
    assert c._last_config is None
    assert c._last_etag is None


def test_kritik_zehirlenmis_etag_sonraki_istege_tasinmaz() -> None:
    """18: dogrulanmamis yanit sonrasi `If-None-Match` GONDERILMEZ.

    Aksi halde saldirgan bir kez imzasiz 200 + ETag verip ardindan 304 ile
    dogrulanmamis katalogu kalicilastirabilirdi.
    """
    s = _Session(_Resp(_CONFIG, imza=None, etag='"kotu"'), _Resp(_CONFIG, etag='"v1"'))
    c = _client(s)
    with pytest.raises(GatewayConfigError):
        c.fetch_config()
    c.fetch_config()  # ikinci cagri basarili
    assert "If-None-Match" not in s.istek_headerlari[1]


def test_dogrulanmis_config_sonrasi_304_kabul() -> None:
    """19: verified 200 -> onbellek -> conditional istek -> 304 kabul."""
    s = _Session(_Resp(_CONFIG, etag='"v1"'), _Resp({}, status_code=304, imza=None))
    c = _client(s)
    ilk = c.fetch_config()
    ikinci = c.fetch_config()
    assert ikinci is ilk, "304'te onbellekteki config donmeliydi"
    assert s.istek_headerlari[1].get("If-None-Match") == '"v1"'


def test_304_onbellek_yokken_reddedilir() -> None:
    """20: 304 + cache yok -> reject + ETag temizlenir."""
    c = _client(_Session(_Resp({}, status_code=304, imza=None)))
    with pytest.raises(GatewayConfigError, match="304"):
        c.fetch_config()
    assert c._last_etag is None


def test_yeni_client_ilk_istegi_kosulsuz() -> None:
    """21: process yeniden basladiginda ilk istek conditional OLMAZ."""
    s = _Session(_Resp(_CONFIG, etag='"v1"'))
    _client(s).fetch_config()
    assert "If-None-Match" not in s.istek_headerlari[0]


# ==========================================================================
# 22-32. /pending
# ==========================================================================


def test_pending_imzali_200_parse_edilir() -> None:
    c = _client(_Session(_Resp(_PENDING)))
    poll = c.fetch_pending_commands()
    assert poll.config_nonce == 7
    assert poll.refresh_nonce == 3


@pytest.mark.parametrize(
    ("imza", "kalip"),
    [(None, "signature missing"), ("e" * 64, "signature mismatch"), ("kisa", "malformed")],
)
def test_pending_dogrulanmamis_yanit_reddedilir(imza, kalip: str) -> None:
    c = _client(_Session(_Resp(_PENDING, imza=imza)))
    with pytest.raises(GatewayConfigError, match=kalip):
        c.fetch_pending_commands()


def test_pending_eksik_imza_rollback_modunda_kabul(caplog) -> None:
    caplog.set_level("WARNING")
    c = _client(_Session(_Resp(_PENDING, imza=None)), require=False)
    assert c.fetch_pending_commands().config_nonce == 7
    assert "backend_response_signature_missing_legacy_allowed" in caplog.text


def test_pending_gecersiz_imza_rollback_modunda_da_reddedilir() -> None:
    c = _client(_Session(_Resp(_PENDING, imza="f" * 64)), require=False)
    with pytest.raises(GatewayConfigError, match="signature mismatch"):
        c.fetch_pending_commands()


def test_pending_govde_kurcalanirsa_reddedilir() -> None:
    """27: imza dogru govdeden uretilmis ama govde degistirilmis."""
    dogru = _Resp(_PENDING)
    kurcali = _Resp({**_PENDING, "config_nonce": 999}, imza=dogru.headers["X-Config-Signature"])
    c = _client(_Session(kurcali))
    with pytest.raises(GatewayConfigError, match="signature mismatch"):
        c.fetch_pending_commands()


@pytest.mark.parametrize("imza", [None, "1" * 64, "bozuk"])
def test_kritik_dogrulanmamis_pending_komut_uretmez(imza) -> None:
    """28-32: reddedilen yanit `PendingPoll` bile uretmez.

    Komut kuyruga girmez, nonce uygulanmaz; dolayisiyla `ledger.start_dispatch`,
    delivery ACK ve `operate_device` yollarina ULASILAMAZ — bunlarin hepsi
    `PendingPoll`den SONRA gelir.
    """
    dolu = {
        "commands": [
            {
                "id": 1,
                "device_code": "SN2_0",
                "command": "reset_all_fcis",
                "dnp3_index": 7,
                "delivery_token": "SAHTE",
            }
        ],
        "config_nonce": 42,
        "refresh_nonce": 42,
        "is_active": True,
    }
    c = _client(_Session(_Resp(dolu, imza=imza)))
    with pytest.raises(GatewayConfigError):
        c.fetch_pending_commands()


def test_dogrulanmamis_pending_state_e_uygulanmaz() -> None:
    """28/32: state'e komut ve nonce GECMEZ (uctan uca)."""
    from dnp3_gateway.state import GatewayState

    st = GatewayState()
    dolu = {
        "commands": [{"id": 9, "device_code": "D", "command": "x", "dnp3_index": 1}],
        "config_nonce": 55,
        "refresh_nonce": 55,
        "is_active": True,
    }
    c = _client(_Session(_Resp(dolu, imza=None)))
    with pytest.raises(GatewayConfigError):
        poll = c.fetch_pending_commands()
        st.apply_pending_poll(poll)  # buraya ULASILMAMALI

    assert st.take_pending_commands() == []
    assert st.take_config_refresh_request() is False
