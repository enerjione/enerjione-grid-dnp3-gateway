#!/usr/bin/env python3
"""Smart sessizlik + kurtarma SAHA KABUL analizi (pcap + gateway loglari).

Kabul raporunun ISTEDIGI HER SAYIYI uretir; elle paket sayilmaz.

KULLANIM
--------
    # 1) Yakalama (>= fiziksel Session Timeout'un 2 KATI)
    sudo tcpdump -i any -n -w /tmp/sn2.pcap "host <CIHAZ_IP> and tcp port <PORT>"

    # 2) Loglar
    docker logs eg-gw-<kod> > /tmp/gw.log 2>&1

    # 3) Analiz
    python3 scripts/field_smart_verify.py \\
        --pcap /tmp/sn2.pcap --log /tmp/gw.log \\
        --device-ip 188.59.29.74 --gateway-ip 172.19.0.3 \\
        --device-code SN2_0 --session-timeout 120

CIKIS KODU: 0 = KABUL, 1 = RED, 2 = kullanim/ortam hatasi

NEDEN AYRI BIR ARAC: 2026-08-20 sahasinda "10 baytlik cerceve" ve
"~5.73 saniyelik trafik" elle yorumlandi ve ilk turda yanlis atfedildi
(Grid'in `link status period=0` ayarina). Bu arac cerceveleri KOD
COZUMLEYEREK siniflar; yorum payi birakmaz.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field

# DNP3 fonksiyon kodlari (application layer).
FC_READ = 0x01
FC_WRITE = 0x02
FC_CONFIRM = 0x00
FC_ENABLE_UNSOL = 0x14
FC_DISABLE_UNSOL = 0x15
FC_RESPONSE = 0x81
FC_UNSOL_RESPONSE = 0x82

# Data link layer fonksiyon kodlari (CTRL & 0x0F, PRM=1).
LINK_FC_RESET = 0x00
LINK_FC_USER_DATA = 0x04
LINK_FC_REQUEST_LINK_STATUS = 0x09

#: Class objeleri: group 60, variation 1..4 = Class 0..3.
CLASS_VARS = {1: "Class0", 2: "Class1", 3: "Class2", 4: "Class3"}


@dataclass
class Cerceve:
    t: float
    gateway_kaynakli: bool
    uzunluk: int
    tur: str  # link_status | link_status_reply | read_scan | integrity | confirm | response | unsol | other
    detay: str = ""


@dataclass
class Rapor:
    cerceveler: list[Cerceve] = field(default_factory=list)
    ilk_t: float = 0.0
    son_t: float = 0.0
    tcp_close_t: float | None = None
    ihlaller: list[str] = field(default_factory=list)


def _tshark_var() -> bool:
    try:
        subprocess.run(["tshark", "-v"], capture_output=True, check=False, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _pcap_oku(pcap: str, device_ip: str, gateway_ip: str, port: int) -> list[tuple[float, bool, bytes, str]]:
    """(zaman, gateway_kaynakli, tcp_payload, tcp_flags) listesi.

    `tshark` KULLANILIR: tcpdump ham yuku hex olarak vermez. tshark yoksa
    arac 2 ile cikar — TAHMINE DAYALI analiz YAPMAZ.
    """
    if not _tshark_var():
        print(
            "HATA: `tshark` bulunamadi (apt install tshark).\n"
            "      Bu arac ham DNP3 yukunu COZUMLER; tcpdump tek basina yetmez.\n"
            "      Tahmine dayali analiz BILEREK yapilmaz.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    filtre = f"tcp.port=={port} && (ip.addr=={device_ip})"
    cmd = [
        "tshark",
        "-r",
        pcap,
        "-Y",
        filtre,
        "-T",
        "fields",
        "-e",
        "frame.time_epoch",
        "-e",
        "ip.src",
        "-e",
        "tcp.flags",
        "-e",
        "tcp.len",
        "-e",
        "tcp.payload",
    ]
    try:
        cikti = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=300).stdout
    except subprocess.CalledProcessError as exc:
        print(f"HATA: tshark basarisiz: {exc.stderr[:300]}", file=sys.stderr)
        raise SystemExit(2) from exc

    sonuc = []
    for satir in cikti.splitlines():
        p = satir.split("\t")
        if len(p) < 5:
            continue
        try:
            t = float(p[0])
            uzunluk = int(p[3] or 0)
        except ValueError:
            continue
        gw = (p[1] or "").strip() == gateway_ip
        ham = bytes.fromhex((p[4] or "").replace(":", "")) if p[4] else b""
        sonuc.append((t, gw, ham if uzunluk else b"", p[2] or ""))
    return sonuc


def _dnp3_siniflandir(yuk: bytes, gateway_kaynakli: bool) -> tuple[str, str]:
    """Ham TCP yukunu DNP3 cerceve turune COZUMLE (varsayim YOK)."""
    if len(yuk) < 4 or yuk[0] != 0x05 or yuk[1] != 0x64:
        return "other", f"DNP3 degil ({len(yuk)} bayt)"
    ctrl = yuk[3]
    link_fc = ctrl & 0x0F
    prm = (ctrl >> 6) & 1

    if link_fc == LINK_FC_REQUEST_LINK_STATUS and prm == 1:
        return "link_status", "REQUEST_LINK_STATUS (FC=9) — LINK KEEPALIVE"
    if len(yuk) == 10 and prm == 0:
        return "link_status_reply", "LINK_STATUS yaniti"
    if link_fc == LINK_FC_RESET:
        return "link_reset", "RESET_LINK_STATES"
    if len(yuk) < 13:
        return "other", f"link katmani ({len(yuk)} bayt, FC={link_fc})"

    app_fc = yuk[12]
    n_user = yuk[2] - 5
    objeler = []
    i = 13
    while i + 2 < 10 + n_user and i + 2 < len(yuk):
        g, v = yuk[i], yuk[i + 1]
        if g == 60 and v in CLASS_VARS:
            objeler.append(CLASS_VARS[v])
        i += 3

    if app_fc == FC_READ:
        if "Class0" in objeler:
            return "integrity", f"READ integrity {objeler}"
        if objeler:
            return "read_scan", f"READ event scan {objeler}"
        return "read_other", "READ (sinif objesi yok)"
    if app_fc == FC_CONFIRM:
        return "confirm", "uygulama CONFIRM (unsolicited'a zorunlu yanit)"
    if app_fc in (FC_ENABLE_UNSOL, FC_DISABLE_UNSOL):
        return "unsol_cfg", f"{'ENABLE' if app_fc == FC_ENABLE_UNSOL else 'DISABLE'}_UNSOLICITED"
    if app_fc == FC_WRITE:
        return "write", "WRITE (zaman senk olabilir)"
    if app_fc == FC_UNSOL_RESPONSE:
        return "unsol", "UNSOLICITED RESPONSE (cihaz kaynakli)"
    if app_fc == FC_RESPONSE:
        return "response", "RESPONSE (cihaz kaynakli)"
    return "other", f"app FC=0x{app_fc:02x}"


LOG_DESENLERI = [
    ("master_enabled", re.compile(r"yadnp3_master_enabled")),
    ("link_open", re.compile(r"yadnp3_master_link_open")),
    ("link_close", re.compile(r"yadnp3_master_link_close")),
    ("smart_idle_entered", re.compile(r"smart_idle_entered")),
    ("smart_idle_wakeup", re.compile(r"smart_idle_wakeup")),
    ("recovered", re.compile(r"yadnp3_device_recovered")),
    ("recovery_timeout", re.compile(r"yadnp3_device_recovery_timeout")),
    ("device_stale", re.compile(r"yadnp3_device_stale")),
    ("comm_lost", re.compile(r"yadnp3_comm_lost_announced")),
    ("relink", re.compile(r"yadnp3_device_relink")),
    ("integrity_poll", re.compile(r"integrity_poll_requested|auto_classify_poll")),
    ("policy_mismatch", re.compile(r"device_policy_mismatch")),
    ("periodic_scans_enabled", re.compile(r"yadnp3_periodic_scans_enabled")),
]


def _log_analiz(log_yolu: str, device_code: str) -> tuple[Counter, list[str], dict[str, str]]:
    sayim: Counter = Counter()
    zaman_cizelgesi: list[str] = []
    profil: dict[str, str] = {}
    try:
        with open(log_yolu, encoding="utf-8", errors="replace") as f:
            for satir in f:
                if device_code and device_code not in satir:
                    continue
                for ad, desen in LOG_DESENLERI:
                    if desen.search(satir):
                        sayim[ad] += 1
                        zaman_cizelgesi.append(f"{ad}: {satir.strip()[:190]}")
                        if ad == "master_enabled":
                            for alan in (
                                "ip_endpoint_type",
                                "configured_policy",
                                "effective_policy",
                                "operation_mode",
                                "periodic_scans",
                                "event_scan",
                                "baseline_scan",
                            ):
                                m = re.search(rf"{alan}=(\S+)", satir)
                                if m:
                                    profil[alan] = m.group(1)
                        break
    except OSError as exc:
        print(f"HATA: log okunamadi: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return sayim, zaman_cizelgesi, profil


def main() -> int:
    ap = argparse.ArgumentParser(description="Smart sessizlik + kurtarma saha kabul analizi")
    ap.add_argument("--pcap", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--device-ip", required=True)
    ap.add_argument("--gateway-ip", required=True)
    ap.add_argument("--port", type=int, default=20001)
    ap.add_argument("--device-code", default="")
    ap.add_argument(
        "--session-timeout",
        type=float,
        default=120.0,
        help="Fiziksel Horstmann Listening Session Timeout (sn). GOMULU DEGIL; olcut icin.",
    )
    a = ap.parse_args()

    paketler = _pcap_oku(a.pcap, a.device_ip, a.gateway_ip, a.port)
    if not paketler:
        print("HATA: filtreye uyan paket yok — IP/port dogru mu?", file=sys.stderr)
        return 2

    cerceveler: list[Cerceve] = []
    tcp_close_t = None
    for t, gw, yuk, bayraklar in paketler:
        try:
            bayrak_i = int(bayraklar, 16) if bayraklar.startswith("0x") else int(bayraklar or 0)
        except ValueError:
            bayrak_i = 0
        if bayrak_i & 0x01 or bayrak_i & 0x04:  # FIN | RST
            tcp_close_t = t if tcp_close_t is None else tcp_close_t
        if not yuk:
            continue  # saf ACK/SYN — DNP3 yuku DEGIL
        tur, detay = _dnp3_siniflandir(yuk, gw)
        cerceveler.append(Cerceve(t=t, gateway_kaynakli=gw, uzunluk=len(yuk), tur=tur, detay=detay))

    if not cerceveler:
        print("HATA: hic DNP3 uygulama/link yuku yok — yakalama dogru mu?", file=sys.stderr)
        return 2

    t0, t1 = paketler[0][0], paketler[-1][0]
    sure = t1 - t0

    # "Gerekli ilk alisveris" penceresi: ilk cihaz-kaynakli RESPONSE/UNSOL'e
    # kadar olan kisim + ona verilen CONFIRM. Sonrasi SESSIZ OLMALI.
    son_anlamli = None
    for c in cerceveler:
        if c.tur in ("response", "unsol", "confirm", "integrity", "read_scan"):
            son_anlamli = c

    # IHLALLER — ilk alisveristen SONRA tekrarlayan gateway trafigi.
    link_status_sayisi = sum(1 for c in cerceveler if c.gateway_kaynakli and c.tur == "link_status")
    scan_sayisi = sum(1 for c in cerceveler if c.gateway_kaynakli and c.tur == "read_scan")
    integrity_sayisi = sum(1 for c in cerceveler if c.gateway_kaynakli and c.tur == "integrity")

    sessiz_baslangic = son_anlamli.t if son_anlamli else t0
    sessiz_sure = (tcp_close_t or t1) - sessiz_baslangic

    sayim, zaman_cizelgesi, profil = _log_analiz(a.log, a.device_code)

    # ---- RAPOR ----
    print("=" * 74)
    print("SMART SESSIZLIK + KURTARMA — SAHA KABUL ANALIZI")
    print("=" * 74)
    print(f"  yakalama suresi              : {sure:.1f} sn")
    print(f"  fiziksel Session Timeout     : {a.session_timeout:.0f} sn (gomulu DEGIL, olcut)")
    print(f"  toplam DNP3 yuklu cerceve    : {len(cerceveler)}")
    print(
        f"  son anlamli DNP3 alisverisi  : "
        f"{f'+{son_anlamli.t - t0:.1f} sn ({son_anlamli.tur})' if son_anlamli else 'YOK'}"
    )
    print(f"  TCP kapanisi (FIN/RST)       : {f'+{tcp_close_t - t0:.1f} sn' if tcp_close_t else 'GORULMEDI'}")
    print(f"  olculen sessizlik suresi     : {sessiz_sure:.1f} sn")
    print()
    print("  GATEWAY KAYNAKLI TEKRARLAYAN TRAFIK (hepsi 0 OLMALI):")
    print(f"    REQUEST_LINK_STATUS (FC=9) : {link_status_sayisi}")
    print(f"    Class 1/2/3 event scan     : {scan_sayisi}")
    print(f"    integrity poll             : {integrity_sayisi}")
    print()
    if profil:
        print("  CIHAZ PROFILI (loglardan):")
        for k, v in profil.items():
            print(f"    {k:<22} = {v}")
        print()
    print("  LOG SAYIMLARI:")
    for ad, _ in LOG_DESENLERI:
        print(f"    {ad:<24} : {sayim.get(ad, 0)}")
    print()

    # ---- KABUL OLCUTLERI ----
    ihlaller: list[str] = []
    if link_status_sayisi:
        ihlaller.append(f"{link_status_sayisi} adet REQUEST_LINK_STATUS (link keepalive KAPALI OLMALI)")
    if scan_sayisi:
        ihlaller.append(f"{scan_sayisi} adet Class 1/2/3 event scan (smart'ta OLMAMALI)")
    if integrity_sayisi > 1:
        ihlaller.append(f"{integrity_sayisi} adet integrity poll (en fazla 1 acilis pollu)")
    if sayim.get("device_stale"):
        ihlaller.append(f"{sayim['device_stale']} adet device_stale (saglikli smart oturumda OLMAMALI)")
    if sayim.get("comm_lost"):
        ihlaller.append(f"{sayim['comm_lost']} adet comm_lost_announced")
    if sayim.get("recovery_timeout"):
        ihlaller.append(f"{sayim['recovery_timeout']} adet recovery_timeout")
    if sayim.get("relink", 0) > 1:
        ihlaller.append(f"{sayim['relink']} adet relink (salinim belirtisi)")
    if profil.get("periodic_scans") == "true":
        ihlaller.append("periodic_scans=true — cihaz `continuous` kosuyor (KONFIGURASYON)")
    if profil.get("effective_policy") and profil["effective_policy"] != "smart":
        ihlaller.append(f"effective_policy={profil['effective_policy']} (smart bekleniyordu)")
    if sure < a.session_timeout * 2:
        ihlaller.append(
            f"yakalama SURESI YETERSIZ: {sure:.0f}sn < 2x{a.session_timeout:.0f}sn — "
            "keepalive'in kapandigi DOGRULANAMAZ"
        )
    if tcp_close_t is None:
        ihlaller.append("TCP kapanisi GORULMEDI — dogal uyku dogrulanamadi")

    print("=" * 74)
    if ihlaller:
        print("SONUC: FIELD RETEST FAILED")
        for i in ihlaller:
            print(f"  - {i}")
        print()
        print("Zaman cizelgesi (ilk 25):")
        for satir in zaman_cizelgesi[:25]:
            print(f"  {satir}")
        return 1

    print("SONUC: SMART QUIET + RECOVERY FIELD ACCEPTED")
    print(f"  sessizlik {sessiz_sure:.0f} sn boyunca korundu; cihaz oturumu KENDISI kapatti.")
    print("  Gateway kaynakli tekrarlayan DNP3/link yuku: 0")
    print()
    print("Zaman cizelgesi:")
    for satir in zaman_cizelgesi[:25]:
        print(f"  {satir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
