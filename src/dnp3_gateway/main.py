"""Gateway ana giris noktasi.

Calisma akisi:
    1. Loglama + health HTTP sunucusu acilir.
    2. Arkaplan thread'i backend API'den `CONFIG_REFRESH_SEC` araliklarla
       konfigurasyon ceker ve `GatewayState`'i gunceller.
    3. Ana thread `default_poll_interval_sec` araliginda uyanip okunma vakti
       gelen cihazlari `poller.run_poll_cycle` ile okur/yayinlar.
       Telemetri dogrudan NATS JetStream'e basilir (standart publisher);
       HTTP ingest yalnizca TELEMETRY_PUBLISHER=http ile rollback yoludur.
    4. SIGINT/SIGTERM/SIGBREAK alindiginda graceful shutdown:
         - config refresh thread durdurulur
         - poller kapanir
         - DNP3 session'lari + JetStream baglantisi kapanir
         - outbox retrier durdurulur
         - health HTTP sunucusu kapatilir
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any

from dnp3_gateway import __version__
from dnp3_gateway.adapters import TelemetryReader, build_adapter
from dnp3_gateway.auth import GatewayIdentity, bootstrap_gateway_identity
from dnp3_gateway.auth.instance_lock import acquire_instance_lock
from dnp3_gateway.backend import (
    BackendConfigClient,
    GatewayConfigError,
    _log_allowlist_state,
    health_header,
    parse_device_ip_allowlist,
)
from dnp3_gateway.command_authorization import authorize_output_command
from dnp3_gateway.command_freshness import validate_command_freshness
from dnp3_gateway.config import Settings, get_settings
from dnp3_gateway.health_server import (
    ThreadLiveness,
    _build_health_body,
    start_health_server,
)
from dnp3_gateway.logging_setup import configure_logging, register_secret
from dnp3_gateway.messaging import CommandLedger, Outbox, OutboxRetrier
from dnp3_gateway.messaging.errors import is_transient
from dnp3_gateway.messaging.resilient_publisher import ResilientPublisher
from dnp3_gateway.poller import run_poll_cycle
from dnp3_gateway.resource_guard import ClockGuard, DiskGuard
from dnp3_gateway.state import GatewayState

logger = logging.getLogger("dnp3_gateway")


def _print_console_banner(
    *,
    cfg: Settings,
    identity: GatewayIdentity,
    actual_health_port: int,
) -> None:
    """Konsolda port / URL / coklu instance kurallarini net gosterir."""

    host = cfg.worker_health_host
    port = actual_health_port
    # Banner'da gosterilen URL: 0.0.0.0 bind container icindir, kullanici
    # tarayicidan/curl'den asla 0.0.0.0:PORT yazmaz. Loopback'e dusur.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    health_url = f"http://{display_host}:{port}/health"
    config_url = f"{cfg.backend_api_url.rstrip('/')}/gateways/{identity.gateway_code}/config"
    print("", flush=True)
    print("  EnerjiOne DNP3 Gateway", flush=True)
    print(f"  surum {__version__}  |  gateway {identity.gateway_code}", flush=True)
    print(f"  health   {health_url}", flush=True)
    print(f"  backend  {cfg.backend_api_url.rstrip('/')}", flush=True)
    # Kurulumda "gateway backend'e ulasiyor mu" sorusunun ilk adresi budur;
    # operator bunu dogrudan curl'leyebilsin diye banner'da gosteriyoruz.
    print(f"  config   {config_url}", flush=True)
    # Telemetri hangi yoldan gidiyor — kurulumun ilk dogrulama noktasi.
    # "Yerel kurulum yaptim ama neden yavas?" sorusunun cevabi cogu zaman
    # burada gizliydi; artik banner'da acikca yaziyor.
    if cfg.telemetry_publisher != "nats":
        print("  telemetri  HTTP ingest (TELEMETRY_PUBLISHER=http, rollback yolu)", flush=True)
    elif cfg.install_mode == "local":
        print("  telemetri  NATS (yerel kurulum — HTTP yedegi YOK)", flush=True)
    else:
        print("  telemetri  NATS (uzak kurulum — erisilemezse HTTP yedegi devreye girer)", flush=True)
    if cfg.is_mock_mode:
        print("  UYARI: GATEWAY_MODE=mock — sahadan okuma YOK, degerler ureticidir.", flush=True)
    print("", flush=True)


def _mask_secret(value: str, *, keep: int = 2) -> str:
    """Token mask: ilk + son N karakter haric ortayi gizle.

    Default keep=2 (eski 4'ten dusuruldu): 32 karakter token'da bile sadece
    4 karakter gozukur, brute-force riski yok.
    """
    v = value.strip()
    if len(v) <= 2 * keep + 2:
        return "***"
    return f"{v[:keep]}...{v[-keep:]}"


def _execute_pending_commands(
    reader,
    state: GatewayState,
    pending,
    *,
    gateway_code: str = "",
    max_age_sec: float = 120.0,
    require_timestamp: bool = True,
    clock_skew_tolerance_sec: float = 5.0,
) -> list[dict]:
    """Bekleyen komutlari operate_device (CROB) ile calistirir, sonuc listesi doner.

    Her sonuc: {id, ok, status, error?} — backend command-results endpoint'inin
    bekledigi bicim. Cihaz bulunamaz/hata olursa ok=False + anlamli status.
    Bir komutun hatasi digerlerini durdurmaz.

    YEREL YETKILENDIRME (F1+F2): CROB gonderilmeden once komut, cihazin KENDI
    sinyal katalogu ile dogrulanir (bkz. `command_authorization`). Bu, hem
    `/pending` hem de `config.pending_commands` kanalini kapsar — ikisi de
    `state.take_pending_commands()` kuyrugundan gecer, ayri bir dogrulama
    noktasina gerek yoktur.

    Reddedilen komut SESSIZCE DUSURULMEZ: kalici bir sonuc uretir, caller onu
    ledger'a yazar ve backend'e bildirir. Ledger'da `start_dispatch` zaten
    tuketildigi icin ayni komut sonraki poll'larda TEKRAR islenmez.
    """
    results: list[dict] = []
    devices_by_code = {d.code: d for d in state.devices()}
    for cmd in pending:
        device = devices_by_code.get(cmd.device_code)
        if device is None:
            logger.warning(
                "pending_command_device_not_found id=%s device=%s",
                cmd.id,
                cmd.device_code,
            )
            results.append(
                {
                    "id": cmd.id,
                    "ok": False,
                    "status": "device_not_found",
                    "error": f"cihaz config'te yok: {cmd.device_code}",
                }
            )
            continue

        # ---- F3: TAZELIK -----------------------------------------------
        # Yetkilendirmeden ONCE: tazelik, istegin ICERIGINDEN bagimsiz bir
        # ozelliktir. "Bu istegi hic degerlendirmeli miyiz" sorusu, "dogru
        # noktayi mi gosteriyor" sorusundan once gelir; suresi gecmis bir
        # komut katalog cozumleme yoluna hic girmemeli.
        #
        # `start_dispatch`ten SONRA oldugu icin red de terminal bir sonuc
        # uretir, ledger'a yazilir ve backend'e teslim edilir; ayni komut
        # sonraki poll'larda YENIDEN degerlendirilmez.
        tazelik = validate_command_freshness(
            cmd.created_at,
            now=datetime.now(timezone.utc),
            max_age_sec=max_age_sec,
            require_timestamp=require_timestamp,
            clock_skew_tolerance_sec=clock_skew_tolerance_sec,
            # F3C: backend'in turettigi DEGISMEZ son kullanma ani. Yerel
            # `max_age_sec` gateway tarafinda yapilandirilabilir oldugu icin
            # tek basina yeterli degil — daha genis ayarlanirsa backend'in
            # kapattigi pencere burada acik kalirdi. Savunma derinligi.
            delivery_not_after=getattr(cmd, "delivery_not_after", None),
        )
        if tazelik.legacy_allowed:
            # Backend `created_at` gondermiyor. Gecisin sessizce kalicilasmasini
            # onlemek icin GORUNUR uyari; yalnizca GERCEK komut geldiginde
            # basilir, komut-poll bos donduginde degil (log storm yok).
            logger.warning(
                "command_timestamp_missing_legacy_allowed gateway=%s device=%s "
                "command_id=%s command=%s dnp3_index=%s — backend `created_at` "
                "gondermiyor; COMMAND_REQUIRE_TIMESTAMP kapali oldugu icin komut "
                "calistiriliyor",
                gateway_code,
                cmd.device_code,
                cmd.id,
                cmd.command,
                cmd.dnp3_index,
            )
        elif not tazelik.fresh:
            logger.warning(
                "command_freshness_rejected gateway=%s device=%s command_id=%s "
                "command=%s dnp3_index=%s created_at=%s age_sec=%s ttl_sec=%s "
                "reason=%s detail=%s — CROB GONDERILMEDI",
                gateway_code,
                cmd.device_code,
                cmd.id,
                cmd.command,
                cmd.dnp3_index,
                cmd.created_at,
                None if tazelik.age_sec is None else round(tazelik.age_sec, 1),
                max_age_sec,
                tazelik.status,
                tazelik.detail,
            )
            results.append(
                {
                    "id": cmd.id,
                    "ok": False,
                    "status": tazelik.status,
                    "error": tazelik.detail,
                }
            )
            continue

        # ---- F1 + F2: yerel cikis yetkilendirmesi --------------------------
        # Cihazin KENDI seti kullanilir; global index listesi DEGIL. Ayni slug
        # modeller arasinda farkli index'e duser (master.boost_mode: SN2=26,
        # Pole Master=30) ve global liste ikisini de kabul ederdi.
        karar = authorize_output_command(
            state.signals_for(device),
            dnp3_index=cmd.dnp3_index,
            command=cmd.command,
        )
        if not karar.authorized:
            logger.warning(
                "command_authorization_rejected gateway=%s device=%s command_id=%s "
                "command=%s dnp3_index=%s reason=%s detail=%s — CROB GONDERILMEDI",
                gateway_code,
                cmd.device_code,
                cmd.id,
                cmd.command,
                cmd.dnp3_index,
                karar.status,
                karar.detail,
            )
            results.append(
                {
                    "id": cmd.id,
                    "ok": False,
                    "status": karar.status,
                    "error": karar.detail,
                }
            )
            continue

        try:
            res = reader.operate_device(
                device=device,
                index=cmd.dnp3_index,
                op_type=cmd.op_type,
                count=cmd.count,
                on_time_ms=cmd.on_time_ms,
                off_time_ms=cmd.off_time_ms,
            )
            ok = bool(res.get("ok"))
            logger.info(
                "pending_command_executed id=%s device=%s index=%s control=%s ok=%s "
                "status=%s dnp3_status=%s dnp3_state=%s",
                cmd.id,
                cmd.device_code,
                cmd.dnp3_index,
                res.get("control"),
                ok,
                res.get("status"),
                res.get("dnp3_status"),
                res.get("dnp3_state"),
            )
            results.append(
                {
                    "id": cmd.id,
                    "ok": ok,
                    "status": str(res.get("status", "unknown")),
                    "error": res.get("error"),
                    # Detayli DNP3 status (backend device_commands'a kaydedilir).
                    "dnp3_status": res.get("dnp3_status"),
                    "dnp3_state": res.get("dnp3_state"),
                    "dnp3_task": res.get("dnp3_task"),
                    "control": res.get("control"),
                    "duration_ms": res.get("duration_ms"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("pending_command_failed id=%s device=%s", cmd.id, cmd.device_code)
            results.append({"id": cmd.id, "ok": False, "status": "error", "error": str(exc)[:400]})
    return results


def _run_command_poll(
    *,
    client: BackendConfigClient,
    reader,
    state: GatewayState,
    ledger,
    stop_event: Event,
    poll_sec: int,
    config_wake: Event,
    max_age_sec: float = 120.0,
    require_timestamp: bool = True,
    clock_skew_tolerance_sec: float = 5.0,
) -> None:
    """Hafif komut-poll thread'i (config'ten AYRI, ~1sn).

    Her dongu:
      1. GET /pending -> komutlar + config_nonce + refresh_nonce (state'e uygula)
      2. Bekleyen komutlari command_ledger ile IDEMPOTENT calistir:
         start_dispatch(id) False -> zaten islendi, ATLA (restart-safe dedup);
         True -> CROB gonder, record_result.
      3. Sonuclari at-least-once teslim et (pending_results -> report ->
         mark_delivered / dead_letter).
      4. config_nonce arttiysa config thread'ini erken uyandir (config_wake.set).

    Komut anlik gelsin diye kisa interval. Config'in agir serialize'i YOK.
    """
    # Hata log dedup: komut-poll 1sn'de bir calistigi icin backend restart
    # penceresinde (502/timeout) her saniye WARNING = log spam. Art arda ayni
    # hatada SADECE ilkini logla; duzelince "recovered" de.
    consecutive_errors = 0
    while not stop_event.is_set():
        try:
            # BEKLEYEN ACK'LERI ONCE GONDER — sira onemli.
            #
            # Poll once yapilsaydi, gateway'in kabul ettigi ama ACK'i henuz
            # gitmemis bir komut backend tarafinda hala `pending` gorunur ve
            # ayni istekte mutlak TTL doldugunda sonlandirilabilirdi. ACK'i one
            # almak, kabul edilmis bir komutun teslim onayinin o degerlendirmeden
            # ONCE ulasmasini saglar.
            #
            # Ayrica restart sonrasi kurtarma yolu da budur: defterdeki
            # `ack_state='pending'` kayitlar ilk turda gider.
            _deliver_delivery_acks(client, ledger)

            poll = client.fetch_pending_commands()
            state.apply_pending_poll(poll)
            # SCADA komut yolunun sagligi /health'e yansisin (bkz.
            # state.record_command_poll: bu kanal olurken panel "ok" diyordu).
            state.record_command_poll(ok=True)
            if consecutive_errors:
                logger.info(
                    "command_poll_recovered gateway=%s after_errors=%d",
                    client.gateway_code,
                    consecutive_errors,
                )
                consecutive_errors = 0

            # config degisti -> config thread'i hemen cek
            if state.take_config_refresh_request():
                config_wake.set()
                logger.info("config_refresh_triggered_by_nonce gateway=%s", client.gateway_code)

            # Komutlari idempotent calistir
            pending = state.take_pending_commands()
            fresh = []
            for cmd in pending:
                # ---- MUKERRER TESLIM MI? -------------------------------
                #
                # ONCE deftere bakilir. Kayit VARSA komut daha once DAYANIKLI
                # olarak kabul edilmistir: fiziksel komut TEKRARLANMAZ ve
                # yeniden bir calistirma karari URETILMEZ (tazelik/TTL/F1/F2
                # yeniden degerlendirilmez — o kararlar ilk kabulde verildi).
                #
                # Eskiden bu durumda komut sessizce ATLANIYORDU. F3C ile bu
                # yetmiyor: backend komutu yeniden sunduysa ACK'in ona
                # ULASMADIGI anlasilir; ACK yeniden kuyruklanmazsa backend
                # komutu hicbir zaman `sent` goremez ve kira/deneme tukenene
                # kadar bosuna yeniden sunar.
                kayitli, _jeton = ledger.kayitli_jeton(cmd.id)
                if kayitli:
                    if getattr(cmd, "delivery_token", None) and ledger.ack_yeniden_kuyrukla(cmd.id):
                        logger.info(
                            "command_delivery_ack_requeued gateway=%s command_id=%s "
                            "— komut zaten dayanikli kabul edilmisti; ACK yeniden "
                            "kuyruklandi (FIZIKSEL KOMUT TEKRARLANMADI)",
                            client.gateway_code,
                            cmd.id,
                        )
                    continue

                # ---- YENI KOMUT ----------------------------------------
                #
                # DAYANIKLI YAZIM FIZIKSEL KOMUTTAN ONCE. `start_dispatch`
                # COMMIT edilmeden CROB gonderilmez; bu sira, prosesin tam o
                # anda olmesi halinde bile komutun tekrarlanmamasini saglar.
                #
                # Jeton varsa kayit ACK BEKLIYOR durumunda acilir — yani ACK
                # ancak COMMIT'ten SONRA uretilebilir.
                if ledger.start_dispatch(cmd.id, getattr(cmd, "delivery_token", None)):
                    fresh.append(cmd)
            if fresh:
                results = _execute_pending_commands(
                    reader,
                    state,
                    fresh,
                    gateway_code=client.gateway_code,
                    max_age_sec=max_age_sec,
                    require_timestamp=require_timestamp,
                    clock_skew_tolerance_sec=clock_skew_tolerance_sec,
                )
                for res in results:
                    ledger.record_result(res)

            # Bu turda dogan ACK'ler hemen gitsin (dongu basindaki gonderim
            # onlari henuz gormemisti). Aksi halde her ACK bir poll gecikirdi.
            _deliver_delivery_acks(client, ledger)

            # At-least-once sonuc teslimi (bu turdaki + onceki turlardan kalanlar)
            _deliver_ledger_results(client, ledger)
        except GatewayConfigError as exc:
            # /pending erisilemedi — bir sonraki turda tekrar denenir (komut
            # ledger'da guvende; kaybolmaz). Art arda hatada SADECE ilkini logla
            # (backend restart penceresinde spam onle).
            consecutive_errors += 1
            if consecutive_errors == 1:
                logger.warning("command_poll_error gateway=%s error=%s", client.gateway_code, exc)
            elif consecutive_errors % 60 == 0:
                # Uzun sureli kesintide ~1dk'da bir hatirlatma (tamamen susmasin).
                logger.warning(
                    "command_poll_error gateway=%s suruyor (%d ardisik) error=%s",
                    client.gateway_code,
                    consecutive_errors,
                    exc,
                )
            state.record_command_poll(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            if consecutive_errors == 1:
                logger.exception("command_poll_unexpected gateway=%s", client.gateway_code)
            state.record_command_poll(ok=False, error=str(exc))
        stop_event.wait(timeout=max(1, poll_sec))


#: Komut sonucu teslimi kac kez KALICI hata alirsa dead-letter'a tasinir.
#: Kalici hatalarda bile birkac deneme birakiyoruz: backend deploy'u sirasinda
#: gecici olarak 404 donebilir.
_COMMAND_RESULT_MAX_PERMANENT_FAILURES = 3

#: Tek POST'ta gonderilecek azami sonuc sayisi. Kuyruk birikirse govde
#: buyuyup `command_poll_timeout_sec`'i asiyordu — kendi kendini besleyen ariza.
_COMMAND_RESULT_BATCH = 50


#: Tek istekte gonderilecek azami teslim onayi. Backend tavani 500; buradaki
#: sinir onun altinda kalir ve govdeyi kucuk tutar.
_COMMAND_ACK_BATCH = 100


def _deliver_delivery_acks(client: BackendConfigClient, ledger) -> None:
    """Bekleyen teslim onaylarini backend'e gonderir (F3C).

    SONUC KUYRUGUNDAN AYRI BIR YASAM DONGUSU. Ikisini karistirmak yanlis
    olurdu: ACK "komutu dayanikli olarak kabul ettim" der ve komut cihazda
    calismadan ONCE uretilir; sonuc ise "cihaz sunu yapti" der. Bir komutun
    ACK'i teslim edilmisken sonucu hala kuyrukta olabilir.

    HATA POLITIKASI: ACK teslim edilemezse kayit defterde `pending` KALIR ve
    bir sonraki turda (ve proses/container restart'indan sonra) yeniden
    denenir. Dead-letter YOK — ACK'ten vazgecmek, backend'in komutu hicbir
    zaman `sent` gormemesi ve kira/deneme tukenince `delivery_failed`
    isaretlemesi demektir. Yani vazgecmenin bedeli sessiz degil, gorunur bir
    teslim basarisizligidir; sonsuz yeniden deneme bu yuzden dogru davranis.

    Bu fonksiyon ISTISNA SIZDIRMAZ: cagiran taraf komut poll dongusudur ve
    ACK teslimindeki bir hata komut kanalini dusurmemelidir.
    """
    try:
        bekleyen = ledger.pending_acks()
    except Exception:  # noqa: BLE001
        logger.debug("pending_acks okunamadi", exc_info=True)
        return
    if not bekleyen:
        return

    parca = bekleyen[:_COMMAND_ACK_BATCH]
    try:
        islenen = client.report_delivery_acks(parca)
    except Exception as exc:  # noqa: BLE001
        gecici = is_transient(exc)
        # JETON LOGLANMAZ — yalnizca sayi ve komut id'leri.
        logger.warning(
            "command_delivery_ack_failed count=%d gecici=%s error=%s — ACK defterde kaldi, yeniden denenecek",
            len(parca),
            gecici,
            str(exc)[:200],
        )
        return

    for cid in islenen:
        try:
            ledger.mark_ack_delivered(int(cid))
        except Exception:  # noqa: BLE001
            logger.debug("mark_ack_delivered basarisiz id=%s", cid, exc_info=True)
    if islenen:
        logger.info(
            "command_delivery_ack_sent gateway=%s count=%d",
            client.gateway_code,
            len(islenen),
        )


def _deliver_ledger_results(client: BackendConfigClient, ledger) -> None:
    """command_ledger'da teslim bekleyen sonuclari backend'e bildir.

    KALICI/GECICI AYRIMI (0.5.1'de eklendi)
    ---------------------------------------
    Eskiden HER hata "gecici" kabul ediliyor ve sonuc ledger'da 'pending'
    kaliyordu. Backend KALICI bir 4xx dondugu anda (token rotasyonu -> 401,
    sema degisikligi -> 422, silinmis command_id -> 404) kuyruk BIR DAHA
    BOSALMIYORDU:
      * O andan sonraki TUM komut sonuclari backend'e ulasmiyor; operator
        panelinde komutlar sonsuza kadar "bekliyor" gorunurken kesiciler
        FIZIKSEL OLARAK surulmus oluyordu.
      * Liste her turda buyudugu icin POST govdesi surekli sisiyor ve
        `command_poll_timeout_sec` (4sn) asiliyordu — kendi kendini besleyen
        ariza.
      * Saniyede bir WARNING log'u basiliyor, disari acilan hicbir sayac yoktu.

    Artik:
      * Gecici hata (ag / 408,425,429,502,503,504) -> aynen yeniden denenir.
      * Kalici hata -> ilgili sonuclarin basarisizlik sayaci artar; esik
        asilinca `mark_delivery_dead_letter` ile isaretlenir ve KUYRUK ILERLER.
      * Gonderim parcali (batch) yapilir; govde sinirsiz buyumez.
    """
    pending_results = ledger.pending_results()
    if not pending_results:
        return

    parca = pending_results[:_COMMAND_RESULT_BATCH]
    try:
        client.report_command_results(parca)
    except Exception as exc:  # noqa: BLE001
        gecici = is_transient(exc)
        if gecici:
            logger.warning(
                "command_result_delivery_failed count=%d error=%s — GECICI, sonraki turda tekrar",
                len(parca),
                exc,
            )
            return
        # KALICI: sayaci artir, esigi asani dead-letter'a al ve ILERLE.
        for res in parca:
            try:
                cid = int(res["id"])
            except (KeyError, TypeError, ValueError):
                continue
            adet = ledger.bump_delivery_failure(cid)
            if adet >= _COMMAND_RESULT_MAX_PERMANENT_FAILURES:
                ledger.mark_delivery_dead_letter(cid, str(exc)[:500])
                logger.error(
                    "command_result_dead_letter id=%s failures=%d error=%s — sonuc "
                    "backend'e teslim EDILEMEDI ve kuyruktan cikarildi. Komut "
                    "cihaza GONDERILMIS olabilir; ledger'da kayitlidir.",
                    cid,
                    adet,
                    str(exc)[:200],
                )
        logger.warning(
            "command_result_delivery_permanent count=%d error=%s — kalici hata, kuyruk ilerletildi",
            len(parca),
            exc,
        )
        return

    for res in parca:
        try:
            ledger.mark_delivered(int(res["id"]))
        except Exception:  # noqa: BLE001
            logger.debug("ledger_mark_delivered_failed id=%r", res.get("id"), exc_info=True)


def _run_config_refresh(
    *,
    client: BackendConfigClient,
    state: GatewayState,
    config_ready: Event,
    stop_event: Event,
    refresh_sec: int,
    config_wake: Event | None = None,
) -> None:
    """Backend config refresh thread'i. Basarili durumda sabit interval (5dk);
    hata durumunda exponential backoff (`refresh_sec → 2*refresh_sec → ... → 300s cap`).

    `config_wake`: komut-poll config_nonce artisini gorunce bu Event'i set eder;
    thread interval'i beklemeyi birakip config'i HEMEN ceker (cihaz IP/ayar
    degisince ~1sn'de yansir, 5dk beklenmez).

    401/403/404 hatalarinda spesifik mesaj uretir; bu durumlar genelde manuel
    duzeltme gerektirir (token degismis, gateway code yanlis vs.) — health
    endpoint'i `state.last_refresh_error` uzerinden bunu raporlar.
    """
    import random

    last_active_state: bool | None = None
    consecutive_failures = 0
    base_interval = max(5, refresh_sec)
    max_backoff = 300  # 5 dakika cap
    while not stop_event.is_set():
        try:
            config = client.fetch_config()
            changed = state.update(config)
            config_ready.set()
            if consecutive_failures:
                logger.info(
                    "config_refresh_recovered gateway=%s after_failures=%d",
                    config.gateway_code,
                    consecutive_failures,
                )
                consecutive_failures = 0
            if changed:
                logger.info(
                    "config_refresh gateway=%s version=%s devices=%s signals=%s active=%s",
                    config.gateway_code,
                    config.config_version,
                    len(config.devices),
                    len(config.signals),
                    config.is_active,
                )
            if last_active_state is None or last_active_state != config.is_active:
                last_active_state = config.is_active
                if config.is_active:
                    logger.info("gateway_polling_resumed gateway=%s", config.gateway_code)
                else:
                    logger.warning(
                        "gateway_polling_suspended gateway=%s (is_active=False)",
                        config.gateway_code,
                    )
            # Saglikli refresh — sabit interval bekle AMA config_nonce tetigi
            # gelirse erken uyan (config degisti, hemen cek). config_wake
            # komut-poll tarafindan set edilir; uyaninca temizlenir.
            if config_wake is not None:
                config_wake.wait(timeout=base_interval)
                config_wake.clear()
            else:
                stop_event.wait(timeout=base_interval)
            continue
        except GatewayConfigError as exc:
            err = str(exc)
            consecutive_failures += 1
            state.record_refresh_error(err)
            # 401/403: token problemi — auto-recovery yok, manuel mudahale gerek
            if "401" in err or "403" in err:
                logger.error(
                    "config_auth_error gateway=%s consecutive=%d error=%s "
                    "— GATEWAY_TOKEN backend gateways.token ile eslesmiyor; "
                    ".env GATEWAY_TOKEN'i kontrol edin",
                    client.gateway_code,
                    consecutive_failures,
                    err,
                )
            elif "404" in err:
                logger.error(
                    "config_404_error gateway=%s consecutive=%d "
                    "— Backendde '%s' kodlu gateway yok. Arayüz: Mühendislik → "
                    "Gateway Yönetimi → ayni Kod + ayni Token ile kayit acin",
                    client.gateway_code,
                    consecutive_failures,
                    client.gateway_code,
                )
            else:
                # Network / 5xx / timeout — geri donusumlu, exponential backoff uygula
                if consecutive_failures in (1, 5, 30, 100, 1000):
                    # Sadece 1, 5, 30, 100, 1000 inci hatalarda WARN log; aksi
                    # halde DEBUG, log spam'i onlenir
                    logger.warning(
                        "config_refresh_error gateway=%s consecutive=%d error=%s",
                        client.gateway_code,
                        consecutive_failures,
                        err,
                    )
                else:
                    logger.debug(
                        "config_refresh_error gateway=%s consecutive=%d error=%s",
                        client.gateway_code,
                        consecutive_failures,
                        err,
                    )
        except Exception as exc:  # noqa: BLE001
            # GatewayConfigError disinda beklenmedik bir hata: pydantic
            # ValidationError, requests.exceptions.SSLError, ConnectionResetError,
            # urllib3 dekoder vb. Bu thread oldukcecekse gateway icin
            # "/health: config_refresh_failing" sessiz kalir ve config bir daha
            # YENILENMEZ. Bunu engellemek icin general Exception'i yakalayip
            # state'e yaziyoruz; backoff loop'u devam eder.
            err = f"unexpected_error: {type(exc).__name__}: {exc}"
            consecutive_failures += 1
            try:
                state.record_refresh_error(err)
            except Exception:  # noqa: BLE001
                # state.record_refresh_error de basarisiz olursa thread olmesin
                pass
            if consecutive_failures in (1, 5, 30, 100, 1000):
                logger.exception(
                    "config_refresh_unexpected_error gateway=%s consecutive=%d",
                    client.gateway_code,
                    consecutive_failures,
                )
            else:
                logger.debug(
                    "config_refresh_unexpected_error gateway=%s consecutive=%d error=%s",
                    client.gateway_code,
                    consecutive_failures,
                    err,
                )
        # Exponential backoff: base * 2^n, ±%20 jitter, cap 300s
        backoff = min(max_backoff, base_interval * (2 ** min(consecutive_failures - 1, 8)))
        jitter = backoff * random.uniform(-0.2, 0.2)
        wait_time = max(base_interval, backoff + jitter)
        stop_event.wait(timeout=wait_time)


class _MaintenanceStage:
    """Periyodik bakim isleri icin son-calisma zamanlarini tutar (monotonic)."""

    __slots__ = ("last_disk_check", "last_prune")

    def __init__(self) -> None:
        self.last_disk_check: float = 0.0
        self.last_prune: float = 0.0


# Bakim araliklari (saniye).
_DISK_CHECK_INTERVAL_SEC = 60.0
_PRUNE_INTERVAL_SEC = 6 * 3600.0


def _run_periodic_maintenance(
    *,
    now: float,
    stage: _MaintenanceStage,
    disk_guard: Any,
    outbox: Any,
    ledger: Any,
    cfg: Settings,
) -> None:
    """Disk olcumu + kalici tablo budamasi. Hicbir hata poll akisini durdurmaz.

    NEDEN: "disk-full circuit breaker" mesaj sayiyordu, BAYT saymiyordu;
    dead_letter ve command_ledger tablolarinda ise RETENTION YOKTU. Uc ayri
    arizanin (retrier olumu, log rotation kirilmasi, poller durmasi) ortak
    tetikleyicisi olan "disk" degiskeni hicbir yerde olculmuyordu.
    """
    try:
        if now - stage.last_disk_check >= _DISK_CHECK_INTERVAL_SEC:
            stage.last_disk_check = now
            disk_guard.check()
    except Exception:  # noqa: BLE001
        logger.debug("disk_check_failed", exc_info=True)

    try:
        if now - stage.last_prune >= _PRUNE_INTERVAL_SEC:
            stage.last_prune = now
            removed_dl = outbox.prune_dead_letter(
                retain_days=cfg.outbox_dead_letter_retain_days,
                max_rows=cfg.outbox_dead_letter_max_rows,
            )
            removed_cmd = ledger.prune(retain_days=cfg.command_ledger_retain_days)
            if removed_dl or removed_cmd:
                logger.info(
                    "state_pruned dead_letter=%d command_ledger=%d",
                    removed_dl,
                    removed_cmd,
                )
    except Exception:  # noqa: BLE001
        logger.warning("state_prune_failed", exc_info=True)


def _install_signal_handlers(stop_event: Event) -> None:
    def _handler(signum, _frame):  # type: ignore[no-untyped-def]
        logger.info("signal_received signum=%s shutdown=starting", signum)
        stop_event.set()

    # Platform bazli sinyal listesi:
    #   - SIGINT (Ctrl+C): tum platformlarda
    #   - SIGTERM: POSIX (Linux/macOS); Windows'ta var ama event-driven
    #     (NSSM stop'ta tetiklenmez)
    #   - SIGBREAK (Ctrl+Break / Windows console close): SADECE Windows.
    #     NSSM "AppStopMethodConsole" Ctrl+Break gonderir -> SIGBREAK.
    signals_to_install = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals_to_install.append(sigbreak)
    for sig in signals_to_install:
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:
            # Windows alt thread'lerde bazi sinyaller atanamaz; sessiz gecmek
            # yerine WARN log atalim ki "neden Ctrl+Break calismadi" debug edilebilir.
            logger.warning(
                "signal_handler_install_failed signal=%s error=%s — bu sinyal "
                "graceful shutdown'i tetiklemeyecek; manuel restart gerekebilir",
                sig,
                exc,
            )


def _tls_verify_param(cfg: Settings) -> bool | str:
    """requests `verify` argumani: True | False | CA bundle yolu."""

    if cfg.backend_api_ca_path and str(cfg.backend_api_ca_path).strip():
        return str(cfg.backend_api_ca_path).strip()
    return cfg.backend_api_verify_ssl


def _build_http_broker(cfg: Settings, identity: GatewayIdentity) -> Any:
    from dnp3_gateway.messaging.http_publisher import HttpTelemetryPublisher

    return HttpTelemetryPublisher(
        base_url=cfg.backend_api_url,
        identity=identity,
        timeout_sec=cfg.config_timeout_sec,
        verify=_tls_verify_param(cfg),
    )


def _build_telemetry_broker(cfg: Settings, identity: GatewayIdentity) -> Any:
    """Kurulum moduna gore telemetri tasima yolunu kurar.

    Uc olasi sonuc:
      * TELEMETRY_PUBLISHER=http  -> yalnizca HTTP (bilincli rollback).
      * INSTALL_MODE=local        -> yalnizca NATS. Yedek yol YOK; NATS
        erisilemezse mesajlar outbox'ta birikir ve /health acikca sikayet
        eder. Sessiz HTTP'ye dusus, ayni makinedeki bir yapilandirma
        hatasini gizleyip performans hedefini kaybettirir.
      * INSTALL_MODE=remote       -> NATS birincil + HTTP yedek. Yedege
        dusus ve geri donus `TelemetryTransportRouter` tarafindan yonetilir.
    """
    if cfg.telemetry_publisher != "nats":
        # Rollback yolu: backend HTTP ingest. Her olcum backend + Postgres
        # outbox zincirinden gecer; yalnizca bilincli secimle kullanilir.
        logger.warning(
            "telemetry_transport_selected mode=%s active=http fallback=yok — "
            "TELEMETRY_PUBLISHER=http ile ROLLBACK yolu secildi; telemetri "
            "backend HTTP ingest uzerinden gidecek (NATS kullanilmiyor).",
            cfg.install_mode,
        )
        return _build_http_broker(cfg, identity)

    from dnp3_gateway.messaging.jetstream_publisher import JetStreamPublisher

    nats_broker = JetStreamPublisher.create(
        url=cfg.nats_url,
        subject_prefix=cfg.nats_subject_prefix,
        gateway_code=identity.gateway_code,
        connect_timeout_sec=cfg.nats_connect_timeout_sec,
        publish_timeout_sec=cfg.nats_publish_timeout_sec,
        tls_ca_path=cfg.nats_tls_ca_path,
        credentials_path=cfg.nats_credentials_path,
    )
    if nats_broker is None:
        logger.error(
            "jetstream_publisher_create_failed — nats-py paketi yuklu degil. "
            "Gateway baslamiyor. requirements.txt + pip install -r"
        )
        raise SystemExit(1)

    from dnp3_gateway.messaging.transport_router import TelemetryTransportRouter

    yerel = cfg.install_mode == "local"
    http_broker = None if yerel else _build_http_broker(cfg, identity)
    router = TelemetryTransportRouter(
        nats_broker=nats_broker,
        http_broker=http_broker,
        install_mode=cfg.install_mode,
        fail_threshold=cfg.telemetry_fallback_fail_threshold,
        probe_interval_sec=cfg.telemetry_fallback_probe_interval_sec,
    )
    # Boot'ta aktif yol ve yedek politikasi TEK satirda gorunur olmali —
    # "su an veri hangi yoldan gidiyor?" sorusunun log'daki ilk cevabi budur.
    logger.info(
        "telemetry_transport_selected install_mode=%s active=%s fallback=%s "
        "nats_subject=%s backend_ingest=%s",
        cfg.install_mode,
        router.active_transport,
        "http" if router.fallback_enabled else "yok (yerel kurulumda NATS zorunlu)",
        getattr(nats_broker, "subject", "?"),
        cfg.backend_api_url if router.fallback_enabled else "-",
    )
    return router


def run(current_settings: Settings | None = None) -> int:
    # `get_settings()` LAZY: import aninda degil, burada kurulur. Uretim
    # yolunda __main__ zaten kendi Settings'ini gecirir.
    cfg = current_settings or get_settings()
    # Iki asamali logging: identity'yi bilmeden once minimal (stdout-only)
    # konfigure ederiz ki bootstrap log'lari kaybolmasin. Identity'yi
    # ogrendikten sonra TEKRAR konfigure ederiz; bu sefer per-instance
    # rotating file handler da acilir (LOG_FILE_PATH set ise).
    configure_logging(level=cfg.log_level, fmt=cfg.log_format)

    identity = bootstrap_gateway_identity(settings=cfg, app_version=__version__)
    # Multi-instance lock — ayni GATEWAY_CODE iki kez calismasin (outbox
    # corrupt + duplicate publish riskini onler). Lock dosyasi state_dir
    # icinde acik kalir; main fonksiyonu cikinca OS otomatik release eder.
    # Referansi local degiskende tutuyoruz ki garbage collect close etmesin.
    _instance_lock = acquire_instance_lock(
        state_dir=cfg.gateway_state_dir,
        gateway_code=identity.gateway_code,
    )
    # Identity hazir — rotating dosya handler'i da ekleyerek logging'i tekrar
    # yapilandir (idempotent: eski handler'lar temizlenir).
    configure_logging(
        level=cfg.log_level,
        fmt=cfg.log_format,
        file_path=cfg.log_file_path,
        file_max_bytes=cfg.log_file_max_bytes,
        file_backup_count=cfg.log_file_backup_count,
        gateway_code=identity.gateway_code,
        instance_id=identity.instance_id,
    )
    # Sirli verileri (token, NATS parolasi) log redaction listesine kaydet —
    # boylece exception/3rd party log'lara kazara sizmasin. Bu cagri
    # configure_logging SONRASI yapilmali (filter aktif olduktan).
    try:
        register_secret(identity.token)
        if cfg.gateway_refresh_token:
            register_secret(cfg.gateway_refresh_token)
        from urllib.parse import urlparse as _urlparse

        # NATS URL parolasi (primary publisher): nats://user:PASSWORD@host
        _nats_parsed = _urlparse(cfg.nats_url) if cfg.nats_url else None
        if _nats_parsed and _nats_parsed.password:
            register_secret(_nats_parsed.password)
    except Exception:  # noqa: BLE001
        # Secret kayit hatasi gateway'i durdurmamali
        pass
    # Konsola (stdout): log seviyesinden bagimsiz; kurulumda token eslemesini dogrulamak icin.
    if cfg.show_gateway_token_on_start:
        print(f"[dnp3-gateway] GATEWAY_TOKEN={identity.token}", flush=True)
    else:
        print(
            f"[dnp3-gateway] GATEWAY_TOKEN (maskeli)={_mask_secret(identity.token)}  "
            f"(tam metin icin .env: SHOW_GATEWAY_TOKEN_ON_START=true)",
            flush=True,
        )
    logger.info(
        "dnp3_gateway_starting version=%s gateway=%s instance=%s env=%s mode=%s "
        "publisher=%s install_mode=%s backend=%s",
        __version__,
        identity.gateway_code,
        identity.instance_id,
        identity.app_environment,
        cfg.gateway_mode,
        cfg.telemetry_publisher,
        cfg.install_mode,
        cfg.backend_api_url,
    )
    # Deprecation uyarisi: nats_dual_publish_enabled artik anlamsiz (JetStream
    # tek yol). Operator eski .env'den geliyorsa bilgilendir.
    if cfg.nats_dual_publish_enabled:
        logger.warning(
            "nats_dual_publish_enabled=True ayarlanmis ama DEPRECATED — "
            "gateway 0.4.x'te JetStream artik tek primary publisher, dual-publish "
            "modunu yok sayiyor. .env'den NATS_DUAL_PUBLISH_ENABLED=false yapin "
            "veya satiri silin."
        )
    # Loud uyari: insecure plaintext opt-out — operator clear-text http://public
    # ya da nats://public icin bilincli izin vermis. Boot'ta yuksek sesle log
    # atalim ki gozden kacmasin; uretim icin gecici (TLS kurulana kadar).
    if cfg.gateway_insecure_allow_plaintext:
        logger.warning(
            "GATEWAY_INSECURE_ALLOW_PLAINTEXT=true — production validator "
            "public host'a clear-text HTTP/nats:// izin veriyor. Token ve "
            "telemetri MITM riski altinda. Mumkun olan en kisa zamanda TLS "
            "kurun (backend Caddy/Let's Encrypt, NATS tls:// listener)."
        )
    # M5: Production'da LOG_LEVEL=DEBUG olursa loud WARN. DEBUG seviyesinde
    # 3rd-party kutuphaneler (requests, urllib3, sqlite3) cookie/payload/
    # header detaylari loglayabilir. `register_secret` + broker URL regex
    # cogu sirri yakalar AMA tum hassas verileri garanti edemez. Operator
    # gecici debug yapiyorsa OK; uzun sureli production DEBUG istenmez.
    if identity.app_environment == "production" and cfg.log_level.upper() == "DEBUG":
        logger.warning(
            "PRODUCTION_DEBUG_LOG_LEVEL: LOG_LEVEL=DEBUG bircok 3rd-party "
            "kutuphanenin (requests, urllib3, sqlite3 vb.) gizli detay "
            "logu uretmesine yol acar. Redaction filter saldirgan attack "
            "vector'lerini kapatsa da garanti vermez. Geçici debug oturumu "
            "icin OK; uzun sureli prod'da INFO/WARNING tavsiye edilir."
        )

    # Disk cache: backend kapali iken / container restart'ta gateway en son
    # gordugu config ile (cihaz IP'leri, sinyal listesi, master_address)
    # polling'e devam eder. Yeni config geldiginde uzerine yazilir.
    config_cache_path = Path(cfg.gateway_state_dir) / f"config_{identity.gateway_code}.json"
    state = GatewayState(
        cache_path=config_cache_path,
        cache_max_age_hours=cfg.config_cache_max_age_hours,
    )
    if state.load_from_cache():
        # Disk'te onceki config varsa polling hemen baslayabilir; backend
        # ulasilmazsa bile veri toplama durmaz.
        config_ready_initial = True
    else:
        config_ready_initial = False
    # Disk ve saat denetimi: /health'in gormedigi iki kor nokta.
    disk_guard = DiskGuard(cfg.gateway_state_dir)
    clock_guard = ClockGuard()
    config_ready = Event()
    if config_ready_initial:
        config_ready.set()
    stop_event = Event()

    # publisher_holder: health server publisher'a referans tutmasin diye
    # 0-arglik callable; publisher daha sonra olusturulup buraya yazilir.
    # Boylece health_server start sonrasi publisher injection olabilir.
    publisher_holder: dict[str, Any] = {"publisher": None}

    def _publisher_provider() -> Any:
        return publisher_holder["publisher"]

    # reader_provider: /refresh-all endpoint'i icin reader'a erisim.
    # Reader bir sonraki adimlarda olusturulup state'e baglanacak; aradaki
    # holder pattern ile injection.
    reader_holder: dict[str, Any] = {"reader": None}

    def _reader_provider() -> Any:
        return reader_holder["reader"]

    # ledger_provider: /operate endpoint'i idempotency icin CommandLedger'a
    # erisir. Ledger health server'dan SONRA olusturuldugu icin holder pattern.
    ledger_holder: dict[str, Any] = {"ledger": None}

    def _ledger_provider() -> Any:
        return ledger_holder["ledger"]

    # /refresh-all endpoint icin auth token. GATEWAY_REFRESH_TOKEN set edilmemis
    # ise endpoint TAMAMEN DEVRE DISI kalir (health_server.py refresh_token
    # bos olunca 503 doner). Eski fallback "GATEWAY_TOKEN'a dus" kaldirildi —
    # CHANGELOG ile tutarsizdi ve token leak'i tum gateway'e yayan attack
    # surface yaratiyordu.
    # /refresh-all ve /operate HTTP endpoint'leri: backend'in gateway'e PUSH
    # yaptigi opsiyonel yol (NAT arkasi gateway'de erisilemez). Refresh-all +
    # komutlar ZATEN pull kanaliyla calisir (config-poll refresh_nonce +
    # /pending komut-poll). Bu token'lar SADECE dogrudan HTTP push isteniyorsa
    # gerekir — normal kurulumda gerekmez. Bos olmalari sorun DEGIL; INFO.
    refresh_endpoint_token = (cfg.gateway_refresh_token or "").strip()
    if not refresh_endpoint_token:
        logger.info(
            "refresh_push_endpoint_off gateway=%s (GATEWAY_REFRESH_TOKEN bos) — "
            "refresh-all pull kanaliyla calisir; HTTP push kapali.",
            identity.gateway_code,
        )

    command_endpoint_token = (cfg.gateway_command_token or "").strip()
    if command_endpoint_token:
        register_secret(command_endpoint_token)
    else:
        logger.info(
            "operate_push_endpoint_off gateway=%s (GATEWAY_COMMAND_TOKEN bos) — "
            "komutlar pull kanaliyla (/pending komut-poll) calisir; HTTP push kapali.",
            identity.gateway_code,
        )

    # Arkaplan thread'leri burada kaydedilir; /health olu thread'i raporlar.
    liveness = ThreadLiveness()

    health, metrics, actual_health_port = start_health_server(
        host=cfg.worker_health_host,
        port=cfg.worker_health_port,
        state=state,
        gateway_code=identity.gateway_code,
        gateway_mode=cfg.gateway_mode,
        config_ready=config_ready,
        instance_id=identity.instance_id,
        app_environment=identity.app_environment,
        publisher_provider=_publisher_provider,
        reader_provider=_reader_provider,
        ledger_provider=_ledger_provider,
        # Backend bu endpoint'leri Bearer token ile cagirir.
        refresh_token=refresh_endpoint_token,
        command_token=command_endpoint_token,
        # Trusted reverse-proxy CIDR'leri — `X-Forwarded-For` sadece bu
        # subnet'lerden gelen istekler icin okunur. Bos ise XFF yok sayilir
        # (saldirgan rate-limit bypass yapamaz).
        trusted_proxies=cfg.health_trusted_proxies,
        # Arkaplan thread'lerinin canliligi + poll dongusu watchdog'u.
        # Bir thread sessizce olurse (retrier -> telemetri teslimi durur,
        # command-poll -> SCADA komut kanali olur) /health artik unhealthy doner.
        liveness=liveness,
        poll_interval_sec=cfg.default_poll_interval_sec,
        disk_guard=disk_guard,
        clock_guard=clock_guard,
    )
    # Banner: gercek (auto-assigned olabilir) health portunu yansitabilmek icin
    # health_server start sonrasi yazdirilir.
    _print_console_banner(cfg=cfg, identity=identity, actual_health_port=actual_health_port)

    broker = _build_telemetry_broker(cfg, identity)

    # Outbox: broker'a yayinlanmamis mesajlari diske yazar,
    # retrier sonra gonderir. Process restart'a dayanikli (kalici SQLite);
    # at-least-once delivery. Disk-full koruma: max_pending asilirsa publisher
    # OutboxFullError raise eder, poller cycle'i durdurur (sessiz veri kaybi
    # yerine kontrollu duraklatma).
    outbox_path = Path(cfg.gateway_state_dir) / f"outbox_{identity.gateway_code}.db"
    outbox = Outbox(outbox_path, max_pending=cfg.outbox_max_pending)

    # Komut ledger (restart-safe idempotency + at-least-once sonuc teslimi).
    # start_dispatch duplicate CROB'u onler; acilista recover_unknown_results
    # yarim kalmis komutlari backend'e 'unknown' bildirir.
    ledger_path = Path(cfg.gateway_state_dir) / f"command_ledger_{identity.gateway_code}.db"
    command_ledger = CommandLedger(ledger_path)
    # /operate endpoint'i idempotency icin ledger'a erissin (cift CROB onlemi).
    ledger_holder["ledger"] = command_ledger
    _recovered = command_ledger.recover_unknown_results()
    if _recovered:
        logger.warning(
            "command_ledger_recovered count=%d — yarim kalmis komutlar 'unknown' bildirilecek",
            len(_recovered),
        )
    # config degisince config thread'ini erken uyandirmak icin Event.
    config_wake = Event()
    pending = outbox.pending_count()
    dead_letter_count = outbox.dead_letter_count()
    if pending:
        logger.warning(
            "outbox_pending_messages count=%s db=%s — retrier hizla bosaltacak",
            pending,
            outbox_path,
        )
    if dead_letter_count:
        logger.warning(
            "outbox_dead_letter_count count=%s — manuel inceleme gerekebilir "
            "(SELECT * FROM outbox_dead_letter ORDER BY moved_at DESC)",
            dead_letter_count,
        )

    publisher = ResilientPublisher(broker=broker, outbox=outbox)
    # Health server'in /health/metrics endpoint'leri publisher uzerinden
    # outbox + circuit breaker durumunu okuyabilsin diye holder'a yaz
    publisher_holder["publisher"] = publisher
    # Toplu drenaj yolu: broker publish_batch destekliyorsa (HTTP ingest)
    # retrier 200 mesaji TEK POST + TEK DELETE transaction'i ile bosaltir.
    # Bu, birikmis outbox'in drenaj hizini onlarca kat artirir.
    _batch_drain = publisher.publish_outbox_rows if callable(getattr(broker, "publish_batch", None)) else None
    retrier = OutboxRetrier(
        outbox,
        publish_fn=publisher.publish_outbox_row,
        publish_batch_fn=_batch_drain,
        # Kuyruk bosaldiginda outbox-full breaker'i kapatmayi dene.
        # Bu kanal olmadan, kuyruk dead-letter yoluyla bosaldiginda breaker
        # acik kalir ve gateway RESTART'a kadar hic telemetri yayinlamaz.
        on_idle=publisher.notify_outbox_idle,
        poll_interval_sec=cfg.outbox_retrier_poll_interval_sec,
        batch_size=cfg.outbox_retrier_batch_size,
        max_retries=cfg.outbox_max_retries,
        min_backoff_sec=cfg.outbox_retrier_min_backoff_sec,
        max_backoff_sec=cfg.outbox_retrier_max_backoff_sec,
    )
    retrier.start()
    liveness.register("outbox-retrier", retrier)

    # opendnp3 callback'leri (Now) native thread'lerden cagrilir; saat
    # sapmasi gozlemcisini modul seviyesinde adapter'a baglariz.
    try:
        from dnp3_gateway.adapters import dnp3_yadnp3_master as _yadnp3_mod

        _yadnp3_mod.set_clock_guard(clock_guard)
    except Exception:  # noqa: BLE001 — yadnp3 kurulu olmayabilir
        logger.debug("clock_guard_adapter_bind_skipped", exc_info=True)

    reader: TelemetryReader = build_adapter(cfg)
    # Health server /refresh-all endpoint'i icin injection
    reader_holder["reader"] = reader
    logger.info("telemetry_adapter=%s", type(reader).__name__)
    if cfg.is_mock_mode:
        logger.warning(
            "TELEMETRI_KAYNAGI=MOCK — sahadan DNP3 okunmuyor; uretici degerler. "
            "JetStream akisi calisir ama kaynak cihaz degil. Gercek okuma: "
            "GATEWAY_MODE=dnp3, yadnp3 wheel kurulu, cihaz IP/port/DNP3 adresi backend config."
        )
    else:
        logger.info(
            "TELEMETRI_KAYNAGI=DNP3 — library=%s; baglanti: backend devices[] "
            "(ip, dnp3_address, istege bagli dnp3_tcp_port; yoksa varsayilan port=%s).",
            (cfg.dnp3_library or "yadnp3"),
            cfg.dnp3_tcp_port,
        )

    # Cihaz IP allowlist'i ETKIN Settings'ten cozumlenir (import-time singleton
    # degil) — boylece `--env-file .env.GW-002` ile baslatilan instance kendi
    # allowlist'ini kullanir. Gecersiz CIDR girisleri burada bir kez, net sekilde
    # loglanir; hepsi gecersizse fail-closed davranilir (tum cihazlar reddedilir).
    device_ip_allowlist = parse_device_ip_allowlist(cfg.dnp3_device_allowed_subnets)
    _log_allowlist_state(device_ip_allowlist)
    if device_ip_allowlist.fail_closed:
        raise SystemExit(
            "DNP3_DEVICE_ALLOWED_SUBNETS tanimli ama tek bir gecerli CIDR yok "
            f"(gecersiz girisler: {', '.join(device_ip_allowlist.invalid_entries)}). "
            "Bu haliyle gateway hicbir cihaza baglanamaz. Ayari duzeltin, "
            "ornek: DNP3_DEVICE_ALLOWED_SUBNETS=192.168.10.0/24,10.0.5.0/24"
        )

    # Saglik ozeti: saniyede bir zaten atilan `/pending` istegine baslik olarak
    # biner (bkz. backend/health_header.py). Ek istek, ek baglanti YOK.
    #
    # NEDEN GEREKLI: backend cihaz durumunu yalnizca telemetri geldiginde
    # guncelleyebiliyor. Ariza bekleyen bir gosterge saatlerce sessiz kalabilir;
    # "veri gelmiyor" ile "haberlesme koptu" ayrimini yapabilecek tek yer
    # gateway, cunku DNP3 link durumu burada tutuluyor.
    # Durum ve sorun listesi `/health` ile AYNI kaynaktan geliyor. Burada
    # elle "ok" yazmak, outbox dolarken ya da bir thread olmusken bile
    # backend'e "iyiyim" dedirtirdi — duzeltmeye calistigimiz yalanin ta
    # kendisi. Tek dogruluk kaynagi `_build_health_body`.
    #
    # ONBELLEK: govde uretimi outbox/metrik/thread anlik goruntusu aliyor;
    # komut-poll saniyede bir kosuyor ve bu kutu 2 cekirdekli. Backend zaten
    # 25 saniyede birden sik yazmiyor, dolayisiyla saniyede bir uretmenin
    # karsiligi yok. 10 saniye, yazma araliginin yarisindan kisa — bayat
    # veri gonderme riski yok.
    saglik_onbellek_sec = 10.0
    saglik_onbellek: dict[str, Any] = {"ts": 0.0, "govde": None}

    def _saglik_govdesi() -> dict[str, Any]:
        simdi = time.monotonic()
        if saglik_onbellek["govde"] is not None and (simdi - saglik_onbellek["ts"]) < saglik_onbellek_sec:
            return saglik_onbellek["govde"]

        okuyucu = reader_holder.get("reader")
        govde, _http = _build_health_body(
            state=state,
            gateway_code=identity.gateway_code,
            gateway_mode=cfg.gateway_mode,
            config_ready=config_ready,
            instance_id=identity.instance_id,
            app_environment=identity.app_environment,
            health_port=actual_health_port,
            publisher=publisher_holder.get("publisher"),
            metrics=metrics,
            reader=okuyucu,
            liveness=liveness,
            poll_interval_sec=cfg.default_poll_interval_sec,
            disk_guard=disk_guard,
            clock_guard=clock_guard,
            ledger_provider=_ledger_provider,
        )

        # Cihaz KODLARI yalnizca bu kimlik dogrulamali baslikta gider.
        # `/health` auth'suz oldugu icin govdedeki `devices` sadece sayim
        # tasir; kod bazli durumu adapter'dan ayrica aliyoruz.
        per_device: dict[str, Any] = {}
        saglik_fn = getattr(okuyucu, "device_health", None)
        if callable(saglik_fn):
            try:
                per_device = saglik_fn() or {}
            except Exception:  # noqa: BLE001
                per_device = {}

        outbox_snap = govde.get("outbox") or {}
        sonuc = health_header.build_payload(
            status=str(govde.get("status") or "unknown"),
            device_summary=govde.get("devices"),
            per_device=per_device,
            outbox_pending=outbox_snap.get("outbox_pending"),
            outbox_dead_letter=outbox_snap.get("outbox_dead_letter"),
            uptime_sec=(govde.get("metrics") or {}).get("uptime_sec"),
            gateway_version=__version__,
            issues=govde.get("issues"),
            # F3C: yetenek + defter kimligi. Teslim KARARI bu alandan
            # verilmez (saglik 30sn'de bir gider, bayat olabilir); burasi
            # yalnizca operatorun/rollout dogrulamasinin filoyu
            # `gateway_health` uzerinden gorebilmesi icin.
            ledger_epoch=command_ledger.epoch,
        )
        saglik_onbellek["ts"] = simdi
        saglik_onbellek["govde"] = sonuc
        return sonuc

    config_client = BackendConfigClient(
        base_url=cfg.backend_api_url,
        identity=identity,
        timeout_sec=cfg.config_timeout_sec,
        verify=_tls_verify_param(cfg),
        response_max_bytes=cfg.backend_response_max_bytes,
        device_ip_allowlist=device_ip_allowlist,
        clock_guard=clock_guard,
        require_response_signature=cfg.require_backend_response_signature,
    )

    # Command-poll icin AYRI client + session. Config-refresh thread'i 5dk'da
    # bir agir config fetch yapar; ayni session paylasilirsa 1sn'lik command
    # poll o fetch'e takilip "Read timed out" alirdi. Ayri session + kisa
    # (connect, read) timeout ile poll bagimsiz kalir.
    command_client = BackendConfigClient(
        base_url=cfg.backend_api_url,
        identity=identity,
        timeout_sec=(3, cfg.command_poll_timeout_sec),
        verify=_tls_verify_param(cfg),
        response_max_bytes=cfg.backend_response_max_bytes,
        device_ip_allowlist=device_ip_allowlist,
        require_response_signature=cfg.require_backend_response_signature,
        # Saglik ozeti komut-poll'e biner: config-refresh 5 dakikada bir kosar,
        # komut-poll saniyede bir. Cihaz kaybini 5 dakika gec ogrenmek, "double
        # check" olmasi gereken mekanizmanin amacini bosa cikarirdi.
        health_provider=_saglik_govdesi,
        # HER poll'de TAZE defter kimligi. Callable: defter yeniden
        # yaratilirsa yeni kimlik aninda yansisin (kopya tutmak bayat bir
        # kimlik gondermek olurdu - tam da onlemek istedigimiz sey).
        delivery_provider=lambda: command_ledger.epoch,
    )

    refresh_thread = Thread(
        target=_run_config_refresh,
        kwargs={
            "client": config_client,
            "state": state,
            "config_ready": config_ready,
            "stop_event": stop_event,
            "refresh_sec": cfg.config_refresh_sec,
            "config_wake": config_wake,
        },
        name="config-refresh",
        daemon=True,
    )
    refresh_thread.start()
    liveness.register("config-refresh", refresh_thread)

    # Hafif komut-poll thread'i (config'ten AYRI, ~1sn). Komutlar burada gelir;
    # config 5dk'da bir. reader operate_device destekliyorsa baslat.
    command_thread: Thread | None = None
    if hasattr(reader, "operate_device"):
        command_thread = Thread(
            target=_run_command_poll,
            kwargs={
                "client": command_client,
                "reader": reader,
                "state": state,
                "ledger": command_ledger,
                "stop_event": stop_event,
                "poll_sec": cfg.command_poll_sec,
                "config_wake": config_wake,
                "max_age_sec": cfg.command_max_age_sec,
                "require_timestamp": cfg.command_require_timestamp,
                "clock_skew_tolerance_sec": cfg.command_clock_skew_tolerance_sec,
            },
            name="command-poll",
            daemon=True,
        )
        command_thread.start()
        liveness.register("command-poll", command_thread)
        logger.info(
            "command_poll_started gateway=%s poll_sec=%d (config_refresh_sec=%d)",
            identity.gateway_code,
            cfg.command_poll_sec,
            cfg.config_refresh_sec,
        )

    _install_signal_handlers(stop_event)

    # Ilk config gelene kadar 15 saniye bekle. Backend erisilemezse yine de
    # dongu calismaya baslar ama `is_active` False oldugu icin mesaj yayinlamaz.
    if not config_ready.wait(timeout=15):
        logger.warning("first_config_not_received_yet gateway=%s", identity.gateway_code)

    logger.info(
        "dnp3_gateway_running health=%s:%s instance=%s",
        cfg.worker_health_host,
        actual_health_port,
        identity.instance_id,
    )

    # Ardisik cycle hatasi sayaci (log spam kontrolu icin).
    cycle_failures = 0
    maintenance = _MaintenanceStage()

    try:
        while not stop_event.is_set():
            now_monotonic = time.monotonic()
            # Operator tetikli "tum cihazlara sorgu at" bayragi varsa
            # cycle baslamadan once integrity poll iste; cevaplar SOE
            # callback'i ile cache'e yazilacak ve sonraki cycle'larda
            # poller normal akista publish edecek.
            try:
                # Shutdown sirasinda refresh-all tetiklenirse reader.close()
                # ile yarisi olabilir; stop_event onceligi ver, tetikleme.
                if (
                    not stop_event.is_set()
                    and state.take_refresh_request()
                    and hasattr(reader, "refresh_all_devices")
                ):
                    ok, total = reader.refresh_all_devices()  # type: ignore[attr-defined]
                    logger.info("manual_refresh_all_triggered ok=%d total=%d", ok, total)
            except Exception:  # noqa: BLE001
                logger.exception("manual_refresh_all_dispatch_failed")

            # NOT: Komut calistirma artik AYRI _run_command_poll thread'inde
            # (config'ten ayrildi, ~1sn). Ana dongu sadece DNP3 okuma/publish yapar.
            #
            # CYCLE GOVDESI SARMALANMIS: eskiden yalnizca KeyboardInterrupt
            # yakalaniyordu. Tek cihazli bir gateway'de disk dolarsa
            # `outbox.enqueue` sqlite3.OperationalError firlatir, seri kol bunu
            # yakalamaz, ana dongu de yakalamaz -> PROSES OLUR. Docker'da
            # `restart: unless-stopped` ile crash-loop, Windows'ta NSSM'in
            # restart ayari yoksa gateway kalici olarak durur.
            # Artik dongu hatayi loglayip bir sonraki cycle'a gecer; kalici
            # sorun /health uzerinden zaten gorunur (watchdog + thread durumu).
            try:
                due_count = len(state.due_devices(now_monotonic))
                published = run_poll_cycle(
                    gateway_code=identity.gateway_code,
                    state=state,
                    reader=reader,
                    publisher=publisher,
                    now_monotonic=now_monotonic,
                    max_parallel=cfg.max_parallel_devices,
                    device_timeout_sec=cfg.device_poll_timeout_sec,
                    cycle_timeout_sec=cfg.cycle_timeout_sec,
                    stop_event=stop_event,
                    metrics=metrics,
                )
                if due_count or published:
                    metrics.record_cycle(devices=due_count, published=published)
                if published:
                    logger.info(
                        "poll_cycle gateway=%s published=%s devices=%s version=%s",
                        identity.gateway_code,
                        published,
                        due_count,
                        state.config_version(),
                    )
                cycle_failures = 0
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001
                cycle_failures += 1
                # Ilk hatayi ve sonra seyrek olanlari logla (spam onlemi).
                if cycle_failures in (1, 5, 50) or cycle_failures % 500 == 0:
                    logger.exception(
                        "poll_cycle_failed consecutive=%d — dongu devam ediyor",
                        cycle_failures,
                    )
                metrics.inc_read_error()

            # Periyodik bakim: disk olcumu, budama. Cycle'dan bagimsiz olarak
            # hata versin diye ayri try; hicbiri poll akisini durdurmamali.
            _run_periodic_maintenance(
                now=now_monotonic,
                stage=maintenance,
                disk_guard=disk_guard,
                outbox=outbox,
                ledger=command_ledger,
                cfg=cfg,
            )

            stop_event.wait(timeout=max(1, cfg.default_poll_interval_sec))
    except KeyboardInterrupt:
        logger.info("dnp3_gateway_interrupted")
    finally:
        # Graceful shutdown — kaynaklarin sirayla ve guvenli sekilde
        # kapatilmasi. Her bir adim try/except ile koruma altinda; bir
        # adimda hata olursa sonraki adim yine calisir, tum kaynak temizlenir.
        logger.info("dnp3_gateway_shutdown_starting")
        stop_event.set()
        config_wake.set()  # config thread wait()'ten erken uyansin

        # 0) Komut-poll thread + ledger kapat.
        try:
            if command_thread is not None and command_thread.is_alive():
                command_thread.join(timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.debug("command_thread_join_error", exc_info=True)
        try:
            command_ledger.close()
        except Exception:  # noqa: BLE001
            logger.debug("command_ledger_close_error", exc_info=True)

        # 1) Config refresh thread: stop_event set, thread'in mevcut wait()
        # sonlanir; join ile gerçekten bittigini bekle. Daemon=True olsa bile
        # join edilirse temiz cikis garantilenir.
        try:
            if refresh_thread.is_alive():
                refresh_thread.join(timeout=5.0)
                if refresh_thread.is_alive():
                    logger.warning(
                        "refresh_thread_join_timeout — thread 5s icinde sonlanmadi, shutdown'a devam ediliyor"
                    )
        except Exception:  # noqa: BLE001
            logger.debug("refresh_thread_join_error", exc_info=True)

        # 2) Reader: DNP3 master/channel kapanir, TCP RST gonderir
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            logger.debug("reader_close_error", exc_info=True)

        # 3) OutboxRetrier: bg thread durur, in-flight publish'in bitmesini
        # bekler (timeout 3s)
        try:
            retrier.stop(timeout_sec=3.0)
        except Exception:  # noqa: BLE001
            logger.debug("retrier_stop_error", exc_info=True)

        # 4) Broker publisher (HTTP/NATS): connection kapanir
        try:
            publisher.close()
        except Exception:  # noqa: BLE001
            logger.debug("publisher_close_error", exc_info=True)

        # 5) Health HTTP server: yeni baglantilari reddeder ve in-flight
        # request'lerin bitmesini bekler
        try:
            health.shutdown()
            health.server_close()
        except Exception:  # noqa: BLE001
            logger.debug("health_server_shutdown_error", exc_info=True)

        # 6) Final durum raporu
        try:
            pending = outbox.pending_count()
            dl_count = outbox.dead_letter_count()
            if pending:
                logger.warning(
                    "shutdown_pending_outbox count=%s db=%s — sonraki baslangicta gonderilecek",
                    pending,
                    outbox_path,
                )
            if dl_count:
                logger.warning(
                    "shutdown_dead_letter count=%s — operator inceleyebilir",
                    dl_count,
                )
        except Exception:  # noqa: BLE001
            logger.debug("outbox_count_error", exc_info=True)
        logger.info("dnp3_gateway_stopped")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
