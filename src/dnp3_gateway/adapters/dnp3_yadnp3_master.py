"""yadnp3 (OpenDNP3) tabanli DNP3 master adapter.

Mimari farki (dnp3_master.py / nfm-dnp3 ile karsilastir):
  - **Tam DNP3 standart implementasyonu** (OpenDNP3 reference). Outstation
    yadnp3 ile yazildigi icin protokol uyumu %100; nfm-dnp3'teki
    'Transport segment data too short' / TCP RST sorunlarinin kayna iki
    farkli kutuphane idi, bu adapter o sorunu cozer.
  - **Event-driven mimari** built-in: master.AddClassScan ile periyodik
    Class 0/1/2/3 scanlari arka planda calisir, her gelen olcum ISOEHandler
    callback'i ile cache'e yazilir. read_device sadece cache snapshot'i alir.
  - **Octet String (Group 110) destegi** vardir: bytes -> UTF-8 metin.

Tasarim:
  - Tek bir DNP3Manager (process basina), N device icin N master.
  - Her cihaz icin ayri TCPClient channel + master + ISOEHandler.
  - Cache: (object_group, index) -> son raw deger / metin. Thread-safe.
  - read_device cache'den okur; deger yoksa 'no_change' (frontend son iyi
    degeri korur — kullanici istegi).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from dnp3_gateway.adapters.base import SignalReading, TelemetryReader
from dnp3_gateway.backend import DeviceConfig, SignalConfig

logger = logging.getLogger(__name__)


try:  # pragma: no cover - opsiyonel bagimlilik
    import opendnp3  # yadnp3 wheel saglar

    _YADNP3_AVAILABLE = True
    _YADNP3_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001
    opendnp3 = None  # type: ignore[assignment]
    _YADNP3_AVAILABLE = False
    _YADNP3_IMPORT_ERROR = _exc


_OBJECT_GROUP_BINARY_INPUT = 1
_OBJECT_GROUP_BINARY_OUTPUT = 10
_OBJECT_GROUP_COUNTER = 20
_OBJECT_GROUP_ANALOG_INPUT = 30
_OBJECT_GROUP_ANALOG_OUTPUT = 40
_OBJECT_GROUP_STRING = 110

# Recovery doğrulama suresi: OnOpen / stale-edge sonrasi cihazdan fresh frame
# beklemek icin maksimum sure. Bu surede frame gelmezse cihaz tekrar "lost"
# kabul edilir ve SCADA "comm_lost" gormeye devam eder. 15 sn:
#   - Class 0+1+2+3 integrity poll cevabi normal kosullarda <2 sn doner.
#   - 4G/yavas linkler icin ust sinir 5-8 sn.
#   - 15 sn TCP-up + DNP3-down sahte recovery'yi yakalamak icin yeterince agresif,
#     ancak gercek yavas linkleri yanlislikla "lost" yapmaktan korur.
_RECOVERY_GRACE_SEC = 15.0


class Yadnp3AdapterError(RuntimeError):
    """yadnp3 adapter'inda olusan hata."""


class _DeviceCache:
    """Cihaz basina son-okunan degerler. ISOEHandler yazar, read_device okur.

    Event-driven semantik:
      * `set()`: yeni okuma geldi. Onceki degerle aynı ise dirty isaretlenmez
        (Class 0 baseline scan ayni degeri tekrar yazsa bile cycle'da "publish
        edilecek degisiklik" sayilmaz). Farkli ise dirty=True ve sonraki
        `read_and_clear_dirty()` cagrisinda 'good' kaliteyle dondurulur.
      * `is_dirty()` / read_device: sadece degismis sinyaller publish edilir.
        Diger sinyaller 'no_change' donerek bant genisligini koruriz.

    Onceden cache her okumada son snapshot'i 'good' kaliteyle veriyordu;
    bu, 7 cihaz x 175 sinyal = 1225 mesaji her cycle'da yayinlamasina yol
    aciyordu (gercek event-driven degil, snapshot-driven). Dirty flag ile
    yalnizca degisen sinyaller mesaja donusur.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (group, index) -> (raw_float, value_string_or_None)
        self._values: dict[tuple[int, int], tuple[float, str | None]] = {}
        # Son read_device'tan beri degismis sinyaller. read_device basarili
        # publish sonrasi clear eder.
        self._dirty: set[tuple[int, int]] = set()
        self._connected = False
        self._last_update_at: float = 0.0
        # Cihaz stale/disconnected oldugunda comm_lost yayinini SADECE bir kez
        # yapmak icin edge-trigger flag'i. read_device set/clear eder.
        self._stale_announced: bool = False
        # Recovery state machine — uc durum:
        #   "online"     : cihaz saglikli, frame'ler taze geliyor.
        #   "lost"       : link kopuk veya stale; SCADA tarafi comm_lost gorur.
        #   "recovering" : link az once geri geldi (OnOpen) ya da stale'den
        #                  cikis tetiklendi; cihazdan henuz fresh frame gelmedi.
        #                  read_device hala comm_lost yayinlar (SCADA'yi
        #                  yaniltmamak icin), TA Kİ:
        #                    a) yeni bir frame gelene kadar (recovery confirmed),
        #                    b) RECOVERY_GRACE_SEC dolana kadar (timeout -> lost).
        # Bu sayede TCP up + DNP3 dead durumunda sahte "online" yayini olmaz.
        self._state: str = "lost"
        # OnOpen / stale-edge anindan itibaren grace period sayaci icin baslangic.
        self._recovery_started_at: float = 0.0
        # Recovery'nin "fresh frame geldi" anchor'i: bundan sonra gelen ilk
        # cache.set() recovery'yi confirm eder. set() icinde tek seferlik
        # tetiklenir; sonraki set'ler normal davranir.
        self._recovery_anchor_at: float = 0.0
        # Recovery confirmed olduktan sonra read_device'in mark_all_dirty +
        # log atmasi icin tek seferlik bayrak. read_device tuketince temizler.
        self._pending_recovery_publish: bool = False

    def set(self, group: int, index: int, raw: float, value_string: str | None = None) -> None:
        """SOE handler'in cache'e yazma giris noktasi.

        Mantik (saf event-driven):
          * Ilk yazma (prev is None): dirty=True. Bu durum SADECE startup
            integrity poll (Class 0+1+2+3) sirasinda olusur — her sinyal icin
            bir kez. Yani frontend "snapshot"i ilk bagi kuruldugunda alir.
          * Sonraki yazma, deger DEGISTIYSE: dirty=True. Bu durum Class 1/2/3
            event scan'inden gelir — outstation event buffer'inda ne biriktiyse.
          * Sonraki yazma, deger AYNIYSA: dirty=False. Class 0 baseline scan
            (drift safety, default 30sn) ayni degeri tekrar yazsa bile yayin
            tetiklenmez. Boylece RabbitMQ + tag-engine + DB sadece gercek
            degisikliklerle mesgul olur. SCADA standart davranisi.

        Onceki "her yazma dirty" mantigim Class 0 baseline 30sn'de bir 1225
        sinyali tekrar tekrar yayinliyordu — gereksiz yuk. Simdi:
          - Bagi acilis: 1225 publish (snapshot integrity poll)
          - Sonra: sadece degisen sinyaller (ortalama 0-5 / cycle)
          - Class 0 30sn'de bir cagrilir, %99 vakitte ayni deger -> 0 publish
        """
        key = (group, index)
        with self._lock:
            prev = self._values.get(key)
            self._values[key] = (raw, value_string)
            now = time.time()
            self._last_update_at = now
            if prev is None or prev[0] != raw or prev[1] != value_string:
                self._dirty.add(key)
            # Recovery confirmation: cihaz "recovering" durumundaysa ve bu
            # frame OnOpen/stale-edge anchor'indan sonra geldiyse, online'a
            # yukselt ve _pending_recovery_publish=True ile read_device'a
            # sinyal et. Eski impl. burada self._dirty.update(self._values.
            # keys()) yapiyordu (O(N)); ama I/O thread'inde (yadnp3 callback)
            # 600+ cihaz × 175 sinyal = 100K key icin lock altinda set.update
            # yapilmasi GIL'i tutarak baska callback'leri bekletir. Tum dirty
            # isaretlemesi yerine read_device 'in consume_recovery_publish
            # tetigiyle mark_all_dirty cagirmasini bekleriz (poller thread'i
            # zaten lock ister, bu sirada I/O thread'i serbest).
            if self._state == "recovering" and now >= self._recovery_anchor_at:
                self._state = "online"
                self._pending_recovery_publish = True

    def get(self, group: int, index: int) -> tuple[float, str | None] | None:
        with self._lock:
            return self._values.get((group, index))

    def is_dirty(self, group: int, index: int) -> bool:
        with self._lock:
            return (group, index) in self._dirty

    def clear_dirty(self, group: int, index: int) -> None:
        with self._lock:
            self._dirty.discard((group, index))

    def mark_all_dirty(self) -> int:
        """Cache'deki tum (group, index) ciftlerini dirty isaretler.

        Recovery anlik kullanim: cihaz iletisimi koptuktan sonra geri geldiginde
        cihazin sinyal degerleri ayni kalmis olabilir; saf event-driven mantikta
        hicbir sinyal "degisti" olmadigindan publish edilmez ve dis SCADA
        cihazi hala "comm_lost" gorur. Recovery'de bu method cagrilirsa bir
        sonraki read_device cycle'inda son bilinen tum degerler `quality=good`
        ile yayinlanir → tag-engine → outbound (IEC 104, REST, MQTT) → SCADA
        cihazi tekrar "online" gorur. Donus: dirty isaretlenen kayit sayisi.
        """
        with self._lock:
            count = len(self._values)
            self._dirty.update(self._values.keys())
            return count

    def set_connected(self, ok: bool) -> None:
        with self._lock:
            self._connected = ok
            # Link tamamen koptu → state'i lost'a dusur. recovery flag'lerini
            # temizle ki sonraki OnOpen yeniden recovering tetiklesin.
            if not ok:
                self._state = "lost"
                self._recovery_started_at = 0.0
                self._recovery_anchor_at = 0.0
                self._pending_recovery_publish = False

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def size(self) -> int:
        with self._lock:
            return len(self._values)

    def last_update_at(self) -> float:
        with self._lock:
            return self._last_update_at

    # ---- Recovery state machine ------------------------------------------------
    # Cihaz haberlesme durumu icin uc-state model:
    #   lost       : SCADA "comm_lost" gorur; gercek de oyle.
    #   recovering : link az once geldi/dirildi ama henuz fresh frame yok;
    #                SCADA hala "comm_lost" goruyor (sahte online onlemek icin).
    #   online     : fresh frame geldi, normal yayilim.
    # Read_device bu state'i okuyup karar verir.

    def begin_recovery(self) -> None:
        """OnOpen ya da stale-edge sonrasi 'recovering' moduna gec.

        Idempotent: zaten recovering ise grace period sayaci sifirlanmaz
        (tekrar OnOpen flap'i grace'i ayrica uzatmaz).
        """
        now = time.time()
        with self._lock:
            if self._state == "recovering":
                return
            self._state = "recovering"
            self._recovery_started_at = now
            self._recovery_anchor_at = now
            self._pending_recovery_publish = False

    def state(self) -> str:
        with self._lock:
            return self._state

    def recovery_age(self) -> float:
        """Recovering durumunda gecen sure (saniye). Diger durumlarda 0."""
        with self._lock:
            if self._state != "recovering" or self._recovery_started_at == 0.0:
                return 0.0
            return time.time() - self._recovery_started_at

    def fail_recovery(self) -> None:
        """Grace period dolmasina ragmen fresh frame gelmedi: tekrar lost."""
        with self._lock:
            if self._state == "recovering":
                self._state = "lost"
                self._recovery_started_at = 0.0
                self._recovery_anchor_at = 0.0
                self._pending_recovery_publish = False

    def consume_recovery_publish(self) -> bool:
        """Fresh frame ile recovery confirmed olduysa True doner; tek seferlik.

        read_device bunu cagirip True alirsa mark_all_dirty + log + recovery
        announcement yapar. Tukettikten sonra flag temizlenir, sonraki cycle'da
        normal davranis.
        """
        with self._lock:
            if self._pending_recovery_publish:
                self._pending_recovery_publish = False
                return True
            return False


def _make_soe_handler(cache: _DeviceCache, device_code: str) -> Any:
    """ISOEHandler subclass: gelen olcumleri cihaz cache'ine yazar."""
    if not _YADNP3_AVAILABLE:
        raise Yadnp3AdapterError(f"yadnp3 yuklu degil: {_YADNP3_IMPORT_ERROR}")

    class _CacheSOEHandler(opendnp3.ISOEHandler):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()

        def BeginFragment(self, info):  # noqa: N802
            pass

        def EndFragment(self, info):  # noqa: N802
            pass

        def OnDeviceAttribute(self, info, set_, variation, value):  # noqa: N802
            pass

        def Process(self, info, values):  # noqa: N802
            if not values:
                return
            first = values[0].value
            try:
                if isinstance(first, opendnp3.Binary):
                    for it in values:
                        cache.set(_OBJECT_GROUP_BINARY_INPUT, it.index, 1.0 if it.value.value else 0.0)
                elif isinstance(first, opendnp3.Analog):
                    for it in values:
                        cache.set(_OBJECT_GROUP_ANALOG_INPUT, it.index, float(it.value.value))
                elif isinstance(first, opendnp3.Counter):
                    for it in values:
                        cache.set(_OBJECT_GROUP_COUNTER, it.index, float(it.value.value))
                elif isinstance(first, opendnp3.BinaryOutputStatus):
                    for it in values:
                        cache.set(_OBJECT_GROUP_BINARY_OUTPUT, it.index, 1.0 if it.value.value else 0.0)
                elif isinstance(first, opendnp3.AnalogOutputStatus):
                    for it in values:
                        cache.set(_OBJECT_GROUP_ANALOG_OUTPUT, it.index, float(it.value.value))
                elif isinstance(first, opendnp3.OctetString):
                    for it in values:
                        try:
                            raw = bytes(it.value.ToBytes())
                        except Exception:  # noqa: BLE001
                            raw = b""
                        # Bos/bos-baslangic byte'lari NUL'lardan temizle, UTF-8 dene
                        text = raw.rstrip(b"\x00").decode("utf-8", errors="replace") or None
                        cache.set(_OBJECT_GROUP_STRING, it.index, 0.0, value_string=text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "yadnp3_soe_process_error device=%s error=%s",
                    device_code,
                    exc,
                )

    return _CacheSOEHandler()


def _make_master_app(cache: _DeviceCache, device_code: str) -> Any:
    if not _YADNP3_AVAILABLE:
        raise Yadnp3AdapterError(f"yadnp3 yuklu degil: {_YADNP3_IMPORT_ERROR}")

    class _MasterApp(opendnp3.IMasterApplication):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()

        def OnReceiveIIN(self, iin):  # noqa: N802
            pass

        def OnTaskStart(self, type, id):  # noqa: N802, A002
            pass

        def OnTaskComplete(self, info):  # noqa: N802
            pass

        def OnOpen(self):  # noqa: N802
            # Link acildi — connected flag'i set ama henuz "online" sayma.
            # Cihaz DNP3 frame'i cevapladigi anda set() recovery'yi confirm
            # edip state'i "online"a yukseltecek. read_device bu arada hala
            # comm_lost yayinlayarak SCADA'yi yaniltmamaya devam eder.
            cache.set_connected(True)
            cache.begin_recovery()
            logger.info("yadnp3_master_link_open device=%s state=recovering", device_code)

        def OnClose(self):  # noqa: N802
            cache.set_connected(False)
            logger.info("yadnp3_master_link_close device=%s", device_code)

        def AssignClassDuringStartup(self):  # noqa: N802
            # True: OpenDNP3 link acilir acilmaz baslangic integrity poll'u
            # tetikler (Class 0/1/2/3 hepsini bir kerede toplar). Bu olmadigi
            # zaman ilk veri scheduled scan'i (baseline 30sn / event 1sn) bek-
            # liyor; cihaz baglandiktan sonra ilk değerin frontend'e ulaşmasi
            # 30 sn'ye varabilir. True yapmak ilk veriyi <1sn'ye duşurur.
            return True

        def Now(self):  # noqa: N802
            return opendnp3.DNPTime(int(time.time() * 1000))

    return _MasterApp()


class _ManagedMaster:
    """Bir cihaz icin master session + cache.

    Iki kanal modu desteklenir:

    - ``listening`` (default): cihaz dinler, gateway TCP client olarak
      ``device.ip_address:tcp_port``'a baglanir. ``AddTCPClient``.
    - ``initiating``: cihaz master'a outbound baglanir (4G/SIM saha
      cihazlari icin). Gateway bu cihaza ozel ``master_ip_port``
      uzerinde ``AddTCPServer`` ile dinler. OpenDNP3 TCP server kanali
      tek client kabul ettigi icin cihaz basina ayri port gerek; backend
      bunu otomatik atar (20100..20700).
    """

    def __init__(
        self,
        manager: Any,
        *,
        device: DeviceConfig,
        local_address: int,
        tcp_port: int,
        scan_interval_sec: int,
        baseline_interval_sec: int,
    ) -> None:
        self.device = device
        self.cache = _DeviceCache()
        self._manager = manager
        self._scan_interval_sec = max(1, int(scan_interval_sec))
        self._baseline_interval_sec = max(self._scan_interval_sec, int(baseline_interval_sec))
        # Lifecycle flag: shutdown cagirildiktan sonra _master/_channel
        # C++ objeleri freed; read_device/request_integrity_poll concurrent
        # cagirilirsa SIGSEGV riski. shutdown() bunu False yapar; method'lar
        # girise gore early-return ederler.
        self._active: bool = True

        endpoint_type = (device.ip_endpoint_type or "listening").lower()
        channel_mode: str
        channel_endpoint_label: str
        if endpoint_type == "initiating":
            # Gateway TCP server modunda dinler; cihaz buraya baglanir.
            # master_ip_port backend tarafindan atanir; yoksa fallback olarak
            # tcp_port'u kullaniriz.
            listen_port = int(device.master_ip_port or tcp_port)
            self._channel = manager.AddTCPServer(
                f"ch_{device.code}",
                opendnp3.levels.NORMAL,
                opendnp3.ServerAcceptMode.CloseExisting,
                opendnp3.IPEndpoint("0.0.0.0", listen_port),
                None,
            )
            channel_mode = "initiating(server)"
            channel_endpoint_label = f"0.0.0.0:{listen_port}"
        else:
            self._channel = manager.AddTCPClient(
                f"ch_{device.code}",
                opendnp3.levels.NORMAL,
                opendnp3.ChannelRetry.Default(),
                [opendnp3.IPEndpoint(device.ip_address.strip(), int(tcp_port))],
                "0.0.0.0",
                None,
            )
            channel_mode = "listening(client)"
            channel_endpoint_label = f"{device.ip_address}:{tcp_port}"

        self._soe = _make_soe_handler(self.cache, device.code)
        self._app = _make_master_app(self.cache, device.code)
        cfg = opendnp3.MasterStackConfig()
        # Outstation unsolicited frame'leri SOE handler tarafindan dogal islenir;
        # disable etmiyoruz (kucuk mesaj ek yuk yok).
        cfg.master.disableUnsolOnStartup = False
        # responseTimeout: cihazin tek bir cevabi icin maksimum bekleme. 5sn,
        # yavas cihazda bile yetmesi gereken bir alt-sinir; daha kisa olursa
        # zayif TCP'lerde gereksiz timeout uretir.
        cfg.master.responseTimeout = opendnp3.TimeDuration.Seconds(5)
        # taskRetryPeriod: bir scan task'i basarisiz olursa ilk retry beklemesi.
        # 5sn -> 2sn yapmak ilk integrity poll'unun (Class 0) gec gelmesinin
        # onune gecer; cihaz baglandiktan sonra ilk veri 2sn'de cache'e yazilir.
        cfg.master.taskRetryPeriod = opendnp3.TimeDuration.Seconds(2)
        cfg.master.maxTaskRetryPeriod = opendnp3.TimeDuration.Seconds(30)
        cfg.link.LocalAddr = int(local_address)
        cfg.link.RemoteAddr = int(device.dnp3_address)
        self._master = self._channel.AddMaster(
            f"m_{device.code}", self._soe, self._app, cfg
        )
        # Saf event-driven mimari:
        #   1) BAGLANTI ANINDA: AssignClassDuringStartup=True ile OpenDNP3
        #      otomatik integrity poll (Class 0+1+2+3 hepsi) tetikler. Tum
        #      sinyaller bir kez cache'e yazilir, ilk snapshot olusur.
        #   2) SONRASI: Class 1/2/3 event scan SIK (her scan_interval_sec).
        #      Outstation bir nokta degistiginde event buffer'a yazar; master
        #      bu buffer'i okur, SOE handler degisen noktalari cache'e ister,
        #      dirty isaretler. Degismeyen sinyaller olduklari yerde durur.
        #   3) Class 0 baseline scan COK SEYREK (drift kontrolu, default 5dk).
        #      Tum statik noktalari yeniden okur — ama _DeviceCache.set() icin
        #      degeri degismemisse dirty=False, publish edilmez. Sadece kalite
        #      drift'i / kaybolan event'lerin telafisi icin guvenlik agi.
        # Class 1/2/3 SIK event scan: outstation event buffer'inda biriken
        # degisimleri toplar. Sub-second yansima icin scan_interval_sec.
        self._scan_event = self._master.AddClassScan(
            opendnp3.ClassField(False, True, True, True),  # 1+2+3
            opendnp3.TimeDuration.Seconds(self._scan_interval_sec),
            self._soe,
            opendnp3.TaskConfig.Default(),
        )
        # FULL integrity scan (Class 0+1+2+3) periyodik. baseline_interval_sec
        # de bir tum noktalari outstation'dan tekrar ister. Bunun icin Class 0
        # tek basina YETMEZ — bazi outstation'lar binary noktalari Class 0'a
        # dahil etmez (sadece Class 1'e atar). ClassField(True,True,True,True)
        # tam integrity poll'dur ve TUM noktalari class'tan bagimsiz dondurur.
        # cache.set degeri degismediyse dirty olmaz -> RabbitMQ'ya yuk vermez.
        self._scan_class0 = self._master.AddClassScan(
            opendnp3.ClassField(True, True, True, True),  # 0+1+2+3 full integrity
            opendnp3.TimeDuration.Seconds(self._baseline_interval_sec),
            self._soe,
            opendnp3.TaskConfig.Default(),
        )
        self._master.Enable()
        logger.info(
            "yadnp3_master_enabled device=%s mode=%s endpoint=%s remote=%s local=%s "
            "event_scan=%ss integrity_scan=%ss",
            device.code,
            channel_mode,
            channel_endpoint_label,
            device.dnp3_address,
            local_address,
            self._scan_interval_sec,
            self._baseline_interval_sec,
        )

    def shutdown(self) -> None:
        # ONCE flag'i False yap — concurrent read_device/refresh_all/
        # request_integrity_poll cagrilari early-return etsin. Aksi takdirde
        # _master.Disable() ile freed olmus C++ objesini yine kullanmaya
        # calisir ve SIGSEGV uretir (yadnp3 binding C++ ile yazilmis;
        # GIL serbest birakilir).
        self._active = False
        try:
            self._master.Disable()
        except Exception:  # noqa: BLE001
            logger.debug("yadnp3_master_disable_error", exc_info=True)
        # Channel'i da kapat — aksi halde TCP retry mantigi devam eder ve
        # yeni baglanan cihazlarla flap yasanir. Ozellikle TCP server kanali
        # yeni cihazin acmaya calistigi server portu engelleyebilir.
        try:
            self._channel.Shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("yadnp3_channel_shutdown_error", exc_info=True)

    def request_integrity_poll(self) -> bool:
        """Cihaza ANINDA Class 0+1+2+3 integrity poll gonderir.

        Sequenced scan mantigindan farkli olarak `ScanAllObjects` ad-hoc
        bir read task'i kuyruga ekler ve master link uzerinden gonderir.
        SOE handler donen frame'leri ayni cache mantigi ile isler;
        farkli degerler dirty isaretlenir, poller sonraki cycle'da
        yayinlar.

        Returns: True = task kuyruga alindi, False = link kapali / hata.
        """
        # shutdown sonrasi cagri _master freed olmus; SIGSEGV onlemek icin
        # erken don.
        if not self._active:
            return False
        try:
            # ScanClasses imzasi: (field, soe_handler, config=Default).
            # SOE handler'i atlamak TypeError verir (bkz. ya nodp3 binding);
            # bu master'in zaten kendi cache'ini besleyen handler'i (self._soe)
            # kullanilir, boylece donen frame'ler normal akista cache'e yazilir.
            # ClassField(True, True, True, True) = Class 0+1+2+3 full integrity.
            self._master.ScanClasses(
                opendnp3.ClassField(True, True, True, True),
                self._soe,
                opendnp3.TaskConfig.Default(),
            )
            return True
        except Exception:  # noqa: BLE001
            logger.exception("yadnp3_integrity_poll_request_failed")
            return False

    def operate_crob(
        self,
        *,
        index: int,
        op_type: str = "pulse_on",
        count: int = 1,
        on_time_ms: int = 100,
        off_time_ms: int = 100,
        timeout_sec: float = 10.0,
    ) -> dict:
        """Cihaza bir CROB (Control Relay Output Block) DirectOperate gonderir.

        Horstmann SN2 komutlari (Index 0=Configuration Update, 10/11=Trigger
        Config Download, ...) bu binary output index'lerine PULSE_ON ile
        tetiklenir. DirectOperate select-fazi olmadan tek adimda gonderir.

        DirectOperate callback'i opendnp3 IO thread'inde ASENKRON gelir; burada
        threading.Event ile senkron sonuca ceviririz. Doner: {ok, status, error}.
        """
        if not self._active:
            return {"ok": False, "status": "inactive", "error": "master shutdown"}

        op_map = {
            "pulse_on": opendnp3.OperationType.PULSE_ON,
            "pulse_off": opendnp3.OperationType.PULSE_OFF,
            "latch_on": opendnp3.OperationType.LATCH_ON,
            "latch_off": opendnp3.OperationType.LATCH_OFF,
        }
        opt = op_map.get(op_type.lower())
        if opt is None:
            return {"ok": False, "status": "bad_request", "error": f"gecersiz op_type: {op_type!r}"}

        try:
            crob = opendnp3.ControlRelayOutputBlock(opt)
            crob.count = int(count)
            crob.onTimeMS = int(on_time_ms)
            crob.offTimeMS = int(off_time_ms)
        except Exception as exc:  # noqa: BLE001
            # Bazi binding surumlerinde alan adlari farkli olabilir; ctor
            # positional fallback.
            try:
                crob = opendnp3.ControlRelayOutputBlock(
                    opt, opendnp3.TripCloseCode.NUL, False,
                    int(count), int(on_time_ms), int(off_time_ms),
                )
            except Exception:  # noqa: BLE001
                logger.exception("yadnp3_crob_build_failed")
                return {"ok": False, "status": "error", "error": f"CROB olusturulamadi: {exc}"}

        done = threading.Event()
        holder: dict = {"status": "no_result"}

        def _cb(result):  # opendnp3 IO thread
            try:
                summary = getattr(result, "summary", None)
                # ICommandTaskResult.summary -> TaskCompletion enum. SUCCESS bekleriz.
                holder["summary"] = str(summary)
                points = []
                try:
                    for pr in result.to_list():
                        points.append({"index": pr.index, "status": str(pr.status)})
                except Exception:  # noqa: BLE001
                    pass
                holder["points"] = points
                holder["status"] = "ok" if "SUCCESS" in str(summary).upper() else "failed"
            except Exception as exc:  # noqa: BLE001
                holder["status"] = "error"
                holder["error"] = str(exc)
            finally:
                done.set()

        try:
            self._master.DirectOperate(crob, int(index), _cb, opendnp3.TaskConfig.Default())
        except Exception as exc:  # noqa: BLE001
            logger.exception("yadnp3_direct_operate_failed device=%s index=%s", self.device.code, index)
            return {"ok": False, "status": "error", "error": str(exc)}

        if not done.wait(timeout=timeout_sec):
            return {"ok": False, "status": "timeout", "error": f"{timeout_sec}s icinde cevap yok"}

        ok = holder.get("status") == "ok"
        return {"ok": ok, **holder}


class Yadnp3TelemetryReader(TelemetryReader):
    """yadnp3 (OpenDNP3) tabanli, event-driven, kayipsiz veri okuyucu."""

    def __init__(
        self,
        *,
        local_address: int,
        default_dnp3_tcp_port: int,
        scan_interval_sec: int = 5,
        baseline_interval_sec: int = 60,
        log_level: str = "NORMAL",
        manager_threads: int = 0,
    ) -> None:
        if not _YADNP3_AVAILABLE:
            raise Yadnp3AdapterError(
                "yadnp3 (opendnp3) yuklu degil. Wheel kurulu olmali. "
                f"Import error: {_YADNP3_IMPORT_ERROR}"
            )
        self._local_address = int(local_address)
        self._default_dnp3_tcp_port = int(default_dnp3_tcp_port)
        self._scan_interval_sec = int(scan_interval_sec)
        self._baseline_interval_sec = int(baseline_interval_sec)
        # DNP3Manager IO thread sayisi. Eski sabit 2 thread, 100 cihazli
        # instance'ta thread doyumu yapiyordu (her cihazin TCP I/O +
        # scheduler isi tek 2 thread'e diziliyordu). manager_threads=0
        # verilirse heuristic: max(4, ceil(device_count_hint / 25))
        # ama device_count_hint constructor'da bilinmiyor; minimum 4 alacagiz.
        if manager_threads <= 0:
            resolved_threads = 4
        else:
            resolved_threads = max(1, min(64, int(manager_threads)))
        self._manager = opendnp3.DNP3Manager(resolved_threads)
        self._manager_threads = resolved_threads
        self._masters: dict[str, _ManagedMaster] = {}
        # Lock granulariteyi azaltmak icin double-check pattern: lock disinda
        # hizli dict lookup; sadece master OLUSTURURKEN lock alinir.
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_tcp_port(device: DeviceConfig, default_port: int) -> int:
        p = device.dnp3_tcp_port
        if p is not None and 1 <= p <= 65535:
            return int(p)
        return int(default_port)

    @staticmethod
    def _resolve_local_address(device: DeviceConfig, default_addr: int) -> int:
        a = device.master_address
        if a is not None and 0 <= a <= 65519:
            return int(a)
        return int(default_addr)

    def _ensure_master(self, device: DeviceConfig) -> _ManagedMaster:
        """Cihaz icin _ManagedMaster doner — varsa cache'den, yoksa olusturur.

        Double-check pattern: hot-path'te (master zaten var) lock ALINMAZ —
        dict'i lock-free okuruz (CPython dict thread-safe lookup'a izin
        verir). Sadece master ilk kez olusturulacaksa lock alip yeniden
        kontrol + create yapariz. Bu sayede 100 cihazlik cycle'in stabil
        state'inde her read_device() cagrisinda lock contention olmuyor.
        """
        # Hot-path: lock-free dict lookup
        existing = self._masters.get(device.code)
        if existing is not None:
            return existing
        # Cold-path: master daha once yaratilmadi — lock alip ikinci kontrol
        with self._lock:
            existing = self._masters.get(device.code)
            if existing is not None:
                return existing
            port = self._resolve_tcp_port(device, self._default_dnp3_tcp_port)
            local_addr = self._resolve_local_address(device, self._local_address)
            mm = _ManagedMaster(
                self._manager,
                device=device,
                local_address=local_addr,
                tcp_port=port,
                scan_interval_sec=self._scan_interval_sec,
                baseline_interval_sec=self._baseline_interval_sec,
            )
            self._masters[device.code] = mm
            return mm

    def read_device(
        self,
        *,
        device: DeviceConfig,
        signals: list[SignalConfig],
    ) -> list[SignalReading]:
        mm = self._ensure_master(device)
        # Race koruma: ensure_master ve forget_devices/close paralel
        # cagrilirsa mm shutdown edilmis olabilir. _active=False ise master
        # C++ objesi freed; _master.X cagrilari SIGSEGV uretir. Bu durumda
        # comm_lost yayini yapip cik (poller sonraki cycle'da yeniden
        # _ensure_master ile fresh master alir).
        if not mm._active:
            return [
                SignalReading(
                    signal_key=s.key,
                    source=s.source,
                    data_type=s.data_type,
                    raw_value=0.0,
                    scaled_value=0.0,
                    quality="comm_lost",
                    value_string=None,
                )
                for s in signals
            ]
        cache = mm.cache
        connected = cache.is_connected()

        # Stale-data guard: OpenDNP3'in `OnOpen`/`OnClose` callback'leri her
        # turde TCP kopmasinda tetiklenmeyebilir (channel auto-retry icin
        # session ayakta gorunmeye devam edebilir). Bu yuzden link flag'ine
        # ek olarak "son frame'den bu yana gecen sure" kontrolune da bakariz.
        # Threshold = max(4*baseline, 10*scan, 60s) — cok agresif olursa
        # baseline scan'in dogal jitter'inde bile false comm_lost uretir
        # (60sn'lik scan icin 60sn threshold sik tetikliyordu). 4x daha
        # tolerans birakir; gercekten kopuk cihaz icin yine de 2 dakika
        # icinde tetiklenir.
        threshold = max(
            self._baseline_interval_sec * 4,
            self._scan_interval_sec * 10,
            60,
        )
        last_update = cache.last_update_at()
        now = time.time()
        stale = last_update == 0.0 or (now - last_update) > threshold

        # Recovery state machine ile birlikte cihaz haberlesme durumu su sekilde
        # belirlenir:
        #   * connected=False    → state lost; comm_lost yayini (edge'de).
        #   * connected=True ve stale → link gozukuyor ama frame gelmiyor:
        #     stale-edge'de begin_recovery (henuz lost'da degilse) → comm_lost
        #     yayini surdur. Cihaz cevap verince set() recovery'yi onaylar.
        #   * connected=True ve recovering ve grace asildi → fail_recovery,
        #     tekrar lost; SCADA hala comm_lost goruyor.
        #   * connected=True ve recovering ve grace dolmadi → comm_lost yayini.
        #     SAHTE online onlemek icin SCADA'yi "comm_lost" olarak tutariz.
        #   * connected=True ve online → normal event-driven yayin.

        cache_state = cache.state()

        # Stale-edge: link OnOpen demis ama frame gelmiyor. State'i recovery'e
        # cek ki SCADA hala comm_lost gorsun, fresh frame beklensin.
        if connected and stale and cache_state == "online":
            cache.begin_recovery()
            cache_state = "recovering"
            logger.warning(
                "yadnp3_device_stale device=%s ip=%s last_data_age=%ds threshold=%ds "
                "(link acik gozukuyor ama veri eskidi - state=recovering)",
                device.code,
                device.ip_address,
                int(now - last_update) if last_update else -1,
                threshold,
            )
            try:
                mm.request_integrity_poll()
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_stale_integrity_poll_error", exc_info=True)

        # Recovery grace timeout: link acik ama hala fresh frame gelmedi →
        # tekrar lost. SCADA "comm_lost" gormeye devam eder; bu zaten dogru.
        if cache_state == "recovering":
            if cache.recovery_age() > _RECOVERY_GRACE_SEC:
                cache.fail_recovery()
                cache_state = "lost"
                logger.warning(
                    "yadnp3_device_recovery_timeout device=%s ip=%s grace_sec=%.0f "
                    "(fresh frame gelmedi - state=lost)",
                    device.code,
                    device.ip_address,
                    _RECOVERY_GRACE_SEC,
                )

        # Cihaz "online" degil mi? comm_lost yayini.
        # Ilk edge'de quality=comm_lost, sonrakilerde no_change (mesaj flood'unu
        # onlemek icin).
        if cache_state != "online":
            already_announced = getattr(cache, "_stale_announced", False)
            try:
                cache._stale_announced = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            quality_to_emit = "no_change" if already_announced else "comm_lost"
            return [
                SignalReading(
                    signal_key=s.key,
                    source=s.source,
                    data_type=s.data_type,
                    raw_value=0.0,
                    scaled_value=0.0,
                    quality=quality_to_emit,
                    value_string=None,
                )
                for s in signals
            ]

        # Buradan sonrasi: state == "online". Cihaz cevapliyor.
        # Recovery confirmed olduysa (set() flag'i tetiklemis) bir kerelik
        # mark_all_dirty + log. Boylece SCADA tum sinyalleri quality=good ile
        # gorur; cihazin son bilinen degerleri yayilanir.
        if cache.consume_recovery_publish():
            try:
                cache._stale_announced = False  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            redirty_count = cache.mark_all_dirty()
            logger.info(
                "yadnp3_device_recovered device=%s ip=%s redirty=%d "
                "(fresh frame ile dogrulandi)",
                device.code,
                device.ip_address,
                redirty_count,
            )

        readings: list[SignalReading] = []
        for s in signals:
            entry = cache.get(s.dnp3_object_group, s.dnp3_index)
            if entry is None:
                # Henuz okunmadi (yeni bagi/yeni nokta): 'no_change' — frontend
                # son iyi degeri korur, poller bunu yayinlamaz.
                readings.append(
                    SignalReading(
                        signal_key=s.key,
                        source=s.source,
                        data_type=s.data_type,
                        raw_value=0.0,
                        scaled_value=0.0,
                        quality="no_change",
                        value_string=None,
                    )
                )
                continue
            # Event-driven: degisiklik yoksa 'no_change' — bu cycle'da yayinlama.
            # Son set()'ten beri ayni deger okunmadi mi? Dirty flag ile karar.
            if not cache.is_dirty(s.dnp3_object_group, s.dnp3_index):
                readings.append(
                    SignalReading(
                        signal_key=s.key,
                        source=s.source,
                        data_type=s.data_type,
                        raw_value=0.0,
                        scaled_value=0.0,
                        quality="no_change",
                        value_string=None,
                    )
                )
                continue
            raw, value_string = entry
            scaled = raw * s.scale + s.offset
            readings.append(
                SignalReading(
                    signal_key=s.key,
                    source=s.source,
                    data_type=s.data_type,
                    raw_value=raw,
                    # 6 ondalik basamak: DNP3 G30v6 (64-bit double) tipindeki
                    # cihazlardan gelen elektrik olcumlerinde (V/A) anlamli
                    # hassasiyet 4-6 basamaga kadar gider. 4 basamak bazi
                    # voltaj olcumlerinde anlamli digit'i kesebiliyordu.
                    scaled_value=round(scaled, 6),
                    quality="good",
                    value_string=value_string,
                )
            )
            # Bu sinyal su an yayinlanacak — dirty flag'i temizle. Bir sonraki
            # cycle'a kadar tekrar set() cagirilmazsa "no_change" donecek.
            cache.clear_dirty(s.dnp3_object_group, s.dnp3_index)
        return readings

    def operate_device(
        self,
        *,
        device: DeviceConfig,
        index: int,
        op_type: str = "pulse_on",
        count: int = 1,
        on_time_ms: int = 100,
        off_time_ms: int = 100,
        timeout_sec: float = 10.0,
    ) -> dict[str, Any]:
        # Var olan master'i kullan; yoksa ac (read akisiyla ayni cache paylasilir).
        mm = self._ensure_master(device)
        if not mm._active:
            return {
                "ok": False,
                "status": "inactive",
                "device_code": device.code,
                "index": int(index),
                "error": "master shutdown",
            }
        result = mm.operate_crob(
            index=index,
            op_type=op_type,
            count=count,
            on_time_ms=on_time_ms,
            off_time_ms=off_time_ms,
            timeout_sec=timeout_sec,
        )
        return {"device_code": device.code, "index": int(index), **result}

    def forget_devices(self, active_device_codes: set[str]) -> int:
        """Backend config'inden cikarilmis cihazlarin master/channel'larini kapat.

        Aksi halde silinen cihazlarin TCP baglanti deneme kanalı acik kalir;
        ayni IP+port'a baglanmaya calisip diger cihazlarla flap yapar (zombie
        master). Config refresh akisinda her seferinde cagirilir.

        Sira:
          1) Lock altinda master'lari dict'ten cikar VE _active=False isaretle.
             Bu sayede paralel read_device cagrilari erken-return eder, shutdown
             sirasinda yarisma olmaz.
          2) Lock disinda shutdown'lari calistir — _channel.Shutdown() blocking
             olabilir; lock'i bos tutmak gerek.
        """
        cleaned: list[tuple[str, _ManagedMaster]] = []
        with self._lock:
            stale_codes = [c for c in self._masters if c not in active_device_codes]
            for code in stale_codes:
                mm = self._masters.pop(code, None)
                if mm is None:
                    continue
                mm._active = False  # paralel okuyuculari erken-return'e zorla
                cleaned.append((code, mm))
        # Shutdown'lari lock disinda calistir.
        for code, mm in cleaned:
            try:
                mm.shutdown()
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_master_shutdown_error device=%s", code, exc_info=True)
            logger.info("yadnp3_master_forgotten device=%s reason=removed_from_config", code)
        return len(cleaned)

    def refresh_all_devices(self) -> tuple[int, int]:
        """Aktif tum cihazlara ANINDA full integrity poll (Class 0+1+2+3).

        Kullanim: SCADA/operator "tum cihazlara sorgu at" tetiklerse, ya da
        haberlesme bir sure kopuk kalip yeniden gelirse, hat boyunca tum
        sinyallerin guncel degerini DB'ye yazmak icin manuel tetik.

        Returns: (basarili_count, toplam_master_count). Basarili = task
        kuyruga alindigi cihazlar; gercek frame yaniti ayri bir asenkron
        akista cache'e yazilir.
        """
        ok = 0
        # Lock altinda master listesini snapshot al; sonra lock'u birak ve
        # integrity poll cagrilarini disarida yap — ScanClasses C++ tarafa
        # kuyruga ekleme/IO blocking olabilir, ana lock'i tutmak diger
        # _ensure_master ve forget_devices cagrilarini gereksiz bloklar.
        with self._lock:
            snapshot = list(self._masters.items())
            total = len(snapshot)
        for code, mm in snapshot:
            if not mm._active:
                continue  # shutdown araliginda — atla
            try:
                if mm.request_integrity_poll():
                    ok += 1
                    logger.info("yadnp3_integrity_poll_requested device=%s", code)
                else:
                    logger.warning("yadnp3_integrity_poll_failed device=%s", code)
            except Exception:  # noqa: BLE001
                logger.exception("yadnp3_integrity_poll_error device=%s", code)
        return ok, total

    def close(self) -> None:
        # Iki asama: lock altinda dict'ten cikar + _active=False, lock disinda
        # shutdown. forget_devices ile ayni race-guard pattern'i.
        with self._lock:
            to_shutdown = list(self._masters.values())
            for mm in to_shutdown:
                mm._active = False
            self._masters.clear()
        for mm in to_shutdown:
            try:
                mm.shutdown()
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_master_shutdown_error", exc_info=True)
        try:
            self._manager.Shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("yadnp3_manager_shutdown_error", exc_info=True)
