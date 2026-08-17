"""Kuyruklanmis komut duzlemi icin AYRI credential (F5B — gateway hazirligi).

NEDEN
-----
Bugun `/config` ile `/pending` ayni `GATEWAY_TOKEN` ile korunuyor. O token
sizarsa yalnizca konfigurasyon degil FIZIKSEL KOMUT duzlemi de ele gecer:
saldirgan komut enjekte edebilir ve `/pending` yanitini gecerli imzayla
uretebilir.

`GATEWAY_COMMAND_DELIVERY_TOKEN` komut duzlemini kimlik duzleminden ayirir:

  /config                  -> X-Gateway-Token            (kimlik)
  /pending                 -> + X-Gateway-Command-Token  (komut duzlemi)
  /command-delivery-acks   -> + X-Gateway-Command-Token
  /command-results         -> + X-Gateway-Command-Token

ve `/pending` YANIT IMZASININ anahtari komut token'i olur.

BU DOSYANIN KILITLEDIGI EN ONEMLI SEY
-------------------------------------
GERI DUSME YOK: komut token'i yapilandirilmisken `/pending` yaniti kimlik
token'iyla imzalanmissa REDDEDILIR. Geri dusulseydi ayrimin guvenlik degeri
sifir olurdu — config duzlemini ele geciren biri komut duzlemini de
imzalayabilirdi.

GECIS DURUMU
------------
Backend F5A henuz sahada degil. Token BOSKEN davranis v1.10 ile birebir
ayni kalir; asagidaki "legacy" testleri bunu kilitler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from dnp3_gateway.auth import COMMAND_TOKEN_HEADER, GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient
from dnp3_gateway.backend.config_client import GatewayConfigError

KIMLIK_TOKEN = "kimlik-token-yeterince-uzun"
KOMUT_TOKEN = "komut-duzlemi-token-yeterince-uzun"


def _imza(body: bytes, token: str) -> str:
    return hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()


class _Resp:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        imza_token: str | None = KIMLIK_TOKEN,
        etag: str | None = None,
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
        if imza_token is not None:
            self.headers["X-Config-Signature"] = _imza(self.content, imza_token)

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int = 65536, decode_unicode: bool = False):  # noqa: ARG002
        if self.content:
            yield self.content

    def close(self) -> None:
        return None


class _Session:
    """Gonderilen basliklari kaydeder; ayni yaniti doner."""

    def __init__(self, yanit: _Resp) -> None:
        self._yanit = yanit
        self.get_headers: list[dict[str, str]] = []
        self.post_headers: list[dict[str, str]] = []
        self.post_urls: list[str] = []

    def get(self, url: str, **kw: Any) -> _Resp:  # noqa: ARG002
        self.get_headers.append(dict(kw.get("headers") or {}))
        return self._yanit

    def post(self, url: str, **kw: Any) -> _Resp:
        self.post_urls.append(url)
        self.post_headers.append(dict(kw.get("headers") or {}))
        return self._yanit


def _identity() -> GatewayIdentity:
    return GatewayIdentity(
        gateway_code="GW-001",
        token=KIMLIK_TOKEN,
        instance_id="test",
        app_version="0.0.0-test",
        app_environment="development",
    )


def _client(session: _Session, *, komut_token: str = "") -> BackendConfigClient:
    return BackendConfigClient(
        base_url="http://backend/api/v1",
        identity=_identity(),
        session=session,  # type: ignore[arg-type]
        command_delivery_token=komut_token,
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
_PENDING = {"commands": [], "config_nonce": 4, "refresh_nonce": 2, "is_active": True}
_ACK_YANIT = {"accepted": 1, "rejected": 0}


# ==========================================================================
# 1-5. LEGACY MOD — komut token'i BOS (bugunku saha durumu)
# ==========================================================================


def test_legacy_config_istegi_degismedi() -> None:
    s = _Session(_Resp(_CONFIG))
    _client(s).fetch_config()
    h = s.get_headers[0]
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert COMMAND_TOKEN_HEADER not in h


def test_legacy_pending_istegi_komut_basligi_tasimaz() -> None:
    s = _Session(_Resp(_PENDING))
    _client(s).fetch_pending_commands()
    h = s.get_headers[0]
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert COMMAND_TOKEN_HEADER not in h, "token yokken bos baslik gonderilmemeli"


def test_legacy_pending_yaniti_kimlik_tokeniyla_dogrulanir() -> None:
    """Backend F5A oncesi: `/pending` imzasi hala kimlik token'iyla."""
    s = _Session(_Resp(_PENDING, imza_token=KIMLIK_TOKEN))
    assert _client(s).fetch_pending_commands().config_nonce == 4


def test_legacy_ack_istegi_eski_sozlesme() -> None:
    s = _Session(_Resp(_ACK_YANIT))
    _client(s).report_delivery_acks([{"command_id": 1, "delivery_token": "x"}])
    h = s.post_headers[0]
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert COMMAND_TOKEN_HEADER not in h


def test_legacy_result_istegi_eski_sozlesme() -> None:
    s = _Session(_Resp({"updated": 1}))
    _client(s).report_command_results([{"id": 1, "ok": True, "status": "ok"}])
    h = s.post_headers[0]
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert COMMAND_TOKEN_HEADER not in h


# ==========================================================================
# 6-17. F5 MOD — komut token'i YAPILANDIRILMIS
# ==========================================================================


def test_pending_istegi_komut_basligi_tasir() -> None:
    s = _Session(_Resp(_PENDING, imza_token=KOMUT_TOKEN))
    _client(s, komut_token=KOMUT_TOKEN).fetch_pending_commands()
    h = s.get_headers[0]
    assert h[COMMAND_TOKEN_HEADER] == KOMUT_TOKEN


def test_kimlik_basligi_da_korunur() -> None:
    """7: komut token'i kimligin YERINE GECMEZ; ikisi birlikte gider."""
    s = _Session(_Resp(_PENDING, imza_token=KOMUT_TOKEN))
    _client(s, komut_token=KOMUT_TOKEN).fetch_pending_commands()
    h = s.get_headers[0]
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert h["X-Gateway-Code"] == "GW-001"
    assert h[COMMAND_TOKEN_HEADER] == KOMUT_TOKEN


def test_ack_istegi_komut_basligi_tasir() -> None:
    s = _Session(_Resp(_ACK_YANIT))
    c = _client(s, komut_token=KOMUT_TOKEN)
    c.report_delivery_acks([{"command_id": 1, "delivery_token": "x"}])
    h = s.post_headers[0]
    assert h[COMMAND_TOKEN_HEADER] == KOMUT_TOKEN
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert "command-delivery-acks" in s.post_urls[0]


def test_result_istegi_komut_basligi_tasir() -> None:
    s = _Session(_Resp({"updated": 1}))
    c = _client(s, komut_token=KOMUT_TOKEN)
    c.report_command_results([{"id": 1, "ok": True, "status": "ok"}])
    h = s.post_headers[0]
    assert h[COMMAND_TOKEN_HEADER] == KOMUT_TOKEN
    assert h["X-Gateway-Token"] == KIMLIK_TOKEN
    assert "command-results" in s.post_urls[0]


def test_pending_yaniti_komut_tokeniyla_dogrulanir() -> None:
    s = _Session(_Resp(_PENDING, imza_token=KOMUT_TOKEN))
    assert _client(s, komut_token=KOMUT_TOKEN).fetch_pending_commands().config_nonce == 4


def test_kritik_kimlik_tokeniyla_imzalanmis_pending_reddedilir() -> None:
    """11: GERI DUSME YOK — ayrimin tum guvenlik degeri buna dayanir.

    Komut token'i yapilandirilmisken backend yaniti kimlik token'iyla
    imzalarsa kabul edilseydi, config duzlemini ele geciren biri komut
    duzlemini de imzalayabilirdi.
    """
    s = _Session(_Resp(_PENDING, imza_token=KIMLIK_TOKEN))
    with pytest.raises(GatewayConfigError, match="signature mismatch"):
        _client(s, komut_token=KOMUT_TOKEN).fetch_pending_commands()


@pytest.mark.parametrize(
    ("imza_token", "kalip"),
    [(None, "signature missing"), (KIMLIK_TOKEN, "mismatch"), ("baska-komut-tokeni", "mismatch")],
)
def test_pending_dogrulanmamis_yanit_reddedilir(imza_token, kalip: str) -> None:
    """12/14: eksik ve yanlis-anahtar imzalar reddedilir."""
    s = _Session(_Resp(_PENDING, imza_token=imza_token))
    with pytest.raises(GatewayConfigError, match=kalip):
        _client(s, komut_token=KOMUT_TOKEN).fetch_pending_commands()


def test_pending_bozuk_imza_reddedilir() -> None:
    """13: malformed — kalip on-dogrulamasi F4B'den aynen korunuyor."""
    r = _Resp(_PENDING, imza_token=KOMUT_TOKEN)
    r.headers["X-Config-Signature"] = "kisa"
    with pytest.raises(GatewayConfigError, match="malformed"):
        _client(_Session(r), komut_token=KOMUT_TOKEN).fetch_pending_commands()


def test_config_kimlik_tokeniyla_dogrulanir() -> None:
    """15: `/config` komut token'indan ETKILENMEZ."""
    s = _Session(_Resp(_CONFIG, imza_token=KIMLIK_TOKEN))
    assert _client(s, komut_token=KOMUT_TOKEN).fetch_config().config_version == "v1"


def test_config_komut_tokeniyla_imzalanmissa_reddedilir() -> None:
    """16: config kimlik duzlemine aittir; komut anahtari orada gecmez."""
    s = _Session(_Resp(_CONFIG, imza_token=KOMUT_TOKEN))
    with pytest.raises(GatewayConfigError, match="signature mismatch"):
        _client(s, komut_token=KOMUT_TOKEN).fetch_config()


def test_config_istegi_komut_basligi_tasimaz() -> None:
    """17: kimlik duzlemi istegi komut sirrini SIZDIRMAZ."""
    s = _Session(_Resp(_CONFIG, imza_token=KIMLIK_TOKEN))
    _client(s, komut_token=KOMUT_TOKEN).fetch_config()
    assert COMMAND_TOKEN_HEADER not in s.get_headers[0]


# ==========================================================================
# 18-22. SIR SIZINTISI
# ==========================================================================


def test_istisna_metninde_iki_token_da_temizlenir() -> None:
    """20: `requests` istisnasi baslik/URL yansitabilir; ikisi de scrub."""
    from dnp3_gateway.backend.config_client import _scrub_token_from_text

    ham = f"boom token={KIMLIK_TOKEN} cmd={KOMUT_TOKEN}"
    temiz = _scrub_token_from_text(ham, KIMLIK_TOKEN, KOMUT_TOKEN)
    assert KIMLIK_TOKEN not in temiz
    assert KOMUT_TOKEN not in temiz
    assert temiz.count("***REDACTED***") == 2


def test_hicbir_token_logda_gorunmez(caplog) -> None:
    """18/19: normal akista iki sir de log'a dusmemeli."""
    caplog.set_level("DEBUG")
    s = _Session(_Resp(_PENDING, imza_token=KOMUT_TOKEN))
    _client(s, komut_token=KOMUT_TOKEN).fetch_pending_commands()
    assert KIMLIK_TOKEN not in caplog.text
    assert KOMUT_TOKEN not in caplog.text


def test_production_komut_tokeni_kimlik_tokeniyla_ayni_olamaz(monkeypatch) -> None:
    """21: ayni deger verilirse ayrim KAGIT UZERINDE kalir."""
    from dnp3_gateway.config import Settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("GATEWAY_TOKEN", "A" * 48)
    monkeypatch.setenv("GATEWAY_COMMAND_DELIVERY_TOKEN", "A" * 48)
    with pytest.raises(Exception, match="GATEWAY_COMMAND_DELIVERY_TOKEN"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_komut_tokeni_operate_tokeniyla_ayni_olamaz(monkeypatch) -> None:
    """22: iki uc FARKLI yetki duzlemi."""
    from dnp3_gateway.config import Settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("GATEWAY_TOKEN", "A" * 48)
    monkeypatch.setenv("GATEWAY_COMMAND_TOKEN", "B" * 48)
    monkeypatch.setenv("GATEWAY_COMMAND_DELIVERY_TOKEN", "B" * 48)
    with pytest.raises(Exception, match="GATEWAY_COMMAND_DELIVERY_TOKEN"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_bos_komut_tokeni_kabul_edilir(monkeypatch) -> None:
    """PREP asamasi: backend F5A sahaya cikana kadar bos olmasi hata DEGIL."""
    from dnp3_gateway.config import Settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")
    monkeypatch.setenv("BACKEND_API_URL", "https://api.enerjione.local/api/v1")
    monkeypatch.setenv("GATEWAY_TOKEN", "A" * 48)
    monkeypatch.delenv("GATEWAY_COMMAND_DELIVERY_TOKEN", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.gateway_command_delivery_token == ""


def test_varsayilan_bos(monkeypatch) -> None:
    from dnp3_gateway.config import Settings

    monkeypatch.delenv("GATEWAY_COMMAND_DELIVERY_TOKEN", raising=False)
    assert Settings(_env_file=None).gateway_command_delivery_token == ""  # type: ignore[call-arg]


# ==========================================================================
# 23-28. YAN ETKI GUVENLIGI — dogrulanmamis pending
# ==========================================================================


@pytest.mark.parametrize("imza_token", [None, KIMLIK_TOKEN, "yanlis-token"])
def test_dogrulanmamis_pending_komut_ve_nonce_uretmez(imza_token) -> None:
    """23-28: red JSON parse'tan ONCE; `PendingPoll` bile olusmaz.

    Dolayisiyla state'e komut/nonce girmez ve `ledger.start_dispatch`,
    delivery ACK, `operate_device` yollarina ULASILAMAZ — hepsi
    `PendingPoll`den SONRA gelir.
    """
    from dnp3_gateway.state import GatewayState

    dolu = {
        "commands": [
            {
                "id": 77,
                "device_code": "SN2_0",
                "command": "reset_all_fcis",
                "dnp3_index": 7,
                "delivery_token": "SAHTE",
            }
        ],
        "config_nonce": 99,
        "refresh_nonce": 99,
        "is_active": True,
    }
    st = GatewayState()
    c = _client(_Session(_Resp(dolu, imza_token=imza_token)), komut_token=KOMUT_TOKEN)

    with pytest.raises(GatewayConfigError):
        poll = c.fetch_pending_commands()
        st.apply_pending_poll(poll)  # buraya ULASILMAMALI

    assert st.take_pending_commands() == []
    assert st.take_config_refresh_request() is False
    assert st.take_refresh_request() is False
