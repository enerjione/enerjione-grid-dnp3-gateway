"""Backend `/gateways/{code}/config` endpoint'i icin kucuk HTTP client.

EnerjiOne Grid backend'i her gateway icin kendi cihaz listesini + sinyal
katalogunu donmekle yukumludur. Bu modulun gorevi:

  - Endpoint'i periyodik olarak cagirmak (her `CONFIG_REFRESH_SEC`)
  - JSON payload'unu tipli dataclass'lara (DeviceConfig / SignalConfig /
    GatewayConfig) cevirmek + defansif sema validasyonu uygulamak
  - `X-Config-Signature` HMAC dogrulamasi — ZORUNLU (bkz.
    `REQUIRE_BACKEND_RESPONSE_SIGNATURE`); dogrulanmamis yanit islenmez
  - Ag / token / 5xx hatalarini `GatewayConfigError` ile raise etmek

Backend'in `config_version` hash'i ayni kaldigi surece state.update() True
donmez ve gereksiz I/O yapilmaz. Degistiginde "configuration changed" log
satiri dusurulur, disk cache de tazelenir.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from dnp3_gateway.auth import (
    GatewayIdentity,
    build_command_request_headers,
    build_config_request_headers,
)
from dnp3_gateway.backend import health_header
from dnp3_gateway.backend.http_session import build_http_session
from dnp3_gateway.backend.response_signature import verify_backend_response_signature
from dnp3_gateway.command_parameters import RawParameter, raw_command_parameter

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

# Saglik basligi `/pending`i dusuruyorsa bu kadar sure birakilir, sonra
# yeniden denenir. Backend duzeltilince kendiliginden geri gelsin diye
# kalici degil; ama her saniye yeniden denenip cift istek uretmesin diye de
# kisa degil.
_SAGLIK_BASLIK_KAPALI_SEC = 600.0

# Rollback modunda (imza zorunlu degil) uyarinin baglam basina tekrarlanma
# araligi. `/pending` 1 Hz kosuyor; her yanitta uyarmak log'u bogardi ama
# tamamen susmak gecici override'in kalicilasmasi demek olurdu.
_IMZA_UYARI_ARALIK_SEC = 600.0


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

    #: DNP3 OTURUM YASAM DONGUSU — cihaz basina, ACIKCA yapilandirilir.
    #:
    #:   "continuous" (VARSAYILAN): bugunku davranis. Periyodik Class 1/2/3
    #:       event scan + Class 0 integrity scan + acilis integrity poll'u.
    #:       Her zaman bagli DNP3 ekipmani icin dogru olan budur.
    #:
    #:   "auto": rejim cihazin MASTER `Operation Mode` noktasindan CALISMA
    #:       ANINDA turetilir (Smart -> smart, Boost -> continuous). Mod
    #:       gozlenene kadar gateway SESSIZ kalir (bkz. docs). `smart` gibi
    #:       YALNIZCA `initiating` uc ile gecerlidir.
    #:
    #:   "smart": Horstmann Smart Navigator 2.0 Smart Mode. Cihaz raporunu
    #:       gonderir ve DNP3 oturumu 15 SANIYE bosta kalinca hucresel modemi
    #:       kapatir. Gateway periyodik tarama gonderirse bu sayac hicbir zaman
    #:       dolmaz; bu yuzden `smart` politikada gateway HICBIR tekrarlayan
    #:       tarama kurmaz ve beklenen kapanmayi comm_lost SAYMAZ.
    #:
    #: Varsayilan BILEREK "continuous": mevcut kurulumlarin davranisi
    #: degismesin. Tanimsiz bir deger SESSIZCE varsayilana DUSMEZ —
    #: konfigurasyon dogrulamasini basarisiz kilar (bkz. `_parse_session_policy`).
    #: Bkz. docs/HORSTMANN_SMART_MODE.md
    session_policy: str = "continuous"

    #: Horstmann Dial-In (zamanlanmis rapor) araligi — DAKIKA.
    #:
    #: Cihazin NE ZAMAN haber vermesi BEKLENDIGINI soyler. Gateway bunu
    #: YALNIZCA yasam dongusu beklentisi, saglik ve tanilama icin kullanir;
    #: cihaza Dial-In yapilandirmasi YAZMAZ (o sorumluluk backend/cihaz
    #: yapilandirma hattindadir).
    #:
    #: None = bilinmiyor -> `late` (gecikmis) durumu URETILMEZ ve davranis
    #: 1.13.0 ile birebir aynidir (yalnizca `smart_max_silence_sec` gecerli).
    #: Kabul araligi: 60..1440 dakika.
    dial_in_interval_min: int | None = None

    #: `session_policy=smart|auto` + `ip_endpoint_type=listening` icin:
    #: gateway'in cihaza yeniden baglanma denemelerinin UST araligi (saniye).
    #:
    #: Uyuyan bir Horstmann uyandiginda dinleyici soketini yalnizca
    #: "Socket Listening Timeout" suresince (tipik ~600 sn) acik tutar;
    #: gateway o PENCEREYI yakalamak zorundadir.
    #:
    #: None = opendnp3'un kendi kanal yeniden-baglanma davranisi (1 sn'den
    #: 60 sn'ye ustel) — 600 sn'lik bir pencerede >=10 deneme demektir ve
    #: YETERLIDIR. Deger verilirse yalnizca TAVAN daraltilir; IKINCI bir
    #: yeniden baglanma dongusu KURULMAZ.
    #: Kabul araligi: 5..600 saniye.
    smart_listen_probe_interval_sec: int | None = None

    #: `session_policy="smart"` iken: cihaz bu kadar saniye HIC gecerli DNP3
    #: kaniti gondermezse `smart_idle` durumundan `lost`a dusurulur ve mevcut
    #: comm_lost mekanizmasi calisir.
    #:
    #: None = denetim KAPALI (cihaz suresiz `smart_idle` kalabilir). Adapter'da
    #: gomulu bir "24 saat" varsayimi YOKTUR: dogru deger cihazin Dial-In
    #: rapor programina baglidir ve yalnizca kurulumu yapan bilir.
    #: Kurulum geneli yedek: `DNP3_SMART_MAX_SILENCE_SEC`.
    smart_max_silence_sec: int | None = None


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

    # ----- HAM CROB parametreleri (F6) ------------------------------------
    #
    # Tipleri bilincli olarak `int` DEGIL: backend'in GONDERDIGI sey aynen
    # tasinir. Burada `int(...)` uygulanirsa `"1"`, `1.5` ve `True` sessizce
    # 1 olur ve fiziksel validator'in tip kontrolleri GERCEK sinirda hic
    # calismaz. Ayrica ESKI `or 1` kalibi ACIK `count: 0` niyetini 1'e
    # ceviriyordu — DNP3'te `count=0` "islemi sifir kez uygula" demektir.
    #
    # Daraltma TEK yerde yapilir: `operate_device` icinde, DNP3 oturumu
    # acilmadan once (`command_parameters.validate_command_parameters`).
    # Gecersiz deger burada DUSURULMEZ; tasinir ve TERMINAL bir sonuc
    # uretir, boylece backend sebebi ogrenir — `created_at` ile ayni felsefe.
    count: RawParameter = 1
    on_time_ms: RawParameter = 0
    off_time_ms: RawParameter = 0
    # Backend'in bildirdigi OLUSTURMA damgasi (timezone-aware ISO-8601).
    #
    # HAM METIN olarak tasinir, BILINCLI: burada parse edilseydi bozuk bir
    # damga komutu parse dongusunde SESSIZCE dusururdu ve backend sonucu
    # hicbir zaman ogrenemezdi. Dogrulama `command_freshness` icinde yapilir
    # ve reddedilen komut terminal bir sonuc uretir.
    #
    # Backend F3B ve sonrasi bu alani HER taze komutta gonderir. None =
    # alan gelmedi (yalnizca F3B oncesi bir backend'e rollback halinde);
    # bu durumda komut varsayilan olarak REDDEDILIR — bkz.
    # `COMMAND_REQUIRE_TIMESTAMP`.
    created_at: str | None = None

    # ----- F3C teslim protokolu (command_delivery_ack_v1) -----------------
    #
    # Yalnizca kira/ACK protokolunu konusan backend doldurur. Backend v2.96.0
    # bu alanlari GONDERMEZ; ikisi de None kalir ve gateway eski davranisla
    # calisir (bkz. main.py teslim dali). Sessiz bir uyumsuzluk YOK: alanlarin
    # varligi protokolun hangi yolda oldugunu belirler.

    #: Bu teslimin kimligi. Gateway komutu DAYANIKLI deftere yazdiktan SONRA
    #: `POST /gateways/{code}/command-delivery-acks` ile aynen geri gonderir;
    #: backend komutu ancak o zaman `sent` sayar.
    #:
    #: OPAKTIR: cozulmez, normalize edilmez, LOGLANMAZ.
    delivery_token: str | None = None

    #: Backend'in turettigi DEGISMEZ son kullanma ani (tz-aware ISO-8601).
    #: Kira yenilense bile OTELENMEZ.
    #:
    #: HAM METIN — `created_at` ile ayni gerekce: burada parse edilseydi bozuk
    #: bir deger komutu parse dongusunde SESSIZCE dusururdu ve backend sonucu
    #: hicbir zaman ogrenemezdi. Dogrulama `command_freshness` icinde.
    delivery_not_after: str | None = None


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
    # DUZ liste — profil bilgisi olmayan/eski backend'ler icin. Tum cihazlara
    # ayni set uygulanir; bu yalnizca TEK modelli kurulumda dogrudur.
    signals: list[SignalConfig] = field(default_factory=list)
    # PROFIL (cihaz modeli) bazli setler: {profil_anahtari: [sinyaller]}.
    #
    # NEDEN: duz liste tek modelli kurulumda dogru calisir ama ikinci bir DNP3
    # modeli eklendigi anda bozulur — ayni (object_group, index) cifti iki
    # modelde FARKLI buyuklugu gosterir. Set ayrilmazsa okunan deger YANLIS
    # `signal_key` ile yayinlanir; hata sessizdir, esik alarmi baska bir
    # buyuklugun uzerinden calisir.
    #
    # Bos sozluk = eski backend (alan yok) -> duz listeye dusulur.
    signals_by_profile: dict[str, list[SignalConfig]] = field(default_factory=dict)
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


class CommandResultDeliveryError(GatewayConfigError):
    """Komut sonucu backend'e teslim edilemedi.

    `http_status` tasir; caller GECICI (yeniden dene) ile KALICI (dead-letter'a
    al ve kuyrugu ilerlet) ayrimini bu bilgiyle yapar. GatewayConfigError'dan
    turuyor ki mevcut `except GatewayConfigError` yakalayicilari bozulmasin.
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def _parse_signal_list(signals_raw: Any, *, alan: str = "signals") -> list[SignalConfig]:
    """Ham sinyal listesini dogrulayarak SignalConfig listesine cevirir.

    Hem duz `signals` hem `signals_by_profile` degerleri BURADAN gecer.
    Ayri bir kopya yazilsaydi profil listesi sessizce daha zayif
    dogrulanirdi: scale/offset icin inf/nan reddi, DNP3 grup/index clamp'i
    ve alan kirpma yalnizca duz listeye uygulanmis olurdu.
    """
    if not isinstance(signals_raw, list):
        raise GatewayConfigError(
            f"config response {alan!r} list olmali (gelen tip: {type(signals_raw).__name__})"
        )
    if len(signals_raw) > _MAX_SIGNALS_HARD_LIMIT:
        logger.error(
            "config_signals_overflow alan=%s received=%d hard_limit=%d — fazlasi yok sayilacak.",
            alan,
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
    return signals


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


#: Desteklenen TCP uc tipleri.
#:
#:   listening  — GATEWAY baglanir (TCP client). Klasik saha RTU'su.
#:   initiating — CIHAZ baglanir (TCP server, `master_ip_port` dinlenir).
#:                4G/SIM arkasindaki Horstmann'lar boyle calisir.
#:
#: Bu iki degerin anlami BIRBIRININ TERSI: kimin baglanti actigini belirler.
IP_ENDPOINT_TYPES: frozenset[str] = frozenset({"listening", "initiating"})

#: Initiating dinleyici portunun protokol seviyesindeki araligi.
#: Ayricalikli portlar (<1024) uretim politikasi geregi REDDEDILIR: container
#: root olmayan kullanici ile kosuyor ve o araligi zaten bind EDEMEZ; kabul
#: etmek, calisma zamaninda anlasilamayan bir bind hatasina donusurdu.
MASTER_IP_PORT_MIN = 1024
MASTER_IP_PORT_MAX = 65535


def _parse_ip_endpoint_type(item: dict[str, Any]) -> str:
    """Cihazin TCP uc tipi. Alan yoksa "listening" (bugunku varsayilan).

    TANIMSIZ DEGER SESSIZCE VARSAYILANA DUSMEZ — `GatewayConfigError` atar.

    Gerekce somut: `"initating"` yazim hatasi sessizce `"listening"`e
    duserse, KIMIN BAGLANTI ACTIGI tersine doner. Gateway uyuyan bir
    Horstmann'a TCP client olarak baglanmaya calisir, cihaz hicbir zaman
    baglanamaz ve saha bunu yalnizca "cihaz hic gelmedi" olarak gorur.
    Sessiz ters cevirme, config'i reddetmekten cok daha pahalidir.
    """
    ham = item.get("ip_endpoint_type")
    if ham is None or ham == "":
        return "listening"
    deger = str(ham).strip().lower()
    if deger not in IP_ENDPOINT_TYPES:
        raise GatewayConfigError(
            f"gecersiz ip_endpoint_type={ham!r} (cihaz={item.get('code')!r}); "
            f"izin verilenler: {sorted(IP_ENDPOINT_TYPES)}"
        )
    return deger


def _parse_master_ip_port(item: dict[str, Any], *, endpoint_type: str) -> int | None:
    """Initiating dinleyici portu.

    `listening` cihaz icin alan ANLAMSIZDIR ve None doner (varsa yok sayilir).

    `initiating` cihaz icin ZORUNLUDUR: eksik/0/aralik disi/bozuk deger
    `GatewayConfigError` uretir. Eskiden sessizce `None` olup adapter'da
    `DNP3_TCP_PORT`e (20000) dusuyordu — yani TUM initiating cihazlar ayni
    portu bind etmeye calisir, ilki disindakiler anlasilmaz bir soket
    hatasiyla dusuyordu.
    """
    ham = item.get("master_ip_port")
    if endpoint_type != "initiating":
        return None
    if ham is None or ham == "" or ham is False:
        raise GatewayConfigError(
            f"ip_endpoint_type=initiating icin master_ip_port ZORUNLU "
            f"(cihaz={item.get('code')!r}); cihaz gateway'e baglanacagi icin "
            f"dinlenecek port bilinmek zorunda"
        )
    try:
        port = int(ham)
    except (TypeError, ValueError):
        raise GatewayConfigError(
            f"master_ip_port sayisal degil: {ham!r} (cihaz={item.get('code')!r})"
        ) from None
    if not (MASTER_IP_PORT_MIN <= port <= MASTER_IP_PORT_MAX):
        raise GatewayConfigError(
            f"master_ip_port aralik disi: {port} (cihaz={item.get('code')!r}); "
            f"izin verilen: {MASTER_IP_PORT_MIN}..{MASTER_IP_PORT_MAX}. "
            "Ayricalikli portlar (<1024) uretim politikasi geregi reddedilir: "
            "container root olmayan kullaniciyla kosar ve o araligi bind edemez."
        )
    return port


#: Desteklenen DNP3 oturum yasam dongusu politikalari.
SESSION_POLICIES: frozenset[str] = frozenset({"continuous", "smart", "auto"})

#: Smart sessizlik denetiminin kabul edilen araligi.
#: Alt sinir 60 sn: daha kucuk bir deger NORMAL bir Smart uykusunu bile
#: offline ilan eder ve ozelligi anlamsiz kilardi. Ust sinir 30 gun: bunun
#: otesinde "offline" kavrami operatore hicbir sey soylemez.
_SMART_SILENCE_MIN_SEC = 60
_SMART_SILENCE_MAX_SEC = 30 * 24 * 3600


#: Horstmann Dial-In araliginin kabul edilen araligi (dakika).
#: Alt sinir 60: daha sik bir "zamanlanmis rapor" Smart modun enerji
#: mantigina aykiridir. Ust sinir 1440 (24 saat).
DIAL_IN_INTERVAL_MIN_MIN = 60
DIAL_IN_INTERVAL_MIN_MAX = 1440

#: Listening Smart yeniden baglanma tavaninin kabul araligi (saniye).
#: Alt sinir 5: daha sik denemek uyuyan bir modeme gereksiz radyo yuku
#: bindirir. Ust sinir 600: Horstmann'in tipik Socket Listening Timeout'u;
#: daha buyuk bir tavan uyanik pencereyi tamamen KACIRABILIR.
SMART_LISTEN_PROBE_MIN_SEC = 5
SMART_LISTEN_PROBE_MAX_SEC = 600


def _parse_optional_int(
    item: dict[str, Any], alan: str, *, lo: int, hi: int
) -> int | None:
    """Opsiyonel tamsayi alani; yoksa/gecersizse None + WARNING.

    `session_policy` gibi konfigurasyonu DUSURMEZ: bu alanlarin yanlis
    olmasi sessiz bir yanlis REJIM uretmez, yalnizca bir beklenti/tanilama
    bilgisi eksik kalir ve davranis 1.13.0'a doner.
    """
    ham = item.get(alan)
    if ham is None or ham == "":
        return None
    try:
        n = int(ham)
    except (TypeError, ValueError):
        logger.warning(
            "config_%s_invalid code=%r received=%r — yok sayildi", alan, item.get("code"), ham
        )
        return None
    if not (lo <= n <= hi):
        logger.warning(
            "config_%s_out_of_range code=%r received=%d min=%d max=%d — yok sayildi",
            alan,
            item.get("code"),
            n,
            lo,
            hi,
        )
        return None
    return n


def _parse_session_policy(item: dict[str, Any]) -> str:
    """Cihazin oturum politikasi. Alan yoksa "continuous".

    TANIMSIZ DEGER SESSIZCE VARSAYILANA DUSMEZ — `GatewayConfigError` atar.

    Gerekce: bu alan cihazin haberlesme rejimini belirliyor. `"smrt"` yazim
    hatasi sessizce "continuous"a duserse, Smart moda alinmasi gereken bir
    cihaz periyodik taranmaya devam eder; modem hicbir zaman kapanmaz ve
    kimse bunu fark etmez — tam olarak onlemeye calistigimiz hatanin sessiz
    hali. Konfigurasyon dogrulamasinin basarisiz olmasi GUVENLI taraftir:
    gateway son iyi config'iyle calismaya devam eder ve /health hatayi
    acikca raporlar.
    """
    ham = item.get("session_policy")
    if ham is None or ham == "":
        return "continuous"
    deger = str(ham).strip().lower()
    if deger not in SESSION_POLICIES:
        raise GatewayConfigError(
            f"gecersiz session_policy={ham!r} (cihaz={item.get('code')!r}); "
            f"izin verilenler: {sorted(SESSION_POLICIES)}"
        )
    return deger


def _parse_optional_smart_silence(item: dict[str, Any]) -> int | None:
    """Cihaz bazli Smart sessizlik esigi; yoksa/gecersizse None.

    None = denetim kapali; gateway kurulum geneli yedegine (varsa) duser.
    Gecersiz deger de None'a duser: uydurma bir esikle cihazi erken offline
    ilan etmektense denetimi hic yapmamak dogrudur. Bu alan `session_policy`
    gibi konfigurasyonu DUSURMEZ — cunku yanlis degerin sonucu sessiz bir
    yanlis rejim degil, yalnizca eksik bir denetimdir ve loglanir.
    """
    ham = item.get("smart_max_silence_sec")
    if ham is None or ham == "":
        return None
    try:
        n = int(ham)
    except (TypeError, ValueError):
        logger.warning(
            "config_smart_silence_invalid code=%r received=%r — yok sayildi, "
            "sessizlik denetimi bu cihaz icin kapali",
            item.get("code"),
            ham,
        )
        return None
    if not (_SMART_SILENCE_MIN_SEC <= n <= _SMART_SILENCE_MAX_SEC):
        logger.warning(
            "config_smart_silence_out_of_range code=%r received=%d min=%d max=%d — "
            "yok sayildi, sessizlik denetimi bu cihaz icin kapali",
            item.get("code"),
            n,
            _SMART_SILENCE_MIN_SEC,
            _SMART_SILENCE_MAX_SEC,
        )
        return None
    return n


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


def _scrub_token_from_text(text: str, *tokens: str | None) -> str:
    """Hata mesajinda token tam metin olarak gorunmesin diye redaction.

    BIRDEN FAZLA SIR: gateway kimlik token'i ve (F5) kuyruklanmis komut
    duzlemi token'i ayni istegin basliklarinda birlikte gider; `requests`
    bir istisnada baslik/URL yansitabilir. Ikisi de temizlenir.
    """
    for token in tokens:
        if token and len(token) >= 6:
            text = text.replace(token, "***REDACTED***")
    return text


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
        # `X-Config-Signature` ZORUNLU mu (F4B). Varsayilan fail-closed.
        # False YALNIZCA imza gondermeyen eski bir backend'e kontrollu
        # rollback icindir; baslik geldiyse her kosulda dogrulanir.
        # Bkz. `REQUIRE_BACKEND_RESPONSE_SIGNATURE`.
        require_response_signature: bool = True,
        # F5: kuyruklanmis komut duzlemi credential'i. Bos = mevcut (v1.10)
        # davranis; dolu = `/pending`, ACK ve result uclarinda ayri baslik +
        # `/pending` yanit imzasinin ANAHTARI. `/config` bunu KULLANMAZ.
        command_delivery_token: str = "",
        # Cihaz IP allowlist'i ARTIK DISARIDAN gecirilir. Eskiden modul-seviye
        # bir cache import-time `settings` singleton'ini okuyordu; bu, `--env-file
        # .env.GW-002` ile baslatilan instance'larda YANLIS dosyadan (kok `.env`)
        # okumaya yol aciyordu. main.py artik etkin Settings'ten cozumleyip
        # buraya enjekte eder.
        device_ip_allowlist: DeviceIpAllowlist | None = None,
        # Saat sapmasi gozlemcisi. Backend her yanitta HTTP `Date` basligi
        # doner; ek altyapi olmadan gateway saatinin sapmasini olcebiliyoruz.
        # Gateway saati TUM arsivin tek zaman referansidir ve daha once
        # hicbir yerde dogrulanmiyordu.
        clock_guard: Any = None,
        # Saglik ozeti saglayicisi: cagirildiginda backend'e gonderilecek
        # govdeyi dondurur (bkz. backend/health_header.py). None ise baslik
        # HIC eklenmez ve davranis eskisiyle ayni kalir.
        #
        # Callable secildi cunku saglik anlik bir durum; client'in icinde
        # kopya tutmak bayat veri gondermek olurdu.
        health_provider: Any = None,
        # F3C teslim protokolu: cagirildiginda DEFTER KIMLIGINI (epoch, str)
        # dondurur. None ise `X-E1-Delivery` basligi HIC eklenmez ve gateway
        # yetenegini bildirmemis olur — backend fail-closed davranir.
        #
        # Callable secildi cunku epoch defterin omruyle baglidir; client'in
        # icinde kopya tutmak, defter yeniden yaratildiginda BAYAT bir kimlik
        # gondermek olurdu — tam da onlemek istedigimiz sey.
        delivery_provider: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.gateway_code = identity.gateway_code
        self.timeout_sec = timeout_sec
        self._response_max_bytes = max(64 * 1024, int(response_max_bytes))
        self._require_response_signature = bool(require_response_signature)
        self._command_delivery_token = (command_delivery_token or "").strip()
        # Rollback uyarisi icin hafif dedup: `/pending` 1 Hz kosuyor, her
        # yanitta WARNING basmak log'u bogardi. Baglam ("config"/"pending")
        # basina en fazla `_IMZA_UYARI_ARALIK_SEC`de bir.
        self._imza_uyarisi_at: dict[str, float] = {}
        self._device_ip_allowlist = device_ip_allowlist
        self._clock_guard = clock_guard
        self._health_provider = health_provider
        self._delivery_provider = delivery_provider
        # Saglik basligi backend'i patlatiyorsa gecici olarak birakilir.
        # 0.0 = acik. Bkz. `_saglik_basligini_devre_disi_birak`.
        self._saglik_baslik_kapali_until: float = 0.0
        # Sartli istek onbellegi (ETag). Config nadiren degisir; her yoklamada
        # 193 sinyali indirmemek icin son ETag ve son AYRISTIRILMIS config
        # saklanir. 304 gelince ag uzerinden hicbir sey inmez ve JSON yeniden
        # ayristirilmaz. Bkz. fetch_config.
        self._last_etag: str | None = None
        self._last_config: GatewayConfig | None = None
        # Connection-pooled session: config-refresh ve (ayri client ornegindeki)
        # command-poll thread'leri kendi session'iyla baglanti yarisi yasamaz.
        self._session = session or build_http_session(pool_maxsize=8, verify=verify)

    def _imza_anahtari(self, context: str) -> str:
        """Bu baglam icin yanit imzasinin HMAC anahtari.

        `/config` kimlik duzlemine, `/pending` komut duzlemine aittir. Komut
        duzlemi credential'i yapilandirilmamissa (geriye donuk uyumluluk) komut
        ucu da kimlik token'ina duser — bu GECICI bir gecis davranisidir.
        """
        if context == "pending" and self._command_delivery_token:
            return self._command_delivery_token
        return self.identity.token

    def _imza_dogrula(self, response: Any, body_bytes: bytes, *, context: str) -> None:
        """Yanit imzasini dogrular; kabul edilmezse `GatewayConfigError` atar.

        `/config` ve `/pending` icin TEK yol — iki ayri gevsek HMAC
        implementasyonu birakilmadi. Karar saf `verify_backend_response_signature`
        icinde uretilir; burada yalnizca hataya cevrilir ve rollback izni
        loglanir.

        ANAHTAR SECIMI (F5)
        -------------------
          config  -> her zaman `identity.token`
          pending -> komut duzlemi token'i DOLUYSA yalnizca o; bos ise
                     `identity.token` (yalnizca geriye donuk uyumluluk)

        GERI DUSME YOK: komut token'i yapilandirilmisken dogrulama
        basarisiz olursa `identity.token` ile TEKRAR DENENMEZ. Denenseydi
        ayrimin guvenlik degeri sifirlanirdi — config duzlemini ele geciren
        biri komut duzlemini de imzalayabilirdi.
        """
        sonuc = verify_backend_response_signature(
            body=body_bytes,
            header_value=response.headers.get("X-Config-Signature"),
            token=self._imza_anahtari(context),
            require=self._require_response_signature,
            context=context,
        )
        if not sonuc.accepted:
            raise GatewayConfigError(sonuc.detail)
        if sonuc.legacy_allowed:
            # Sessiz kabul YOK. Ama `/pending` 1 Hz kostugu icin her yanitta
            # basmak log'u bogardi; baglam basina hiz sinirli uyari.
            #
            # "HIC UYARILMADI" SENTINEL'I None, 0.0 DEGIL. `time.monotonic()`
            # Linux'ta BOOT'tan beri gecen suredir: taze acilmis bir cihazda
            # deger 600'un altindadir ve `simdi - 0.0 >= 600` YANLIS doner.
            # Yani ILK uyari — en cok ihtiyac duyulan an — 10 dakikaya kadar
            # bastirilirdi. (CI'da Ubuntu runner'da yakalandi; Windows'ta
            # uptime buyuk oldugu icin gorunmuyordu.)
            simdi = time.monotonic()
            son = self._imza_uyarisi_at.get(context)
            if son is None or simdi - son >= _IMZA_UYARI_ARALIK_SEC:
                self._imza_uyarisi_at[context] = simdi
                logger.warning(
                    "backend_response_signature_missing_legacy_allowed gateway=%s "
                    "context=%s — backend `X-Config-Signature` gondermiyor ve "
                    "REQUIRE_BACKEND_RESPONSE_SIGNATURE kapali; yanit DOGRULANMADAN "
                    "kabul edildi. Bu GECICI bir rollback ayaridir.",
                    self.gateway_code,
                    context,
                )

    def _observe_clock(self, response: Any) -> None:
        """Yanittaki HTTP `Date` basligindan saat sapmasini gozle (best-effort)."""
        guard = self._clock_guard
        if guard is None:
            return
        try:
            guard.observe_http_date(response.headers.get("Date"))
        except Exception:  # noqa: BLE001
            logger.debug("clock_observe_failed", exc_info=True)

    def fetch_config(self) -> GatewayConfig:
        """Config'i ceker. DEGISMEDIYSE agdan hicbir sey indirmez.

        SARTLI ISTEK (ETag / If-None-Match)
        -----------------------------------
        Adres haritasi nadiren degisir ama config-poll surekli calisir. Her
        yoklamada 193 sinyali tel uzerinden cekmek bosuna trafik ve bosuna
        JSON ayristirmasidir.

        Backend bu yanit icin ETag uretiyor (config_version = payload'in
        hash'i) ve `If-None-Match` eslesirse 304 donuyor. Gateway bu header'i
        GONDERMIYORDU: backend'deki 304 yolu bugune kadar HIC calismadi ve her
        yoklamada tam payload indi. Dahasi 304 gelse `status_code != 200`
        kontrolune takilip HATA sayilacakti.

        Artik: ilk cagri tam indirir, sonraki cagrilar yalnizca "degisti mi"
        sorar. Degistiginde tam payload gelir ve onbellek tazelenir.
        """
        url = f"{self.base_url}/gateways/{self.gateway_code}/config"
        headers = build_config_request_headers(self.identity)
        # Sartli istek yalnizca elimizde AYRISTIRILMIS bir config varken
        # gonderilir: 304 gelip donecek bir sey bulamamak, config'i hic
        # alamamaktan beterdir. Restart sonrasi ilk cagri tam iner (surec
        # basina bir kez); backend erisilemezse disk cache devrede.
        if self._last_etag and self._last_config is not None:
            headers["If-None-Match"] = self._last_etag
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

        self._observe_clock(response)

        # 304: config DEGISMEDI. Body yok, imza yok, ayristirma yok.
        # `status_code != 200` kontrolunden ONCE ele alinmali — aksi halde
        # basarili bir "degismedi" yaniti hata sayilirdi.
        if response.status_code == 304:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
            if self._last_config is None:
                # Buraya normalde dusulmez (header'i yalnizca config varken
                # gonderiyoruz). Dusulduyse onbellegi bosalt ki sonraki cagri
                # kosulsuz gitsin ve kilitlenmeyelim.
                self._last_etag = None
                raise GatewayConfigError(
                    "304 alindi ama onbellekte config yok — sonraki cagri kosulsuz yapilacak"
                )
            logger.debug(
                "config_not_modified etag=%s — ag uzerinden veri inmedi",
                self._last_etag,
            )
            return self._last_config

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
            raise GatewayConfigError(f"config request returned {response.status_code}: {preview}")

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

        # AUTHENTICITY — JSON'a DOKUNMADAN ONCE (F4B).
        #
        # Bu govde cihaz listesini ve BINARY OUTPUT KATALOGUNU tasiyor; yani
        # gateway'deki F1/F2 yetkilendirmesinin girdisi. Dogrulanmamis bir
        # katalog ayristirilirsa `_last_config`/`_last_etag` onbellegine
        # girip 304 zincirini de zehirlerdi — bu yuzden red BURADA olur ve
        # asagidaki hicbir satira ulasilmaz.
        self._imza_dogrula(response, body_bytes, context="config")

        try:
            import json as _json

            data: dict[str, Any] = _json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GatewayConfigError(f"config response is not json: {exc}") from exc

        if not isinstance(data, dict):
            raise GatewayConfigError(f"config response root must be object, got {type(data).__name__}")

        # Beklenmedik parse hatalari sozlesmeyi bozmasin: caller (config-refresh
        # thread'i) GatewayConfigError bekliyor. Ciplak bir NameError/TypeError
        # generic except'e duserdi ve teshis edilemez hale gelirdi.
        try:
            config = _parse_gateway_config(
                data,
                default_gateway_code=self.gateway_code,
                allowlist=self._device_ip_allowlist,
            )
        except GatewayConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GatewayConfigError(f"config parse failed: {type(exc).__name__}: {exc}") from exc

        # Onbellegi yalnizca BASARILI ayristirmadan sonra tazele. Once
        # yazsaydik, bozuk bir payload'in ETag'i saklanir ve sonraki cagrilar
        # 304 alip bozuk config'i "gecerli" sayardi.
        etag = (response.headers.get("ETag") or "").strip()
        self._last_config = config
        # ETag yoksa (eski backend) sartli istek gonderilmez; her cagri tam
        # iner — yani onceki davranis. Bozulma yok, sadece kazanc yok.
        self._last_etag = etag or None
        return config

    def _build_health_header(self) -> str | None:
        """Saglik basligini uretir; HER hata sessizce yutulur.

        Baslik gecici olarak devre disi birakilmis olabilir — bkz.
        `_saglik_basligini_devre_disi_birak`.
        """
        saglayici = self._health_provider
        if saglayici is None:
            return None
        if time.monotonic() < self._saglik_baslik_kapali_until:
            return None
        try:
            govde = saglayici()
        except Exception:  # noqa: BLE001
            logger.debug("saglik saglayicisi patladi", exc_info=True)
            return None
        if not isinstance(govde, dict):
            return None
        return health_header.encode_header(govde)

    def _build_delivery_header(self) -> str | None:
        """F3C teslim basligini uretir; HER hata sessizce yutulur.

        SAGLIK BASLIGINDAN AYRI TUTULDU ve onun devre-disi birakma
        mekanizmasina BAGLANMADI: saglik bir teshis kolayligidir, bu baslik ise
        teslim protokolunun kendisidir. Saglik basligi backend'i bozdugu icin
        birakildiginda teslim protokolu de birakilsaydi, gateway sessizce eski
        yola duser ve F3C'nin kapattigi pencere geri acilirdi.

        Baslik uretilemezse gateway yetenegini bildirmemis olur; backend
        varsayilan olarak FAIL-CLOSED davranir (komut teslim etmez). Sessiz bir
        guvenlik kaybi degil, gorunur bir teslim durusu.
        """
        saglayici = self._delivery_provider
        if saglayici is None:
            return None
        try:
            epoch = saglayici()
        except Exception:  # noqa: BLE001
            logger.debug("teslim epoch saglayicisi patladi", exc_info=True)
            return None
        if not epoch or not isinstance(epoch, str):
            return None
        return health_header.encode_delivery_header(epoch)

    def report_delivery_acks(self, acks: list[dict]) -> set[int]:
        """Teslim onaylarini backend'e bildirir (batch POST).

        Doner: backend'in KABUL ettigi `command_id` kumesi. Yalnizca bunlar
        defterde `acked` isaretlenir; gerisi kuyrukta kalip yeniden denenir.

        `acks`: [{"command_id": int, "delivery_token": str}]

        Backend mukerrer ACK'i idempotent kabul eder (`accepted` sayar), yani
        yeniden gonderim zararsizdir. Hata `CommandResultDeliveryError` ile
        raise edilir; cagiran taraf gecici/kalici ayrimini yapar.

        JETON LOGLANMAZ.
        """
        if not acks:
            return set()
        url = f"{self.base_url}/gateways/{self.gateway_code}/command-delivery-acks"
        headers = build_command_request_headers(self.identity, self._command_delivery_token)
        headers["Content-Type"] = "application/json"
        try:
            response = self._session.post(
                url,
                headers=headers,
                json={"acks": acks},
                timeout=self.timeout_sec,
            )
        except requests.RequestException as exc:
            # Ag hatasi -> GECICI. HTTP status yok; caller yeniden dener.
            raise CommandResultDeliveryError(
                _scrub_token_from_text(
                    f"command-delivery-acks POST failed: {exc}",
                    self.identity.token,
                    self._command_delivery_token,
                ),
                http_status=None,
            ) from exc
        if response.status_code >= 400:
            # HTTP STATUS'U TASI: caller kalici (401/404/413/422) ile geciciyi
            # (502/503) ayirt edebilsin. `command-results` ile ayni sozlesme.
            preview = _scrub_token_from_text(
                (response.text or "")[:200], self.identity.token, self._command_delivery_token
            )
            raise CommandResultDeliveryError(
                f"command-delivery-acks POST rejected: HTTP {response.status_code}: {preview}",
                http_status=response.status_code,
            )

        # KABUL EDILENI GOVDEDEN OKUMUYORUZ — backend yalnizca SAYI donuyor
        # (`{"accepted": N, "rejected": M}`), hangi id'lerin kabul edildigini
        # DEGIL. Reddedilen varsa hepsini kuyrukta tutmak yanlis olurdu
        # (sonsuz tekrar); hepsini silmek de yanlis (kabul edilmeyen kaybolur).
        #
        # Cozum: 2xx alindiysa parti ISLENMIS sayilir. Reddedilen bir ACK
        # backend tarafinda ya jeton uyusmazligidir ya da komut terminaldir;
        # her iki durumda da yeniden gondermek DURUMU DEGISTIRMEZ ve komut
        # backend'in kira/deneme mekanizmasiyla zaten sonlandirilir.
        kabul = 0
        try:
            govde = response.json()
            if isinstance(govde, dict):
                kabul = int(govde.get("accepted", 0) or 0)
                ret = int(govde.get("rejected", 0) or 0)
                if ret:
                    logger.warning(
                        "command_delivery_ack_rejected gateway=%s reddedilen=%d kabul=%d "
                        "— backend jetonu ya da komut durumunu kabul etmedi",
                        self.gateway_code,
                        ret,
                        kabul,
                    )
        except ValueError:
            logger.debug("command-delivery-acks yaniti JSON degil", exc_info=True)

        return {int(a["command_id"]) for a in acks}

    def _saglik_basligini_devre_disi_birak(self, sebep: str) -> None:
        """Saglik basligini gecici olarak birak — KOMUT KANALINI KURTARMAK ICIN.

        Baslik bir teshis kolayligidir; `/pending` ise SCADA komut kanalidir.
        Backend basligi isleyemiyorsa dogru davranis basligi birakip komutlari
        akitmaya devam etmektir.
        """
        self._saglik_baslik_kapali_until = time.monotonic() + _SAGLIK_BASLIK_KAPALI_SEC
        logger.error(
            "health_header_disabled sure=%ds sebep=%s — `/pending` yalnizca saglik "
            "basligiyla hata veriyor. Baslik BIRAKILDI, komut kanali calismaya devam "
            "ediyor. Backend tarafinda bu basligi isleyen yol duzeltilmeli "
            "(gateway_health tablosu/migration).",
            int(_SAGLIK_BASLIK_KAPALI_SEC),
            sebep,
        )

    def fetch_pending_commands(self) -> PendingPoll:
        """Hafif komut-poll — GET /gateways/{code}/pending.

        Config'ten AYRI: sadece bekleyen komutlar + config_nonce + refresh_nonce
        (agir device/signal serialize YOK). Gateway 1sn'de bir cagirir. HMAC
        imza fetch_config ile ayni sekilde dogrulanir (MITM/komut enjekte koruma).

        Hata -> GatewayConfigError (caller loglar, bir sonraki poll'de tekrar dener).
        """
        import json as _json

        url = f"{self.base_url}/gateways/{self.gateway_code}/pending"
        headers = build_command_request_headers(self.identity, self._command_delivery_token)
        # SAGLIK OZETI — bu istege biner, ek istek yok.
        #
        # KOMUT KANALI KUTSAL: buradaki hicbir sey `/pending` cagrisini
        # dusurmemeli. Saglayici patlarsa, govde uretilemezse ya da baslik
        # tavani asarsa sessizce vazgecilir ve komutlar normal gider.
        saglik = self._build_health_header()
        if saglik:
            headers[health_header.HEADER_NAME] = saglik
        # F3C TESLIM PROTOKOLU — HER poll'de, cache'siz.
        #
        # Bu baslik hem yetenegi (`command_delivery_ack_v1`) hem de DEFTER
        # KIMLIGINI (epoch) tasir. Epoch'un TAZE olmasi bir performans tercihi
        # degil, cift-calistirma korumasinin kendisidir: defter T aninda
        # sifirlanip epoch yalnizca 30 saniyede bir giden saglik ozetiyle
        # tasinsaydi, aradaki pencerede backend ESKI epoch'a guvenerek komutu
        # yeniden sunar, bos defterli gateway onu YENI sanip CROB'u
        # TEKRARLARDI.
        teslim = self._build_delivery_header()
        if teslim:
            headers[health_header.DELIVERY_HEADER_NAME] = teslim
        try:
            response = self._session.get(url, headers=headers, timeout=self.timeout_sec)
        except requests.RequestException as exc:
            err = _scrub_token_from_text(str(exc), self.identity.token, self._command_delivery_token)
            raise GatewayConfigError(f"pending request failed: {err}") from exc

        # BASLIK KOMUT KANALINI DUSURUYOR MU?
        #
        # Yukaridaki savunma yalnizca basligi URETIRKEN cikan hatalari
        # kapsiyordu. Baslik basariyla uretilip gonderildiginde ve BACKEND onu
        # isleyemediginde hicbir savunma yoktu — sahada tam olarak bu oldu:
        # backend `gateway_health` tablosuna yazmaya calisti, tablo yoktu,
        # transaction bozuldu ve `/pending` 500 dondu. Sonuc: komut kanali
        # SESSIZCE oldu ve `config_nonce` okunamadigi icin yeni eklenen
        # cihazlar 5 dakikaya kadar gorulmedi.
        #
        # Cozum: 5xx alindiysa ve baslik GONDERILMISSE, ayni istegi BASLIKSIZ
        # bir kez daha dene. Basliksiz calisiyorsa suclu baslige aittir;
        # birakilir ve komutlar akmaya devam eder.
        if saglik and 500 <= response.status_code < 600:
            try:
                temiz = dict(headers)
                temiz.pop(health_header.HEADER_NAME, None)
                ikinci = self._session.get(url, headers=temiz, timeout=self.timeout_sec)
            except requests.RequestException:
                ikinci = None
            if ikinci is not None and ikinci.status_code == 200:
                self._saglik_basligini_devre_disi_birak(f"HTTP {response.status_code}")
                response = ikinci

        if response.status_code != 200:
            preview = _scrub_token_from_text(
                (response.text or "")[:200], self.identity.token, self._command_delivery_token
            )
            raise GatewayConfigError(f"pending request returned {response.status_code}: {preview}")

        body_bytes = response.content
        if len(body_bytes) > self._response_max_bytes:
            raise GatewayConfigError("pending response too large")

        # AUTHENTICITY — JSON parse'tan ve state/ledger'a dokunmadan ONCE (F4B).
        #
        # Bu govde FIZIKSEL KOMUT niyetini tasiyor. Dogrulama burada bittigi
        # icin reddedilen bir yanit `PendingPoll` bile uretmez: komut kuyruga
        # girmez, nonce uygulanmaz, `ledger.start_dispatch` ve
        # `operate_device` cagrilmaz.
        self._imza_dogrula(response, body_bytes, context="pending")

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
                            # HAM tasinir; daraltma operate_device icinde (F6).
                            count=raw_command_parameter(item, "count", 1),
                            on_time_ms=raw_command_parameter(item, "on_time_ms", 0),
                            off_time_ms=raw_command_parameter(item, "off_time_ms", 0),
                            created_at=_optional_command_timestamp(item.get("created_at")),
                            # F3C: HAM METIN tasinir, burada dogrulanmaz.
                            # Bozuk bir deger komutu parse dongusunde SESSIZCE
                            # dusurmemeli — `command_freshness` onu reddedip
                            # TERMINAL bir sonuc uretir ve backend ogrenir.
                            delivery_token=_opsiyonel_metin(item.get("delivery_token")),
                            delivery_not_after=_opsiyonel_metin(item.get("delivery_not_after")),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("pending_command_parse_failed id=%r error=%s", item.get("id"), exc)

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
        headers = build_command_request_headers(self.identity, self._command_delivery_token)
        headers["Content-Type"] = "application/json"
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=results,
                timeout=self.timeout_sec,
            )
        except requests.RequestException as exc:
            # Ag hatasi -> GECICI. HTTP status yok; caller yeniden dener.
            raise CommandResultDeliveryError(
                _scrub_token_from_text(
                    f"command-results POST failed: {exc}", self.identity.token, self._command_delivery_token
                ),
                http_status=None,
            ) from exc
        if response.status_code >= 400:
            # HTTP STATUS'U TASI. Eskiden bu bilgi kayboluyordu ve caller
            # kalici (401/422/404) ile gecici (502/503) hatayi ayirt
            # edemiyordu: her ikisinde de sonsuza kadar yeniden deniyordu.
            # Tek bir kalici redde takilan kuyruk BIR DAHA BOSALMIYOR ve
            # o andan sonraki TUM komut sonuclari backend'e ulasmiyordu —
            # operator panelinde komutlar sonsuza kadar "bekliyor" gorunurken
            # kesiciler fiziksel olarak surulmus oluyordu.
            preview = _scrub_token_from_text(
                (response.text or "")[:200], self.identity.token, self._command_delivery_token
            )
            raise CommandResultDeliveryError(
                f"command-results POST rejected: HTTP {response.status_code}: {preview}",
                http_status=response.status_code,
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


def _optional_command_timestamp(raw: Any) -> str | None:
    """Komut olusturma damgasini HAM METIN olarak alir; yoksa None.

    Burada AYRISTIRILMAZ (bkz. PendingCommand.created_at): bozuk bir damga
    parse dongusunde komutu sessizce dusururdu ve backend sonucu hicbir zaman
    ogrenemezdi. Dogrulama `command_freshness` icinde yapilir; orada red
    terminal bir sonuc uretir. Burada yalnizca "gonderildi mi" ayrimi yapilir.
    """
    if raw is None:
        return None
    metin = str(raw).strip()
    return metin or None


def _opsiyonel_metin(raw: Any) -> str | None:
    """F3C teslim ustverisini HAM METIN olarak alir; yoksa None.

    `_optional_command_timestamp` ile ayni sozlesme ama farkli anlam: buradaki
    degerler damga DEGIL (jeton opak, son kullanma ani ISO metin). Ayri isim,
    jetonun bir zaman damgasi sanilmasini onler.

    AYRISTIRMA YOK: bozuk bir deger komutu parse dongusunde SESSIZCE
    dusurmemeli; `command_freshness` onu reddedip TERMINAL sonuc uretir.
    """
    if raw is None:
        return None
    metin = str(raw).strip()
    return metin or None


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
        from dnp3_gateway.config import get_settings

        raw = get_settings().dnp3_device_allowed_subnets or ""
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


def _dogrula_dinleyici_portlari(devices: list[DeviceConfig]) -> None:
    """Ayni gateway icinde `master_ip_port` cakismasini REDDET.

    Kapsam BILEREK gateway-ici: bu proses yalnizca kendi cihazlarini bilir.
    AYNI HOST'taki kardes gateway instance'lari arasindaki host-port
    tekilligi bu repo'dan GORULEMEZ ve burada uydurulmaz — orasi
    deployment/orkestrasyon sorumlulugudur (bkz. docs/BACKEND_TODO.md#B6).
    """
    gorulen: dict[int, str] = {}
    for d in devices:
        if (d.ip_endpoint_type or "").strip().lower() != "initiating":
            continue
        port = d.master_ip_port
        if port is None:
            continue
        onceki = gorulen.get(port)
        if onceki is not None:
            raise GatewayConfigError(
                f"master_ip_port cakismasi: {port} hem {onceki!r} hem {d.code!r} "
                "cihazinda tanimli. Ayni gateway icinde her initiating cihaz "
                "AYRI bir dinleyici portu kullanmak zorundadir."
            )
        gorulen[port] = d.code


def _dogrula_oturum_politikasi_uyumu(devices: list[DeviceConfig]) -> None:
    """Oturum politikasi ile uc tipi uyumu.

    1.14.0'DAN ITIBAREN TUM MATRIS GECERLIDIR:

        listening  + continuous | smart | auto
        initiating + continuous | smart | auto

    1.13.0'da `smart`/`auto` YALNIZCA `initiating` ile gecerliydi ve
    `listening` kombinasyonu REDDEDILIYORDU. O kisit, o surumde Listening
    Smart yasam dongusunun UYGULANMAMIS olmasindandi — gerceklestirilemeyecek
    bir yapilandirmayi kabul etmemek dogruydu. 1.14.0 o yasam dongusunu
    uyguladigi icin kisit KALKTI (genisleme; hicbir gecerli config artik
    gecersiz olmuyor).

    `initiating` uc icin `master_ip_port` zorunlulugu ve dinleyici portu
    tekilligi AYNEN GECERLIDIR (bkz. `_parse_master_ip_port`,
    `_dogrula_dinleyici_portlari`).
    """
    # Su an politikaya OZEL ek bir uyum kisiti YOK. Fonksiyon bilincli olarak
    # KORUNUYOR: uc/politika uyumunun tek dogrulama yeri burasidir ve
    # ileride bir kisit gerekirse (orn. yeni bir politika) dagilmadan
    # buraya eklenir.
    return


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

    rewrite = _os.environ.get("REWRITE_LOOPBACK_TO_HOST", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

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

        # UC TIPI + DINLEYICI PORTU — FAIL-CLOSED, `try` BLOGUNUN DISINDA.
        #
        # Bilerek asagidaki `except (TypeError, ValueError)` sarmalayicisinin
        # DISINDA: o blok bozuk bir alani "cihazi atla" ile karsiliyor.
        # Uc tipi ve dinleyici portu icin SESSIZ ATLAMA da kabul edilemez —
        # cihaz sessizce yoklanmaz kalirdi. `GatewayConfigError` yukari
        # cikar, config REDDEDILIR ve gateway son iyi config'iyle calisir.
        uc_tipi = _parse_ip_endpoint_type(item)

        try:
            # DNP3 link layer adresi: standart aralik 0-65519 (RFC: 65520-65535
            # rezerve). Disindaki degerler segfault uretebilir bazi binding'lerde.
            dnp3_addr = _safe_int(item.get("dnp3_address"), 1, lo=0, hi=65519)
            if item.get("dnp3_address") not in (None, "") and dnp3_addr != int(item.get("dnp3_address") or 0):
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
                    ip_endpoint_type=uc_tipi,
                    master_ip_port=_parse_master_ip_port(item, endpoint_type=uc_tipi),
                    # Reasonable defaults + clamping (poll_interval cok kucuk
                    # ise gateway cycle'i tikar; cok buyuk ise hic okumaz).
                    poll_interval_sec=_safe_int(item.get("poll_interval_sec"), 2, lo=1, hi=3600),
                    timeout_ms=_safe_int(item.get("timeout_ms"), 3000, lo=100, hi=60000),
                    retry_count=_safe_int(item.get("retry_count"), 2, lo=0, hi=20),
                    signal_profile=_truncate(
                        item.get("signal_profile") or "default",
                        _MAX_CODE_LENGTH,
                    ),
                    session_policy=_parse_session_policy(item),
                    smart_max_silence_sec=_parse_optional_smart_silence(item),
                    dial_in_interval_min=_parse_optional_int(
                        item,
                        "dial_in_interval_min",
                        lo=DIAL_IN_INTERVAL_MIN_MIN,
                        hi=DIAL_IN_INTERVAL_MIN_MAX,
                    ),
                    smart_listen_probe_interval_sec=_parse_optional_int(
                        item,
                        "smart_listen_probe_interval_sec",
                        lo=SMART_LISTEN_PROBE_MIN_SEC,
                        hi=SMART_LISTEN_PROBE_MAX_SEC,
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

    # DINLEYICI PORTU CAKISMASI — MASTER KURULMADAN ONCE YAKALA.
    #
    # Iki initiating cihaz ayni `master_ip_port`u tasiyorsa ikinci
    # `AddTCPServer` cagrisi calisma zamaninda anlasilmaz bir soket hatasi
    # uretir (ya da daha kotusu: cihazlar birbirinin oturumunu kapar,
    # `ServerAcceptMode.CloseExisting`). Config seviyesinde reddetmek bunu
    # ONCEDEN ve ACIKCA gorunur kilar.
    _dogrula_dinleyici_portlari(devices)
    _dogrula_oturum_politikasi_uyumu(devices)

    signals = _parse_signal_list(data.get("signals") or [], alan="signals")

    # Profil bazli setler (opsiyonel; eski backend'de alan YOK -> bos sozluk ve
    # duz listeye dusulur). Bozuk bir profil TUM config'i dusurmez: o profil
    # atlanir, digerleri yasar — cihazlarin tamaminin karanliga dusmesindense
    # bir modelin duz listeye dusmesi yeglenir.
    signals_by_profile: dict[str, list[SignalConfig]] = {}
    raw_profiles = data.get("signals_by_profile")
    if isinstance(raw_profiles, dict):
        for profil, ham in raw_profiles.items():
            anahtar = _truncate(str(profil), _MAX_CODE_LENGTH)
            if not anahtar:
                continue
            try:
                # BOS LISTE DE KABUL EDILIR ve saklanir. Backend "bu model icin
                # sinyal tanimli degil" demek icin bilerek bos gonderir; burada
                # atlarsak cihaz duz listeye duser ve KOMSU modelin adreslerini
                # yoklar — B3'un onlemek istedigi sessiz yanlis veri.
                signals_by_profile[anahtar] = _parse_signal_list(
                    ham or [], alan=f"signals_by_profile[{anahtar}]"
                )
            except GatewayConfigError as exc:
                logger.warning(
                    "config_profile_parse_failed profil=%r error=%s — profil atlandi",
                    anahtar,
                    exc,
                )
    elif raw_profiles is not None:
        logger.warning(
            "config_signals_by_profile_invalid tip=%s — yok sayildi",
            type(raw_profiles).__name__,
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
                        # HAM tasinir; daraltma operate_device icinde (F6).
                        count=raw_command_parameter(item, "count", 1),
                        on_time_ms=raw_command_parameter(item, "on_time_ms", 0),
                        off_time_ms=raw_command_parameter(item, "off_time_ms", 0),
                        created_at=_optional_command_timestamp(item.get("created_at")),
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
        signals_by_profile=signals_by_profile,
        refresh_nonce=refresh_nonce,
        config_nonce=config_nonce,
        pending_commands=tuple(pending_commands),
    )
