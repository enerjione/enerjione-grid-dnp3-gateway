"""Ag tanilama (ICMP) — YALNIZCA TESHIS, ASLA SAGLIK KARARI.

IKI IHLAL EDILEMEZ KURAL
------------------------
1. **Bu modulun hicbir ciktisi `comm_lost` URETMEZ.**
2. **Bu modulun hicbir cagrisi DNP3 okuma/poll yolunu BLOKLAMAZ.**

Haberlesme kaybi YALNIZCA DNP3 kanitina ve tanimli denetim esiklerine
(`smart_max_silence_sec`) dayanir. Sonda sonuclari operatorun ARIZANIN
YERINI bulmasi icindir, durum makinesinin girdisi DEGILDIR.

TCP SONDASI NEDEN KALDIRILDI (PR #32 review, madde 2)
-----------------------------------------------------
Onceki hali cihazin DNP3 portuna ham bir `socket.connect()` aciyordu. Bu,
PR'in bilincli olarak KACINDIGI hatanin ta kendisiydi: **uretim DNP3 kanaliyla
YARISAN ikinci bir TCP baglantisi.**

Somut zarar: Smart moddaki bir Horstmann yalnizca sinirli bir Socket
Listening Timeout penceresi boyunca uyanik kalir. Ham tanilama soketi yarisi
KAZANIP once baglanabilir, sonra hicbir DNP3 trafigi uretmeden kapanabilir —
yani gercek opendnp3 oturumunu engelleyerek tam da olcmeye calistigi seyi
BOZAR. Bir olcum aracinin olctugu sistemi bozmasi kabul edilemez.

Yerine: TCP/baglanti bilgisi **yadnp3 kanalinin KENDI durumundan** turetilir
(`IChannelListener.OnStateChange`). Tamamen pasif, ek soket YOK, yaris YOK.
Kutuphane "reddedildi" ile "paket dustu" ayrimini vermedigi icin o ayrim
raporlanmaz; **uydurmak yerine `unknown` denir.**

ICMP NEDEN TEK BASINA SAGLIK OLCUTU OLAMAZ
------------------------------------------
* ICMP saha aglarinda / APN'lerde sikca ENGELLIDIR — cevapsizlik cihazin
  olu oldugunu gostermez.
* Smart moddaki bir modem MESRU olarak uykudadir; ping'e cevap vermemesi
  BEKLENEN davranistir.

Bu yuzden `ping` yalnizca cihazin BEKLENEN rapor penceresi kacirildiginda
calistirilir ve **her zaman arka planda**, sinirli bir yurutucu uzerinden.

TESHIS ZINCIRI
--------------
    ip_probe  unreachable          -> modem / APN / yonlendirme suphesi
    kanal OPEN + DNP3 kaniti yok   -> protokol / oturum sorunu
    ip ok + kanal hic acilmiyor    -> dinleyici / guvenlik duvari / uyku
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Literal

logger = logging.getLogger(__name__)

#: `ip_probe` sonuclari.
IP_REACHABLE = "reachable"
IP_UNREACHABLE = "unreachable"
#: Ortamda ICMP KULLANILAMIYOR (ping ikilisi yok / yetki yok). Bu bir ARIZA
#: DEGILDIR — container root olmayan kullaniciyla kosar ve ham soket acamaz.
IP_UNSUPPORTED = "unsupported"
IP_UNKNOWN = "unknown"

#: `tcp_probe_status` — yadnp3 KANAL DURUMUNDAN turetilir, olculmez.
#: Ham soket ACILMAZ (bkz. modul basligi).
TCP_OPEN = "open"
#: Gateway su an baglanmayi DENIYOR. Bu bir ARIZA DEGILDIR: Smart uykuda
#: beklenen durumdur ve tek basina hicbir sey soylemez.
TCP_CONNECTING = "connecting"
#: Kanal kapali/bilinmiyor. "Reddedildi" ile "paket dustu" AYRIMI YOK —
#: kutuphane bu detayi vermiyor ve UYDURULMAZ.
TCP_UNKNOWN = "unknown"

#: DNP3 oturum kaniti (durum makinesinden TURETILIR, burada olculmez).
DNP3_HEALTHY = "healthy"
DNP3_FAILED = "failed"
DNP3_UNKNOWN = "unknown"

#: ICMP zaman asimi. KISA tutuluyor: sonda arka planda kossa da bir cihazin
#: tanilamasi kuyruktaki digerlerini geciktirmemeli.
DEFAULT_ICMP_TIMEOUT_SEC = 2.0


def icmp_probe(
    host: str, *, timeout_sec: float = DEFAULT_ICMP_TIMEOUT_SEC
) -> Literal["reachable", "unreachable", "unsupported", "unknown"]:
    """ICMP erisilebilirligi — `ping` ikilisi uzerinden.

    **BU CAGRI BLOKLAR** (alt surec baslatir). Poll/okuma thread'inden
    DOGRUDAN cagrilmaz; yalnizca `DiagnosticExecutor` uzerinden kosar.

    HAM SOKET KULLANILMAZ: container root olmayan kullaniciyla kosar ve
    `AF_INET/SOCK_RAW` acamaz. `ping` ikilisi yoksa sonuc `unsupported`tir
    ve bu bir ARIZA DEGILDIR — cagiran taraf bunu saglik karari olarak
    KULLANMAZ.

    `unreachable` da tek basina "cihaz olu" DEMEK DEGILDIR: ICMP engelli
    olabilir ya da Smart modem mesru olarak uykuda olabilir.
    """
    if not host:
        return IP_UNKNOWN
    ping = shutil.which("ping")
    if not ping:
        return IP_UNSUPPORTED

    # Linux: -c sayi, -W saniye. Windows: -n sayi, -w milisaniye.
    # Ikisini de deneyecek kadar akilli olmaya CALISMIYORUZ; platformdan
    # turetiyoruz ve tanimadigimiz cikti `unknown` doner.
    if sys.platform.startswith("win"):
        arg = [ping, "-n", "1", "-w", str(int(max(1, timeout_sec) * 1000)), host]
    else:
        arg = [ping, "-c", "1", "-W", str(int(max(1, timeout_sec))), host]

    try:
        sonuc = subprocess.run(  # noqa: S603 — sabit ikili + dogrulanmis host
            arg,
            capture_output=True,
            timeout=max(1.0, timeout_sec) + 2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("icmp_probe_error host=%s error=%s", host, exc)
        return IP_UNKNOWN
    return IP_REACHABLE if sonuc.returncode == 0 else IP_UNREACHABLE


def diagnose(ip_status: str, tcp_status: str, dnp3_status: str) -> str:
    """Sonda ucluesunden OPERATORE yonelik tek satirlik yorum.

    Yalnizca METIN uretir; hicbir durum karari vermez.
    """
    if dnp3_status == DNP3_HEALTHY:
        return "DNP3 oturumu saglikli"
    if tcp_status == TCP_OPEN:
        return "TCP acik ama gecerli DNP3 kaniti yok — protokol/oturum sorunu (adres, link katmani)"
    if ip_status == IP_REACHABLE and tcp_status in (TCP_CONNECTING, TCP_UNKNOWN):
        return "IP erisilebilir ama DNP3 oturumu acilmiyor — dinleyici/guvenlik duvari/uygulama sorunu"
    if ip_status == IP_UNREACHABLE:
        return "IP erisilemiyor — modem/APN/yonlendirme suphesi (ya da Smart modem uykuda)"
    if ip_status == IP_UNSUPPORTED:
        return "ICMP bu ortamda kullanilamiyor; kanal durumu ve DNP3 kaniti esas alinmali"
    return "yeterli tanilama verisi yok"


# ---------------------------------------------------------------------------
# Sinirli asenkron tanilama yurutucusu
# ---------------------------------------------------------------------------

#: Isci sayisi KUCUK tutulur. Tanilama bir ARKA PLAN isidir; telemetriyle
#: CPU/soket yarisina girmesi kabul edilemez.
DEFAULT_WORKERS = 2

#: Kuyruk SINIRLIDIR. Sinirsiz kuyruk, 500 cihazlik bir filoda dakikalarca
#: eskimis is biriktirir ve bellegi buyutur; dolunca is DUSURULUR — sonuc
#: yalnizca bir teshis satirinin eksik kalmasidir, telemetri ETKILENMEZ.
DEFAULT_QUEUE_SIZE = 64


class DiagnosticExecutor:
    """Tanilama sondalarini SINIRLI bir arka plan havuzunda kosturur.

    NEDEN VAR — SURU ETKISI (thundering herd)
    -----------------------------------------
    Cihaz basina 300sn'lik siklik siniri TEK BASINA YETMEZ. 200 cihaz ayni
    anda `late` olursa (APN kesintisi, saha elektrigi, backend saat kaymasi
    — hepsi gercek senaryolar), her biri ilk sondasini AYNI cycle'da hak
    eder. Senkron cagrilarda bunlar poll thread'inde SIRAYA girer:

        200 cihaz x ~2sn ICMP zaman asimi = ~400 saniye

    Bu sure boyunca gateway HICBIR cihazi okuyamaz. Yani tanilama, teshis
    etmeye calistigi kesintiyi GERCEK bir kesintiye cevirir. Kabul edilemez.

    GARANTILER
    ----------
    * Isci sayisi ve kuyruk boyu SINIRLI.
    * Cihaz basina AYNI ANDA EN FAZLA BIR is (`_ucusta`).
    * Kuyruk dolu -> is DUSURULUR, `submit` yine de ANINDA doner. ASLA bloke
      etmez, `queue.put`ta beklemez.
    * Istisnalar isci icinde yutulur; bir cihazin tanilamasi digerlerini ya
      da havuzu dusuremez.
    * `shutdown()` iscileri temiz kapatir.
    * Saglik/durum kararlari sondanin BITMESINI BEKLEMEZ — sonuc geldiginde
      yalnizca teshis alanlarini tazeler.
    """

    def __init__(
        self,
        *,
        workers: int = DEFAULT_WORKERS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        name: str = "diag",
    ) -> None:
        self._kuyruk: queue.Queue[tuple[str, Callable[[], None]] | None] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._lock = threading.Lock()
        self._ucusta: set[str] = set()
        self._kapali = False
        #: Kuyruk doldugu icin ATLANAN is sayisi. Sessiz kirpma, operatore
        #: "tanilama calisti ve bir sey bulamadi" yalanini soylerdi.
        self.dropped_total = 0
        self.completed_total = 0
        self._isciler = [
            threading.Thread(target=self._calis, name=f"{name}-{i}", daemon=True)
            for i in range(max(1, int(workers)))
        ]
        for t in self._isciler:
            t.start()

    def submit(self, key: str, fn: Callable[[], None]) -> bool:
        """Isi kuyruga ekle. ANINDA doner. Donen: kabul edildi mi.

        `key` tipik olarak cihaz kodudur ve ayni cihaz icin ikinci bir is
        ucustayken yenisi KABUL EDILMEZ.
        """
        with self._lock:
            if self._kapali or key in self._ucusta:
                return False
            self._ucusta.add(key)
        try:
            # `block=False` ZORUNLU: burasi poll thread'idir ve kuyruk
            # doluyken beklemek tam da onlemek istedigimiz seydir.
            self._kuyruk.put((key, fn), block=False)
        except queue.Full:
            with self._lock:
                self._ucusta.discard(key)
                self.dropped_total += 1
                atilan = self.dropped_total
            # Kenar-tetikli degil ama DEBUG: doygunluk normalde gecicidir.
            logger.debug("diagnostic_queue_full key=%s dropped_total=%d", key, atilan)
            return False
        return True

    def _calis(self) -> None:
        while True:
            is_ = self._kuyruk.get()
            if is_ is None:  # kapanis sinyali
                self._kuyruk.task_done()
                return
            key, fn = is_
            try:
                fn()
            except Exception:  # noqa: BLE001
                # IZOLE: bir cihazin tanilamasi havuzu ya da diger cihazlari
                # DUSURMEZ.
                logger.debug("diagnostic_task_error key=%s", key, exc_info=True)
            finally:
                with self._lock:
                    self._ucusta.discard(key)
                    self.completed_total += 1
                self._kuyruk.task_done()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "in_flight": len(self._ucusta),
                "queued": self._kuyruk.qsize(),
                "dropped_total": self.dropped_total,
                "completed_total": self.completed_total,
            }

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Iscileri temiz kapat. Yeni is KABUL EDILMEZ."""
        with self._lock:
            if self._kapali:
                return
            self._kapali = True
        for _ in self._isciler:
            try:
                self._kuyruk.put_nowait(None)
            except queue.Full:
                # Kuyruk doluysa isciler zaten mesgul; daemon thread olduklari
                # icin proses kapanisini ENGELLEMEZLER.
                pass
        for t in self._isciler:
            t.join(timeout=timeout)
