#!/usr/bin/env bash
# Horstmann Smart Mode saha kabulu — DNP3 UYGULAMA SESSIZLIGI OLCUMU
#
# NE OLCER: gateway -> cihaz yonunde, belirtilen pencerede giden **DNP3
# UYGULAMA YUKU**. Smart Mode'un calistiginin tek objektif kaniti budur:
# gateway susmazsa cihazin 15 saniyelik hareketsizlik sayaci dolmaz ve modem
# hicbir zaman kapanmaz.
#
# ------------------------------------------------------------------------
# KRITIK AYRIM — TCP BAGLANTI DENEMESI ile DNP3 UYGULAMA YUKU AYNI SEY DEGIL
# ------------------------------------------------------------------------
# Bu betigin ilk hali "gateway -> cihaz 0 PAKET" ariyordu. Bu olcut
# `listening` uc icin YANLISTIR ve saglikli bir kurulumu FAIL gosterirdi:
#
#   * `listening`te baglantiyi GATEWAY acar. Cihaz uyurken opendnp3
#     `ChannelRetry` ustel geri cekilmeyle SYN gondermeye DEVAM EDER — bu
#     BEKLENEN ve GEREKLI davranistir; cihazin uyandigini boyle fark ederiz.
#   * Bir SYN uygulama yuku TASIMAZ. Cihazin DNP3 hareketsizlik sayacini
#     sifirlayan sey uygulama katmani trafigidir; TCP kurulum denemesi
#     karsi taraf kapaliyken cihaza hic ulasmaz bile.
#
# Dolayisiyla iki sayi AYRI raporlanir:
#
#   TCP baglanti denemesi (SYN)  -> `listening`te BEKLENIR, `initiating`te 0
#   DNP3 uygulama yuku           -> HER IKI UCTA DA 0 OLMALI
#
# GECER OLCUTU: **DNP3 uygulama yuku = 0.** SYN sayisi bilgilendirmedir.
#
# NEDEN tcpdump: on-prem Ubuntu'da GUI yok. Wireshark GEREKMEZ.
#
# NEDEN yon filtresi: ilgisiz host trafigi (SSH, NATS, health) sayilmamali.
#
# KULLANIM:
#   # initiating (cihaz baglanir; port = master_ip_port)
#   sudo ./scripts/field_capture.sh \
#        --device-ip 10.20.5.11 --gateway-ip 10.20.5.1 \
#        --port 20100 --window 20
#
#   # listening (gateway baglanir; port = cihazin DNP3 portu, tipik 20000)
#   sudo ./scripts/field_capture.sh \
#        --device-ip 10.20.5.11 --gateway-ip 10.20.5.1 \
#        --port 20000 --window 20 --endpoint listening
#
# CIKIS KODU: 0 = DNP3 sessizligi dogrulandi, 1 = DNP3 yuku gorulduu,
#             2 = kullanim hatasi
set -euo pipefail

CIHAZ_IP=""
GATEWAY_IP=""
PORT=""
PENCERE=20
ARAYUZ="any"
PCAP=""
UC="initiating"

kullanim() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
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
    --endpoint)   UC="$2"; shift 2 ;;
    -h|--help)    kullanim ;;
    *) echo "bilinmeyen secenek: $1" >&2; kullanim ;;
  esac
done

[[ -z "$CIHAZ_IP" || -z "$GATEWAY_IP" || -z "$PORT" ]] && kullanim
if [[ "$UC" != "initiating" && "$UC" != "listening" ]]; then
  echo "HATA: --endpoint yalnizca 'initiating' ya da 'listening' olabilir" >&2
  exit 2
fi

if ! command -v tcpdump >/dev/null 2>&1; then
  echo "HATA: tcpdump bulunamadi (apt install tcpdump)" >&2
  exit 2
fi

PCAP="${PCAP:-/tmp/sn2-sessizlik-$(date +%Y%m%d-%H%M%S).pcap}"

echo "== Horstmann Smart Mode — DNP3 uygulama sessizligi olcumu =="
echo "   cihaz     : $CIHAZ_IP"
echo "   gateway   : $GATEWAY_IP"
echo "   port      : $PORT"
echo "   uc tipi   : $UC"
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

# --- FILTRELER ------------------------------------------------------------
# TCP yuk uzunlugu = IP toplam uzunluk - IP baslik - TCP baslik.
# `> 0` olan segmentler UYGULAMA VERISI tasir; saf ACK/SYN/FIN tasimaz.
YUK_VAR='((ip[2:2] - ((ip[0]&0x0f)<<2)) - ((tcp[12]&0xf0)>>2)) > 0'
# DNP3 baslangic sekizlileri 0x05 0x64 — yuk'un ilk iki byte'i.
DNP3_SIHIR='tcp[((tcp[12]&0xf0)>>2)] = 0x05 and tcp[((tcp[12]&0xf0)>>2)+1] = 0x64'
# Yalnizca SYN (ACK'siz) = yeni baglanti DENEMESI.
SYN_DENEME='tcp[tcpflags] & (tcp-syn|tcp-ack) = tcp-syn'

say() { tcpdump -r "$PCAP" -n "$1" 2>/dev/null | wc -l | tr -d ' '; }

GW_DNP3=$(say "src host $GATEWAY_IP and tcp port $PORT and $YUK_VAR and $DNP3_SIHIR")
GW_YUK=$(say  "src host $GATEWAY_IP and tcp port $PORT and $YUK_VAR")
GW_SYN=$(say  "src host $GATEWAY_IP and tcp port $PORT and $SYN_DENEME")
DEV_YUK=$(say "dst host $GATEWAY_IP and tcp port $PORT and $YUK_VAR")

echo "gateway -> cihaz  DNP3 uygulama paketi (${PENCERE}s): $GW_DNP3   <-- GECER OLCUTU"
echo "gateway -> cihaz  TCP yuklu paket (tum)             : $GW_YUK"
echo "gateway -> cihaz  TCP baglanti denemesi (SYN)       : $GW_SYN"
echo "cihaz   -> gateway TCP yuklu paket                  : $DEV_YUK"
echo "pcap: $PCAP"
echo

if [[ "$UC" == "listening" ]]; then
  echo "UC=listening: SYN denemeleri BEKLENIR (baglantiyi gateway acar ve"
  echo "cihazin uyandigini boyle fark eder). SYN sayisi 0 ise yeniden baglanma"
  echo "hic denenmiyor demektir — cihaz uyandiginda YAKALANAMAZ."
  if [[ "$GW_SYN" -eq 0 ]]; then
    echo "UYARI: hic SYN gorulmedi; kanal gercekten deniyor mu kontrol edin."
  fi
  echo
fi

if [[ "$GW_DNP3" -eq 0 && "$GW_YUK" -eq 0 ]]; then
  echo "SONUC: PASS — gateway DNP3 uygulama katmaninda SESSIZ."
  echo "Cihazin 15sn hareketsizlik sayaci dolabilir; modem kapanabilir."
  exit 0
fi

echo "SONUC: FAIL — gateway uygulama yuku uretiyor; modem KAPANAMAZ."
echo "Kontrol edin:"
echo "  * /health -> session_policy / effective_session_policy"
echo "  * cihaz session_policy=smart|auto mu?"
echo "  * auto ise mod gozlendi mi (operation_mode)?"
echo "  * log: auto_policy_fallback (continuous'a dusulmus olabilir)"
echo "  * log: auto_classify_poll (oturum basina BIR kez BEKLENIR)"
exit 1
