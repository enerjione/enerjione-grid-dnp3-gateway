"""Backend `/gateways/{code}/config` endpoint'i icin kucuk HTTP client.

EnerjiOne Grid backend'i her gateway icin kendi cihaz listesini + sinyal
katalogunu donmekle yukumludur. Bu modulun gorevi:

  - Endpoint'i periyodik olarak cagirmak (her `CONFIG_REFRESH_SEC`)
  - JSON payload'unu tipli dataclass'lara (DeviceConfig / SignalConfig /
    GatewayConfig) cevirmek + defansif sema validasyonu uygulamak
  - Opsiyonel `X-Config-Signature` HMAC dogrulamasi (MITM koruma)
  - Ag / token / 5xx hatalarini `GatewayConfigError` ile raise etmek

Backend'in `config_version` hash'i ayni kaldigi surece state.update() True
donmez ve gereksiz I/O yapilmaz. Degistiginde "configuration changed" log
satiri dusurulur, disk cache de tazelenir.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import requests

from dnp3_gateway.auth import GatewayIdentity, build_config_request_headers
from dnp3_gateway.backend.http_session import build_http_session

# Modul-seviye logger. ONEMLI: eskiden bazi fonksiyonlar `import logging as
# _logging` satirini KENDI govdesinde yapiyor, baska fonksiyonlar ise ayni
# `_logging` adini import etmeden kullaniyordu. Python'da fonksiyon-lokal
# import global isim alanina girmedigi icin bu, uc ayri kod yolunda
# `NameError: name '_logging' is not defined` uretiyordu:
#   * fetch_pending_commands bozuk komut parse yolu -> TUM komut kanali olur
#   * _parse_allowed_subnets gecersiz CIDR yolu     -> config HIC cekilemez
#   * _allowed_networks_cached allowlist dolu yolu  -> ilk config fetch duser
# Tek bir modul-seviye logger ile bu sinif hata kalici olarak kapatildi.
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceConfig:
    """Tek bir DNP3 outstation cihazinin baglanti parametreleri.

    `dnp3_tcp_port` None ise gateway `.env` `DNP3_TCP_PORT` (varsayilan) kullanilir;
    aksi halde cihaz bazli TCP port (backend/frontend cihaz kaydi).

    `master_address` None ise gateway `.env` `DNP3_LOCAL_ADDRESS` (varsayilan 1)
    kullanilir; aksi halde frontend'de cihaz basina set edilen master/local addr
    (DNP3 link layer LocalAddr) kullanilir. Saha cihazi bu adresi bekler.

    `ip_endpoint_type`:
      - "listening" (default): cihaz dinler, gateway TCP client olarak baglanir
      - "initiating": cihaz master'a outbound baglanir (4G/SIM kart sahasi);
        gateway bu cihaz icin `master_ip_port` portunda TCP server acar

    `signal_profile` backend tarafindan atanan sinyal seti adidir; gateway bu
    string'i sadece tasir, anlamlandirmaz. Backend, gateway'e dondugu `signals`
    listesini bu profile gore filtreler.
    """

    code: str
    name: str
    ip_address: str
    dnp3_address: int = 1
    dnp3_tcp_port: int | None = None
    master_address: int | None = None
    ip_endpoint_type: str = "listening"
    master_ip_port: int | None = None
    poll_interval_sec: int = 2
    timeout_ms: int = 3000
    retry_count: int = 2
    signal_profile: str = "default"


@dataclass(frozen=True)
class SignalConfig:
    """Sinyal kataloğundan gelen tek satir.

    DNP3 adresleme:
      - object_group 1  : binary input
      - object_group 10 : binary output (komut)
      - object_group 20 : counter
      - object_group 30 : analog input
      - object_group 40 : analog output
      - object_group 110: octet string (seri no, firmware, custom etiketler)
    """

    key: str
    label: str
    unit: str | None
    source: str  # master | sat01 | sat02
    dnp3_class: str
    data_type: str  # analog | binary | counter | analog_output | binary_output | string
    dnp3_object_group: int
    dnp3_index: int
    scale: float
    offset: float
    supports_alarm: bool


@dataclass(frozen=True)
class PendingCommand:
    """Backend'in gonderdigi bekleyen cihaz komutu (DNP3 CROB).

    Gateway NAT arkasinda; komut config-poll ile gelir. Her komut icin
    `reader.operate_device(device, index, ...)` cagirilir ve sonuc
    `POST /gateways/{code}/command-results` ile backend'e bildirilir. `id`
    idempotent — ayni id ikinci kez gelirse tekrar calistirilmaz (state dedup).
    """

    id: int
    device_code: str
    command: str
    dnp3_index: int
    op_type: str = "latch_on"
    count: int = 1
    on_time_ms: int = 0
    off_time_ms: int = 0


@dataclass(frozen=True)
class PendingPoll:
    """Hafif komut-poll yaniti (GET /gateways/{code}/pending).

    Config'ten AYRI: komutlar + iki nonce. Gateway 1sn'de bir ceker; nonce'lar
    degistiyse ilgili tetigi calistirir (config_nonce->config'i hemen cek,
    refresh_nonce->integrity poll).
    """

    commands: tuple[PendingCommand, ...] = ()
    config_nonce: int = 0
    refresh_nonce: int = 0
    is_active: bool = True


@dataclass(frozen=True)
class GatewayConfig:
    gateway_code: str
    gateway_name: str
    batch_interval_sec: int
    max_devices: int
    is_active: bool
    config_version: str
    devices: list[DeviceConfig] = field(default_factory=list)
    signals: list[SignalConfig] = field(default_factory=list)
    # Operator "tum cihazlara sorgu at" sayaci. Bir oncekiyle karsilastirilarak
    # integrity poll tetiklemesi yapilir; kalici state.py icinde tutulur.
    refresh_nonce: int = 0
    # Config degisiklik sayaci. config-poll'de senkronlanir (komut-poll otoritatif
    # tetikler). state.py bunu izler; artmissa config erken cekilir.
    config_nonce: int = 0
    # DEPRECATED: komut artik /pending endpoint'inden gelir (config'ten ayrildi).
    # Geriye uyum icin alan duruyor; yeni backend config'te BOS doner.
    pending_commands: tuple[PendingCommand, ...] = ()


class GatewayConfigError(RuntimeError):
    """Backend API config endpoint'inden gecerli bir yanit alinamadi."""


def _parse_optional_dnp3_tcp_port(item: dict[str, Any]) -> int | None:
    """Backend alan adlari: dnp3_tcp_port | dnp3_port | tcp_port. Gecerli: 1-65535."""
    for key in ("dnp3_tcp_port", "dnp3_port", "tcp_port"):
        if key not in item or item[key] is None or item[key] == "":
            continue
        try:
            p = int(item[key])
        except (TypeError, ValueError):
            continue
        if 1 <= p <= 65535:
            return p
    return None


def _parse_optional_master_address(item: dict[str, Any]) -> int | None:
    """Backend alan adlari: master_address | dnp3_master_address | local_address."""
    for key in ("master_address", "dnp3_master_address", "local_address"):
        if key not in item or item[key] is None or item[key] == "":
            continue
        try:
            a = int(item[key])
        except (TypeError, ValueError):
            continue
        if 0 <= a <= 65519:  # DNP3 link addr range (broadcast addrs hariç)
            return a
    return None


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def _rewrite_loopback_ip(ip: str, *, enabled: bool) -> str:
    """Container icinde loopback IP host.docker.internal'a cevirilir.

    Frontend'de kullanici cihazi "127.0.0.1" olarak ayarladiysa (cati yazilim
    ve gateway ayni makinada) ve gateway Docker'da calisiyorsa bu IP
    container'in kendisini gosterir. host.docker.internal Linux Docker 20.10+
    ve Docker Desktop tarafindan host'un IPv4 adresine cevrilir; compose
    template'inde bu mapping zaten "extra_hosts: host-gateway" ile garanti.
    """

    if not enabled:
        return ip
    h = (ip or "").strip().lower()
    if h in _LOOPBACK_HOSTS:
        return "host.docker.internal"
    return ip


# Backend config response icin maksimum boyut. Backend bug yapip 100MB
# garbage JSON donerse memory'de tutmaya calisirken OOM olabilir; bu sinir
# defensive korumadir. 10MB tipik 100 cihaz config'i icin (~50KB) cok cok
# yeterli.
DEFAULT_RESPONSE_MAX_BYTES = 10 * 1024 * 1024


def _scrub_token_from_text(text: str, token: str | None) -> str:
    """Hata mesajinda token tam metin olarak gorunmesin diye redaction."""
    if not token or len(token) < 6:
        return text
    # Tam token varsa ***REDACTED*** ile yer degistir; boylece exception
    # log'larina yansimaz
    return text.replace(token, "***REDACTED***")


class BackendConfigClient:
    """HTTP client: `X-Gateway-Token` + tanimli kimlik basliklari ile auth."""

    def __init__(
        self,
        *,
        base_url: str,
        identity: GatewayIdentity,
        # int/float tek deger VEYA (connect, read) tuple — requests ikisini de
        # kabul eder. Command-poll icin (connect, read) ayrimi kisa read timeout
        # verip agir config fetch'in poll'u bloke etmesini onler.
        timeout_sec: float | tuple[float, float] = 5,
        session: requests.Session | None = None,
        verify: bool | str = True,
        response_max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
        # Cihaz IP allowlist'i ARTIK DISARIDAN gecirilir. Eskiden modul-seviye
        # bir cache import-time `settings` singleton'ini okuyordu; bu, `--env-file
        # .env.GW-002` ile baslatilan instance'larda YANLIS dosyadan (kok `.env`)
        # okumaya yol aciyordu. main.py artik etkin Settings'ten cozumleyip
        # buraya enjekte eder.
        device_ip_allowlist: DeviceIpAllowlist | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.gateway_code = identity.gateway_code
        self.timeout_sec = timeout_sec
        self._response_max_bytes = max(64 * 1024, int(response_max_bytes))
        self._device_ip_allowlist = device_ip_allowlist
        # Connection-pooled session: config-refresh ve (ayri client ornegindeki)
        # command-poll thread'leri kendi session'iyla baglanti yarisi yasamaz.
        self._session = session or build_http_session(pool_maxsize=8, verify=verify)

    def fetch_config(self) -> GatewayConfig:
        url = f"{self.base_url}/gateways/{self.gateway_code}/config"
        headers = build_config_request_headers(self.identity)
        try:
            # stream=True: body'i hemen okuma; Content-Length header'i kontrol
            # edilebilsin. response.content ile sonra gercek body okunur.
            response = self._session.get(
                url,
                headers=headers,
                timeout=self.timeout_sec,
                stream=True,
            )
        except requests.RequestException as exc:
            # Token leak'i onle: requests bazen URL'i log'a yazabilir (header'da
            # token gozukmez ama defansif).
            err_text = _scrub_token_from_text(str(exc), self.identity.token)
            raise GatewayConfigError(f"config request failed: {err_text}") from exc

        # Content-Length kontrolu — backend cok buyuk response gonderirse erken
        # kestir
        try:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            content_length = 0
        if content_length > self._response_max_bytes:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
            raise GatewayConfigError(
                f"config response too large: content_length={content_length} bytes "
                f"(limit={self._response_max_bytes})"
            )

        if response.status_code != 200:
            # Body'i kucuk parca al (token leak/preview icin az miktar yeterli)
            try:
                preview_bytes = response.raw.read(2048, decode_content=True)
                preview = preview_bytes.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                preview = ""
            preview = _scrub_token_from_text(preview[:200], self.identity.token)
            raise GatewayConfigError(
                f"config request returned {response.status_code}: {preview}"
            )

        # Body'i sinirli okuma: max_bytes'i asarsa raise
        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024, decode_unicode=False):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._response_max_bytes:
                    raise GatewayConfigError(
                        f"config response exceeded limit during streaming: "
                        f"received={total} bytes limit={self._response_max_bytes}"
                    )
                chunks.append(chunk)
            body_bytes = b"".join(chunks)
        except GatewayConfigError:
            raise
        except requests.RequestException as exc:
            err_text = _scrub_token_from_text(str(exc), self.identity.token)
            raise GatewayConfigError(f"config response read failed: {err_text}") from exc

        # HMAC signature dogrulamasi (opsiyonel, geriye uyumlu).
        #
        # Backend response'a `X-Config-Signature: <hex sha256>` header
        # eklerse: gateway HMAC-SHA256(gateway_token, body_bytes) hesabini
        # `hmac.compare_digest` ile dogrular. Eslesmezse 403 raise.
        # Backend bu header'i yollamazsa (eski backend veya feature kapali)
        # gateway eskisi gibi devam eder — bu best-effort defense-in-depth,
        # ozellikle HTTPS yokken MITM korumasi saglar.
        sig_header = (response.headers.get("X-Config-Signature") or "").strip().lower()
        if sig_header:
            import hashlib as _hashlib
            import hmac as _hmac

            expected = _hmac.new(
                self.identity.token.encode("utf-8"),
                body_bytes,
                _hashlib.sha256,
            ).hexdigest()
            if not _hmac.compare_digest(sig_header, expected):
                raise GatewayConfigError(
                    "config response signature mismatch — "
                    "backend kompromize veya MITM olabilir, payload reddedildi"
                )

        try:
            import json as _json

            data: dict[str, Any] = _json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GatewayConfigError(f"config response is not json: {exc}") from exc

        if not isinstance(data, dict):
            raise GatewayConfigError(
                f"config response root must be object, got {type(data).__name__}"
            )

        # Beklenmedik parse hatalari sozlesmeyi bozmasin: caller (config-refresh
        # thread'i) GatewayConfigError bekliyor. Ciplak bir NameError/TypeError
        # generic except'e duserdi ve teshis edilemez hale gelirdi.
        try:
            return _parse_gateway_config(
                data,
                default_gateway_code=self.gateway_code,
                allowlist=self._device_ip_allowlist,
            )
        except GatewayConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GatewayConfigError(
                f"config parse failed: {type(exc).__name__}: {exc}"
            ) from exc

    def fetch_pending_commands(self) -> "PendingPoll":
        """Hafif komut-poll — GET /gateways/{code}/pending.

        Config'ten AYRI: sadece bekleyen komutlar + config_nonce + refresh_nonce
        (agir device/signal serialize YOK). Gateway 1sn'de bir cagirir. HMAC
        imza fetch_config ile ayni sekilde dogrulanir (MITM/komut enjekte koruma).

        Hata -> GatewayConfigError (caller loglar, bir sonraki poll'de tekrar dener).
        """
        import hashlib as _hashlib
        import hmac as _hmac
        import json as _json

        url = f"{self.base_url}/gateways/{self.gateway_code}/pending"
        headers = build_config_request_headers(self.identity)
        try:
            response = self._session.get(url, headers=headers, timeout=self.timeout_sec)
        except requests.RequestException as exc:
            err = _scrub_token_from_text(str(exc), self.identity.token)
            raise GatewayConfigError(f"pending request failed: {err}") from exc

        if response.status_code != 200:
            preview = _scrub_token_from_text((response.text or "")[:200], self.identity.token)
            raise GatewayConfigError(
                f"pending request returned {response.status_code}: {preview}"
            )

        body_bytes = response.content
        if len(body_bytes) > self._response_max_bytes:
            raise GatewayConfigError("pending response too large")

        # HMAC imza dogrulama (fetch_config ile ayni; best-effort, header varsa).
        sig_header = (response.headers.get("X-Config-Signature") or "").strip().lower()
        if sig_header:
            expected = _hmac.new(
                self.identity.token.encode("utf-8"), body_bytes, _hashlib.sha256
            ).hexdigest()
            if not _hmac.compare_digest(sig_header, expected):
                raise GatewayConfigError(
                    "pending response signature mismatch — MITM/kompromize, reddedildi"
                )

        try:
            data: dict[str, Any] = _json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GatewayConfigError(f"pending response is not json: {exc}") from exc
        if not isinstance(data, dict):
            raise GatewayConfigError("pending response root must be object")

        # Komutlar (defansif parse; bozuk komut atlanir).
        cmds: list[PendingCommand] = []
        raw_cmds = data.get("commands")
        if isinstance(raw_cmds, list):
            for item in raw_cmds:
                if not isinstance(item, dict):
                    continue
                try:
                    cmds.append(
                        PendingCommand(
                            id=int(item["id"]),
                            device_code=str(item["device_code"]),
                            command=str(item.get("command") or ""),
                            dnp3_index=int(item["dnp3_index"]),
                            op_type=str(item.get("op_type") or "latch_on"),
                            count=int(item.get("count", 1) or 1),
                            on_time_ms=int(item.get("on_time_ms", 0) or 0),
                            off_time_ms=int(item.get("off_time_ms", 0) or 0),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        "pending_command_parse_failed id=%r error=%s", item.get("id"), exc
                    )

        def _int(key: str) -> int:
            try:
                return int(data.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        return PendingPoll(
            commands=tuple(cmds),
            config_nonce=_int("config_nonce"),
            refresh_nonce=_int("refresh_nonce"),
            is_active=bool(data.get("is_active", True)),
        )

    def report_command_results(self, results: list[dict]) -> None:
        """Calistirilan komut sonuclarini backend'e bildirir (batch POST).

        `results`: [{id, ok, status, error?}]. Backend ilgili device_commands
        satirlarini ok/failed yapar. Auth config GET ile ayni (X-Gateway-Token).
        Bos liste -> no-op. Hata raise eder (caller loglar, komut zaten calisti).
        """
        if not results:
            return
        url = f"{self.base_url}/gateways/{self.gateway_code}/command-results"
        headers = build_config_request_headers(self.identity)
        headers["Content-Type"] = "application/json"
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=results,
                timeout=self.timeout_sec,
            )
        except requests.RequestException as exc:
            raise GatewayConfigError(
                _scrub_token_from_text(f"command-results POST failed: {exc}", self.identity.token)
            ) from exc
        if response.status_code >= 400:
            raise GatewayConfigError(
                f"command-results POST rejected: HTTP {response.status_code}"
            )


# Schema-defansif sabitler — backend kompromize olsa bile gateway'in
# kontrolsuz buyume / kaynak tukenme riskini sinirlar.
#
#   MAX_DEVICES_HARD_LIMIT: Tek gateway icin 1000 cihaz cikis durumunda bile
#     6 instance x 1000 = 6000 cihaz. Backend 10K cihaz config'i pushlasa
#     gateway TCP socket exhaustion'a girer; bu limitle kontrollu reddet.
#   MAX_SIGNALS_HARD_LIMIT: 1 cihaz x ~200 sinyal x 1000 cihaz = 200K sinyal
#     katalogu ust sinir.
#   MAX_CODE_LENGTH / MAX_LABEL_LENGTH: string field'lar; XSS/log injection +
#     bellek koruma.
_MAX_DEVICES_HARD_LIMIT = 1000
_MAX_SIGNALS_HARD_LIMIT = 5000
_MAX_CODE_LENGTH = 64
_MAX_LABEL_LENGTH = 256
_MAX_UNIT_LENGTH = 32


def _truncate(value: str, max_len: int) -> str:
    """Uzun string'i kesip uyari log atar (config tarafi sessiz silmek yerine
    bozulmus payload'u tespit edilebilir biraksin)."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= max_len:
        return s
    logger.warning(
        "config_field_truncated len=%d max=%d preview=%r",
        len(s),
        max_len,
        s[: min(40, max_len)],
    )
    return s[:max_len]


def _is_safe_device_ip(ip: str) -> bool:
    """Cihaz IP'sinin formati gecerli mi? Bos string TRUE (`initiating` modunda
    IP yok). Format-level kontrol: URL scheme, path, bosluk yasak.

    Bu validator yalniz acik bozulmalari yakalamak icin. Network-level
    allowlist icin `_is_device_ip_in_allowlist` kullanin.
    """
    if not ip or not ip.strip():
        return True
    s = ip.strip()
    # URL scheme veya path yasak (cihaz IP'si)
    if "://" in s or "/" in s or "\\" in s or " " in s:
        return False
    return True


@dataclass(frozen=True)
class DeviceIpAllowlist:
    """Cihaz IP allowlist'inin cozumlenmis hali (SSRF / ic-ag tarama korumasi).

    Uc durum ayirt edilir — bu ayrim GUVENLIK acisindan kritiktir:

      * `configured=False`            -> operator allowlist tanimlamadi.
        Geriye uyumlu davranis: tum IP'ler kabul edilir.
      * `configured=True, networks>0` -> normal calisma; sadece bu CIDR'ler.
      * `configured=True, networks=0` -> operator allowlist tanimladi AMA
        HICBIRI parse edilemedi (yazim hatasi). Bu durumda FAIL-CLOSED
        davraniriz: tum cihazlar reddedilir. Eski davranis "sessizce herkesi
        kabul et" idi — yani bir yazim hatasi guvenlik kontrolunu tamamen
        devre disi birakiyordu ve kimse fark etmiyordu.
    """

    networks: tuple[Any, ...] = ()
    configured: bool = False
    invalid_entries: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        """Allowlist istendi ama tek bir gecerli giris yok -> her seyi reddet."""
        return self.configured and not self.networks

    def allows(self, ip: str) -> bool:
        """IP bu allowlist'ten geciyor mu?

        Hostname (FQDN) gibi IP olmayan degerler allowlist aktifken reddedilir;
        gateway DNS cozumlemesi yapmaz, operator IP olarak yapilandirmalidir.
        """
        if self.fail_closed:
            return False
        if not self.configured or not self.networks:
            return True
        if not ip or not ip.strip():
            return True  # initiating mode: cihaz bize baglanir, IP'si yok
        import ipaddress as _ip

        try:
            addr = _ip.ip_address(ip.strip())
        except ValueError:
            return False
        return any(addr in net for net in self.networks)


def parse_device_ip_allowlist(raw: str | None) -> DeviceIpAllowlist:
    """`192.168.10.0/24,10.0.5.0/24` formatindaki CIDR listesini cozumler.

    Gecersiz girisler `invalid_entries` icinde raporlanir; sessizce yutulmaz.
    Bos/None giris -> `configured=False` (allowlist kapali, geriye uyumlu).
    """
    import ipaddress as _ip

    text = (raw or "").strip()
    if not text:
        return DeviceIpAllowlist()

    nets: list[Any] = []
    invalid: list[str] = []
    for part in text.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            nets.append(_ip.ip_network(s, strict=False))
        except ValueError:
            invalid.append(s)

    return DeviceIpAllowlist(
        networks=tuple(nets),
        configured=True,
        invalid_entries=tuple(invalid),
    )


def _log_allowlist_state(allowlist: DeviceIpAllowlist) -> None:
    """Allowlist durumunu bir kez, operatorun anlayacagi netlikte logla."""
    for entry in allowlist.invalid_entries:
        logger.error(
            "dnp3_device_allowed_subnets_invalid entry=%r — gecersiz CIDR; "
            "DNP3_DEVICE_ALLOWED_SUBNETS degerini duzeltin "
            "(ornek: 192.168.10.0/24,10.0.5.0/24)",
            entry,
        )
    if allowlist.fail_closed:
        logger.error(
            "dnp3_device_allowlist_fail_closed — DNP3_DEVICE_ALLOWED_SUBNETS "
            "tanimli ama tek bir gecerli CIDR yok. GUVENLIK icin TUM cihazlar "
            "reddediliyor. Ayari duzeltip gateway'i yeniden baslatin."
        )
    elif allowlist.configured:
        logger.info(
            "dnp3_device_allowed_subnets_loaded count=%d invalid=%d",
            len(allowlist.networks),
            len(allowlist.invalid_entries),
        )


# Geriye uyum: eski kod `_parse_allowed_subnets` bekliyordu.
def _parse_allowed_subnets(raw: str) -> list[Any]:
    return list(parse_device_ip_allowlist(raw).networks)


# Modul-seviye cache — SADECE allowlist enjekte edilmediginde kullanilan
# fallback (eski cagri sekli / testler). Uretimde main.py etkin Settings'ten
# cozumleyip BackendConfigClient'a gecirir, bu yol calismaz.
_ALLOWED_NETWORKS_CACHE: DeviceIpAllowlist | None = None


def _allowed_networks_cached() -> DeviceIpAllowlist:
    global _ALLOWED_NETWORKS_CACHE
    if _ALLOWED_NETWORKS_CACHE is not None:
        return _ALLOWED_NETWORKS_CACHE
    try:
        from dnp3_gateway.config import settings as _settings

        raw = _settings.dnp3_device_allowed_subnets or ""
    except Exception:  # noqa: BLE001
        raw = ""
    allowlist = parse_device_ip_allowlist(raw)
    _log_allowlist_state(allowlist)
    _ALLOWED_NETWORKS_CACHE = allowlist
    return _ALLOWED_NETWORKS_CACHE


def _is_device_ip_in_allowlist(ip: str, allowed_networks: Any) -> bool:
    """Geriye uyum sarmalayicisi (eski imza: (ip, list[network]))."""
    if isinstance(allowed_networks, DeviceIpAllowlist):
        return allowed_networks.allows(ip)
    nets = list(allowed_networks or ())
    return DeviceIpAllowlist(networks=tuple(nets), configured=bool(nets)).allows(ip)


def _safe_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    """Int parse + clamping. Negatif/asimi/None hatasiz default'a dusurur.

    Backend kompromize olsa veya bozuk veri gelse cihaz reddedilsin yerine
    default'la calismaya devam edebilsin diye truncate ediyoruz; ancak agir
    kaymalar (orn. dnp3_address=-1) sessizce maskelenmemeli — caller
    `default == lo/hi clamp` semantigine guvenmemeli.
    """
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo or n > hi:
        # Aralik disinda → default'a dus; caller log atar (defansif)
        return default
    return n


def _safe_finite_float(value: Any, default: float) -> float:
    """Float parse + finite kontrolu. inf/nan -> default. JSON payload
    poisoning'i onler (scale='inf' veya 'nan' ile JetStream consumer
    parser'ini patlatma)."""
    if value is None or value == "":
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def _parse_gateway_config(
    data: dict[str, Any],
    *,
    default_gateway_code: str,
    allowlist: DeviceIpAllowlist | None = None,
) -> GatewayConfig:
    """Bos/bozuk alanlari varsayilanlarla doldurarak GatewayConfig ureten helper.

    Defansif schema validation:
      - Devices/signals listesi MAX hard limit'e gore truncate edilir (warn log).
      - String field'lar maksimum uzunlukta truncate edilir.
      - IP field'i acik bozulmalara (URL, path) karsi reddedilir.
    Backend kompromize olunca gateway'in kaynak tuketimi/log injection'la
    yikilmasini onler.
    """

    # Env flag (default True): loopback device IP'yi container icinde
    # host.docker.internal'a cevir. Tipik kurulum (cati yazilim + simulator
    # ayni Windows host'unda) icin gerekli; ozel saha kurulumlarinda
    # kapatilabilir (REWRITE_LOOPBACK_TO_HOST=false).
    import os as _os

    rewrite = (_os.environ.get("REWRITE_LOOPBACK_TO_HOST", "true").strip().lower()
               not in ("0", "false", "no", "off"))

    devices_raw = data.get("devices") or []
    if not isinstance(devices_raw, list):
        raise GatewayConfigError(
            f"config response 'devices' list olmali (gelen tip: {type(devices_raw).__name__})"
        )
    if len(devices_raw) > _MAX_DEVICES_HARD_LIMIT:
        logger.error(
            "config_devices_overflow received=%d hard_limit=%d — fazlasi yok sayilacak. "
            "Backend kompromize olmus olabilir; INSTALLER'i bilgilendirin.",
            len(devices_raw),
            _MAX_DEVICES_HARD_LIMIT,
        )
        devices_raw = devices_raw[:_MAX_DEVICES_HARD_LIMIT]
    devices: list[DeviceConfig] = []
    rejected_ips = 0
    # Allowlist caller tarafindan (main.py -> BackendConfigClient) enjekte edilir.
    # Gecirilmediyse (eski cagri sekli / testler) modul cache'ine duseriz.
    effective_allowlist = allowlist if allowlist is not None else _allowed_networks_cached()
    for item in devices_raw:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        raw_ip = str(item.get("ip_address") or "")
        if not _is_safe_device_ip(raw_ip):
            rejected_ips += 1
            logger.warning(
                "config_device_rejected_unsafe_ip code=%r ip=%r — backend bozuk "
                "veya kompromize, cihaz atlandi",
                item.get("code"),
                raw_ip[:80],
            )
            continue
        if not effective_allowlist.allows(raw_ip):
            rejected_ips += 1
            logger.warning(
                "config_device_rejected_outside_allowlist code=%r ip=%r — "
                "DNP3_DEVICE_ALLOWED_SUBNETS disinda, cihaz atlandi (SSRF/scan koruma)",
                item.get("code"),
                raw_ip[:80],
            )
            continue
        try:
            # DNP3 link layer adresi: standart aralik 0-65519 (RFC: 65520-65535
            # rezerve). Disindaki degerler segfault uretebilir bazi binding'lerde.
            dnp3_addr = _safe_int(item.get("dnp3_address"), 1, lo=0, hi=65519)
            if item.get("dnp3_address") not in (None, "") and dnp3_addr != int(
                item.get("dnp3_address") or 0
            ):
                logger.warning(
                    "config_dnp3_address_out_of_range code=%r received=%r clamped=%d",
                    item.get("code"),
                    item.get("dnp3_address"),
                    dnp3_addr,
                )
            devices.append(
                DeviceConfig(
                    code=_truncate(item["code"], _MAX_CODE_LENGTH),
                    name=_truncate(item.get("name") or item["code"], _MAX_LABEL_LENGTH),
                    ip_address=_rewrite_loopback_ip(raw_ip, enabled=rewrite),
                    dnp3_address=dnp3_addr,
                    dnp3_tcp_port=_parse_optional_dnp3_tcp_port(item),
                    master_address=_parse_optional_master_address(item),
                    ip_endpoint_type=(
                        str(item.get("ip_endpoint_type") or "listening").strip().lower()
                        if str(item.get("ip_endpoint_type") or "").strip().lower() in ("initiating", "listening")
                        else "listening"
                    ),
                    master_ip_port=(
                        int(item["master_ip_port"])
                        if item.get("master_ip_port") not in (None, "", 0) and 1 <= int(item["master_ip_port"]) <= 65535
                        else None
                    ),
                    # Reasonable defaults + clamping (poll_interval cok kucuk
                    # ise gateway cycle'i tikar; cok buyuk ise hic okumaz).
                    poll_interval_sec=_safe_int(
                        item.get("poll_interval_sec"), 2, lo=1, hi=3600
                    ),
                    timeout_ms=_safe_int(item.get("timeout_ms"), 3000, lo=100, hi=60000),
                    retry_count=_safe_int(item.get("retry_count"), 2, lo=0, hi=20),
                    signal_profile=_truncate(
                        item.get("signal_profile") or "default",
                        _MAX_CODE_LENGTH,
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "config_device_parse_failed code=%r error=%s — cihaz atlandi",
                item.get("code"),
                exc,
            )
    if rejected_ips:
        logger.error(
            "config_rejected_devices count=%d — schema validation",
            rejected_ips,
        )

    signals_raw = data.get("signals") or []
    if not isinstance(signals_raw, list):
        raise GatewayConfigError(
            f"config response 'signals' list olmali (gelen tip: {type(signals_raw).__name__})"
        )
    if len(signals_raw) > _MAX_SIGNALS_HARD_LIMIT:
        logger.error(
            "config_signals_overflow received=%d hard_limit=%d — fazlasi yok sayilacak.",
            len(signals_raw),
            _MAX_SIGNALS_HARD_LIMIT,
        )
        signals_raw = signals_raw[:_MAX_SIGNALS_HARD_LIMIT]

    signals: list[SignalConfig] = []
    for item in signals_raw:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        try:
            # scale / offset: inf/nan reddet — JetStream consumer'a sizar ve
            # json.dumps `Infinity` cikarir (allow_nan=True default), bazi
            # tag-engine parser'lari bunu reject eder veya sessizce drop eder.
            raw_scale = item.get("scale")
            raw_offset = item.get("offset")
            scale_val = _safe_finite_float(raw_scale, 1.0)
            offset_val = _safe_finite_float(raw_offset, 0.0)
            # _safe_finite_float "inf"/"nan"/parse hatasinda default'a duser.
            # Default'a indirilen anlamli bir degerse log atalim (saldiri /
            # backend bozulmasi sinyali).
            if raw_scale not in (None, "", 1.0, "1.0") and scale_val == 1.0:
                logger.warning(
                    "config_signal_scale_invalid key=%r received=%r -> default 1.0",
                    item.get("key"),
                    raw_scale,
                )
            signals.append(
                SignalConfig(
                    key=_truncate(item["key"], _MAX_CODE_LENGTH),
                    label=_truncate(item.get("label") or item["key"], _MAX_LABEL_LENGTH),
                    unit=(_truncate(item["unit"], _MAX_UNIT_LENGTH) if item.get("unit") else None),
                    source=_truncate(item.get("source") or "master", _MAX_CODE_LENGTH),
                    dnp3_class=_truncate(item.get("dnp3_class") or "Class 1", _MAX_CODE_LENGTH),
                    data_type=_truncate(item.get("data_type") or "analog", _MAX_CODE_LENGTH),
                    # DNP3 grup ID'leri standartta 1-120; saglik icin 0-255 clamp.
                    dnp3_object_group=_safe_int(item.get("dnp3_object_group"), 30, lo=0, hi=255),
                    # DNP3 index 16-bit (0-65535).
                    dnp3_index=_safe_int(item.get("dnp3_index"), 0, lo=0, hi=65535),
                    scale=scale_val,
                    offset=offset_val,
                    supports_alarm=bool(item.get("supports_alarm", False)),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "config_signal_parse_failed key=%r error=%s — sinyal atlandi",
                item.get("key"),
                exc,
            )

    try:
        refresh_nonce = int(data.get("refresh_nonce", 0) or 0)
    except (TypeError, ValueError):
        refresh_nonce = 0

    # Bekleyen komutlar (opsiyonel; eski backend'de alan yok -> bos). Defensive
    # parse: bozuk komut atlanir, dongu durmaz.
    pending_commands: list[PendingCommand] = []
    raw_cmds = data.get("pending_commands")
    if isinstance(raw_cmds, list):
        for item in raw_cmds:
            if not isinstance(item, dict):
                continue
            try:
                pending_commands.append(
                    PendingCommand(
                        id=int(item["id"]),
                        device_code=str(item["device_code"]),
                        command=str(item.get("command") or ""),
                        dnp3_index=int(item["dnp3_index"]),
                        op_type=str(item.get("op_type") or "latch_on"),
                        count=int(item.get("count", 1) or 1),
                        on_time_ms=int(item.get("on_time_ms", 0) or 0),
                        off_time_ms=int(item.get("off_time_ms", 0) or 0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "config_pending_command_parse_failed id=%r error=%s — komut atlandi",
                    item.get("id"),
                    exc,
                )

    try:
        config_nonce = int(data.get("config_nonce", 0) or 0)
    except (TypeError, ValueError):
        config_nonce = 0

    return GatewayConfig(
        gateway_code=str(data.get("gateway_code") or default_gateway_code),
        gateway_name=str(data.get("gateway_name") or ""),
        batch_interval_sec=int(data.get("batch_interval_sec", 5) or 5),
        max_devices=int(data.get("max_devices", 200) or 200),
        is_active=bool(data.get("is_active", True)),
        config_version=str(data.get("config_version") or ""),
        devices=devices,
        signals=signals,
        refresh_nonce=refresh_nonce,
        config_nonce=config_nonce,
        pending_commands=tuple(pending_commands),
    )
