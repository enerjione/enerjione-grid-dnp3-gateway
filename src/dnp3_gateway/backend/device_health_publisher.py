"""Cihaz basina calisma-zamani sagligi — GIDEN yayin (G-DEVICE-HEALTH-01).

BU KANAL NE DEGILDIR
--------------------
* Komut/olay DENETIM GECMISI DEGILDIR. Ara durum gecisleri saklanmaz;
  cihazin EN SON durumu tutulur ve birlestirilir (coalescing).
* `X-E1-Gateway-Health` basliginin YERINE GECMEZ. O toplu baslik oldugu gibi
  kalir, KUCUK kalir ve buyutulmez (bkz. `device_health_wire` modul basligi).
* Telemetri yolu DEGILDIR. Basarisiz bir saglik gonderimi ne DNP3 okumasini
  ne komutlari ne de telemetri yayinini ETKILER.

IZOLASYON — EN ONEMLI KURAL
---------------------------
Bu yayinci KENDI arka plan thread'inde kosar. `publish`/`mark_dirty`
cagrilari poll thread'inden yapilir ve **ASLA BLOKLAMAZ**: yalnizca bellekteki
son durumu gunceller. Ag isi thread'in kendisindedir.

Bir gateway'in birincil isi cihaz okumak ve komut uygulamaktir; operasyonel
teshis verisi o isi HICBIR kosulda geciktiremez.

BACKPRESSURE — SINIRLI, BIRLESTIREN
-----------------------------------
Backend erisilemezken:
  * bellekte cihaz sayisi kadar kayit tutulur (cihaz basina EN SON durum) —
    gecis basina DEGIL. 200 cihaz = en fazla 200 kayit, kesinti ne kadar
    surerse sursun.
  * diske hicbir sey yazilmaz.
  * eski ara gecisler DUSURULUR; en son durum daha degerlidir.

Bu bilincli: "cihaz 09:00'da online, 09:01'de idle, 09:02'de late oldu"
gecmisi bu kanalin isi degildir. Backend'in ihtiyaci "cihaz SU AN ne
durumda"dir ve kesinti bittiginde tek bir uzlastirma partisi bunu saglar.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dnp3_gateway.backend.device_health_wire import (
    build_device_record,
    build_envelope,
    semantic_signature,
)

logger = logging.getLogger(__name__)

#: Tek HTTP govdesinde gidecek AZAMI cihaz sayisi. 200+ cihazli bir filoda
#: tek dev govde hem backend'i hem proxy'leri zorlar; parti sayisi artsa da
#: her istek ongorulebilir kalir.
DEFAULT_BATCH_MAX = 50

#: Degisiklik yayini icin en kisa bekleme. Durum degisiklikleri ANINDA
#: gitmeli ama ayni saniyede 200 cihaz birden degisirse (config yenilemesi,
#: toplu uyanma) tek partide toplanmalari daha iyidir.
DEFAULT_CHANGE_DEBOUNCE_SEC = 2.0

#: Periyodik TAM anlik goruntu (uzlastirma). Delta'lar kaybolsa bile backend
#: en gec bu surede gercekle hizalanir.
DEFAULT_SNAPSHOT_INTERVAL_SEC = 300.0

#: Yeniden deneme geri cekilmesi (ustel, SINIRLI).
BACKOFF_MIN_SEC = 2.0
BACKOFF_MAX_SEC = 120.0


class DeviceHealthPublisher:
    """Cihaz sagligini backend'e GIDEN yonde, sinirli ve birlestirerek yayinlar.

    Yasam dongusu:
        publisher = DeviceHealthPublisher(...)
        publisher.start()
        ...
        publisher.mark_dirty()      # poll thread'inden; BLOKLAMAZ
        ...
        publisher.stop()
    """

    def __init__(
        self,
        *,
        health_source: Callable[[], dict[str, dict[str, Any]]],
        send: Callable[[dict[str, Any]], None],
        gateway_code: str,
        gateway_instance_id: str,
        boot_id: int,
        batch_max: int = DEFAULT_BATCH_MAX,
        change_debounce_sec: float = DEFAULT_CHANGE_DEBOUNCE_SEC,
        snapshot_interval_sec: float = DEFAULT_SNAPSHOT_INTERVAL_SEC,
        enabled: bool = True,
    ) -> None:
        self._health_source = health_source
        self._send = send
        self._gateway_code = gateway_code
        self._instance_id = gateway_instance_id
        self._boot_id = int(boot_id)
        self._batch_max = max(1, int(batch_max))
        self._debounce = max(0.0, float(change_debounce_sec))
        self._snapshot_interval = max(10.0, float(snapshot_interval_sec))
        self._enabled = bool(enabled)

        self._lock = threading.Lock()
        #: Cihaz basina EN SON gonderilmis SEMANTIK IMZA. Delta hesabi
        #: yalnizca buna bakar.
        #:
        #: TAM KAYIT DEGIL: 1.15.0'da tam kayit saklaniyor ve esitlik
        #: karsilastiriliyordu; `report_overdue_sec` ve `last_frame_epoch`
        #: gibi her poll'da degisen alanlar yuzunden yayinci neredeyse HER
        #: debounce penceresinde POST uretiyordu (sahada ~2 saniyede bir).
        #: bkz. device_health_wire.SEMANTIC_FIELDS.
        self._gonderilen: dict[str, tuple] = {}
        self._sequence = 0
        #: Kacinci TAM snapshot. `snapshot_id` bundan turetilir ve HER YENI
        #: snapshot turunda artar — kismi basarisizliktan sonraki yeniden
        #: deneme YENI bir snapshot'tir (veri yeniden okunur), dolayisiyla
        #: yarim kalan eskisiyle KARISMAMALIDIR.
        self._snapshot_sayaci = 0
        self._uyandir = threading.Event()
        self._dur = threading.Event()
        self._thread: threading.Thread | None = None

        #: Ilk parti HER ZAMAN tam anlik goruntudur.
        self._snapshot_gerekli = True
        self._son_snapshot_at = 0.0

        # Gozlemlenebilirlik (health/metrics).
        self.sent_batches = 0
        self.sent_devices = 0
        self.failed_attempts = 0
        self.dropped_batches = 0
        self.last_success_epoch: float | None = None
        self.last_error: str = ""

    # ---- Poll thread'inden cagrilan yuzey (ASLA BLOKLAMAZ) --------------

    def mark_dirty(self) -> None:
        """Durum degismis olabilir; yayinciyi uyandir. ANINDA doner.

        Ag isi YAPILMAZ, kilit BEKLENMEZ (`Event.set` lock'suzdur). Poll
        dongusu her cycle'da cagirabilir.
        """
        if self._enabled:
            self._uyandir.set()

    def request_snapshot(self) -> None:
        """Bir sonraki turda TAM anlik goruntu gonder (config degisimi vb.)."""
        with self._lock:
            self._snapshot_gerekli = True
        self.mark_dirty()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "boot_id": self._boot_id,
                "sequence": self._sequence,
                "snapshot_count": self._snapshot_sayaci,
                "tracked_devices": len(self._gonderilen),
                "sent_batches": self.sent_batches,
                "sent_devices": self.sent_devices,
                "failed_attempts": self.failed_attempts,
                "dropped_batches": self.dropped_batches,
                "last_success_epoch": self.last_success_epoch,
                "last_error": self.last_error[:200],
            }

    # ---- Yasam dongusu --------------------------------------------------

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._calis, name="device-health", daemon=True)
        self._thread.start()
        logger.info(
            "device_health_publisher_started boot_id=%d batch_max=%d snapshot_sec=%.0f",
            self._boot_id,
            self._batch_max,
            self._snapshot_interval,
        )

    def stop(self, *, timeout: float = 5.0) -> bool:
        """Thread'i durdur. Donen: temiz cikti mi.

        Ucusta bir HTTP istegi varsa onun bitmesi BEKLENIR (istek zaten
        timeout'lu). Bekleyen is AKITILMAZ — saglik verisi best-effort'tur
        ve kapanisi uzatmasi kabul edilemez.
        """
        self._dur.set()
        self._uyandir.set()
        t = self._thread
        if t is None:
            return True
        t.join(timeout=timeout)
        if t.is_alive():
            logger.warning("device_health_publisher_stop_timeout — thread %.1fs icinde cikmadi", timeout)
            return False
        return True

    # ---- Arka plan dongusu ----------------------------------------------

    def _calis(self) -> None:
        backoff = BACKOFF_MIN_SEC
        while not self._dur.is_set():
            # Uyanma: degisiklik tetigi VEYA snapshot zamani.
            bekle = self._sonraki_bekleme()
            self._uyandir.wait(timeout=bekle)
            self._uyandir.clear()
            if self._dur.is_set():
                return

            try:
                gonderildi = self._tur()
            except Exception:  # noqa: BLE001
                # IZOLE: saglik yayini HICBIR kosulda prosesi dusurmez.
                logger.debug("device_health_cycle_error", exc_info=True)
                gonderildi = False

            if gonderildi is None:  # gonderilecek bir sey yoktu
                backoff = BACKOFF_MIN_SEC
                continue
            if gonderildi:
                backoff = BACKOFF_MIN_SEC
                continue

            # BASARISIZ: SINIRLI ustel geri cekilme + jitter. Jitter sart:
            # 50 gateway ayni backend'e baglidir ve senkron yeniden denemeler
            # backend geri geldigi anda ikinci bir yuk dalgasi uretir.
            uyku = min(BACKOFF_MAX_SEC, backoff) * (0.8 + 0.4 * random.random())  # noqa: S311
            if self._dur.wait(timeout=uyku):
                return
            backoff = min(BACKOFF_MAX_SEC, backoff * 2)

    def _sonraki_bekleme(self) -> float:
        """Snapshot zamanina kalan sure (ust sinir: debounce penceresi)."""
        with self._lock:
            son = self._son_snapshot_at
        kalan = self._snapshot_interval - (time.monotonic() - son) if son else 0.0
        return max(0.5, min(kalan if kalan > 0 else 0.5, 30.0))

    def _tur(self) -> bool | None:
        """Tek yayin turu. Donen: True=basarili, False=basarisiz, None=is yok."""
        if self._debounce:
            # Ayni anda degisen cihazlari TEK partide topla.
            if self._dur.wait(timeout=self._debounce):
                return None

        try:
            ham = self._health_source() or {}
        except Exception:  # noqa: BLE001
            logger.debug("device_health_source_error", exc_info=True)
            return None

        with self._lock:
            snapshot = self._snapshot_gerekli or not self._son_snapshot_at
            if not snapshot and self._son_snapshot_at:
                snapshot = (time.monotonic() - self._son_snapshot_at) >= self._snapshot_interval

        kayitlar: list[dict[str, Any]] = []
        guncel: dict[str, tuple] = {}
        for kod, saglik in ham.items():
            try:
                kayit = build_device_record(kod, saglik)
            except Exception:  # noqa: BLE001
                logger.debug("device_health_record_error device=%s", kod, exc_info=True)
                continue
            imza = semantic_signature(kayit)
            guncel[kod] = imza
            # DELTA YALNIZCA SEMANTIK IMZAYA BAKAR. Gonderilen parti yine
            # TAM kayittir (gozlem alanlari GUNCEL degerleriyle gider);
            # sadece TETIKLEME karari sadelestirildi.
            if snapshot or self._gonderilen.get(kod) != imza:
                kayitlar.append(kayit)

        # Silinen cihazlar: artik config'te yoklar. Snapshot zaten tam durumu
        # tasidigi icin ayrica "silindi" mesaji GONDERILMEZ; backend snapshot
        # ile uzlasir.
        if not kayitlar:
            with self._lock:
                self._gonderilen = guncel
            return None

        toplam = len(guncel)
        parti_sayisi = (len(kayitlar) + self._batch_max - 1) // self._batch_max

        # SNAPSHOT KIMLIGI HER TURDA YENI URETILIR.
        #
        # Kismi bir basarisizliktan sonraki yeniden deneme YENI bir
        # snapshot'tir: veri `health_source()`tan YENIDEN okunur ve bu arada
        # cihaz seti degismis olabilir. Ayni kimligi surdurmek, backend'in
        # yarim kalan ESKI partilerle yeni partileri BIRLESTIRMESINE yol
        # acardi — tutarsiz bir tablo, ya da "eksik kalanlari sil" mantigi
        # varsa var olan cihazlarin SILINMESI.
        #
        # SAATTEN BAGIMSIZ: kimlik `boot_id` + artan sayacdir.
        snapshot_id: str | None = None
        if snapshot:
            with self._lock:
                self._snapshot_sayaci += 1
                snapshot_id = f"{self._boot_id}-{self._snapshot_sayaci}"

        basarili = True
        for i in range(0, len(kayitlar), self._batch_max):
            parca = kayitlar[i : i + self._batch_max]
            parti_index = i // self._batch_max
            with self._lock:
                self._sequence += 1
                sira = self._sequence
            govde = build_envelope(
                gateway_code=self._gateway_code,
                gateway_instance_id=self._instance_id,
                boot_id=self._boot_id,
                sequence=sira,
                # Bir snapshot birden fazla partiye bolunebilir; HEPSI
                # `snapshot=true` ve AYNI `snapshot_id`yi tasir.
                snapshot=snapshot,
                devices=parca,
                device_total=toplam,
                snapshot_id=snapshot_id,
                snapshot_batch_index=parti_index if snapshot else None,
                snapshot_batch_count=parti_sayisi if snapshot else None,
            )
            try:
                self._send(govde)
            except Exception as exc:  # noqa: BLE001
                basarili = False
                with self._lock:
                    self.failed_attempts += 1
                    self.dropped_batches += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "device_health_send_failed batch=%d/%d snapshot_id=%s devices=%d error=%s — "
                    "yarim kalan snapshot backend tarafinda ATILMALI; yeniden deneme YENI "
                    "snapshot_id ile gelir",
                    parti_index + 1,
                    parti_sayisi,
                    snapshot_id or "-",
                    len(parca),
                    self.last_error[:160],
                )
                break
            with self._lock:
                self.sent_batches += 1
                self.sent_devices += len(parca)
                self.last_success_epoch = time.time()
                # YALNIZCA GONDERILEN kayitlar "gonderildi" sayilir; parti
                # yarida kalirsa kalanlar bir sonraki turda YENIDEN denenir.
                for k in parca:
                    self._gonderilen[k["device_code"]] = semantic_signature(k)

        if basarili:
            with self._lock:
                self._gonderilen = guncel
                if snapshot:
                    self._snapshot_gerekli = False
                    self._son_snapshot_at = time.monotonic()
            logger.info(
                "device_health_published devices=%d batches=%d snapshot=%s snapshot_id=%s seq_last=%d",
                len(kayitlar),
                parti_sayisi,
                snapshot,
                snapshot_id or "-",
                self._sequence,
            )
        return basarili


# ---------------------------------------------------------------------------
# Boot sayaci — SAATTEN BAGIMSIZ siralama capasi
# ---------------------------------------------------------------------------

BOOT_ID_FILE = "device_health_boot.id"


def next_boot_id(state_dir: str | Path, *, gateway_code: str) -> int:
    """Her proses baslangicinda ARTAN, diskte tutulan sayac.

    NEDEN `gateway_instance_id` YETMEZ: o kimlik disk uzerinde KALICIDIR ve
    restart'ta AYNI kalir. Restart sonrasi `sequence` sifirlandiginda backend
    iki farkli calismayi ayirt edemez ve BAYAT bir yeniden gonderim daha yeni
    durumu ezebilir.

    NEDEN DUVAR SAATI DEGIL: sahada RTC'si bos acilan gateway'ler ve NTP
    siçramalari gercektir; saate bagli siralama tam da o anlarda tersine
    doner. Bu sayac saatten BAGIMSIZDIR.

    Dosya okunamaz/yazilamazsa `1` doner ve WARNING loglanir: siralama
    zayiflar ama yayin DURMAZ (best-effort kanal).
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (gateway_code or "gw"))[:50] or "gw"
    yol = Path(state_dir) / f"{safe}_{BOOT_ID_FILE}"
    mevcut = 0
    try:
        yol.parent.mkdir(parents=True, exist_ok=True)
        if yol.is_file():
            mevcut = int(yol.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        logger.debug("device_health_boot_id_read_error path=%s", yol, exc_info=True)
        mevcut = 0
    yeni = mevcut + 1
    try:
        gecici = yol.with_suffix(".tmp")
        gecici.write_text(f"{yeni}\n", encoding="utf-8")
        gecici.replace(yol)  # atomik
    except OSError:
        logger.warning(
            "device_health_boot_id_write_failed path=%s — siralama capasi kalici DEGIL; "
            "backend restart sonrasi bayat gonderimi ayirt edemeyebilir",
            yol,
        )
        return yeni if yeni > 1 else 1
    return yeni
