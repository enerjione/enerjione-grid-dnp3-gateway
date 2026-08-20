#!/usr/bin/env bash
# Horstmann Smart Mode saha kabulu — SESSIZLIK OLCUMU
#
# NE OLCER: gateway -> cihaz yonunde, belirtilen pencerede giden DNP3
# baytlarini. Smart Mode'un calistiginin TEK objektif kaniti budur:
# gateway susmazsa cihazin 15 saniyelik hareketsizlik sayaci dolmaz ve
# modem hicbir zaman kapanmaz.
#
# NEDEN tcpdump: on-prem Ubuntu'da GUI yok. Wireshark GEREKMEZ.
#
# NEDEN yon filtresi: ilgisiz host trafigi (SSH, NATS, health) sayilmamali.
# Yalnizca `src=<gateway> and tcp port <master_ip_port>` sayilir.
#
# KULLANIM:
#   sudo ./scripts/field_capture.sh \
#        --device-ip 10.20.5.11 --gateway-ip 10.20.5.1 \
#        --port 20100 --window 20
#
# CIKIS KODU: 0 = sessizlik dogrulandi, 1 = trafik gorulduu, 2 = kullanim hatasi
set -euo pipefail

CIHAZ_IP=""
GATEWAY_IP=""
PORT=""
PENCERE=20
ARAYUZ="any"
PCAP=""

kullanim() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --device-ip)  CIHAZ_IP="$2"; shift 2 ;;
    --gateway-ip) GATEWAY_IP="$2"; shift 2 ;;
    --port)       PORT="$2"; shift 2 ;;
    --window)     PENCERE="$2"; shift 2 ;;
    --interface)  ARAYUZ="$2"; shift 2 ;;
    --pcap)       PCAP="$2"; shift 2 ;;
    -h|--help)    kullanim ;;
    *) echo "bilinmeyen secenek: $1" >&2; kullanim ;;
  esac
done

[[ -z "$CIHAZ_IP" || -z "$GATEWAY_IP" || -z "$PORT" ]] && kullanim

if ! command -v tcpdump >/dev/null 2>&1; then
  echo "HATA: tcpdump bulunamadi (apt install tcpdump)" >&2
  exit 2
fi

PCAP="${PCAP:-/tmp/sn2-sessizlik-$(date +%Y%m%d-%H%M%S).pcap}"

echo "== Horstmann Smart Mode sessizlik olcumu =="
echo "   cihaz     : $CIHAZ_IP"
echo "   gateway   : $GATEWAY_IP"
echo "   port      : $PORT"
echo "   pencere   : ${PENCERE}s"
echo "   pcap      : $PCAP"
echo
echo "NOT: olcumu cihaz verisini AKTARDIKTAN SONRA baslatin; aktarim aninda"
echo "     trafik olmasi NORMALDIR. Olculen sey aktarim SONRASI sessizliktir."
echo

# Her iki yonu de kaydet (teshis icin), sayimi tek yonde yap.
tcpdump -i "$ARAYUZ" -n -w "$PCAP" \
        "host $CIHAZ_IP and tcp port $PORT" >/dev/null 2>&1 &
TCPDUMP_PID=$!
trap 'kill "$TCPDUMP_PID" 2>/dev/null || true' EXIT

sleep "$PENCERE"
kill "$TCPDUMP_PID" 2>/dev/null || true
wait "$TCPDUMP_PID" 2>/dev/null || true
trap - EXIT

# `greater 1` -> saf ACK'leri disla; yalnizca VERI tasiyan segmentler.
GW_TO_DEV=$(tcpdump -r "$PCAP" -n \
    "src host $GATEWAY_IP and tcp port $PORT and greater 1" 2>/dev/null | wc -l | tr -d ' ')
DEV_TO_GW=$(tcpdump -r "$PCAP" -n \
    "dst host $GATEWAY_IP and tcp port $PORT and greater 1" 2>/dev/null | wc -l | tr -d ' ')

echo "gateway -> cihaz DNP3 paketi (${PENCERE}s): $GW_TO_DEV"
echo "cihaz -> gateway DNP3 paketi (${PENCERE}s): $DEV_TO_GW"
echo "pcap: $PCAP"
echo

if [[ "$GW_TO_DEV" -eq 0 ]]; then
  echo "SONUC: PASS — gateway sessiz. Cihazin 15sn hareketsizlik sayaci dolabilir."
  exit 0
fi

echo "SONUC: FAIL — gateway hala trafik uretiyor; modem KAPANAMAZ."
echo "Kontrol edin:"
echo "  * /health -> session_policy / effective_session_policy"
echo "  * cihaz session_policy=smart|auto mu?"
echo "  * auto ise mod gozlendi mi (operation_mode)?"
echo "  * log: auto_policy_fallback (continuous'a dusulmus olabilir)"
exit 1
