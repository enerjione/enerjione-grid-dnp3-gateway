"""Telemetri tasima yolu secici: NATS birincil, HTTP yedek.

NEDEN BU MODUL VAR
------------------
Gateway iki farkli kurulum senaryosunda calisir ve bu ikisinin DOGRU
davranisi FARKLIDIR:

1. YEREL KURULUM ("bu cihaza kur" — backend ile ayni makine)
   NATS ZORUNLU. Ayni makinede NATS'a erisilememesi bir YAPILANDIRMA
   HATASIDIR. Sessizce HTTP'ye dusmek bu hatayi GIZLER: sistem "calisiyor"
   gorunur, panelde veri akar, ama her olcum backend HTTP + Postgres
   outbox zincirinden gecer ve 500 cihaz hedefi tutturulamaz. Ariza ancak
   yuk testinde, haftalar sonra fark edilir. Bu yuzden yerel modda yedek
   yol YOKTUR: NATS erisilemezse mesajlar outbox'ta birikir (kayip yok)
   ve /health acikca `telemetry_backend_unreachable` der.

2. UZAK KURULUM ("baska cihaza kur" — sahadaki ayri sunucu)
   Once NATS denenir; erisilemezse HTTP ile veri gitmeye DEVAM eder.
   Burada fallback BEKLENEN davranistir: saha kurulumunda 4222 portu
   kapali/NAT arkasinda olabilir, operatorun elinde yalnizca backend'in
   HTTPS ucu vardir. Veri akmaya devam etmeli. NATS sonradan acilirsa
   gateway GERI DONER — HTTP'de kalici olarak takili kalmaz.

TASARIM
-------
Bu sinif broker ARAYUZUNU uygular (`publish`, `publish_batch`, `close`,
`is_ready`, `counters_snapshot`) ve `ResilientPublisher`in altina, iki
gercek publisher'in ustune girer:

    poller -> ResilientPublisher -> TelemetryTransportRouter -> JetStream
                     |                        `-------------> HTTP ingest
                     v
                  Outbox (her ikisi de basarisizsa)

Bu konumlandirma iki seyi ayni anda saglar:
  * FALLBACK'TE KAYIP YOK — NATS'a yazilamayan mesaj AYNI CAGRIDA HTTP'ye
    verilir. Router istisna firlatmadigi surece ResilientPublisher mesaji
    "teslim edildi" sayar; firlatirsa outbox'a yazar. Yani ucuncu bir
    "arada kalmis" durum yok.
  * OUTBOX DRENAJI DA YEDEK YOLU KULLANIR — retrier `publish_outbox_row(s)`
    ile ayni router'a gelir. NATS uzun sure kapali kalsa bile birikmis
    tampon HTTP uzerinden bosalir; "tampon bosa akmaz".

DURUM DEGISIMI = OLAY, DENEME = SESSIZ
--------------------------------------
Her basarisiz NATS denemesi log'a yazilsaydi 500 cihazli sahada saniyede
binlerce satir uretirdi ve gercek olay kaybolurdu. Bu yuzden yalnizca
AKTIF YOL DEGISTIGINDE tek bir WARNING/INFO satiri atilir
(`telemetry_transport_switched`). Aradaki denemeler sessizdir; sayilari
`/health` govdesinde gorulur.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

MODE_LOCAL = "local"
MODE_REMOTE = "remote"

TRANSPORT_NATS = "nats"
TRANSPORT_HTTP = "http"

# Kac ARDISIK NATS hatasindan sonra aktif yol HTTP'ye cevrilir.
# 1 yapmak tek bir publish timeout'unda yol degistirir (gereksiz salinim);
# cok buyuk yapmak her mesajda NATS timeout'u odemek demektir (yavaslik).
_DEFAULT_FAIL_THRESHOLD = 3

# HTTP'ye dustukten sonra NATS'a donmek icin ne siklikta yoklanir.
_DEFAULT_PROBE_INTERVAL_SEC = 30.0

# Bir yola gectikten sonra en az bu kadar sure orada kalinir. NATS "bir acilip
# bir kapanan" durumdayken (ag flap) yol degistirme dongusune girmeyi onler.
_DEFAULT_MIN_DWELL_SEC = 10.0


class TelemetryTransportRouter:
    """Iki publisher arasinda aktif yolu yoneten broker cephesi.

    `http_broker=None` verilirse yedek yol KAPALIDIR (yerel kurulum):
    NATS hatasi oldugu gibi yukari yayilir ve mesaj outbox'a yazilir.
    """

    def __init__(
        self,
        *,
        nats_broker: Any,
        http_broker: Any | None,
        install_mode: str,
        fail_threshold: int = _DEFAULT_FAIL_THRESHOLD,
        probe_interval_sec: float = _DEFAULT_PROBE_INTERVAL_SEC,
        min_dwell_sec: float = _DEFAULT_MIN_DWELL_SEC,
    ) -> None:
        if nats_broker is None:
            raise ValueError("TelemetryTransportRouter icin nats_broker zorunludur")
        self._nats = nats_broker
        self._http = http_broker
        self.install_mode = install_mode
        self._fail_threshold = max(1, int(fail_threshold))
        self._probe_interval = float(probe_interval_sec)
        self._min_dwell = float(min_dwell_sec)

        self._lock = threading.Lock()
        self._active = TRANSPORT_NATS
        self._active_since = time.time()
        self._active_since_mono = time.monotonic()
        self._nats_consecutive_failures = 0
        self._switch_count = 0
        self._last_switch_reason: str | None = None
        self._next_probe_at_mono = 0.0
        # Yedek yolla teslim edilen mesaj sayisi — "fallback calisti mi"
        # sorusunun tek sayisal cevabi.
        self._fallback_delivered = 0

    # ---- Broker arayuzu --------------------------------------------------
    @property
    def fallback_enabled(self) -> bool:
        return self._http is not None

    @property
    def active_transport(self) -> str:
        return self._active

    @property
    def is_ready(self) -> bool:
        """Telemetri SU AN herhangi bir yoldan teslim edilebiliyor mu?

        Yerel modda tek yol NATS oldugu icin cevap dogrudan NATS'in
        durumudur — NATS dustugunde /health bunu `telemetry_backend_unreachable`
        olarak gostermeli, "sorun yok" dememeli.
        """
        nats_ready = bool(getattr(self._nats, "is_ready", False))
        if not self.fallback_enabled:
            return nats_ready
        http_ready = bool(getattr(self._http, "is_ready", False))
        return nats_ready or http_ready

    def publish(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self._probe_nats_if_due()
        if self._active == TRANSPORT_NATS:
            try:
                self._nats.publish(
                    payload,
                    message_id=message_id,
                    correlation_id=correlation_id,
                    headers=headers,
                )
                self._note_nats_success()
                return
            except Exception as exc:  # noqa: BLE001
                if not self.fallback_enabled:
                    # Yerel kurulum: yedek yol YOK. Hata yukari cikar,
                    # ResilientPublisher mesaji outbox'a yazar.
                    self._note_nats_failure(exc)
                    raise
                self._note_nats_failure(exc)
                # Mesaji KAYBETME: ayni cagrida HTTP'ye ver.
                self._publish_via_http(
                    payload,
                    message_id=message_id,
                    correlation_id=correlation_id,
                    headers=headers,
                )
                return
        # Aktif yol HTTP
        self._publish_via_http(
            payload,
            message_id=message_id,
            correlation_id=correlation_id,
            headers=headers,
        )

    def publish_batch(self, items: list[dict[str, Any]]) -> None:
        """Toplu yayin — tekli publish ile ayni yol secimi mantigi.

        TEKRAR (duplicate) NOTU: NATS batch'i KISMEN basarisiz olursa
        (bir kismi gitti, bir kismi gitmedi) tum batch HTTP'ye aktarilir ve
        giden mesajlar iki kez islenebilir. Bilincli tercih: at-least-once
        garantisini korumak, kayip vermemek. Baskin senaryoda (NATS tamamen
        erisilemez) `JetStreamNotReadyError` HICBIR mesaj gonderilmeden
        firlatilir, dolayisiyla tekrar da olusmaz.
        """
        if not items:
            return
        self._probe_nats_if_due()
        if self._active == TRANSPORT_NATS:
            try:
                self._nats.publish_batch(items)
                self._note_nats_success()
                return
            except Exception as exc:  # noqa: BLE001
                if not self.fallback_enabled:
                    self._note_nats_failure(exc)
                    raise
                self._note_nats_failure(exc)
                self._publish_batch_via_http(items)
                return
        self._publish_batch_via_http(items)

    def close(self) -> None:
        try:
            self._nats.close()
        except Exception:  # noqa: BLE001
            logger.debug("transport_router_nats_close_error", exc_info=True)
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                logger.debug("transport_router_http_close_error", exc_info=True)

    def counters_snapshot(self) -> dict[str, int]:
        """Iki yolun sayaclarini birlestirir (health endpoint bunu okur)."""
        toplam = {"publish_failures": 0, "publish_successes": 0}
        for broker in (self._nats, self._http):
            if broker is None:
                continue
            fn = getattr(broker, "counters_snapshot", None)
            if not callable(fn):
                continue
            try:
                snap = fn()
            except Exception:  # noqa: BLE001
                continue
            toplam["publish_failures"] += int(snap.get("publish_failures", 0))
            toplam["publish_successes"] += int(snap.get("publish_successes", 0))
        return toplam

    # ---- Gozlemlenebilirlik ----------------------------------------------
    def transport_status(self) -> dict[str, Any]:
        """/health govdesi icin aktif yol durumu.

        Sahada en pahali sorulardan biri "su an veri hangi yoldan gidiyor?"
        idi; log'a bakmadan cevaplanamiyordu. Artik tek bir GET yeter.
        """
        with self._lock:
            active = self._active
            since = self._active_since
            switches = self._switch_count
            reason = self._last_switch_reason
            fallback_delivered = self._fallback_delivered
            consecutive = self._nats_consecutive_failures
        return {
            "install_mode": self.install_mode,
            "active_transport": active,
            "fallback_enabled": self.fallback_enabled,
            "active_since_epoch": since,
            "transport_switches": switches,
            "last_switch_reason": reason,
            "fallback_delivered_total": fallback_delivered,
            "nats_consecutive_failures": consecutive,
            "nats_ready": bool(getattr(self._nats, "is_ready", False)),
            "http_ready": (bool(getattr(self._http, "is_ready", False)) if self._http is not None else None),
        }

    # ---- Ic mantik --------------------------------------------------------
    def _publish_via_http(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None,
        headers: dict[str, Any] | None,
    ) -> None:
        if self._http is None:  # pragma: no cover — cagrilmadan once kontrol var
            raise RuntimeError("http yedek yol kapali")
        self._http.publish(
            payload,
            message_id=message_id,
            correlation_id=correlation_id,
            headers=headers,
        )
        with self._lock:
            if self._active == TRANSPORT_HTTP or self._nats_consecutive_failures:
                self._fallback_delivered += 1

    def _publish_batch_via_http(self, items: list[dict[str, Any]]) -> None:
        if self._http is None:  # pragma: no cover
            raise RuntimeError("http yedek yol kapali")
        self._http.publish_batch(items)
        with self._lock:
            if self._active == TRANSPORT_HTTP or self._nats_consecutive_failures:
                self._fallback_delivered += len(items)

    def _note_nats_success(self) -> None:
        with self._lock:
            self._nats_consecutive_failures = 0

    def _note_nats_failure(self, exc: BaseException) -> None:
        """Ardisik hatalari say; esik asilirsa aktif yolu HTTP'ye cevir.

        Tek tek hatalar loglanmaz (500 cihazda saniyede binlerce satir).
        Yalnizca yol degisimi olay uretir.
        """
        with self._lock:
            self._nats_consecutive_failures += 1
            sayi = self._nats_consecutive_failures
            if not self.fallback_enabled or self._active != TRANSPORT_NATS:
                return
            if sayi < self._fail_threshold:
                return
            if not self._dwell_doldu_locked():
                return
            self._switch_locked(TRANSPORT_HTTP, reason=f"nats_failed_x{sayi}: {exc}")

    def _probe_nats_if_due(self) -> None:
        """HTTP'deyken NATS'a donmeyi dene.

        Yoklama GERCEK bir publish degil, NATS istemcisinin baglanti
        durumudur (`is_ready`). JetStreamPublisher zaten arka planda
        surekli yeniden baglanmayi deniyor; burada sadece sonucu okuyoruz.
        Boylece yoklama maliyeti sifira yakin ve telemetri akisini
        etkilemiyor.
        """
        if self._active != TRANSPORT_HTTP or not self.fallback_enabled:
            return
        simdi = time.monotonic()
        with self._lock:
            if self._active != TRANSPORT_HTTP:
                return
            if simdi < self._next_probe_at_mono:
                return
            self._next_probe_at_mono = simdi + self._probe_interval
            if not self._dwell_doldu_locked():
                return
            if not bool(getattr(self._nats, "is_ready", False)):
                return
            self._nats_consecutive_failures = 0
            self._switch_locked(TRANSPORT_NATS, reason="nats_reachable_again")

    def _dwell_doldu_locked(self) -> bool:
        return (time.monotonic() - self._active_since_mono) >= self._min_dwell

    def _switch_locked(self, hedef: str, *, reason: str) -> None:
        """Aktif yolu degistir ve TEK bir olay logu at. Lock ALTINDA cagrilir."""
        onceki = self._active
        if onceki == hedef:
            return
        self._active = hedef
        self._active_since = time.time()
        self._active_since_mono = time.monotonic()
        self._switch_count += 1
        self._last_switch_reason = reason[:300]
        if hedef == TRANSPORT_HTTP:
            # Yedege yeni dustuk: ilk geri-donus yoklamasi TAM bir aralik
            # sonra yapilsin. Aksi halde bir sonraki mesaj hemen yoklar ve
            # NATS "bagli ama publish reddediyor" durumundaysa (stream dolu,
            # yetki hatasi) her mesajda bir NATS timeout'u odenirdi.
            self._next_probe_at_mono = time.monotonic() + self._probe_interval
            logger.warning(
                "telemetry_transport_switched from=%s to=%s mode=%s sebep=%s — "
                "telemetri artik backend HTTP ingest uzerinden gidiyor. Veri "
                "akisi SURUYOR ancak NATS yoluna gore yavastir; NATS erisimi "
                "geri gelince otomatik donulecek (yoklama %.0fs).",
                onceki,
                hedef,
                self.install_mode,
                reason,
                self._probe_interval,
            )
        else:
            logger.info(
                "telemetry_transport_switched from=%s to=%s mode=%s sebep=%s — "
                "NATS yeniden erisilebilir, birincil yola donuldu.",
                onceki,
                hedef,
                self.install_mode,
                reason,
            )
