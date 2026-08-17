"""Backend HTTP cagrilarina eklenecek guvenli baslik ureticileri."""

from __future__ import annotations

from uuid import uuid4

from dnp3_gateway.auth.identity import GatewayIdentity


def build_config_request_headers(identity: GatewayIdentity) -> dict[str, str]:
    """Config cekme (GET) istegi icin baslik sozlugu.

    Ileride: ayni fonksiyon uzerine HMAC, OAuth2 client assertion veya
    mTLS sertifika seri numarasi (custom header) eklenebilir; consumer
    (BackendConfigClient) tek cagrida toplu header birlestirmesi yapar.
    """

    correlation = str(uuid4())
    h: dict[str, str] = {
        "X-Gateway-Token": identity.token,
        # Path ile ayni olmali; backend path/header uyumsuzlugunda 400 doner (defans derinligi).
        "X-Gateway-Code": identity.gateway_code,
        "X-Gateway-Instance-Id": identity.instance_id,
        "X-Request-Id": correlation,
        "User-Agent": f"EnerjiOne-Dnp3Gateway/{identity.app_version} (env={identity.app_environment})",
    }
    h["X-Gateway-Client"] = f"dnp3-gateway/{identity.app_version}"
    return h


#: Kuyruklanmis komut duzlemi (queued-command plane) icin AYRI credential.
#: `/pending`, `/command-delivery-acks` ve `/command-results` bu basligi da
#: tasir. Normal kimligin (`X-Gateway-Token`) YERINE GECMEZ — iki baslik
#: birlikte gonderilir.
COMMAND_TOKEN_HEADER = "X-Gateway-Command-Token"


def build_command_request_headers(
    identity: GatewayIdentity, command_delivery_token: str | None = None
) -> dict[str, str]:
    """Kuyruklanmis komut ucları icin baslik sozlugu.

    Normal kimlik basliklarinin TAMAMINI icerir; uzerine — yalnizca token
    yapilandirilmissa — `X-Gateway-Command-Token` eklenir.

    NEDEN AYRI CREDENTIAL: bugun `/config` ile `/pending` ayni `GATEWAY_TOKEN`
    ile korunuyor. O token sizarsa yalnizca konfigurasyon degil, FIZIKSEL
    KOMUT duzlemi de ele gecer. Ayri bir sir, komut duzlemini config
    duzleminden ayirir (F5).

    BOS TOKEN ICIN BASLIK HIC EKLENMEZ. Bos string gondermek backend'de
    "credential var ama gecersiz" ile "credential yok" ayrimini bozardi;
    gecis doneminde backend'in eski gateway'i tanimasi bu ayrima dayanir.
    """
    h = build_config_request_headers(identity)
    jeton = (command_delivery_token or "").strip()
    if jeton:
        h[COMMAND_TOKEN_HEADER] = jeton
    return h
