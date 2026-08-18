"""Per-gateway docker-compose.yml renderlayici — CLI ve library olarak kullanilir.

Frontend "Yeni gateway ekle" akisi:
    1. Backend API: POST /gateways  -> code + token uretilir, DB'ye yazilir
    2. Backend API: GET  /gateways/{code}/docker-compose -> bu modulu cagirir
    3. Frontend: dosyayi indirir; kullanici sunucuda `docker compose -f gw-XXX.yml up -d`

CLI:
    python scripts/render_compose.py \
        --code GW-001 \
        --token "32-karakter-token" \
        --name "Saha A SCADA" \
        --backend-url https://api.enerjione.local/api/v1 \
        --nats-url nats://nats.enerjione.local:4222 \
        --host-port 8020 \
        --image ghcr.io/enerjione/enerjione-grid-dnp3-gateway:0.4.6 \
        --output ./gw-001.yml

Library:
    from scripts.render_compose import render_compose
    yaml_text = render_compose(code="GW-001", token=..., ...)
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
import sys
from pathlib import Path

DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "docker" / "compose.template.yml"
DEFAULT_ENV_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "docker" / ".env.template"

# Kod formati: kucuk/buyuk harf, rakam, tire — alfanumerik (URL/dosya adi guvenli).
_CODE_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")


class RenderError(ValueError):
    """Sablon renderleme hatasi."""


def generate_token(length: int = 48) -> str:
    """Yeni gateway icin yeterince guclu rastgele token. Production icin >=32."""

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _validate_code(code: str) -> None:
    if not _CODE_REGEX.match(code):
        raise RenderError(
            f"GATEWAY_CODE gecersiz: {code!r}. Kural: alfanumerik, '-' veya '_', 2-64 karakter, harf/rakamla baslar."
        )


def _render_text(template: str, replacements: dict[str, str]) -> str:
    """Cift-suslu yer tutuculari ({{KEY}}) replacements ile degistirir."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in replacements:
            raise RenderError(f"Sablonda doldurulmamis yer tutucu: {{{{ {key} }}}}")
        return replacements[key]

    return re.sub(r"\{\{\s*([A-Z0-9_]+)\s*\}\}", _sub, template)


INSTALL_MODES = ("local", "remote")


def _validate_install_mode(mode: str) -> str:
    """`local` | `remote` — VARSAYILANI YOK, bilincli secilmeli.

    Bu deger NATS erisilemedigi anda ne olacagini belirler; yanlis secim
    yalnizca ARIZA aninda gorunur (yerel kurulumda sessizce HTTP'ye dusme).
    Sessiz bir varsayilan tam da duzeltilen hatanin kaynagiydi.
    """
    if mode not in INSTALL_MODES:
        raise RenderError(f"INSTALL_MODE gecersiz: {mode!r}. Gecerli: {list(INSTALL_MODES)}")
    return mode


def render_compose(
    *,
    code: str,
    token: str,
    name: str,
    backend_url: str,
    nats_url: str,
    host_port: int,
    install_mode: str,
    image: str = "ghcr.io/enerjione/enerjione-grid-dnp3-gateway:latest",
    app_environment: str = "production",
    template_path: Path = DEFAULT_TEMPLATE_PATH,
) -> str:
    """Tek bir gateway icin docker compose YAML'i uretir."""

    _validate_code(code)
    if len(token) < 16:
        raise RenderError("GATEWAY_TOKEN cok kisa (>=16 karakter olmali)")
    if not 1 <= host_port <= 65535:
        raise RenderError(f"host_port aralik disi: {host_port}")

    template = template_path.read_text(encoding="utf-8")
    return _render_text(
        template,
        {
            "GATEWAY_CODE": code,
            "GATEWAY_CODE_LOWER": code.lower(),
            "GATEWAY_TOKEN": token,
            "GATEWAY_NAME": name,
            "BACKEND_API_URL": backend_url.rstrip("/"),
            "NATS_URL": nats_url,
            "HOST_HEALTH_PORT": str(host_port),
            "IMAGE": image,
            "APP_ENVIRONMENT": app_environment,
            "INSTALL_MODE": _validate_install_mode(install_mode),
        },
    )


def render_env(
    *,
    code: str,
    token: str,
    name: str,
    backend_url: str,
    nats_url: str,
    install_mode: str,
    app_environment: str = "production",
    template_path: Path = DEFAULT_ENV_TEMPLATE_PATH,
) -> str:
    """Per-instance .env dosyasini renderlar (compose'a alternatif --env-file akis)."""

    _validate_code(code)
    template = template_path.read_text(encoding="utf-8")
    return _render_text(
        template,
        {
            "GATEWAY_CODE": code,
            "GATEWAY_TOKEN": token,
            "GATEWAY_NAME": name,
            "BACKEND_API_URL": backend_url.rstrip("/"),
            "NATS_URL": nats_url,
            "APP_ENVIRONMENT": app_environment,
            "INSTALL_MODE": _validate_install_mode(install_mode),
        },
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render per-gateway docker compose YAML")
    p.add_argument("--code", required=True, help="Gateway kodu (orn. GW-001)")
    p.add_argument(
        "--token",
        default=None,
        help="Gateway token. Verilmezse rastgele 48-karakter uretilir ve stderr'a yazilir.",
    )
    p.add_argument("--name", default="EnerjiOne DNP3 Gateway", help="Insan-okur isim")
    p.add_argument(
        "--backend-url",
        required=True,
        help="Backend public URL (orn. https://hsl.formelektrik.com/api/v1)",
    )
    p.add_argument(
        "--nats-url",
        required=True,
        help="NATS JetStream URL (orn. nats://enerjione-nats:4222 veya tls://nats.host:4222)",
    )
    p.add_argument(
        "--host-port",
        type=int,
        required=True,
        help="Host'ta health endpoint icin acilacak port (her instance icin farkli)",
    )
    p.add_argument(
        "--image",
        default="ghcr.io/enerjione/enerjione-grid-dnp3-gateway:latest",
        help="Docker image tag (default: ghcr.io/enerjione/enerjione-grid-dnp3-gateway:latest)",
    )
    p.add_argument(
        "--install-mode",
        required=True,
        choices=INSTALL_MODES,
        help=(
            "Kurulum tipi. `local`: gateway backend ile ayni makinede — NATS zorunlu, "
            "HTTP yedegi YOK. `remote`: uzak saha — NATS birincil + HTTP yedegi. VARSAYILANI YOK: yanlis secim yalnizca ariza aninda gorunur."
        ),
    )
    p.add_argument(
        "--app-environment",
        default="production",
        choices=("development", "staging", "production"),
    )
    p.add_argument(
        "--output",
        default=None,
        help="Cikis dosya yolu (yoksa stdout'a yazar)",
    )
    p.add_argument(
        "--render-env",
        action="store_true",
        help="docker-compose yerine .env dosyasi renderla (host'ta python ile dogrudan calistirma)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    token_generated = args.token is None
    token = args.token or generate_token()
    if token_generated:
        # Token'i konsola/stderr'a yazma — screen-share, CI log, omuz-ustu
        # gozlem ile sizma riski. Sadece --output dosyasina yaz. stdout
        # render edilen yaml/.env ise token zaten dosya icindedir.
        print(
            "[render_compose] yeni token uretildi (>=48 char). "
            "--output dosyasinin icinde GATEWAY_TOKEN= satirinda; konsolda gosterilmiyor.",
            file=sys.stderr,
        )

    if args.render_env:
        rendered = render_env(
            code=args.code,
            token=token,
            name=args.name,
            backend_url=args.backend_url,
            nats_url=args.nats_url,
            install_mode=args.install_mode,
            app_environment=args.app_environment,
        )
    else:
        rendered = render_compose(
            code=args.code,
            token=token,
            name=args.name,
            backend_url=args.backend_url,
            nats_url=args.nats_url,
            host_port=args.host_port,
            install_mode=args.install_mode,
            image=args.image,
            app_environment=args.app_environment,
        )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        # Unix izinleri (mode 0o600 — sadece sahibi). Windows'ta os.chmod
        # NTFS ACL'i tam manipule etmez ama "read-only" bilgilendirici;
        # gercek koruma icin admin tarafindan icacls/NTFS ACL ayarlanmalidir.
        try:
            out_path.chmod(0o600)
        except OSError:
            pass
        print(f"[render_compose] yazildi: {out_path}  (mode 0600)", file=sys.stderr)
    else:
        if token_generated:
            print(
                "[render_compose] UYARI: --output verilmedi; rendered yaml/.env "
                "ICINDE token stdout'a yaziliyor. Bu cikti'yi disk'e bos bir "
                "dizine yonlendirip (>> dosya) dosya izinlerini hemen kisitlayin "
                "veya --output kullanin.",
                file=sys.stderr,
            )
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
