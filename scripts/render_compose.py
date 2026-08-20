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
        --initiating-ports 20100-20199 \
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


# ---------------------------------------------------------------------------
# INITIATING DINLEYICI PORTLARI (G-INIT-02)
# ---------------------------------------------------------------------------
#
# NEDEN GEREKLI: `ip_endpoint_type=initiating` cihazlarda baglantiyi CIHAZ
# baslatir. Gateway container ici `master_ip_port` uzerinde TCP server acar;
# bridge ag modunda o port HOST'a acilmazsa Horstmann hicbir zaman
# baglanamaz. Sablon 1.12.0'a kadar YALNIZCA health portunu yayinliyordu.
#
# NEDEN ARALIK (cihaz basina tek tek degil): compose gateway OLUSTURULURKEN
# renderlanir, cihazlar SONRA eklenir. Cihaz basina port yayini her yeni
# cihazda compose'u yeniden render + container recreate demek olurdu.
# Onceden ayrilmis bir blok, cihaz eklemeyi container'a dokunmadan mumkun
# kilar — sahada asil isteyecegimiz sey budur.
#
# NEDEN KIMLIK ESLEMESI (host:container ayni): cihaz `master_ip_port`a
# baglanir ve gateway container ICINDE ayni portu dinler. Host tarafinda
# farkli bir port kullanmak zinciri koparirdi.
#
# NEDEN HOST NETWORKING DEGIL: host ag modu container'in TUM host
# arayuzlerini paylasmasi demektir; dar bir port blogu yerine sinirsiz
# maruziyet olurdu. Ayrica ayni host'ta N gateway izolasyonunu kaybederdik.
#
# MALIYET (dokumante edilmeli): Docker varsayilan `userland-proxy: true`
# ile yayinlanan HER port icin bir `docker-proxy` sureci baslatir. 100'luk
# bir blok ~100 kucuk surec demektir. Blogu gercek initiating cihaz sayisina
# gore olcun; gerekirse daemon'da `userland-proxy: false` kullanin.
#: Ayricalikli portlar reddedilir — container root olmayan kullanici ile
#: kosar ve <1024 araligini bind EDEMEZ (config parser ile ayni kural).
INITIATING_PORT_MIN = 1024
INITIATING_PORT_MAX = 65535

#: Tek bir gateway icin yayinlanabilecek TOPLAM port sayisi tavani.
#: Kaza eseri `1024-65535` yazilmasi binlerce docker-proxy sureci uretirdi.
INITIATING_PORT_MAX_TOTAL = 512


def parse_initiating_ports(raw: str | None) -> list[tuple[int, int]]:
    """`"20100-20199"` / `"20100"` / `"20100-20149,20300"` -> [(bas, son), ...].

    Bos/None -> bos liste (port YAYINLANMAZ; 1.12.0 davranisi korunur).

    Dogrulama BILEREK katidir: bu deger dogrudan uretilen YAML'e giriyor.
    Bicimsiz bir metni oldugu gibi kopyalamak, compose'u calisma zamaninda
    anlasilmaz bir hatayla dusururdu.
    """
    if raw is None:
        return []
    metin = raw.strip()
    if not metin:
        return []

    araliklar: list[tuple[int, int]] = []
    for parca_ham in metin.split(","):
        parca = parca_ham.strip()
        if not parca:
            raise RenderError(f"initiating port listesinde bos oge: {raw!r}")
        if "-" in parca:
            bolum = parca.split("-")
            if len(bolum) != 2:
                raise RenderError(f"gecersiz port araligi: {parca!r} (beklenen: BAS-SON)")
            bas_m, son_m = bolum[0].strip(), bolum[1].strip()
        else:
            bas_m = son_m = parca
        if not (bas_m.isdigit() and son_m.isdigit()):
            raise RenderError(f"port sayisal degil: {parca!r}")
        bas, son = int(bas_m), int(son_m)
        if bas > son:
            raise RenderError(f"port araliginda BAS > SON: {parca!r}")
        for deger in (bas, son):
            if not (INITIATING_PORT_MIN <= deger <= INITIATING_PORT_MAX):
                raise RenderError(
                    f"port aralik disi: {deger} (izin verilen: "
                    f"{INITIATING_PORT_MIN}..{INITIATING_PORT_MAX}). Ayricalikli "
                    "portlar reddedilir: container root olmayan kullaniciyla kosar."
                )
        araliklar.append((bas, son))

    # CAKISMA KONTROLU — ayni rendera iki kez ayni portu yayinlamak compose'u
    # calisma zamaninda dusurur; burada yakalamak cok daha ucuzdur.
    sirali = sorted(araliklar)
    for onceki, sonraki in zip(sirali, sirali[1:], strict=False):
        if sonraki[0] <= onceki[1]:
            raise RenderError(
                f"initiating port araliklari cakisiyor: {onceki[0]}-{onceki[1]} ve {sonraki[0]}-{sonraki[1]}"
            )

    toplam = sum(son - bas + 1 for bas, son in araliklar)
    if toplam > INITIATING_PORT_MAX_TOTAL:
        raise RenderError(
            f"toplam yayinlanan port sayisi cok yuksek: {toplam} "
            f"(tavan {INITIATING_PORT_MAX_TOTAL}). Docker her port icin bir "
            "docker-proxy sureci baslatir; blogu gercek cihaz sayisina gore olcun."
        )
    return sirali


#: Sablondaki yer tutucunun render sonrasi aldigi bicim. Bu SATIRIN TAMAMI
#: gercek eslemelerle degistirilir ya da silinir (bkz. `_yerlestir_portlar`).
_PORT_SENTINEL = "__INITIATING_PORTS__"


def _render_initiating_ports(
    araliklar: list[tuple[int, int]], *, bind_host: str, indent: str = "      "
) -> list[str]:
    """Compose `ports:` girdi SATIRLARI. Bos aralik -> aciklayici yorum satiri.

    Esleme KIMLIKTIR (host == container): cihaz `master_ip_port`a baglanir ve
    gateway container icinde ayni portu dinler.
    """
    if not araliklar:
        return [
            f"{indent}# (initiating dinleyici portu ayrilmadi — bu gateway'de",
            f"{indent}#  yalnizca `listening` cihazlar var)",
        ]
    satirlar = []
    for bas, son in araliklar:
        hedef = f"{bas}-{son}" if bas != son else f"{bas}"
        onek = f"{bind_host}:" if bind_host else ""
        satirlar.append(f'{indent}- "{onek}{hedef}:{hedef}"')
    return satirlar


def _yerlestir_portlar(rendered: str, satirlar: list[str]) -> str:
    """Sentinel SATIRINI gercek port eslemeleriyle degistir.

    Neden satir-degistirme (duz yer tutucu yerine): sablonun KENDISI gecerli
    YAML kalmali — testler ve editorler onu dogrudan parse ediyor. Bu yuzden
    sablonda gecerli bir liste ogesi (`- "..."`) duruyor ve tam satir burada
    degistiriliyor.
    """
    cikti: list[str] = []
    bulundu = False
    for satir in rendered.splitlines():
        if _PORT_SENTINEL in satir:
            bulundu = True
            cikti.extend(satirlar)
            continue
        cikti.append(satir)
    if not bulundu:
        raise RenderError(
            "compose sablonunda initiating port yer tutucusu bulunamadi — sablon ile renderlayici uyumsuz"
        )
    son = "\n".join(cikti)
    return son + "\n" if rendered.endswith("\n") else son


#: Uretim imajinin GHCR yolu. Etiket VERSION'dan turetilir.
IMAGE_REPO = "ghcr.io/enerjione/enerjione-grid-dnp3-gateway"


def _surum() -> str:
    """Depodaki VERSION dosyasi."""
    return (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()


def default_image() -> str:
    """`--image` verilmediginde kullanilacak EXACT semver imaj.

    Eskiden varsayilan `:latest` idi. Uretim compose ciktisinin `:latest`e
    bagli kalmasi iki sey demekti: (1) "bu kurulumda hangi surum var"
    sorusu dosyaya bakarak CEVAPLANAMAZ, (2) siradan bir
    `docker compose pull` gateway'i sessizce baska bir surume gecirir.

    `:latest` YAYIN politikasi degismedi (release workflow onu basmaya devam
    eder); degisen yalnizca uretilen compose'un neye SABITLENDIGI.
    """
    return f"{IMAGE_REPO}:{_surum()}"


INSTALL_MODES = ("local", "remote")


def _validate_bool_flag(value: bool) -> str:
    """Compose'a yazilacak bool metni.

    RENDER EDILMIS VARSAYILAN budur; operator onu compose'u DUZENLEMEDEN
    `.env` uzerinden ezebilir (bkz. sablondaki `${VAR:-varsayilan}`).
    """
    return "true" if value else "false"


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
    image: str | None = None,
    app_environment: str = "production",
    initiating_ports: str | None = None,
    initiating_bind_host: str = "0.0.0.0",
    device_health_enabled: bool = False,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
) -> str:
    """Tek bir gateway icin docker compose YAML'i uretir.

    `initiating_ports`: `ip_endpoint_type=initiating` cihazlarin dinleneceGi
    HOST port blogu (orn. `"20100-20199"`). Verilmezse port YAYINLANMAZ ve
    ciktinin davranisi 1.12.0 ile ayni kalir — yani yalnizca `listening`
    cihazlari olan kurulumlar etkilenmez.
    """

    _validate_code(code)
    if len(token) < 16:
        raise RenderError("GATEWAY_TOKEN cok kisa (>=16 karakter olmali)")
    if not 1 <= host_port <= 65535:
        raise RenderError(f"host_port aralik disi: {host_port}")

    araliklar = parse_initiating_ports(initiating_ports)
    # SAGLIK PORTU CAKISMASI: health 127.0.0.1'e baglanir ama ayni host portu
    # iki kez yayinlanamaz. Sessizce uretip compose'u calisma zamaninda
    # dusurmektense burada acikca reddediyoruz.
    for bas, son in araliklar:
        if bas <= host_port <= son:
            raise RenderError(
                f"host_port {host_port} initiating port blogu {bas}-{son} ICINDE; "
                "saglik portu ile dinleyici blogu cakisamaz"
            )

    template = template_path.read_text(encoding="utf-8")
    rendered = _render_text(
        template,
        {
            "GATEWAY_CODE": code,
            "GATEWAY_CODE_LOWER": code.lower(),
            "GATEWAY_TOKEN": token,
            "GATEWAY_NAME": name,
            "BACKEND_API_URL": backend_url.rstrip("/"),
            "NATS_URL": nats_url,
            "HOST_HEALTH_PORT": str(host_port),
            "IMAGE": image or default_image(),
            "APP_ENVIRONMENT": app_environment,
            "INSTALL_MODE": _validate_install_mode(install_mode),
            "DEVICE_HEALTH_ENABLED_DEFAULT": _validate_bool_flag(device_health_enabled),
            "INITIATING_PORT_MAPPINGS": _PORT_SENTINEL,
        },
    )
    return _yerlestir_portlar(rendered, _render_initiating_ports(araliklar, bind_host=initiating_bind_host))


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
        default=None,
        help=(
            "Docker image. Verilmezse VERSION dosyasindan EXACT semver turetilir "
            "(orn. ghcr.io/enerjione/enerjione-grid-dnp3-gateway:1.11.4). Uretim ciktisi bilerek `:latest`e BAGLANMAZ."
        ),
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
        "--initiating-ports",
        default=None,
        help=(
            "initiating cihazlarin dinlenecegi HOST port blogu "
            "(orn. 20100-20199 veya 20100-20149,20300). Verilmezse port "
            "YAYINLANMAZ — yalnizca `listening` cihazi olan kurulumlar icin "
            "dogru olan budur. Ayni host'ta N gateway varsa bloklar AYRIK olmali."
        ),
    )
    p.add_argument(
        "--initiating-bind-host",
        default="0.0.0.0",
        help=(
            "initiating portlarinin baglanacagi host arayuzu. Varsayilan tum "
            "arayuzler; saha agina bakan tek bir IP vererek maruziyeti daraltin."
        ),
    )
    p.add_argument(
        "--device-health-enabled",
        action="store_true",
        help=(
            "Cihaz basina calisma-zamani saglik kanalini RENDER edilmis varsayilan "
            "olarak ACIK yapar (POST /gateways/{code}/device-health). VARSAYILAN KAPALI: "
            "backend ucu tanimadan acilirsa her turda 404 alinir. "
            "BU BAYRAK OLMADAN DA sonradan acilabilir — render edilmis compose "
            "`${DEVICE_HEALTH_PUBLISH_ENABLED:-...}` kullanir, yani compose'u "
            "DUZENLEMEDEN `.env` ile ezmek yeterlidir."
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
            initiating_ports=args.initiating_ports,
            initiating_bind_host=args.initiating_bind_host,
            device_health_enabled=args.device_health_enabled,
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
