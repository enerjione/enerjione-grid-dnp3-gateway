"""Ag tanilama sondalari (ICMP / TCP) — YALNIZCA TESHIS.

EN ONEMLI KURAL
---------------
**Bu modulun hicbir ciktisi `comm_lost` URETMEZ.**

Haberlesme kaybi YALNIZCA DNP3 kanitina ve tanimli denetim esiklerine
(`smart_max_silence_sec`) dayanir. Sonda sonuclari operatorun ARIZAYI
YERINI BULMASI icindir, durum makinesinin girdisi DEGILDIR.

ICMP NEDEN TEK BASINA SAGLIK OLCUTU OLAMAZ
------------------------------------------
* ICMP saha aglarinda / APN'lerde sikca ENGELLIDIR — cevapsizlik cihazin
  olu oldugunu gostermez.
* Smart moddaki bir modem MESRU olarak uykudadir; ping'e cevap vermemesi
  BEKLENEN davranistir.
* Aktif trafik Smart modda oturum/guc davranisini etkileyebilir.

Bu yuzden `ping` yalnizca cihazin BEKLENEN rapor penceresi kacirildiginda
(ya da acikca istendiginde) calistirilir; Boost cihazlarda daha serbesttir
cunku orada surekli erisilebilirlik ZATEN beklenir.

TESHIS ZINCIRI
--------------
    ip_probe  unreachable          -> modem / APN / yonlendirme suphesi
    ip ok + tcp_probe closed       -> dinleyici / guvenlik duvari / uygulama
    tcp open + DNP3 kaniti yok     -> protokol / oturum sorunu
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
from typing import Literal

logger = logging.getLogger(__name__)

#: `ip_probe` sonuclari.
IP_REACHABLE = "reachable"
IP_UNREACHABLE = "unreachable"
#: Ortamda ICMP KULLANILAMIYOR (ping ikilisi yok / yetki yok). Bu bir ARIZA
#: DEGILDIR — container root olmayan kullaniciyla kosar ve ham soket acamaz.
IP_UNSUPPORTED = "unsupported"
IP_UNKNOWN = "unknown"

#: `tcp_probe` sonuclari.
TCP_OPEN = "open"
TCP_CLOSED = "closed"
TCP_TIMEOUT = "timeout"
TCP_UNKNOWN = "unknown"

#: DNP3 oturum kaniti (durum makinesinden TURETILIR, burada olculmez).
DNP3_HEALTHY = "healthy"
DNP3_FAILED = "failed"
DNP3_UNKNOWN = "unknown"

#: Sonda zaman asimlari. KISA tutuluyor: bu cagrilar poll thread'inden
#: yapilabiliyor ve bir cihazin tanilamasi digerlerini geciktirmemeli.
DEFAULT_TCP_TIMEOUT_SEC = 2.0
DEFAULT_ICMP_TIMEOUT_SEC = 2.0


def tcp_probe(
    host: str, port: int, *, timeout_sec: float = DEFAULT_TCP_TIMEOUT_SEC
) -> Literal["open", "closed", "timeout", "unknown"]:
    """TCP portu acik mi? Ayricalik GEREKTIRMEZ, guvenilirdir.

    `closed` ile `timeout` AYRI raporlanir cunku farkli seyler soyler:
      * `closed` (RST)  -> host AYAKTA ama dinleyici yok/kapali
      * `timeout`       -> paket kayboluyor: guvenlik duvari sessizce
                           dusuruyor ya da host/modem erisilemez
    """
    if not host or not port:
        return TCP_UNKNOWN
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(max(0.1, float(timeout_sec)))
    try:
        sock.connect((host, int(port)))
        return TCP_OPEN
    except TimeoutError:
        return TCP_TIMEOUT
    except ConnectionRefusedError:
        return TCP_CLOSED
    except OSError as exc:
        # Yonlendirme yok / ag erisilemez -> "kapali" DEMEK DEGIL.
        logger.debug("tcp_probe_error host=%s port=%s error=%s", host, port, exc)
        return TCP_UNKNOWN
    finally:
        try:
            sock.close()
        except OSError:
            pass


def icmp_probe(
    host: str, *, timeout_sec: float = DEFAULT_ICMP_TIMEOUT_SEC
) -> Literal["reachable", "unreachable", "unsupported", "unknown"]:
    """ICMP erisilebilirligi — `ping` ikilisi uzerinden.

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
    import sys

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
    if ip_status == IP_REACHABLE and tcp_status in (TCP_CLOSED, TCP_TIMEOUT):
        return "IP erisilebilir ama DNP3 portu acilmiyor — dinleyici/guvenlik duvari/uygulama sorunu"
    if ip_status == IP_UNREACHABLE:
        return "IP erisilemiyor — modem/APN/yonlendirme suphesi (ya da Smart modem uykuda)"
    if ip_status == IP_UNSUPPORTED:
        return "ICMP bu ortamda kullanilamiyor; TCP sondasi ve DNP3 kaniti esas alinmali"
    return "yeterli tanilama verisi yok"
