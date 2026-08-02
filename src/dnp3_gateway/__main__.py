"""`py -m dnp3_gateway` giris noktasi.

Coklu instance / esnek deploy:
  Ayni dizinde N gateway calistirmak icin her birine ayri `.env` ve farkli
  HEALTH_PORT verilir. Cihaz, sinyal, strategy, polling parametreleri backend
  tarafindan dinamik gelir; .env yalniz kimlik (GATEWAY_CODE/TOKEN), backend
  URL ve makine-yerel parametreleri tutar.

Ornekler:
  py -m dnp3_gateway
  py -m dnp3_gateway --env-file .env.gw002
  py -m dnp3_gateway --gateway-code GW-003 --health-port 8022
  py -m dnp3_gateway --health-port 0    # OS'tan rastgele bos port al
"""

from __future__ import annotations

import argparse
import os
import sys

from dnp3_gateway import __version__
from dnp3_gateway.config import ConfigError, load_settings
from dnp3_gateway.main import run

#: Konfigurasyon gecersiz. Diger hatalardan ayirt edilebilsin diye ayri kod;
#: NSSM/Docker tarafinda "yanlis ayar" ile "cokme" ayrilabilir.
EXIT_CONFIG_ERROR = 78  # sysexits.h EX_CONFIG


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dnp3_gateway",
        description=(
            "EnerjiOne DNP3 Gateway. Saha DNP3 outstation cihazlarini poll edip "
            "telemetriyi NATS JetStream'e basar. Ayni PC'de coklu instance icin "
            "--env-file ile farkli .env, --health-port ile farkli port verin."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help="Yuklenecek .env dosyasi (varsayilan: ./.env). Coklu instance icin .env.gw002 vb.",
    )
    parser.add_argument(
        "--gateway-code",
        default=None,
        metavar="CODE",
        help="GATEWAY_CODE override (env'i ezer). Backend kayitli ayni kod olmali.",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "WORKER_HEALTH_PORT override (env'i ezer). 0 verirseniz OS'tan "
            "rastgele bos port atanir; gercek port log + /health icinde gorunur."
        ),
    )
    parser.add_argument(
        "--max-parallel-devices",
        type=int,
        default=None,
        metavar="N",
        help="MAX_PARALLEL_DEVICES override (env'i ezer). Yuksek deger -> daha hizli cycle.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dnp3-gateway {__version__}",
    )
    return parser


def _apply_cli_overrides(args: argparse.Namespace) -> None:
    """CLI argumanlarini Settings inşasi oncesinde process env'ine yazar."""
    if args.gateway_code:
        os.environ["GATEWAY_CODE"] = args.gateway_code
    if args.health_port is not None:
        os.environ["WORKER_HEALTH_PORT"] = str(args.health_port)
    if args.max_parallel_devices is not None:
        os.environ["MAX_PARALLEL_DEVICES"] = str(args.max_parallel_devices)


def _report_config_error(exc: ConfigError, env_file: str | None) -> None:
    """Konfigurasyon hatasini OPERATORE yonelik bicimde yazdirir.

    Hem stderr'e hem stdout'a yaziyoruz: NSSM `AppStdout`/`AppStderr` yalnizca
    birini yonlendirmis olabilir ve bu mesaj sahadaki TEK teshis izidir
    (logging henuz kurulmadi, health server acilmadi).
    """
    kaynak = env_file or ".env"
    metin = "\n".join(
        [
            "",
            "=" * 72,
            "  KONFIGURASYON HATASI — gateway baslatilamadi",
            "=" * 72,
            "",
            str(exc),
            "",
            f"  Kontrol edilecek dosya : {kaynak}",
            "  Ayarlarin tam listesi  : .env.example",
            "  Kurulum rehberi        : docs/RUNBOOK.md",
            "",
            "  Duzeltip servisi yeniden baslatin.",
            "=" * 72,
            "",
        ]
    )
    print(metin, file=sys.stderr, flush=True)
    print(metin, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    _apply_cli_overrides(args)

    # Settings ARGPARSE'TAN SONRA kurulur: `--env-file` ve `--help` bu noktaya
    # kadar islenmis olur. Eskiden dogrulama `config` modulu import edilirken
    # calisiyor, bu yuzden `--help` bile bozuk bir .env yuzunden patliyordu.
    try:
        if args.env_file:
            cfg = load_settings(_env_file=args.env_file)
        else:
            cfg = load_settings()
    except ConfigError as exc:
        _report_config_error(exc, args.env_file)
        return EXIT_CONFIG_ERROR
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
