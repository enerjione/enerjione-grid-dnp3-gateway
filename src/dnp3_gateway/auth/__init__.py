"""Gateway -> backend guvenli haberlesme: kimlik, token, istek basliklari.

Birden fizik/VM uzerinde N adet proses; her biri backend'de ayri `gateways`
kaydina baglanir. Erisim: surumlu User-Agent, X-Gateway-Code (path dogrulama),
X-Gateway-Instance-Id (ornek izleme), X-Request-Id (yuruyen baglam).
"""

from dnp3_gateway.auth.headers import (
    COMMAND_TOKEN_HEADER,
    build_command_request_headers,
    build_config_request_headers,
)
from dnp3_gateway.auth.identity import (
    GatewayIdentity,
    bootstrap_gateway_identity,
    ensure_credentials_allowed,
    resolve_instance_id,
)

__all__ = [
    "GatewayIdentity",
    "bootstrap_gateway_identity",
    "COMMAND_TOKEN_HEADER",
    "build_command_request_headers",
    "build_config_request_headers",
    "ensure_credentials_allowed",
    "resolve_instance_id",
]
