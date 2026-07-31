"""run_gateway.ps1 icin: .env yuklenmis Settings ozetini yazdir (Python tarafinda)."""

from __future__ import annotations

import os
import sys

# proje kok: scripts/..
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "src"))

from dnp3_gateway.config import Settings  # noqa: E402


def main() -> None:
    s = Settings()
    host = s.worker_health_host
    port = s.worker_health_port
    health = f"http://{host}:{port}/health"
    cfg_url = f"{s.backend_api_url.rstrip('/')}/gateways/{s.gateway_code.strip()}/config"
    print(f"[run] GATEWAY_CODE={s.gateway_code.strip()}", flush=True)
    print(
        f"[run] GATEWAY_MODE={s.gateway_mode.strip()}  APP_ENVIRONMENT={s.app_environment.strip()}",
        flush=True,
    )
    print(f"[run] Saglik (HTTP) .. {health}  (WORKER_HEALTH_PORT={port})", flush=True)
    print(
        f"[run] DNP3 (saha) .... varsayilan TCP .env DNP3_TCP_PORT={s.dnp3_tcp_port} "
        "(cihaz bazli port: backend devices[].dnp3_tcp_port)",
        flush=True,
    )
    print(f"[run] Backend config . GET {cfg_url}", flush=True)
    print(
        "[run] Coklu proses: her biri FARKLI GATEWAY_CODE + backendde ayri kayit + ayri .env veya -GatewayCode",
        flush=True,
    )


if __name__ == "__main__":
    main()
