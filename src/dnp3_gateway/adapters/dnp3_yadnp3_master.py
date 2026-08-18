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
import math
import threading
import time
from typing import Any

from dnp3_gateway.adapters.base import SignalReading, TelemetryReader
from dnp3_gateway.backend import DeviceConfig, SignalConfig
from dnp3_gateway.command_parameters import validate_command_parameters

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
_OBJECT_GROUP_DOUBLE_BIT_BINARY = 3
_OBJECT_GROUP_BINARY_OUTPUT = 10
_OBJECT_GROUP_COUNTER = 20
_OBJECT_GROUP_FROZEN_COUNTER = 21
_OBJECT_GROUP_ANALOG_INPUT = 30
_OBJECT_GROUP_ANALOG_OUTPUT = 40
_OBJECT_GROUP_STRING = 110

# Opsiyonel tipler: binding surumune gore bulunmayabilir; yoklugunda ilgili
# dal sessizce devre disi kalir (eskiden bu tipler HIC ele alinmiyordu).
_DOUBLE_BIT_CLS = getattr(opendnp3, "DoubleBitBinary", None) if _YADNP3_AVAILABLE else None
_FROZEN_COUNTER_CLS = getattr(opendnp3, "FrozenCounter", None) if _YADNP3_AVAILABLE else None


# ---- DNP3 kalite bayraklari -------------------------------------------------
# DNP3 her olcumu bir bayrak byte'i ile birlikte tasir. Gateway bu byte'i
# TAMAMEN YOK SAYIYORDU ve her degeri `quality="good"` olarak yayinliyordu:
# outstation bir noktayi ONLINE=0 (gecersiz), RESTART, LOCAL_FORCED (operator
# elle zorlamis), OVER_RANGE veya REFERENCE_ERR ile raporlasa bile SCADA bunu
# gecerli bir olcum sanıyordu.
#
# Somut ornek: outstation CT referansini kaybedip analog noktayi
# `value=0.0, flags=ONLINE|REFERENCE_ERR` raporluyor -> SCADA hat akimini 0 A
# kabul ediyor -> "hat enerjisiz" yorumu, yanlis alarm bastirma.
#
# BAYRAK BYTE'I TIPE GORE OKUNUR — 5, 6 ve 7. bitler object group'a gore
# TAMAMEN FARKLI anlam tasir. Bu ayrim yapilmadan (eski hali) bayrak yayina
# acildiginda sahayi bozacak iki somut hata vardi:
#
#   1. G3 Double-bit'te 0x40/0x80 KESICI POZISYONUDUR, kalite degil. Kutuphane
#      ile dogrulandi: DETERMINED_OFF -> flags=0x41, INDETERMINATE -> 0xC1.
#      Eski tip-kor esleme 0x41'i "REFERENCE_ERR" sanip **her ACIK kesiciyi**
#      `quality=invalid` yayinlardi.
#   2. G1/G10'da 0x20 OVER_RANGE degil CHATTER_FILTER / RESERVED1'dir;
#      G20/G21'de ROLLOVER'dir. Hicbiri "bu deger guvenilmez" demez.
#
# Asagidaki tablo yadnp3 3.2.1.1'in kendi enum'lariyla dogrulanmistir
# (`test_bayrak_tablosu_kutuphaneyle_uyumlu` bunu CI'da pinler).
#
#            0x20              0x40             0x80
#   G1   CHATTER_FILTER     RESERVED          STATE
#   G3   CHATTER_FILTER     STATE1            STATE2
#   G10  RESERVED1          RESERVED2         STATE
#   G20  ROLLOVER           DISCONTINUITY     RESERVED
#   G21  ROLLOVER           DISCONTINUITY     RESERVED
#   G30  OVERRANGE          REFERENCE_ERR     RESERVED
#   G40  OVERRANGE          REFERENCE_ERR     RESERVED
#   G110 (bayrak TASIMAZ — OctetString kalite byte'i olmadan cache'e yazilir)

# 0. - 4. bitler TUM gruplarda ayni anlami tasir.
_FLAG_ONLINE = 0x01
_FLAG_RESTART = 0x02
_FLAG_COMM_LOST = 0x04
_FLAG_REMOTE_FORCED = 0x08
_FLAG_LOCAL_FORCED = 0x10
_FLAG_FORCED = _FLAG_REMOTE_FORCED | _FLAG_LOCAL_FORCED

# Tipe gore degisen bitler.
_BIT5 = 0x20
_BIT6 = 0x40

#: Bit 5/6'nin "bu SAYIYA guvenme" anlamina geldigi gruplar (OVERRANGE /
#: REFERENCE_ERR). YALNIZCA analog gruplar. Diger gruplarda ayni bitler ya
#: durum bilgisidir (STATE1/STATE2), ya nokta davranisi hakkinda BILGILENDIRME
#: yapar (CHATTER_FILTER, ROLLOVER, DISCONTINUITY), ya da RESERVED'dir —
#: hicbiri olcumu gecersiz kilmaz. Bu bitler ham `dnp3_flags` byte'inda
#: korunur; backend isterse oradan decode eder.
_DEGER_BUTUNLUGU_BITLERI: dict[int, int] = {
    _OBJECT_GROUP_ANALOG_INPUT: _BIT5 | _BIT6,
    _OBJECT_GROUP_ANALOG_OUTPUT: _BIT5 | _BIT6,
}

#: Kalite bayragi TASIYAN gruplar. Bu kumede olan bir grup icin bayrak
#: okunamamissa bu bir ANOMALIDIR (bkz. `map_dnp3_quality`); kumede olmayan
#: gruplar (G110) icin bayragin yoklugu NORMALDIR.
_KALITE_TASIYAN_GRUPLAR: frozenset[int] = frozenset(
    {
        _OBJECT_GROUP_BINARY_INPUT,
        _OBJECT_GROUP_DOUBLE_BIT_BINARY,
        _OBJECT_GROUP_BINARY_OUTPUT,
        _OBJECT_GROUP_COUNTER,
        _OBJECT_GROUP_FROZEN_COUNTER,
        _OBJECT_GROUP_ANALOG_INPUT,
        _OBJECT_GROUP_ANALOG_OUTPUT,
    }
)


def _extract_flags(measurement: Any) -> int | None:
    """opendnp3 olcumunden ham bayrak byte'ini cikarir; okunamazsa None.

    Binding surumleri arasinda `flags` bir nesne (`.value`) ya da dogrudan int
    olabilir; ikisini de destekliyoruz.
    """
    flags = getattr(measurement, "flags", None)
    if flags is None:
        return None
    raw = getattr(flags, "value", flags)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# --- Cihazin kendi olay zaman damgasi (B2) ---------------------------------
#
# NEDEN GEREKLI: gateway `source_timestamp` olarak KENDI saatini basiyor ve
# bir cihazin tum sinyallerine ayni degeri veriyor. 4G link 10 dakika koparsa
# outstation olaylari KENDI damgalariyla event buffer'inda biriktirir; link
# gelince hepsi tek fragment'ta bosalir ve gateway hepsini ayni saniyeye
# damgalar. Ariza SURESI ve olay SIRASI kalici olarak kaybolur.
#
# NEDEN GUVENILMIYOR: saha cihazinin RTC pili biter ve cihaz 2000-01-01'den
# saymaya baslar. Boyle bir damgayi oldugu gibi kabul etmek, olay verisini
# 26 yil geriye tasimak demektir. Damga DOGRULANIYOR; gecmezse degeri
# ATMIYORUZ, yalnizca "bu damgaya guvenilmez" diyoruz.

#: Damga bu kadar GELECEKTE ise cihazin saati ileri kaymis demektir.
#: Kucuk bir tolerans: cihaz ve gateway saatleri birkac saniye ayrisabilir.
_DAMGA_GELECEK_TOLERANS_SN = 120.0

#: Damga bu kadar GERIDE ise guvenilmez. Event buffer'i pratikte saatler
#: tutar; 7 gun fazlasiyla comert. RTC'si olmus bir cihazin bildirdigi
#: 2000-01-01 bu pencerenin cok disinda kalir ve yakalanir.
_DAMGA_GECMIS_TOLERANS_SN = 7 * 24 * 3600.0


def _extract_device_time(measurement: Any) -> tuple[float | None, str | None]:
    """Olcumden (epoch_saniye, damga_kalitesi) cikarir.

    Donen kalite: "synchronized" | "unsynchronized" | "invalid" | None
      * None  -> cihaz damga GONDERMEDI (statik/Class 0 okumasi). Eksiklik
                 degil, o okumanin dogasi.
      * invalid -> damga var ama GUVENILMEZ (cihaz oyle dedi ya da deger
                   makul araligin disinda). `device_event_at` GONDERILMEZ.
    """
    ham = getattr(measurement, "time", None)
    if ham is None:
        return None, None

    # Binding surumleri arasinda API farkli: `.value` (ms) ya da dogrudan int.
    deger = getattr(ham, "value", ham)
    try:
        ms = int(deger)
    except (TypeError, ValueError):
        return None, None
    if ms <= 0:
        # Epoch 0 = "saat kurulmamis". Damga yokmus gibi davranmak yanlis
        # olurdu: cihaz bir sey SOYLEDI ve soyledigi sey gecersiz.
        return None, "invalid"

    epoch = ms / 1000.0

    # Cihazin KENDI beyani varsa once ona bak: opendnp3 damganin senkron
    # olup olmadigini bildirebiliyor.
    kalite_ham = getattr(ham, "quality", None)
    beyan = str(getattr(kalite_ham, "name", kalite_ham) or "").strip().upper()
    if beyan and ("INVALID" in beyan or "NOT_SYNC" in beyan or "UNSYNC" in beyan):
        # Cihaz "bu saate guvenme" diyor — makul araliktaysa bile.
        return epoch, "unsynchronized" if "SYNC" in beyan else "invalid"

    return epoch, "synchronized"


def dogrula_cihaz_zamani(
    epoch: float | None, kalite: str | None, simdi: float
) -> tuple[float | None, str | None]:
    """Damgayi makul araliga gore suzer. Donus: (kullanilacak_epoch, kalite).

    OLCUM ASLA ATILMAZ — yalnizca damga reddedilir. Reddedilen damga
    `device_event_at` olarak GONDERILMEZ; backend o okuma icin gateway
    saatine (`source_timestamp`) duser ve `timestamp_quality` neden
    duruldugunu soyler.
    """
    if epoch is None:
        return None, kalite
    if epoch > simdi + _DAMGA_GELECEK_TOLERANS_SN:
        return None, "invalid"
    if epoch < simdi - _DAMGA_GECMIS_TOLERANS_SN:
        # Klasik vaka: RTC pili bitmis cihaz 2000-01-01'den sayiyor.
        return None, "invalid"
    return epoch, kalite


def _zaman_kwargs(measurement: Any) -> dict[str, Any]:
    """`cache.set(**...)` icin damga alanlari. Hata HICBIR kosulda okuma
    yolunu dusurmemeli — damga bir ek bilgidir, olcumun kendisi degil."""
    try:
        epoch, kalite = _extract_device_time(measurement)
    except Exception:  # noqa: BLE001
        return {}
    return {"device_time": epoch, "time_quality": kalite}


def _double_bit_to_float(value: Any) -> float:
    """DoubleBit enum -> sayisal gosterim.

    DNP3 kodlamasiyla ayni: 0=INTERMEDIATE, 1=DETERMINED_OFF, 2=DETERMINED_ON,
    3=INDETERMINATE. Backend bu noktalari `analog` gibi tasiyabilsin diye
    float'a cevriliyor.
    """
    for attr in ("value", "name"):
        v = getattr(value, attr, None)
        if isinstance(v, int):
            return float(v)
    try:
        return float(int(value))
    except (TypeError, ValueError):
        return 0.0


def map_dnp3_quality(flags: int | None, object_group: int) -> str:
    """Ham DNP3 bayrak byte'ini gateway kalite sozlugune esler.

    `object_group` ZORUNLU: 5/6/7. bitler tipe gore farkli anlam tasidigi icin
    bayrak byte'i tipi bilinmeden okunamaz (bkz. yukaridaki tablo). Varsayilan
    deger BILEREK verilmedi — tip-kor bir cagri sessizce yanlis kalite uretir.

    Donen sozluk backend'in v2.28.0'dan beri tanidigi 5 token ile SINIRLIDIR:
      `good | invalid | restart | forced | comm_lost`
    Yeni token EKLENMEZ; kapsam (nokta/cihaz) ayrimini govdedeki `dnp3_flags`
    alaninin VARLIGI belirler (bkz. `poller.build_telemetry_payload`).

    Oncelik sirasi en kotu durumdan iyiye:
      comm_lost > restart > invalid (ONLINE yok / OVER_RANGE / REFERENCE_ERR)
      > forced (LOCAL/REMOTE_FORCED) > good

    `comm_lost` EN USTTE kalir: backend'de cihaz seviyesine kilitli tek
    kalitedir. Altina cekilirse gercekten olu bir cihaz "online ama bir noktasi
    bozuk" gorunurdu.

    Bayrak yoklugu (`flags is None`) TIPE GORE degerlendirilir:
      * G110 (OctetString) -> `good`. Bu tip kalite byte'i TASIMAZ; seri no /
        IMEI / firmware noktalari bayraksiz cache'e yazilir. Bunlari `invalid`
        yapmak, bayrak yayina acildigi anda tum string noktalarini bozardi.
      * Kalite tasiyan gruplar -> `invalid` (FAIL-SAFE). Bayragi okunamamis bir
        olcumu `good` saymak, tam da kapatmaya calistigimiz hatanin ta kendisi
        olurdu: desteksiz bir "bu deger saglam" iddiasi. Bu yol yadnp3
        3.2.1.1'de ULASILAMAZ (yedi kalite tipinin hepsi `.flags` tasiyor);
        binding degisirse diye duran bilincli bir savunmadir ve cagri yerinde
        nokta basina bir kez WARNING loglanir.
      * Bilinmeyen grup -> yalnizca ortak bitler degerlendirilir; 5/6. bitlerin
        anlami bilinmediginden onlardan `invalid` URETILMEZ (uydurma kalite
        vermektense ham byte'i backend'e birakiyoruz).

    Bkz. docs/BACKEND_TODO.md#B1 ve `DNP3_PUBLISH_QUALITY_FLAGS` env bayragi.
    """
    if object_group == _OBJECT_GROUP_STRING:
        # G110 hicbir kosulda kalite byte'i tasimaz.
        return "good"
    if flags is None:
        return "invalid" if object_group in _KALITE_TASIYAN_GRUPLAR else "good"
    if flags & _FLAG_COMM_LOST:
        return "comm_lost"
    if flags & _FLAG_RESTART:
        return "restart"
    if not (flags & _FLAG_ONLINE):
        return "invalid"
    if flags & _DEGER_BUTUNLUGU_BITLERI.get(object_group, 0):
        return "invalid"
    if flags & _FLAG_FORCED:
        return "forced"
    return "good"


# Recovery doğrulama suresi: OnOpen / stale-edge sonrasi cihazdan fresh frame
# beklemek icin maksimum sure. Bu surede frame gelmezse cihaz tekrar "lost"
# kabul edilir ve SCADA "comm_lost" gormeye devam eder. 15 sn:
#   - Class 0+1+2+3 integrity poll cevabi normal kosullarda <2 sn doner.
#   - 4G/yavas linkler icin ust sinir 5-8 sn.
#   - 15 sn TCP-up + DNP3-down sahte recovery'yi yakalamak icin yeterince agresif,
#     ancak gercek yavas linkleri yanlislikla "lost" yapmaktan korur.
_RECOVERY_GRACE_SEC = 15.0

# KOPUK CIHAZI KENDILIGINDEN YOKLAMA
# ----------------------------------
# SAHADA GOZLENDI: "bazen haberlesme gidiyor, manuel refresh atinca geliyor."
#
# Otomatik integrity poll YALNIZCA iki durumda tetikleniyordu: stale-edge'de
# (online -> recovering) TEK SEFER, ve relink'te (lost -> recovering) ama
# yalnizca veri ZATEN geliyorsa. Yani "cihaz lost + link acik gorunuyor +
# veri gelmiyor" durumunda gateway cihaza HICBIR SEY SORMUYOR; ya TCP'nin
# kopup yeniden acilmasini (OnOpen) ya da verinin kendiliginden gelmesini
# PASIF bekliyordu. Kilidi kiran tek sey operatorun `/refresh-all`'u idi.
#
# Bu senaryo 4G/GSM hatlarda kural: modem RRC idle'a duser, TCP soketi
# FIN/RST uretmeden yari-acik kalir, opendnp3 OnClose gormez, outstation da
# unsolicited veri gondermeyi keser. Iki taraf da sessizdir ve kimse ilk
# hamleyi yapmaz — cihaz sonsuza kadar 'lost' kalir.
#
# Cozum iki katmanli:
#   1) lost + link acik iken PERIYODIK integrity poll (manuel refresh'in
#      yaptiginin aynisi). Sessiz outstation'i konusturur.
#   2) Yoklama sonuc vermezse oturumu ZORLA yeniden kur. Yari-acik sokete
#      poll gondermek ise yaramaz; tek cikis kanali kapatip yeniden acmaktir.
#
# 30 sn: 4G'de RRC idle -> aktif gecisi saniyeler surer, 30 sn cihaz basina
# saatte 120 istek demektir (500 cihazda hepsi kopuk olsa bile ihmal
# edilebilir). Daha sik yapmak kopuk sahada gereksiz radyo trafigi uretir.
_LOST_PROBE_INTERVAL_SEC = 30.0

# Kac sonucsuz yoklamadan sonra TCP oturumu yeniden kurulur.
# 3 x 30 sn = ~90 sn: gercekten yavas bir cihaza sans tanir, ama olu bir
# soketle dakikalarca oyalanmaz.
_LOST_RELINK_AFTER_PROBES = 3

# TAZE OTURUMU YIKMA — sahada olculdu (2026-08-11).
# Zorla relink, ACIK olan TCP oturumunu kapatir. Yerel agda yeniden baglanma
# milisaniyeler surer; 4G/GSM hatta ise ~3 DAKIKA surdu (11:26:37 master
# enable -> 11:29:23 link open). Yani yavas hatta relink toparlanmayi
# HIZLANDIRMIYOR, GECIKTIRIYOR. Daha kotusu dongu kuruyordu:
#   link acilir -> 15sn grace dolar -> lost -> 90sn yoklama -> relink ->
#   3 dk yeniden baglanma -> ayni yerden basa
# Cihaz hicbir zaman kararli bir pencere bulamiyor. Bu yuzden oturum en az
# bu kadar sure ACIK KALMADAN yikilmaz; o sureye kadar yalnizca yoklanir.
_LOST_RELINK_MIN_LINK_AGE_SEC = 300.0

# Ard arda relink'ler arasinda ustel bekleme. Ilk relink sonrasi bu kadar,
# sonra iki kati... tavana kadar. Relink ise yaramiyorsa (cihaz gercekten
# kapali) saniyede bir TCP acip kapatmanin anlami yok; modem/hat uzerinde
# gereksiz yuk yaratir.
_LOST_RELINK_BACKOFF_BASE_SEC = 300.0
_LOST_RELINK_BACKOFF_MAX_SEC = 3600.0

# Komut gonderildikten sonra bu sure boyunca oturum YIKILMAZ. CROB gorevi
# master uzerinde asenkron kosar; ortasinda `shutdown()` cagrilirsa gorev
# cevap alamadan olur ve sonuc `CommandStatus.UNDEFINED` doner. Poll thread'i
# ile komut yolu arasinda baska bir koordinasyon yok.
_LOST_RELINK_COMMAND_GRACE_SEC = 60.0

# BAYATLIK ESIGI — "bu sure boyunca hic ses cikmadiysa gercekten kopuktur".
# Cok agresif olursa baseline scan'in dogal jitter'inde bile false comm_lost
# uretir (60sn'lik scan icin 60sn esik sik tetikliyordu); 4 kat tolerans
# birakilir. Sabitler MODUL SEVIYESINDE cunku (a) esik iki yerde gerekiyor
# — bayatlik karari ve komut yolunun ulasilabilirlik olcutu — ve ayri ayri
# hesaplanip birbirinden kaymamalari sart, (b) testler gercek DNP3 trafigiyle
# kopma senaryosunu makul surede kosabilmeli.
_STALE_BASELINE_MULT = 4
_STALE_SCAN_MULT = 10
_STALE_MIN_THRESHOLD_SEC = 60


def _bayatlik_esigi_sn(scan_interval_sec: float, baseline_interval_sec: float) -> float:
    """Bayatlik/ulasilabilirlik esigi (saniye) — TEK kaynak."""
    return float(
        max(
            baseline_interval_sec * _STALE_BASELINE_MULT,
            scan_interval_sec * _STALE_SCAN_MULT,
            _STALE_MIN_THRESHOLD_SEC,
        )
    )


# OLCUM SESSIZLIGI YOKLAMASI — "kopuk deme, SOR".
# Cihaz cevap veriyor (kanit taze) ama olcum tablosu bayat kaldiysa periyodik
# integrity poll gonderilir. Bu, eskiden sahte comm_lost URETEN kosulun
# yerine gecen hamledir: ayni belirtiye artik "kopuk ilan et" yerine
# "tekrar sor" ile cevap veriyoruz.
#
# 60 sn: durgun bir fiderde bu kosul KALICIDIR (hicbir deger degismez), yani
# maliyeti surekli odenir. 500 cihazda 60 sn = saniyede ~8 integrity poll —
# 2 cekirdekli saha kutusunun ve 4G hattinin kaldirabilecegi bir tempo.
# Daha sik yapmak durgun sahada hicbir yeni bilgi getirmez.
_DATA_SILENCE_POLL_INTERVAL_SEC = 60.0

# G110 tarama tekrar denemesi. Ad-hoc ScanRange istegi DUSEBILIR ve binding
# exception atmadigi icin bunu anlayamayiz; basari, istegin gonderilmesiyle
# degil cihazdan gercekten bir string gelmesiyle olculur. Gelmezse:
# 15, 30, 60, 120, 240 sn ... tavana kadar.
_G110_BACKOFF_BASE_SEC = 15.0
_G110_BACKOFF_MAX_SEC = 240.0
# Deneme tavani. Ulasilirsa BIR KEZ WARNING basilir; periyodik scan'e
# donusmemesi icin sinirli.
_G110_MAX_DENEME = 6


# G110 (Octet String) okuma bloklarinin turetilmesi
# -------------------------------------------------
# Aralilar eskiden SINIF SABITI idi: ((3, 23), (65000, 65020)) — SN2.0'in
# index haritasi. Ayni gateway'e Pole Master Kit eklendiginde o cihazin 54
# string'inin 30'u HIC istenmiyordu (SIM CCID, sebeke bilgileri, sat04-sat09).
# Artik her cihazin KENDI sinyal setinden turetiliyor.
#
# Bitisik olmayan index'ler bloklara gruplanir: iki index arasindaki mesafe
# bu esikten kucuk/esitse ayni bloga girer. Amac tek tek yuzlerce istek
# yerine birkac dar blok gondermek; 8 SN2.0'in gercek bosluklarini
# (3->5, 17->19, 65003->65009, 65014->65020) kapsayacak kadar genis,
# 23 -> 65000 gibi ucurumlari birlestirmeyecek kadar dar.
_G110_BLOK_BOSLUK = 8

# Tek bir blogun ust genislik siniri. 0-65535 gibi genis tek aralik ASLA
# istenmemeli — Mayis 2026'da cihazi bozdu (revert 1302b83). Blok gruplama
# zaten bunu engelliyor; bu sinir ikinci baraj: hesaplanan blok bu genisligi
# asarsa parcalanir.
_G110_BLOK_MAX_GENISLIK = 512

# Toplam blok sayisi kapagi: her blok ayri bir DNP3 read task'i demek.
# Asilirsa fazlasi kirpilir ve WARNING atilir (sessiz kirpma olmaz).
_G110_MAX_BLOK = 16


def _g110_bloklari(
    signals: list[SignalConfig] | None,
    *,
    device_code: str = "",
) -> tuple[tuple[int, int], ...]:
    """Cihazin G110 sinyallerinden dar okuma bloklari uret.

    Girdi `read_device`in zaten aldigi sinyal listesidir; ekstra veri
    gerekmez. G110 sinyali olmayan cihaz icin bos demet doner ve hicbir
    ScanRange istegi gonderilmez.
    """
    indeksler = sorted(
        {
            int(s.dnp3_index)
            for s in (signals or [])
            if int(s.dnp3_object_group or 0) == 110 and s.dnp3_index is not None
        }
    )
    if not indeksler:
        return ()

    bloklar: list[tuple[int, int]] = []
    bas = son = indeksler[0]
    for i in indeksler[1:]:
        if i - son <= _G110_BLOK_BOSLUK:
            son = i
        else:
            bloklar.append((bas, son))
            bas = son = i
    bloklar.append((bas, son))

    # Genislik bariyeri: bir blok tavani asarsa parcala (nokta kaybetmeden).
    parcali: list[tuple[int, int]] = []
    for bas, son in bloklar:
        if son - bas + 1 <= _G110_BLOK_MAX_GENISLIK:
            parcali.append((bas, son))
            continue
        logger.warning(
            "yadnp3_g110_blok_genis device=%s blok=%s-%s genislik=%s max=%s — parcalaniyor",
            device_code,
            bas,
            son,
            son - bas + 1,
            _G110_BLOK_MAX_GENISLIK,
        )
        p = bas
        while p <= son:
            parcali.append((p, min(son, p + _G110_BLOK_MAX_GENISLIK - 1)))
            p += _G110_BLOK_MAX_GENISLIK

    if len(parcali) > _G110_MAX_BLOK:
        logger.warning(
            "yadnp3_g110_blok_sayisi device=%s blok=%d max=%d — fazlasi kirpildi; "
            "sinyal setindeki index dagilimi cok parcali olabilir",
            device_code,
            len(parcali),
            _G110_MAX_BLOK,
        )
        parcali = parcali[:_G110_MAX_BLOK]
    return tuple(parcali)


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
        # (group, index) -> (raw_float, value_string_or_None, dnp3_flags_or_None)
        self._values: dict[tuple[int, int], tuple[float, str | None, int | None]] = {}
        # (group, index) -> surum sayaci. `set()` her DEGER DEGISIKLIGINDE
        # artirir. Poller yayini onaylarken (`commit_published`) okudugu surumu
        # geri verir; surum degismisse bayrak TEMIZLENMEZ — yani okuma ile
        # yayin arasinda gelen yeni deger kaybolmaz (TOCTOU korumasi).
        self._versions: dict[tuple[int, int], int] = {}
        self._version_seq: int = 0
        # Yayinlanmayi bekleyen (degismis) sinyaller. Bayrak SADECE yayin
        # kalicilastiktan sonra, commit_published ile temizlenir.
        self._dirty: set[tuple[int, int]] = set()
        # Outstation IIN bayraklarinin son gorulen hali (edge-trigger log icin).
        self._iin_flags: dict[str, bool] = {}
        self._connected = False
        # Mevcut link oturumunun acilis damgasi (monotonic). Taze bir oturumu
        # zorla yikmamak icin okunur; bkz. `link_age`.
        self._link_opened_at = 0.0
        # G110 (string) okuma durumu — bkz. `g110_iste` / `g110_gerekli`.
        self._g110_bekliyor = False
        self._g110_geldi = False
        self._g110_deneme = 0
        self._g110_sonraki_at = 0.0
        # SURE OLCUMLERI MONOTONIC SAATLE YAPILIR.
        #
        # Bu alan "en son ne zaman frame geldi" sorusunu cevapliyor ve
        # `read_device` bundan bayatlik (stale) karari uretiyor. Duvar saatiyle
        # olculdugunde bir NTP siçramasi — ya da RTC'si bos bir sahada acilista
        # yapilan saat duzeltmesi — `now - last_update` degerini birden
        # devasa yapar ve gateway TUM cihazlari ayni anda comm_lost ilan eder.
        # 300 cihazli bir sahada bu, SCADA'da toplu "haberlesme yok" dalgasi ve
        # ardindan toplu recovery yayini demektir; hicbir cihazda gercek bir
        # sorun yokken. Monotonic saat NTP'den etkilenmez.
        self._last_update_at: float = 0.0
        # Duvar saati karsiligi — YALNIZCA gosterim icin (`/health`
        # `last_frame_epoch`). Karar mekanizmasinda KULLANILMAZ.
        self._last_update_wall: float = 0.0
        # KANIT SAATI — "cihaz konusuyor mu?" (VERI GELDI MI DEGIL)
        # ---------------------------------------------------------
        # SAHA ARIZASI (SN2.0, 2026-08): bir arıza gostergesi saatlerce ayni
        # degerleri tasiyabilir. event-driven modda degismeyen deger YAYIN
        # URETMEZ, dolayisiyla `_last_update_at` ilerlemez ve gateway ayakta
        # olan cihazi `comm_lost` ilan ederdi. Sonuclari uc katliydi:
        #   1) SCADA'da sahte "haberlesme koptu",
        #   2) `read_device` tum sinyalleri 0.0/comm_lost'a cevirdigi icin
        #      GERCEK bir alarm degeri yayina hic cikmiyordu,
        #   3) `operate_crob` state != online diye KESICI KOMUTUNU reddediyordu.
        # Yani "deger degismedi" sessizce "kumanda edemiyorum"a donusuyordu.
        #
        # Oysa DNP3 canliligi ZATEN soyluyor ve biz kanitlari atiyorduk:
        #   * outstation'in HER uygulama yaniti bir IIN tasir (OnReceiveIIN),
        #   * her tamamlanan gorev SUCCESS/FAILURE olarak sonuclanir
        #     (OnTaskComplete) — bos donen bir Class 1/2/3 event scan'i bile
        #     SUCCESS'tir ve cihazin cevap verdiginin ispatidir.
        # Olculdu (gercek DNP3 loopback, sessiz outstation): saniyede bir
        # `USER_TASK/SUCCESS` + IIN geliyor, SIFIR veri noktasiyla.
        #
        # Bu yuzden canlilik AYRI bir saatte tutulur. `set()` bu saati de
        # besledigi icin DAIMA `kanit_yasi <= veri_yasi`; yani bu ayrim var
        # olan bir kopma tespitini GECIKTIREBILIR ama YENI bir sahte kopma
        # URETEMEZ. Gercek kopmada (yari-acik soket, yanlis master adresi)
        # ne IIN gelir ne de gorev SUCCESS doner — kanit saati de bayatlar ve
        # mevcut lost/probe/relink kendiliginden isler.
        #
        # Monotonic: NTP siçramasi tum sahayi ayni anda "kopuk" yapmasin.
        self._last_evidence_at: float = 0.0
        # Cihaz stale/disconnected oldugunda comm_lost yayinini SADECE bir kez
        # yapmak icin edge-trigger flag'i.
        #
        # DIKKAT — bu bayrak YAYIN ONAYINDAN SONRA set edilir (bkz.
        # confirm_stale_announced). Okuma aninda set edilseydi ve o yayin
        # kalicilastirilamasaydi (disk dolu -> OutboxFullError) cihaz SCADA'da
        # SONSUZA KADAR son iyi degeriyle canli gorunurdu: bir sonraki cycle
        # "zaten duyurdum" deyip `no_change` uretir, `no_change` yayinlanmaz.
        # Kopmus bir gostergenin canli gorunmesi, degerlerin geç gelmesinden
        # cok daha tehlikelidir.
        self._stale_announced: bool = False
        # Duyuru "kusagi". Cihaz toparlandiginda artar; boylece onceki kopma
        # epizoduna ait gecikmis bir yayin onayi YENI epizodu duyurulmus
        # saymaz (peek/commit surum kontrolunun ayni mantigi).
        self._stale_gen: int = 0
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

    def set(
        self,
        group: int,
        index: int,
        raw: float,
        value_string: str | None = None,
        *,
        flags: int | None = None,
        device_time: float | None = None,
        time_quality: str | None = None,
    ) -> None:
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
            if group == _OBJECT_GROUP_STRING:
                # G110 VARIS ONAYI: tarama istegi gonderilmis olabilir ama
                # DUSMUS de olabilir ve `ScanRange()` exception atmadigi icin
                # bunu anlayamayiz. Bu yuzden basariyi ISTEK degil GELEN
                # DEGER tanimlar; bayrak burada temizlenir, tekrar deneme durur.
                self._g110_geldi = True
                self._g110_bekliyor = False
            prev = self._values.get(key)
            self._values[key] = (raw, value_string, flags, device_time, time_quality)
            now = time.monotonic()
            self._last_update_at = now
            self._last_update_wall = time.time()
            # Veri gelmesi en guclu canlilik kanitidir; kanit saatini de besler.
            # Bu sayede `kanit_yasi <= veri_yasi` degismezligi korunur.
            self._last_evidence_at = now
            # Kalite bayragi degisimi de bir DEGISIKLIKTIR: deger ayni kalsa
            # bile nokta 'gecerli'den 'REFERENCE_ERR'e gecmisse SCADA bunu
            # gormeli.
            if prev is None or prev[0] != raw or prev[1] != value_string or prev[2] != flags:
                self._dirty.add(key)
                # Surumu ilerlet: poller okudugu surumu commit_published ile
                # geri verir. Okuma ile yayin arasinda buraya yeni bir deger
                # gelirse surum uyusmaz ve bayrak temizlenmez — eski degerin
                # yayinlanmasi yeni degeri KAYBETMEZ.
                self._version_seq += 1
                self._versions[key] = self._version_seq
            # Recovery confirmation: cihaz "recovering" durumundaysa ve bu
            # frame OnOpen/stale-edge anchor'indan sonra geldiyse, bu cihazin
            # gercekten DNP3 cevapladiginin ispatidir → online'a yukselt ve
            # **bu noktada tum bilinen sinyalleri dirty isaretle** ki bir
            # sonraki read_device cycle'inda (en gec scan_interval_sec sonra)
            # hepsi quality=good ile yayinlansin. Onceden burada sadece
            # _pending_recovery_publish=True isaretliyordum ve read_device
            # mark_all_dirty cagiriyordu; ama integrity poll cevap geldiginde
            # binlerce sinyal pesi sira cache.set ile yazilir, herbiri sadece
            # KENDI key'ini dirty yapar. Recovery confirm aninda mark_all_dirty
            # cagrarak son bilinen TUM degerlerin yayinlanmasini garantiliyoruz
            # (bazi sinyaller hala eski deger olabilir, integrity poll'un
            # devami gelene kadar zaman gecmis olabilir).
            if self._state == "recovering" and now >= self._recovery_anchor_at:
                self._state = "online"
                self._pending_recovery_publish = True
                # Tum cache'i dirty isaretle (surumleri de ilerleterek).
                # Boylece bir sonraki cycle'da butun sinyaller yayinlanir.
                self._mark_all_dirty_unsafe()

    def get(self, group: int, index: int) -> tuple[float, str | None, int | None] | None:
        with self._lock:
            return self._values.get((group, index))

    def is_dirty(self, group: int, index: int) -> bool:
        with self._lock:
            return (group, index) in self._dirty

    def clear_dirty(self, group: int, index: int) -> None:
        with self._lock:
            self._dirty.discard((group, index))

    # ---- Atomik okuma / yayin onayi ------------------------------------------

    def peek_if_dirty(
        self, group: int, index: int
    ) -> tuple[float, str | None, int | None, int, float | None, str | None] | None:
        """Degismisse (deger, metin, bayrak, surum, cihaz_zamani, damga_kalitesi)
        doner; DEGISTIRMEZ.

        TEK lock altinda calisir. Eski kod ayni is icin lock'u UC KEZ aliyordu
        (`get` -> `is_dirty` -> `clear_dirty`) ve aralarda DNP3 IO thread'i
        yazabildigi icin yeni degerler kaliciyla kayboluyordu.

        Bayrak burada TEMIZLENMEZ; temizleme yayin kalicilastiktan sonra
        `commit_published` ile yapilir.
        """
        key = (group, index)
        with self._lock:
            if key not in self._dirty:
                return None
            entry = self._values.get(key)
            if entry is None:
                return None
            return (
                entry[0],
                entry[1],
                entry[2],
                self._versions.get(key, 0),
                entry[3] if len(entry) > 3 else None,
                entry[4] if len(entry) > 4 else None,
            )

    def commit_published(self, items: list[tuple[int, int, int]]) -> int:
        """Yayini onayla: surum HALA AYNIYSA dirty bayragini temizle.

        `items`: (group, index, yayinlanan_surum) uclulari.
        Surum degismisse (arada yeni olcum geldi) bayrak KORUNUR ve yeni deger
        bir sonraki cycle'da yayinlanir. Donus: temizlenen bayrak sayisi.
        """
        cleared = 0
        with self._lock:
            for group, index, version in items:
                key = (group, index)
                if key not in self._dirty:
                    continue
                if self._versions.get(key, 0) != version:
                    # Okuma ile yayin arasinda yeni deger geldi — bayragi
                    # temizleme, yeni deger yayinlansin.
                    continue
                self._dirty.discard(key)
                cleared += 1
        return cleared

    def dirty_count(self) -> int:
        with self._lock:
            return len(self._dirty)

    # ---------------------------------------------------------------
    # comm_lost duyurusu — okuma/onay ayrimi
    # ---------------------------------------------------------------

    def begin_stale_announce(self) -> tuple[bool, tuple[str, int]]:
        """comm_lost yayinina hazirlan.

        Doner: `(zaten_duyuruldu, jeton)`. Jeton yayin kalicilastiktan sonra
        `confirm_stale_announced`'a geri verilir. Bayrak BURADA set EDILMEZ —
        yayin basarisiz olursa cihaz sessizce "duyuruldu" sayilmamali.
        """
        with self._lock:
            return self._stale_announced, ("stale", self._stale_gen)

    def confirm_stale_announced(self, token: Any) -> bool:
        """Yayin kalicilastiktan sonra duyuruyu isaretle.

        Jeton eski bir kusaga aitse (arada cihaz toparlandi) yok sayilir:
        yeni bir kopma yasanirsa SCADA yine tek bir comm_lost gorur.
        """
        if not (isinstance(token, tuple) and len(token) == 2 and token[0] == "stale"):
            return False
        with self._lock:
            if token[1] != self._stale_gen or self._stale_announced:
                return False
            self._stale_announced = True
            return True

    def reset_stale_announce(self) -> None:
        """Cihaz toparlandi: bir sonraki kopma tekrar duyurulabilsin."""
        with self._lock:
            self._stale_announced = False
            self._stale_gen += 1

    def note_iin(self, name: str, value: bool) -> bool:
        """IIN bayragini kaydet; DURUM DEGISTIYSE True doner (edge-trigger).

        IIN her outstation yanitinda gelir; degisiklik olmadan loglamak
        saniyede onlarca satir uretir. Bu yuzden yalnizca gecislerde log atariz.
        """
        with self._lock:
            prev = self._iin_flags.get(name)
            self._iin_flags[name] = value
            return prev != value

    def iin_snapshot(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._iin_flags)

    def _mark_all_dirty_unsafe(self) -> int:
        """Tum kayitlari dirty isaretle + SURUMLERINI ILERLET.

        Surum ilerletmek sart: o sirada uctan uca devam eden bir yayin
        (`read_device` -> publish -> `commit_published`) eski surumle geri
        donup bu yeni dirty bayragini temizleyebilirdi — yani recovery
        sonrasi toplu yeniden yayin sessizce iptal olurdu.
        Caller'in lock'u tutuyor olmasi gerekir.
        """
        count = 0
        for key in self._values:
            self._dirty.add(key)
            self._version_seq += 1
            self._versions[key] = self._version_seq
            count += 1
        return count

    def mark_all_dirty(self) -> int:
        """Cache'deki tum (group, index) ciftlerini dirty isaretler.

        Recovery anlik kullanim: cihaz iletisimi koptuktan sonra geri geldiginde
        cihazin sinyal degerleri ayni kalmis olabilir; saf event-driven mantikta
        hicbir sinyal "degisti" olmadigindan publish edilmez ve dis SCADA
        cihazi hala "comm_lost" gorur. Bu method cagrilirsa bir sonraki
        read_device cycle'inda son bilinen tum degerler `quality=good` ile
        yayinlanir → tag-engine → outbound (IEC 104, REST, MQTT) → SCADA
        cihazi tekrar "online" gorur. Donus: dirty isaretlenen kayit sayisi.

        Ayni mekanizma operator tetikli "tum cihazlara sorgu at" (refresh-all)
        icin de kullanilir; delta-only yayinin TEK telafi mekanizmasidir.
        """
        with self._lock:
            return self._mark_all_dirty_unsafe()

    # ---- G110 (string) okuma durumu -------------------------------------
    # NEDEN BAYRAK: tarama istegi `OnOpen` icinden yapilamaz — o callback
    # opendnp3'un IO thread'inden gelir ve oradan ScanRange cagirmak riskli.
    # OnOpen yalnizca "istendi" der; gercek istegi poll thread'i (read_device)
    # gonderir.
    #
    # NEDEN TEKRAR DENEME: ad-hoc ScanRange istegi dusebilir ve `ScanRange()`
    # exception atmadigi icin bunu anlayamayiz. Bu yuzden basari, ISTEGIN
    # GONDERILMESIYLE degil cihazdan GERCEKTEN bir G110 degeri gelmesiyle
    # olculur (`set()` icinde isaretlenir). Gelmezse ustel backoff ile
    # yeniden denenir.
    def g110_iste(self) -> None:
        """Link acildi — string okumasi (yeniden) istensin."""
        with self._lock:
            self._g110_bekliyor = True
            self._g110_geldi = False
            self._g110_deneme = 0
            self._g110_sonraki_at = 0.0

    def g110_gerekli(self) -> bool:
        """SU AN bir tarama istegi gonderilmeli mi? (deneme sayacini ilerletir)

        True donerse caller `scan_g110_once()` cagirmali. Backoff dolmadiysa
        ya da deger zaten geldiyse False doner.
        """
        with self._lock:
            if not self._g110_bekliyor or self._g110_geldi:
                return False
            simdi = time.monotonic()
            if simdi < self._g110_sonraki_at:
                return False
            if self._g110_deneme >= _G110_MAX_DENEME:
                return False
            self._g110_deneme += 1
            # 15, 30, 60, 120... sn — tavana kadar.
            gecikme = min(
                _G110_BACKOFF_MAX_SEC,
                _G110_BACKOFF_BASE_SEC * (2 ** (self._g110_deneme - 1)),
            )
            self._g110_sonraki_at = simdi + gecikme
            return True

    def g110_durum(self) -> tuple[bool, bool, int]:
        """(bekliyor, geldi, deneme) — teshis/test icin."""
        with self._lock:
            return self._g110_bekliyor, self._g110_geldi, self._g110_deneme

    def g110_tukendi_mi(self) -> bool:
        """Deneme tavanina ulasildi ve hala deger gelmedi mi?"""
        with self._lock:
            return self._g110_bekliyor and not self._g110_geldi and self._g110_deneme >= _G110_MAX_DENEME

    def link_age(self) -> float:
        """Mevcut link oturumu kac saniyedir acik? Kapaliysa -1.

        Zorla relink karari bunu okur: TAZE kurulmus bir oturumu yikmak
        yavas hatlarda toparlanmayi HIZLANDIRMAZ, geciktirir (4G'de yeniden
        baglanma ~3 dakika surebiliyor).
        """
        with self._lock:
            if not self._connected or self._link_opened_at == 0.0:
                return -1.0
            return time.monotonic() - self._link_opened_at

    def set_connected(self, ok: bool) -> None:
        with self._lock:
            if ok and not self._connected:
                self._link_opened_at = time.monotonic()
            elif not ok:
                self._link_opened_at = 0.0
                # Link koptu: string'ler statik ama cihaz degistirilmis /
                # konfigi guncellenmis olabilir. Sonraki OnOpen yeniden
                # tetiklesin.
                self._g110_bekliyor = False
                self._g110_geldi = False
                self._g110_deneme = 0
                self._g110_sonraki_at = 0.0
            self._connected = ok
            # Link tamamen koptu → state'i lost'a dusur. recovery flag'lerini
            # temizle ki sonraki OnOpen yeniden recovering tetiklesin.
            if not ok:
                self._state = "lost"
                self._recovery_started_at = 0.0
                self._recovery_anchor_at = 0.0
                self._pending_recovery_publish = False
                # Kanit da GECERSIZ: link kapandiysa onceki yanitlar artik
                # "cihaz ulasilabilir" demez. Sifirlanmasaydi kopmus bir cihaz
                # kanit esigi dolana kadar komut kabul ediyor gorunurdu.
                self._last_evidence_at = 0.0

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def size(self) -> int:
        with self._lock:
            return len(self._values)

    def last_update_at(self) -> float:
        """Son frame'in MONOTONIC damgasi. Bayatlik karari icin kullanilir."""
        with self._lock:
            return self._last_update_at

    def last_update_wall(self) -> float:
        """Son frame'in duvar saati karsiligi — yalnizca gosterim icin."""
        with self._lock:
            return self._last_update_wall

    def note_evidence(self) -> None:
        """Outstation'dan CANLILIK KANITI geldi (veri gelmis olmasi sart degil).

        Cagiranlar opendnp3 IO thread'indeki iki callback:
          * `OnReceiveIIN`   — her uygulama yanitinda gelir,
          * `OnTaskComplete` — YALNIZCA `result == SUCCESS` oldugunda.

        Basarisiz gorevler (RESPONSE_TIMEOUT / NO_COMMS) BILEREK kanit
        sayilmaz; ayrica ASSIGN_CLASS / ENABLE_UNSOLICITED gibi cihazin
        desteklemedigi gorevler saglikli sahada da FAILURE_BAD_RESPONSE
        dondurebilir — onlari "kopuk" delili saymak da yanlis olurdu.
        Bu yuzden kural tek yonlu: yalnizca BASARI kanittir.
        """
        now = time.monotonic()
        with self._lock:
            self._last_evidence_at = now

    def last_evidence_at(self) -> float:
        """Son canlilik kanitinin MONOTONIC damgasi (0.0 = hic kanit yok)."""
        with self._lock:
            return self._last_evidence_at

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
        # Monotonic: grace period bir SUREDIR; NTP siçramasi onu bitirmemeli
        # (ya da sonsuza uzatmamali).
        now = time.monotonic()
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
            return time.monotonic() - self._recovery_started_at

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

    # Ilk basarili G110 okumasini bir kez INFO'ya basmak icin (bkz. Process).
    g110_bildirildi = False

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
            nonlocal g110_bildirildi
            if not values:
                return
            first = values[0].value
            try:
                if isinstance(first, opendnp3.Binary):
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_BINARY_INPUT,
                            it.index,
                            1.0 if it.value.value else 0.0,
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif isinstance(first, opendnp3.Analog):
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_ANALOG_INPUT,
                            it.index,
                            float(it.value.value),
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif isinstance(first, opendnp3.Counter):
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_COUNTER,
                            it.index,
                            float(it.value.value),
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif isinstance(first, opendnp3.BinaryOutputStatus):
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_BINARY_OUTPUT,
                            it.index,
                            1.0 if it.value.value else 0.0,
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif isinstance(first, opendnp3.AnalogOutputStatus):
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_ANALOG_OUTPUT,
                            it.index,
                            float(it.value.value),
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif _DOUBLE_BIT_CLS is not None and isinstance(first, _DOUBLE_BIT_CLS):
                    # G3 Double-bit Binary Input — kesici pozisyonu icin DNP3'un
                    # standart tipidir (INTERMEDIATE / DETERMINED_OFF /
                    # DETERMINED_ON / INDETERMINATE). Eskiden bu tip SESSIZCE
                    # dusuruluyordu: cihaz kesici pozisyonunu G3 ile raporlarsa
                    # nokta backend'e HIC gelmiyor, operator eksikligi hicbir
                    # logdan anlayamiyordu.
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_DOUBLE_BIT_BINARY,
                            it.index,
                            _double_bit_to_float(it.value.value),
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif _FROZEN_COUNTER_CLS is not None and isinstance(first, _FROZEN_COUNTER_CLS):
                    for it in values:
                        cache.set(
                            _OBJECT_GROUP_FROZEN_COUNTER,
                            it.index,
                            float(it.value.value),
                            flags=_extract_flags(it.value),
                            **_zaman_kwargs(it.value),
                        )
                elif isinstance(first, opendnp3.OctetString):
                    for it in values:
                        try:
                            raw = bytes(it.value.ToBytes())
                        except Exception:  # noqa: BLE001
                            raw = b""
                        # Bos/bos-baslangic byte'lari NUL'lardan temizle, UTF-8 dene
                        text = raw.rstrip(b"\x00").decode("utf-8", errors="replace") or None
                        logger.debug(
                            "yadnp3_g110_string device=%s index=%s text=%r",
                            device_code,
                            it.index,
                            text,
                        )
                        cache.set(_OBJECT_GROUP_STRING, it.index, 0.0, value_string=text)
                    # ILK basarili okumayi INFO'ya cikar: bu hatanin sessiz
                    # kalma sebebi tum G110 loglarinin DEBUG olmasiydi
                    # (varsayilan LOG_LEVEL=INFO ile hic gorunmuyorlardi).
                    # Cihaz basina TEK satir — log spam yok.
                    if not g110_bildirildi:
                        g110_bildirildi = True  # noqa: F841 — nonlocal asagida
                        logger.info(
                            "yadnp3_g110_okundu device=%s nokta=%d — string "
                            "(seri no / IMEI / IP / firmware) alindi",
                            device_code,
                            len(values),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "yadnp3_soe_process_error device=%s error=%s",
                    device_code,
                    exc,
                )

    return _CacheSOEHandler()


# Saat sapmasi gozlemcisine modul-seviye referans. opendnp3 callback'leri
# (IMasterApplication.Now) native thread'lerden cagrildigi icin adapter
# ornegine erisimleri yok; buraya set_clock_guard() ile enjekte edilir.
_clock_guard_ref: dict[str, Any] = {"guard": None}


def set_clock_guard(guard: Any) -> None:
    """Saat sapmasi gozlemcisini adapter'a bagla (main.py cagirir)."""
    _clock_guard_ref["guard"] = guard
    with _saat_uyari_lock:
        _saat_uyarilan.clear()


# Saat sapmasi uyarisi verilmis cihazlar (log spam onlemi). `Now()` her zaman
# senkronizasyon isteginde cagrilir; uyariyi cihaz basina bir kez basiyoruz.
_saat_uyari_lock = threading.Lock()
_saat_uyarilan: set[str] = set()


def _saat_uyarisi_ver(device_code: str) -> bool:
    with _saat_uyari_lock:
        if device_code in _saat_uyarilan:
            return False
        _saat_uyarilan.add(device_code)
        return True


def _gecersiz_dnptime() -> Any:
    """opendnp3'e "bu zaman gecerli degil" diyen bir DNPTime uretir.

    Binding surumleri arasinda API farkli olabildigi icin sirayla deneriz:
      1. `DNPTime(0, TimestampQuality.INVALID)` — acik ve dogru olan.
      2. `DNPTime(0)` — epoch 0; outstation'lar bunu makul bir saat olarak
         kabul etmez, pratikte yazim etkisiz kalir.
    Hicbiri kurulamazsa None doneriz; opendnp3 tarafinda bu da yazimi engeller.
    """
    kalite_cls = getattr(opendnp3, "TimestampQuality", None)
    if kalite_cls is not None:
        for ad in ("INVALID", "Invalid", "NOT_SYNCHRONIZED"):
            kalite = getattr(kalite_cls, ad, None)
            if kalite is not None:
                try:
                    return opendnp3.DNPTime(0, kalite)
                except Exception:  # noqa: BLE001
                    break
    try:
        return opendnp3.DNPTime(0)
    except Exception:  # noqa: BLE001
        return None


def _iin_bit(iin: Any, *names: str) -> bool:
    """IIN nesnesinden bir bayragi oku; binding surumune gore isim degisebilir."""
    for name in names:
        val = getattr(iin, name, None)
        if isinstance(val, bool):
            return val
        # lsb/msb alt nesneleri (bazi binding'lerde IINField.LSB.<bit>)
        for holder_name in ("LSB", "lsb", "MSB", "msb"):
            holder = getattr(iin, holder_name, None)
            if holder is not None:
                sub = getattr(holder, name, None)
                if isinstance(sub, bool):
                    return sub
    return False


def _report_iin(cache: _DeviceCache, device_code: str, iin: Any) -> None:
    """Outstation IIN bayraklarindan operatorun bilmesi gerekenleri logla.

    Log spam onlemi: her bayrak icin EDGE-TRIGGER — durum degistiginde bir
    kez yazilir (IIN her yanitta gelir, saniyede onlarca kez loglanamaz).
    """
    overflow = _iin_bit(iin, "eventBufferOverflow", "EVENT_BUFFER_OVERFLOW")
    need_time = _iin_bit(iin, "needTime", "NEED_TIME")
    device_restart = _iin_bit(iin, "deviceRestart", "DEVICE_RESTART")

    if cache.note_iin("event_buffer_overflow", overflow) and overflow:
        logger.error(
            "dnp3_event_buffer_overflow device=%s — outstation olay tamponu doldu "
            "ve EN ESKI OLAYLARI DUSURDU. Telemetride aciklik olusmus olabilir; "
            "scan araligini kisaltmayi veya link kalitesini kontrol etmeyi dusunun.",
            device_code,
        )
    if cache.note_iin("device_restart", device_restart) and device_restart:
        logger.warning(
            "dnp3_device_restart device=%s — outstation yeniden baslamis; "
            "integrity poll ile tam durum yeniden alinacak",
            device_code,
        )
    if cache.note_iin("need_time", need_time) and need_time:
        logger.info(
            "dnp3_need_time device=%s — cihaz saat yazilmasini bekliyor (DNP3_TIME_SYNC ayarina bakin)",
            device_code,
        )


def _make_master_app(cache: _DeviceCache, device_code: str) -> Any:
    if not _YADNP3_AVAILABLE:
        raise Yadnp3AdapterError(f"yadnp3 yuklu degil: {_YADNP3_IMPORT_ERROR}")

    class _MasterApp(opendnp3.IMasterApplication):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()

        def OnReceiveIIN(self, iin):  # noqa: N802
            # IIN yalnizca outstation'in GERCEK bir uygulama yanitiyla gelir —
            # yani bu cagri "cihaz konusuyor"un kanitidir. Kanit ONCE islenir:
            # `_report_iin` icindeki beklenmedik bir hata canlilik bilgisini
            # dusurmemeli (asil sikayet zaten sahte kopmaydi).
            cache.note_evidence()
            # Outstation'in bize soyledigi iki kritik durum:
            #   * IIN2.3 EVENT_BUFFER_OVERFLOW -> outstation event tamponu
            #     dolmus ve EN ESKI OLAYLARI DUSURMUS. Yani telemetride
            #     acikliklar var ve kimse bilmiyor.
            #   * IIN1.4 NEED_TIME -> cihaz saat yazilmasini bekliyor.
            try:
                _report_iin(cache, device_code, iin)
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_iin_report_error device=%s", device_code, exc_info=True)

        def OnTaskStart(self, type, id):  # noqa: N802, A002
            pass

        def OnTaskComplete(self, info):  # noqa: N802
            # ESKIDEN `pass` IDI. Tamamlanan her gorev cihazin cevap verip
            # vermedigini soyluyordu ve bu bilgi cope gidiyordu — sessiz ama
            # SAGLAM bir cihazin sahte comm_lost almasinin sebebi buydu.
            #
            # SADECE SUCCESS kanittir (bkz. `note_evidence` docstring'i).
            # Enum karsilastirmasi ISIMLE yapiliyor: binding surumleri arasinda
            # enum nesnesi degisebilir ama `.name` sabittir; ayrica bu callback
            # opendnp3'un IO thread'inden geldigi icin hicbir istisna disari
            # sizmamali.
            try:
                sonuc = getattr(info, "result", None)
                if getattr(sonuc, "name", str(sonuc)) == "SUCCESS":
                    cache.note_evidence()
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_task_complete_error device=%s", device_code, exc_info=True)

        def OnOpen(self):  # noqa: N802
            # Link acildi — connected flag'i set ama henuz "online" sayma.
            # Cihaz DNP3 frame'i cevapladigi anda set() recovery'yi confirm
            # edip state'i "online"a yukseltecek. read_device bu arada hala
            # comm_lost yayinlayarak SCADA'yi yaniltmamaya devam eder.
            cache.set_connected(True)
            cache.begin_recovery()
            # String taramasini ISTE — istegi buradan GONDERME. Bu callback
            # opendnp3'un IO thread'inden gelir; ScanRange'i poll thread'i
            # (read_device) gonderir.
            cache.g110_iste()
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
            """opendnp3 outstation'a saat yazarken bu degeri kullanir.

            SAAT SAPMASI KORUMASI — GERCEK, no-op DEGIL.
            Bu metod eskiden guard'i kontrol edip yalnizca DEBUG log basiyor,
            sonra YINE DE `time.time()` donduruyordu; yorumu ise korumanin
            "_apply_time_sync'te" oldugunu iddia ediyordu — oyle bir mekanizma
            YOKTU. Yani sapmis gateway saati 300 outstation'a yaziliyordu ve
            kod bunun onlendigini soyluyordu.

            Artik sapma guvensizken `DNPTime` yerine GECERSIZ bir zaman
            (`is_valid=False` / epoch 0) donduruyoruz: opendnp3 bu durumda
            outstation'a saat YAZMAZ, cihaz kendi saatinde kalir ve IIN1.4
            (NEED_TIME) bayragiyla durumu bildirmeye devam eder — ki bu bayrak
            artik loglaniyor (bkz. _report_iin).

            Yanlis saati 300 cihaza yazmak, hic yazmamaktan cok daha kotudur:
            cihazin kendi olay tamponu da bozulur ve baska bir master'in
            okudugu damgalar da yanlis olur.
            """
            guard = _clock_guard_ref.get("guard")
            if guard is not None:
                try:
                    if not guard.is_safe_for_time_sync:
                        if _saat_uyarisi_ver(device_code):
                            # `unknown` ile `unsafe` AYRI raporlanir: ikisi de
                            # yazmayi engeller ama operatorun yapacagi sey farkli.
                            durum = getattr(guard, "clock_state", "unsafe")
                            if durum == "unknown":
                                logger.warning(
                                    "dnp3_time_sync_suspended device=%s sebep=clock_unknown — "
                                    "gateway saati backend'e karsi HENUZ DOGRULANMADI "
                                    "(soguk acilis / backend erisilemez); outstation'a saat "
                                    "YAZILMIYOR. Backend'e ilk basarili istekte cozulur. "
                                    "Telemetri, polling ve komut akisi ETKILENMEZ.",
                                    device_code,
                                )
                            else:
                                logger.error(
                                    "dnp3_time_sync_suspended device=%s sebep=clock_unsafe — "
                                    "gateway saati guvenilmez (sapma esigi asildi); "
                                    "outstation'a saat YAZILMIYOR. Sunucuda NTP/w32time "
                                    "servisini kontrol edin.",
                                    device_code,
                                )
                        return _gecersiz_dnptime()
                except Exception:  # noqa: BLE001
                    logger.debug("clock_guard_check_failed", exc_info=True)
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
        time_sync: str = "lan",
    ) -> None:
        self.device = device
        self.cache = _DeviceCache()
        # Bu master'i belirleyen baglanti parametrelerinin imzasi. Caller
        # (_ensure_master) kurduktan sonra doldurur; config'te bu degerlerden
        # biri degisirse master yeniden kurulur.
        self.connection_fingerprint: tuple = ()
        self._manager = manager
        self._scan_interval_sec = max(1, int(scan_interval_sec))
        self._baseline_interval_sec = max(self._scan_interval_sec, int(baseline_interval_sec))
        # G110 (string) okuma bloklari — CIHAZ BASINA, o cihazin sinyal
        # setinden turetilir (`_g110_bloklari`). Sinif sabiti OLAMAZ: ayni
        # gateway'de SN2.0 ile Pole Master Kit birlikte bulunabilir ve
        # string index haritalari farklidir.
        self.g110_ranges: tuple[tuple[int, int], ...] = ()
        # Son CROB gonderiminin monotonic damgasi. Zorla relink bunu okur:
        # komut ucustayken oturumu yikmak gorevi cevapsiz birakir ve sonuc
        # CommandStatus.UNDEFINED doner (sahada olculdu 2026-08-11).
        self.last_command_at: float = 0.0

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
        # ---- Zaman senkronizasyonu (master -> outstation) ------------------
        # ESKIDEN HIC AYARLANMIYORDU: opendnp3 varsayilani TimeSyncMode::None
        # oldugu icin gateway hicbir cihaza saat yazmiyordu. Outstation'lar
        # tipik olarak ayda 10-60sn RTC drift yapar ve guc kesintisinden sonra
        # saatlerini 2000-01-01'e resetler; master zaman yazmadigi icin cihaz
        # kendi olay damgalarini yanlis saatle uretmeye ve IIN1.4 (NEED_TIME)
        # bayragini sonsuza kadar set tutmaya devam ediyordu.
        #
        # Bu bugun gorunmuyordu cunku gateway zaten cihaz damgasini atip kendi
        # saatini basiyordu (bkz. BACKEND_TODO.md#B2). B2 acildigi an — cihaz
        # damgasi yayina girdiginde — zaman-senk olmadan durum KOTULESIRDI:
        # "hepsi ayni yanlis saat" yerine "hepsi FARKLI yanlis saat".
        self._apply_time_sync(cfg, time_sync)
        cfg.link.LocalAddr = int(local_address)
        cfg.link.RemoteAddr = int(device.dnp3_address)
        self._master = self._channel.AddMaster(f"m_{device.code}", self._soe, self._app, cfg)
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
        # G110 (Octet String) — cihaz integrity poll'de DONDURMUYOR (teshis
        # 2026-07-20: SOE fragment'larinda OctetString yok, "Not Class 0").
        # String'ler STATIK (seri no/IMEI/IP/firmware) — surekli okumaya gerek
        # yok. Periyodik scan EKLENMEZ; string yalnizca baglanti kuruldugunda
        # (recovery confirmed) BIR KEZ dar-range read ile okunur (scan_g110_once).
        # Bu, cihaz/modem yukunu minimuma indirir + Mayis'ta bozan surekli/genis
        # scan'den kacinir.
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

    @staticmethod
    def _apply_time_sync(cfg: Any, mode: str) -> None:
        """`cfg.master.timeSyncMode` ayarla. Binding desteklemiyorsa sessiz gec.

        `mode`: "lan" (TCP icin dogru olan LAN prosedu) veya "none".
        Enum adlari binding surumleri arasinda degisebildigi icin birkac
        alternatif denenir; hicbiri yoksa eski davranis (senkronizasyon yok)
        korunur ve bir kez WARNING atilir.
        """
        want = (mode or "lan").strip().lower()
        enum_cls = getattr(opendnp3, "TimeSyncMode", None)
        if enum_cls is None:
            logger.warning(
                "yadnp3_time_sync_unsupported — binding TimeSyncMode sunmuyor; "
                "outstation saatleri senkronize EDILMEYECEK"
            )
            return
        if want in ("none", "off", "disabled"):
            candidates = ("None_", "NONE", "None")
        else:
            candidates = ("LAN", "SerialTimeSync", "NonLAN")
        for name in candidates:
            value = getattr(enum_cls, name, None)
            if value is not None:
                cfg.master.timeSyncMode = value
                return
        logger.warning("yadnp3_time_sync_enum_not_found mode=%s — saat senkronizasyonu kapali", want)

    def _g110_gvid(self):
        """GroupVariationID(110, 0) — surum farki icin iki isim dener."""
        for name in ("GroupVariationID", "GroupVariation"):
            cls = getattr(opendnp3, name, None)
            if cls is not None:
                try:
                    return cls(110, 0)
                except Exception:  # noqa: BLE001
                    continue
        return None

    def scan_g110_once(self) -> bool:
        """G110 string'leri BIR KEZ dar-range oku (one-shot, periyodik DEGIL).

        Baglanti kuruldugunda / recovery confirmed / manuel refresh'te cagrilir.
        String'ler statik oldugu icin tek okuma yeterli; surekli scan cihaz/modem
        yuku + link riski yaratir. `ScanRange` yoksa sessizce atla.

        Aralilar `g110_ranges`ten gelir ve CIHAZ BASINA turetilir (bkz.
        `_g110_bloklari`). Eskiden bu bir SINIF SABITI idi — ((3,23),
        (65000,65020)) — ve SN2.0'in index haritasiydi. Ayni gateway'de
        Pole Master Kit gibi baska bir model bulundugunda o cihazin
        string'lerinin buyuk kismi HIC istenmiyordu (54 noktanin 30'u).

        Returns: True = en az bir range task kuyruga alindi."""
        if not self.g110_ranges:
            # Cihazin G110 sinyali yok — hicbir istek gonderilmez.
            return False
        if not self.cache.is_connected():
            # LINK KAPALI: ad-hoc ScanRange master OFFLINE iken kuyruga
            # ALINMAZ, aninda fail edilir — ama `ScanRange()` exception
            # atmadigi icin eskiden "queued" yazip True donuyorduk. Sahte
            # basari uretmek yerine acikca vazgeciyoruz.
            logger.debug(
                "yadnp3_g110_scan_atlandi device=%s sebep=link_kapali",
                self.device.code,
            )
            return False
        gv = self._g110_gvid()
        scan_range = getattr(self._master, "ScanRange", None)
        if gv is None or scan_range is None:
            return False
        ok = False
        for start, stop in self.g110_ranges:
            try:
                scan_range(gv, int(start), int(stop), self._soe, opendnp3.TaskConfig.Default())
                ok = True
                logger.debug(
                    "yadnp3_g110_scan_queued device=%s range=%s-%s",
                    self.device.code,
                    start,
                    stop,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "yadnp3_g110_scan_once_failed device=%s range=%s-%s",
                    self.device.code,
                    start,
                    stop,
                    exc_info=True,
                )
        return ok

    def shutdown(self) -> None:
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

    def kanit_esigi_sn(self) -> float:
        """Canlilik kanitinin bayat sayildigi sure.

        `read_device`'in bayatlik esigiyle AYNI deger (tek kaynak:
        `_bayatlik_esigi_sn`). Anlami: bu sure boyunca outstation'dan tek bir
        yanit bile alamadiysak cihaz gercekten ulasilamaz durumdadir.
        """
        return _bayatlik_esigi_sn(self._scan_interval_sec, self._baseline_interval_sec)

    def kanit_yasi(self) -> float:
        """Son canlilik kanitinin uzerinden gecen sure; kanit yoksa -1."""
        kanit = self.cache.last_evidence_at()
        if kanit == 0.0:
            return -1.0
        return time.monotonic() - kanit

    def ulasilabilir(self) -> bool:
        """Cihaz SU AN cevap veriyor mu? (veri uretiyor mu DEGIL)

        Komut yolu bunu kullanir: kesici komutunu bloke etmesi gereken tek sey
        cihaza ULASILAMAMASIDIR — degerlerinin degismemesi degil.
        """
        if not self.cache.is_connected():
            return False
        yas = self.kanit_yasi()
        if yas < 0:
            return False
        return yas <= self.kanit_esigi_sn()

    def request_integrity_poll(self) -> bool:
        """Cihaza ANINDA Class 0+1+2+3 integrity poll gonderir.

        Sequenced scan mantigindan farkli olarak `ScanAllObjects` ad-hoc
        bir read task'i kuyruga ekler ve master link uzerinden gonderir.
        SOE handler donen frame'leri ayni cache mantigi ile isler;
        farkli degerler dirty isaretlenir, poller sonraki cycle'da
        yayinlar.

        Returns: True = task kuyruga alindi, False = link kapali / hata.
        """
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
            # G110 string'ler integrity'ye dahil degil (Not Class 0); recovery/
            # manuel refresh aninda bir kez dar-range oku (best-effort).
            self.scan_g110_once()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("yadnp3_integrity_poll_request_failed")
            return False

    # CROB operation code eslemesi. Horstmann SN2 Device Profile'i PULSE
    # DESTEKLEMEZ (yalnizca LATCH_ON/LATCH_OFF). Bu yuzden default LATCH_ON;
    # pulse_* verilse bile calisir ama cihaz reddeder — dogru kullanim latch.
    _OP_MAP_LAZY: dict[str, Any] | None = None

    @classmethod
    def _op_map(cls) -> dict[str, Any]:
        if cls._OP_MAP_LAZY is None:
            cls._OP_MAP_LAZY = {
                "latch_on": opendnp3.OperationType.LATCH_ON,
                "latch_off": opendnp3.OperationType.LATCH_OFF,
                "pulse_on": opendnp3.OperationType.PULSE_ON,
                "pulse_off": opendnp3.OperationType.PULSE_OFF,
            }
        return cls._OP_MAP_LAZY

    def operate_crob(
        self,
        *,
        index: int,
        op_type: str = "latch_on",
        count: int = 1,
        on_time_ms: int = 0,
        off_time_ms: int = 0,
        timeout_sec: float = 10.0,
        mode: str = "direct",
    ) -> dict:
        """Cihaza bir CROB (Control Relay Output Block) gonderir.

        mode:
          - "direct" (VARSAYILAN): DirectOperate — tek adimda OPERATE. Bu
            cihazda (Horstmann SN2, saha testi) SELECT-before-OPERATE SELECT
            fazinda NOT_SUPPORTED donuyor; DirectOperate ise gercekten calisiyor
            (alarm fiziksel resetleniyor). Bu yuzden default DirectOperate.
          - "sbo": SelectAndOperate — once SELECT, cevabini bekler, SUCCESS ise
            ayni CROB ile OPERATE. Bazi cihazlar/index'ler bunu ister; opsiyonel.

        Default op_type=latch_on (Device Profile PULSE desteklemez — pulse
        gonderilirse cihaz NOT_SUPPORTED doner). Default on/off time=0 (LATCH'te
        sure anlamsiz).

        Donen dict (detayli DNP3 status):
          ok, status (ok|failed|timeout|offline|error|bad_request),
          dnp3_task (TaskCompletion), dnp3_status (per-point CommandStatus),
          dnp3_state (CommandPointState), control (op_type), mode, points[].
          Toplam sure operate_device'da olculur.
        """
        # Komut BASLANGICINI isaretle: poll thread'i bu pencerede zorla
        # relink yapmasin (bkz. _LOST_RELINK_COMMAND_GRACE_SEC).
        self.last_command_at = time.monotonic()
        # ---- Baglanti fail-fast: cihaz ULASILAMAZ ise komut GONDERME ----
        # (kopuk cihazda DirectOperate/SBO 10s bloklar sonra timeout doner;
        # erken kesip net "offline" don.)
        #
        # OLCUT `state == "online"` DEGIL, ULASILABILIRLIK (2026-08 saha arizasi)
        # ---------------------------------------------------------------------
        # `state` cihazin VERI URETIP uretmedigine bagliydi: degeri degismeyen
        # saglikli bir SN2.0 'lost'a dusuyor ve bu kapi KESICI KOMUTUNU
        # reddediyordu. Yani "deger degismedi" sessizce "kumanda edemiyorum"a
        # donusuyordu — bu arizanin en tehlikeli belirtisi buydu.
        #
        # Fail-fast'in GERCEK gerekcesi "olu bir soket uzerinde 10 saniye
        # bloklama" idi; onun dogru olcutu de canlilik kanitidir. Kanit tazeyse
        # cihaz bu saniyelerde cevap vermis demektir, komut bloklamaz.
        # Kanit yoksa davranis eskisi gibi: hic gonderme.
        #
        # Not: kanit taze ama komut yine de basarisiz olursa artik UYDURULMUS
        # bir "offline" degil, cihazin GERCEK DNP3 sonucu (timeout/failed +
        # TaskCompletion) doner — operator icin dogru bilgi budur.
        state = self.cache.state()
        if not self.ulasilabilir():
            logger.warning(
                "yadnp3_operate_skipped_offline device=%s index=%s state=%s "
                "kanit_yasi=%.0fs — komut GONDERILMEDI",
                self.device.code,
                index,
                state,
                self.kanit_yasi(),
            )
            return {
                "ok": False,
                "status": "offline",
                "error": f"cihaza ulasilamiyor (state={state}); komut gonderilmedi",
                "control": op_type,
            }

        opt = self._op_map().get(op_type.lower())
        if opt is None:
            return {"ok": False, "status": "bad_request", "error": f"gecersiz op_type: {op_type!r}"}

        try:
            crob = opendnp3.ControlRelayOutputBlock(opt)
            crob.count = int(count)
            crob.onTimeMS = int(on_time_ms)
            crob.offTimeMS = int(off_time_ms)
        except Exception as exc:  # noqa: BLE001
            try:
                crob = opendnp3.ControlRelayOutputBlock(
                    opt,
                    opendnp3.TripCloseCode.NUL,
                    False,
                    int(count),
                    int(on_time_ms),
                    int(off_time_ms),
                )
            except Exception:  # noqa: BLE001
                logger.exception("yadnp3_crob_build_failed")
                return {
                    "ok": False,
                    "status": "error",
                    "error": f"CROB olusturulamadi: {exc}",
                    "control": op_type,
                }

        mode_l = (mode or "direct").lower()
        if mode_l not in ("direct", "sbo"):
            mode_l = "direct"

        done = threading.Event()
        holder: dict = {"status": "no_result", "control": op_type, "mode": mode_l}

        def _cb(result):  # opendnp3 IO thread
            try:
                # summary = TaskCompletion enum (transport/link seviyesi):
                #   SUCCESS / FAILURE_NO_COMMS / FAILURE_RESPONSE_TIMEOUT /
                #   FAILURE_MESSAGE_FORMAT_ERROR / FAILURE_BAD_RESPONSE / ...
                summary = getattr(result, "summary", None)
                holder["dnp3_task"] = str(summary)
                task_ok = summary == getattr(opendnp3.TaskCompletion, "SUCCESS", None)

                # Per-point sonuc: gercek DNP3 CommandStatus (SUCCESS/NO_SELECT/
                # FORMAT_ERROR/NOT_SUPPORTED/...) + CommandPointState (SBO fazi:
                # SELECT_SUCCESS/SELECT_FAIL/SELECT_MISMATCH/OPERATE_FAIL/SUCCESS).
                points = []
                all_points_ok = True
                first_status = None
                first_state = None
                try:
                    plist = result.to_list()
                except Exception:  # noqa: BLE001
                    plist = []
                if not plist:
                    all_points_ok = False  # cevapta point yok -> basarisiz say
                for pr in plist:
                    st = getattr(pr, "status", None)
                    stt = getattr(pr, "state", None)
                    points.append(
                        {
                            "index": getattr(pr, "index", None),
                            "status": str(st),
                            "state": str(stt),
                        }
                    )
                    if first_status is None:
                        first_status = st
                        first_state = stt
                    if st != getattr(opendnp3.CommandStatus, "SUCCESS", None):
                        all_points_ok = False

                holder["points"] = points
                holder["dnp3_status"] = str(first_status)
                holder["dnp3_state"] = str(first_state)
                # BASARI KARARI: hem transport (TaskCompletion.SUCCESS) HEM
                # her point CommandStatus.SUCCESS olmali. Eskiden yalniz summary
                # string'ine bakiliyordu -> outstation transport-SUCCESS ama point
                # NO_SELECT/NOT_SUPPORTED ise yanlis-pozitif olabiliyordu.
                ok = task_ok and all_points_ok
                holder["status"] = "ok" if ok else "failed"
            except Exception as exc:  # noqa: BLE001
                holder["status"] = "error"
                holder["error"] = str(exc)
            finally:
                done.set()

        # Komut gonder. mode=direct -> DirectOperate (tek adim OPERATE; bu cihazda
        # calisan yontem). mode=sbo -> SelectAndOperate (SELECT sonra OPERATE).
        try:
            if mode_l == "sbo":
                self._master.SelectAndOperate(crob, int(index), _cb, opendnp3.TaskConfig.Default())
            else:
                self._master.DirectOperate(crob, int(index), _cb, opendnp3.TaskConfig.Default())
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "yadnp3_operate_failed device=%s index=%s mode=%s",
                self.device.code,
                index,
                mode_l,
            )
            return {"ok": False, "status": "error", "error": str(exc), "control": op_type, "mode": mode_l}

        if not done.wait(timeout=timeout_sec):
            return {
                "ok": False,
                "status": "timeout",
                "error": f"{timeout_sec}s icinde komut cevabi yok",
                "control": op_type,
                "mode": mode_l,
            }

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
        time_sync: str = "lan",
        publish_quality_flags: bool = False,
        device_count_hint: int = 0,
        lost_probe_interval_sec: float = 0.0,
        lost_relink_after_probes: int = 0,
    ) -> None:
        if not _YADNP3_AVAILABLE:
            raise Yadnp3AdapterError(
                f"yadnp3 (opendnp3) yuklu degil. Wheel kurulu olmali. Import error: {_YADNP3_IMPORT_ERROR}"
            )
        self._local_address = int(local_address)
        self._default_dnp3_tcp_port = int(default_dnp3_tcp_port)
        self._scan_interval_sec = int(scan_interval_sec)
        self._baseline_interval_sec = int(baseline_interval_sec)
        self._time_sync = (time_sync or "lan").strip().lower()
        # Kalite bayraklarinin YAYINA girmesi backend hazir olunca acilir
        # (bkz. docs/BACKEND_TODO.md#B1); bayraklar her durumda okunur.
        self._publish_dnp3_quality = bool(publish_quality_flags)
        # DNP3Manager IO thread sayisi.
        #
        # Bu heuristik yorumda TARIF EDILIYOR ama UYGULANMIYORDU: "device_count_hint
        # constructor'da bilinmiyor" denip sabit 4 aliniyordu. Sonuc: 400 cihazli
        # sahada 100 CIHAZ/THREAD (2026-08-04 olcumu). Her cihazin TCP I/O'su ve
        # scheduler isi bu dort thread'e diziliyor; cihaz sayisi arttikca DNP3
        # yaniti gecikir ve bu gecikme hicbir sayacta gorunmez (cihaz "online"
        # kalir, yalnizca veri bayatlar).
        #
        # `device_count_hint` artik veriliyor: operatorun olcek beklentisini
        # tasiyan `MAX_PARALLEL_DEVICES` ayarindan geliyor (bkz. adapters/factory).
        # Hedef ~25 cihaz/thread; taban 4 (kucuk kurulum), tavan 32 (bu noktadan
        # sonra thread eklemek context-switch maliyetinden baska sey getirmiyor).
        if manager_threads > 0:
            resolved_threads = max(1, min(64, int(manager_threads)))
        else:
            ipucu = max(0, int(device_count_hint or 0))
            resolved_threads = max(4, min(32, -(-ipucu // 25))) if ipucu else 4
        self._manager = opendnp3.DNP3Manager(resolved_threads)
        self._manager_threads = resolved_threads
        self._masters: dict[str, _ManagedMaster] = {}
        # Lock granulariteyi azaltmak icin double-check pattern: lock disinda
        # hizli dict lookup; sadece master OLUSTURURKEN lock alinir.
        self._lock = threading.Lock()
        # NaN/Inf uyarisi verilmis noktalar (log spam onlemi). Arizali bir
        # nokta her cycle yeniden okunur; uyariyi bir kez basiyoruz.
        self._nan_lock = threading.Lock()
        self._nan_uyarilan: set[tuple[int, int]] = set()
        # Kalite bayragi TASIMASI GEREKIRKEN bayraksiz gelen noktalar.
        # Ayni gerekce (log spam onlemi); NaN kumesinden ayri tutuluyor ki
        # iki farkli anomali birbirinin uyarisini bastirmasin.
        self._bayrak_uyarilan: set[tuple[int, int]] = set()
        # Kopuk cihaz yoklamasi: cihaz kodu -> {"son": monotonic, "deneme": int}.
        # Master nesnesinde DEGIL burada tutuluyor, cunku zorla relink master'i
        # yok edip yeniden kuruyor; sayac master ile birlikte silinirse esik
        # her seferinde sifirlanir ve relink dongusune girilir.
        # 0/None -> modul varsayilani. Instance degeri set edilirse o kazanir;
        # testler modul sabitini monkeypatch'leyebilsin diye cagri aninda okunur.
        self._lost_probe_interval_sec = float(lost_probe_interval_sec or 0.0)
        self._lost_relink_after_probes = int(lost_relink_after_probes or 0)
        self._lost_probe_lock = threading.Lock()
        self._lost_probe: dict[str, dict[str, float]] = {}
        self._lost_probe_total = 0
        self._forced_relink_total = 0
        # Olcum sessizligi yoklamasi: cihaz kodu -> {"son": monotonic, "uyarildi": 0|1}.
        # `_lost_probe_lock` ile korunuyor (ayri bir lock acmaya deger bir
        # cekisme yok; ikisi de yalnizca sozluk guncelliyor).
        self._veri_sessizligi: dict[str, dict[str, float]] = {}
        self._veri_sessizligi_poll_total = 0
        # G110 okunamadi uyarisi verilmis cihazlar (cihaz basina TEK uyari).
        self._g110_uyarilan: set[str] = set()

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

    def _connection_fingerprint(self, device: DeviceConfig) -> tuple:
        """Master'i belirleyen TUM baglanti parametrelerinin imzasi.

        Bunlardan biri degistiginde mevcut master GECERSIZDIR ve yeniden
        kurulmasi gerekir.
        """
        return (
            (device.ip_address or "").strip(),
            self._resolve_tcp_port(device, self._default_dnp3_tcp_port),
            int(device.dnp3_address),
            self._resolve_local_address(device, self._local_address),
            (device.ip_endpoint_type or "listening").strip().lower(),
            int(device.master_ip_port or 0),
        )

    def _ensure_master(
        self,
        device: DeviceConfig,
        signals: list[SignalConfig] | None = None,
    ) -> _ManagedMaster:
        """Cihaz icin _ManagedMaster doner — varsa cache'den, yoksa olusturur.

        Double-check pattern: hot-path'te (master zaten var VE imzasi ayni)
        lock ALINMAZ. Sadece master ilk kez yaratilacaksa ya da baglanti
        parametreleri degistiyse lock alip create/rebuild yapariz.

        IMZA KONTROLU NEDEN VAR:
        Eskiden master YALNIZCA `device.code` ile anahtarlaniyordu ve bir kez
        olusturulduktan sonra `ip_address`, `dnp3_tcp_port`, `dnp3_address`,
        `master_address`, `ip_endpoint_type`, `master_ip_port` degerleri bir
        daha KARSILASTIRILMIYORDU. `forget_devices` yalnizca config'ten SILINEN
        cihazlari kapatiyordu.

        Sonuc: saha ekibi bir RTU'yu degistirip backend'de IP'yi
        10.20.5.11 -> 10.20.5.19 yaptiginda config ~1sn'de gateway'e ulasiyor,
        /health yeni IP'yi gosteriyor, ama master ESKI IP'ye TCP denemeye devam
        ediyordu. Cihaz kalici comm_lost oluyor ve operator health'te dogru
        IP'yi gordugu icin sorunu gateway'de aramiyordu. Daha kotusu: eski IP
        baska bir cihaza atanmissa (DHCP/yeniden adresleme) gateway O CIHAZDAN
        okuyup degerleri ESKI device_code altinda yayinliyordu — SCADA'da
        yanlis fider olcumu. Duzelme ancak process restart ile oluyordu.

        (Fallback adapter dnp3_master.py'de bu mantik zaten vardi; PRIMARY
        adapter'da yoktu.)
        """
        want = self._connection_fingerprint(device)
        g110 = _g110_bloklari(signals, device_code=device.code)
        # Hot-path: lock-free dict lookup + imza karsilastirmasi
        existing = self._masters.get(device.code)
        if existing is not None and existing.connection_fingerprint == want:
            self._g110_araliklarini_guncelle(existing, g110, signals)
            return existing

        hazir: _ManagedMaster | None = None
        with self._lock:
            existing = self._masters.get(device.code)
            if existing is not None and existing.connection_fingerprint == want:
                hazir = existing
            elif existing is not None:
                # Baglanti parametreleri degisti -> eskisini kapat, yeniden kur.
                logger.warning(
                    "yadnp3_master_rebuild device=%s eski=%s yeni=%s — cihaz "
                    "baglanti parametreleri degisti, oturum yeniden kuruluyor",
                    device.code,
                    existing.connection_fingerprint,
                    want,
                )
                try:
                    existing.shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("yadnp3_master_shutdown_error device=%s", device.code, exc_info=True)
                self._masters.pop(device.code, None)

            if hazir is None:
                port = self._resolve_tcp_port(device, self._default_dnp3_tcp_port)
                local_addr = self._resolve_local_address(device, self._local_address)
                mm = _ManagedMaster(
                    self._manager,
                    device=device,
                    local_address=local_addr,
                    tcp_port=port,
                    scan_interval_sec=self._scan_interval_sec,
                    baseline_interval_sec=self._baseline_interval_sec,
                    time_sync=self._time_sync,
                )
                mm.connection_fingerprint = want
                mm.g110_ranges = g110
                self._masters[device.code] = mm

        # Yarista baska bir thread kurmus: yalnizca bloklari tazele.
        if hazir is not None:
            self._g110_araliklarini_guncelle(hazir, g110, signals)
            return hazir

        # STRING OKUMASI BURADA TETIKLENMEZ.
        # v1.6.2'de buradan `scan_g110_once()` cagriliyordu ve yorumu "task
        # kuyruga alinir, oturum acilinca kosar" diyordu — BU YANLISTI.
        # `Enable()` asenkron; bu noktada TCP oturumu ACIK DEGIL (listening
        # modda cihaz kendi baglanana kadar dakikalar surebilir). opendnp3'te
        # ad-hoc ScanRange master OFFLINE iken kuyruga ALINMAZ, aninda fail
        # edilir; `ScanRange()` exception atmadigi icin kod "queued" yazip
        # True donuyordu — SAHTE BASARI. Kararli baglanan cihazda string'ler
        # bu yuzden hic gelmiyordu.
        # Artik tetikleme link ACILDIKTAN SONRA, poll thread'inden yapiliyor:
        # OnOpen -> cache.g110_iste(), read_device -> scan_g110_once().
        return mm

    @staticmethod
    def _g110_araliklarini_guncelle(
        mm: _ManagedMaster,
        g110: tuple[tuple[int, int], ...],
        signals: list[SignalConfig] | None,
    ) -> None:
        """Mevcut master'in G110 bloklarini sinyal setine gore tazele.

        `signals=None` ile cagrilan yollar (komut gonderme) bloklara
        DOKUNMAZ; aksi halde komut yolu, sinyal listesini bilmedigi icin
        okuma yolunun kurdugu bloklari silerdi.

        Bloklar BOS iken doluya gecerse string okumasi bir kez tetiklenir:
        cihazla ilk temas komut yoluyla olmus (master signals'siz kurulmus)
        ya da config refresh ile yeni string noktalari eklenmis olabilir.
        """
        if signals is None or mm.g110_ranges == g110:
            return
        onceden_bos = not mm.g110_ranges
        mm.g110_ranges = g110
        if onceden_bos and g110:
            # Taramayi BURADAN gonderme (link acik olmayabilir); iste yeter.
            # Poll thread'i link acikken gerçekleştirir.
            mm.cache.g110_iste()

    # ---- Kopuk cihaz yoklamasi ------------------------------------------
    def _lost_probe_durumu(self, mm: Any) -> dict[str, float]:
        """Test/teshis icin bir cihazin yoklama sayaclari."""
        kod = mm.device.code
        with self._lost_probe_lock:
            d = self._lost_probe.get(kod)
            return dict(d) if d else {"son": 0.0, "deneme": 0}

    def _lost_probe_sifirla(self, device_code: str) -> None:
        # Hot-path: saglikli cihazlarda her cycle cagrilir (bkz. read_device
        # "cihaz ses verdiyse" blogu). Kayit yoksa lock bile alma.
        if device_code not in self._lost_probe:
            return
        with self._lost_probe_lock:
            self._lost_probe.pop(device_code, None)

    def recovery_stats(self) -> dict[str, int]:
        """Kopuk cihaz kurtarma sayaclari — /health bunu raporlar.

        "Gateway acaba yeniden baglanmayi deniyor mu?" sorusu sahada
        yalnizca soket durumuna bakarak (SYN_SENT ornekleyerek) cevaplanabiliyordu.
        Artik sayacla cevaplanir.
        """
        with self._lost_probe_lock:
            return {
                "lost_probe_total": self._lost_probe_total,
                "forced_relink_total": self._forced_relink_total,
                "devices_probing": len(self._lost_probe),
                "data_silence_poll_total": self._veri_sessizligi_poll_total,
                # SU AN sessizlik epizodunda olanlar (kapanmis epizotlar hiz
                # siniri icin kayitta kalir ama BURAYA SAYILMAZ).
                "devices_alive_no_data": sum(1 for d in self._veri_sessizligi.values() if d.get("aktif")),
            }

    def _veri_sessizligini_yokla(
        self, mm: Any, device: DeviceConfig, *, now: float, veri_yasi: float
    ) -> None:
        """Cihaz cevap veriyor ama olcum bayat: periyodik integrity poll.

        BU FONKSIYON CIHAZ DURUMUNU DEGISTIRMEZ. Tek yaptigi soru sormak.
        Eskiden ayni kosul cihazi 'recovering' -> 'lost' zincirine sokuyor,
        SCADA'ya sahte comm_lost gonderiyor ve kesici komutunu bloke ediyordu.

        `_kopuk_cihazi_yokla`dan farki: o cihaz ZATEN 'lost' iken (kanit da yok)
        calisir ve sonuc alamazsa TCP oturumunu YIKAR. Burada kanit TAZE —
        yani calisan bir oturum var; onu yikmak (sahada olculdu: 4G'de yeniden
        baglanma ~3 dakika) toparlanmayi hizlandirmaz, geciktirir. Bu yuzden
        burada relink YOK, yalnizca sorgu var.
        """
        kod = device.code
        with self._lost_probe_lock:
            d = self._veri_sessizligi.get(kod)
            if d is None:
                # Ilk kez bu duruma girdi: hemen sor, sonra periyodik.
                d = {"son": 0.0, "uyarildi": 0.0, "aktif": 0.0}
                self._veri_sessizligi[kod] = d
            d["aktif"] = 1.0
            # HIZ SINIRI EPIZOTLAR ARASINDA DA GECERLI. `son` damgasi olcum
            # tazelendiginde SILINMEZ (bkz. `_veri_sessizligi_sifirla`): silinse
            # her tazelenme sayaci sifirlar ve sorgu araligi kendiliginden
            # "bayatlik esigi" kadar sikisirdi — 500 cihazda esik 120 sn iken
            # niyet edilenin iki kati trafik demekti.
            if d["son"] and (now - d["son"]) < _DATA_SILENCE_POLL_INTERVAL_SEC:
                return
            d["son"] = now
            self._veri_sessizligi_poll_total += 1
            ilk_kez = not d["uyarildi"]
            d["uyarildi"] = 1.0

        if ilk_kez:
            # Edge-trigger: durgun bir fiderde bu durum saatlerce surebilir,
            # her turda log basmak defteri doldururdu.
            logger.info(
                "yadnp3_veri_sessizligi device=%s ip=%s veri_yasi=%.0fs — cihaz "
                "CEVAP VERIYOR (kanit taze) ama olcum tazelenmiyor; kopuk "
                "SAYILMADI, integrity poll gonderiliyor",
                kod,
                device.ip_address,
                veri_yasi,
            )
        try:
            mm.request_integrity_poll()
        except Exception:  # noqa: BLE001
            logger.debug("yadnp3_veri_sessizligi_poll_error device=%s", kod, exc_info=True)

    def _veri_sessizligi_sifirla(self, device_code: str) -> None:
        """Olcum yeniden akmaya basladi: epizodu kapat.

        Kayit SILINMEZ, yalnizca pasife alinir — `son` damgasi hiz sinirinin
        epizotlar arasinda da gecerli olmasi icin korunur (bkz.
        `_veri_sessizligini_yokla`). `uyarildi` sifirlanir ki YENI bir sessizlik
        epizodu tekrar bir kez loglansin.
        """
        d = self._veri_sessizligi.get(device_code)
        if d is None or not d.get("aktif"):
            return
        with self._lost_probe_lock:
            d["aktif"] = 0.0
            d["uyarildi"] = 0.0

    def _kopuk_cihazi_yokla(self, mm: Any, device: DeviceConfig, *, connected: bool) -> None:
        """'lost' cihaza periyodik integrity poll; sonuc yoksa oturumu yenile.

        LINK KAPALIYKEN HICBIR SEY YAPMAYIZ: opendnp3 kanali zaten kendi
        TCP retry'ini suruyor (SYN gonderiyor). Araya girip master'i yeniden
        kurmak devam eden baglanti denemesini iptal eder ve toparlanmayi
        GECIKTIRIR. Bu yol yalnizca "soket acik gorunuyor ama veri yok"
        durumu icindir — 4G'de yari-acik soket senaryosu.
        """
        if not connected:
            return
        kod = device.code
        simdi = time.monotonic()
        aralik = getattr(self, "_lost_probe_interval_sec", 0.0) or _LOST_PROBE_INTERVAL_SEC
        esik = getattr(self, "_lost_relink_after_probes", 0) or _LOST_RELINK_AFTER_PROBES
        with self._lost_probe_lock:
            d = self._lost_probe.get(kod)
            if d is None:
                d = {"son": 0.0, "deneme": 0}
                self._lost_probe[kod] = d
            if simdi - d["son"] < aralik:
                return
            d["son"] = simdi
            d["deneme"] += 1
            deneme = int(d["deneme"])
            if deneme <= esik:
                self._lost_probe_total += 1
            else:
                self._forced_relink_total += 1

        if deneme <= esik:
            # Sessiz outstation'i konustur. Log DEBUG: 500 cihazli bir sahada
            # toplu kopmada her yoklamayi INFO basmak log'u bogar; olay
            # niteligindeki tek satir asagidaki relink uyarisidir.
            logger.debug(
                "yadnp3_lost_probe device=%s deneme=%d/%d — link acik gorunuyor "
                "ama veri gelmiyor, integrity poll gonderiliyor",
                kod,
                deneme,
                esik,
            )
            try:
                mm.request_integrity_poll()
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_lost_probe_error device=%s", kod, exc_info=True)
            return

        # ---- ZORLA RELINK'TEN VAZGECME KOSULLARI --------------------------
        # Bu uc kontrol sahada olculen bir dongunun sonucu (2026-08-11):
        # relink acik TCP oturumunu yikiyor, 4G'de yeniden baglanma ~3 dakika
        # suruyor ve cihaz hicbir zaman kararli bir pencere bulamiyordu.

        # 1) TAZE OTURUM: link henuz yeni acildiysa yikma, yoklamaya devam et.
        try:
            link_yasi = float(mm.cache.link_age())
        except Exception:  # noqa: BLE001
            link_yasi = -1.0
        if 0.0 <= link_yasi < _LOST_RELINK_MIN_LINK_AGE_SEC:
            logger.debug(
                "yadnp3_relink_ertelendi device=%s sebep=taze_oturum link_yasi=%.0fs min=%.0fs",
                kod,
                link_yasi,
                _LOST_RELINK_MIN_LINK_AGE_SEC,
            )
            self._relink_denemesini_geri_al(kod)
            return

        # 2) KOMUT UCUSTA: CROB gorevi master uzerinde asenkron kosar.
        # Ortasinda shutdown() cagrilirsa gorev cevap alamadan olur ve sonuc
        # CommandStatus.UNDEFINED doner — sahada tam olarak bu gorulduu.
        son_komut = float(getattr(mm, "last_command_at", 0.0) or 0.0)
        if son_komut and (simdi - son_komut) < _LOST_RELINK_COMMAND_GRACE_SEC:
            logger.info(
                "yadnp3_relink_ertelendi device=%s sebep=komut_ucusta yas=%.0fs — "
                "oturum yikilirsa komut cevapsiz kalir (CommandStatus.UNDEFINED)",
                kod,
                simdi - son_komut,
            )
            self._relink_denemesini_geri_al(kod)
            return

        # 3) USTEL BEKLEME: onceki relink ise yaramadiysa saniyede bir TCP
        # acip kapatmanin anlami yok; modem/hat uzerinde gereksiz yuk.
        with self._lost_probe_lock:
            d = self._lost_probe.setdefault(kod, {"son": simdi, "deneme": 0})
            bekleme = float(d.get("relink_bekleme") or 0.0)
            son_relink = float(d.get("son_relink") or 0.0)
        if son_relink and bekleme and (simdi - son_relink) < bekleme:
            logger.debug(
                "yadnp3_relink_ertelendi device=%s sebep=backoff kalan=%.0fs",
                kod,
                bekleme - (simdi - son_relink),
            )
            self._relink_denemesini_geri_al(kod)
            return

        # Yoklamalar sonuc vermedi: soket olu ama kapanmamis. Tek cikis
        # oturumu bastan kurmak. Master'i dusuruyoruz; bir sonraki cycle'da
        # `_ensure_master` yeni kanal + master yaratir.
        logger.warning(
            "yadnp3_forced_relink device=%s ip=%s — %d integrity poll cevapsiz "
            "kaldi, TCP oturumu zorla yeniden kuruluyor (yari-acik soket "
            "suphesi; 4G/GSM hatlarda modem RRC idle'a dusunce olusur)",
            kod,
            device.ip_address,
            esik,
        )
        try:
            mm.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("yadnp3_forced_relink_shutdown_error device=%s", kod, exc_info=True)
        with self._lock:
            if self._masters.get(kod) is mm:
                self._masters.pop(kod, None)
        # Yoklama sayacini sifirla ama BACKOFF'u koru: bir sonraki relink
        # daha uzun beklesin. `_lost_probe_sifirla` her seyi silseydi ard
        # arda relink dongusune girilirdi (sahada gorulen davranis).
        with self._lost_probe_lock:
            onceki = float((self._lost_probe.get(kod) or {}).get("relink_bekleme") or 0.0)
            yeni_bekleme = min(
                _LOST_RELINK_BACKOFF_MAX_SEC,
                onceki * 2.0 if onceki else _LOST_RELINK_BACKOFF_BASE_SEC,
            )
            self._lost_probe[kod] = {
                "son": 0.0,
                "deneme": 0,
                "son_relink": simdi,
                "relink_bekleme": yeni_bekleme,
            }

    def _relink_denemesini_geri_al(self, device_code: str) -> None:
        """Relink ertelendi — sayaci esikte tut, sonsuza kadar buyutme.

        Deneme sayaci esigin bir ustunde birakilirsa her yoklama araliginda
        yeniden relink denenir ve erteleme kontrolleri bosa doner. Esige geri
        cekmek, kosullar duzeldiginde relink'in bir sonraki turda calismasini
        saglar; `_forced_relink_total` sayaci da sismez.
        """
        esik = getattr(self, "_lost_relink_after_probes", 0) or _LOST_RELINK_AFTER_PROBES
        with self._lost_probe_lock:
            d = self._lost_probe.get(device_code)
            if d is not None:
                d["deneme"] = esik
            if self._forced_relink_total > 0:
                self._forced_relink_total -= 1

    def read_device(
        self,
        *,
        device: DeviceConfig,
        signals: list[SignalConfig],
    ) -> list[SignalReading]:
        # signals GECIRILIYOR: G110 okuma bloklari cihazin kendi sinyal
        # setinden turetiliyor (bkz. _g110_bloklari).
        mm = self._ensure_master(device, signals)
        cache = mm.cache
        connected = cache.is_connected()

        # G110 (string) okumasi — link ACILDIKTAN SONRA, POLL THREAD'inden.
        # `OnOpen` yalnizca "istendi" der (o callback opendnp3'un IO
        # thread'inden gelir; oradan ScanRange cagirmak riskli). Gercek istek
        # burada gonderilir. Basari, istegin gonderilmesiyle DEGIL cihazdan
        # gercekten bir G110 degeri gelmesiyle olculur; gelmezse `g110_gerekli`
        # ustel backoff ile yeniden izin verir. Deger gelince tekrar denenmez
        # (string'ler statik — periyodik scan YOK).
        if connected and cache.g110_gerekli():
            mm.scan_g110_once()
        elif cache.g110_tukendi_mi() and device.code not in self._g110_uyarilan:
            self._g110_uyarilan.add(device.code)
            logger.warning(
                "yadnp3_g110_okunamadi device=%s ip=%s deneme=%d — string "
                "(G110) degerleri cihazdan alinamadi; seri no / IMEI / IP / "
                "firmware alanlari BOS kalacak. Cihaz bu noktalari destekliyor "
                "mu ve sinyal index'leri dogru mu kontrol edin.",
                device.code,
                device.ip_address,
                _G110_MAX_DENEME,
            )

        # Stale-data guard: OpenDNP3'in `OnOpen`/`OnClose` callback'leri her
        # turde TCP kopmasinda tetiklenmeyebilir (channel auto-retry icin
        # session ayakta gorunmeye devam edebilir). Bu yuzden link flag'ine
        # ek olarak "son ses cikardigindan bu yana gecen sure"ye de bakariz.
        # Esik `_bayatlik_esigi_sn` ile TEK yerde hesaplanir (komut yolundaki
        # ulasilabilirlik olcutuyle ayni deger olmasi sart).
        threshold = _bayatlik_esigi_sn(self._scan_interval_sec, self._baseline_interval_sec)
        # MONOTONIC: bayatlik bir SUREDIR, tarih degil. Duvar saatiyle
        # olculdugunde tek bir NTP siçramasi (ya da RTC'si bos bir sahada
        # acilis sonrasi saat duzeltmesi) TUM cihazlari ayni anda "bayat"
        # yapip toplu sahte comm_lost dalgasi uretirdi.
        #
        # BAYATLIK ARTIK KANIT SAATINDEN URETILIYOR (veri saatinden DEGIL).
        # "Veri gelmiyor" ile "haberlesme yok" ayni sey degildir: event-driven
        # modda degeri degismeyen saglikli bir cihaz veri URETMEZ ama her
        # tarama turunda cevap verir. Eski kod bu ikisini ayirt edemedigi icin
        # sessiz cihazi kopuk ilan ediyordu (bkz. `_last_evidence_at`).
        # `set()` kanit saatini de besledigi icin kanit_yasi <= veri_yasi;
        # yani bu satir sahte kopmayi kaldirir, GERCEK kopmayi geciktirmez:
        # kanit da susmussa esik yine ayni anda dolar.
        last_update = cache.last_update_at()
        last_evidence = cache.last_evidence_at()
        now = time.monotonic()
        stale = last_evidence == 0.0 or (now - last_evidence) > threshold
        # Olcum sessizligi AYRI bir gozlemdir ve comm_lost URETMEZ; yalnizca
        # "cihaza tekrar sor" tetigidir (asagida).
        veri_bayat = last_update == 0.0 or (now - last_update) > threshold

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

        # CIHAZ SES VERDIYSE YOKLAMAYI SIFIRLA — durum makinesinden BAGIMSIZ.
        # Taze veri geliyorsa cihaz konusuyor demektir; relink sayacinin
        # ilerlemeye devam etmesi anlamsiz. Eskiden sifirlama yalnizca
        # relink/online yollarina bagliydi; cihaz yoklama penceresinde geri
        # gelip durum makinesi henuz 'lost'ta iken sayac ilerlemeye devam
        # edebiliyor ve calisan bir oturum yikilabiliyordu.
        if connected and not stale:
            self._lost_probe_sifirla(device.code)

        # CIHAZ KONUSUYOR AMA OLCUM GELMIYOR -> KOPUK ILAN ETME, SOR.
        # ------------------------------------------------------------
        # Kanit taze (outstation cevapliyor) ama olcum bayat. Iki sebebi olur:
        #   a) NORMAL: durgun bir fiderde hicbir deger degismiyor. Yapilacak
        #      bir sey yok, cihaz saglikli.
        #   b) ARIZA: Class 0 integrity poll'u dusuyor (yavas link, buyuk
        #      fragment, event tamponu sorunu). Cihaz "yasiyorum" diyor ama
        #      tablo tazelenmiyor.
        # Ikisini disaridan ayirt edemeyiz; ikisinde de DOGRU hamle ayni:
        # cihaza yeniden sormak. Eski kod bunun yerine cihazi comm_lost ilan
        # ediyordu — hem yanlis hem de kesici komutunu bloke eden karar.
        # Kopuk cihaz yoklamasinin (`_kopuk_cihazi_yokla`) bu duruma karsiligi
        # YOKTU: o yalnizca cihaz ZATEN 'lost' iken devreye giriyor.
        if connected and not stale and veri_bayat:
            self._veri_sessizligini_yokla(
                mm, device, now=now, veri_yasi=(now - last_update) if last_update else -1.0
            )
        elif not veri_bayat:
            self._veri_sessizligi_sifirla(device.code)

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

        # KALICI 'lost' KILIDINDEN CIKIS
        # -----------------------------
        # 'lost' durumundan cikisin TEK yolu OnOpen callback'iydi. TCP soketi
        # hic kopmazsa (4G modem RRC idle, yavas outstation) OnClose/OnOpen bir
        # daha TETIKLENMEZ. Senaryo: cihaz 240sn veri gondermiyor -> stale-edge
        # recovering yapiyor -> cevap grace'ten (15sn) SONRA, orn. 22. saniyede
        # geliyor -> fail_recovery zaten calismis, state='lost'. Bundan sonra
        # cihaz DNP3'te SAGLIKLI konusmaya devam etse, cache degerleri
        # guncellense bile gateway o cihazin TUM sinyallerini SONSUZA KADAR
        # comm_lost/no_change yayinliyordu. Ustelik operate_crob da
        # state != "online" diye komut gondermeyi reddediyordu — cihaz
        # calisirken kesici komutu verilemiyordu.
        #
        # Cozum: link ACIK ve TAZE veri geliyorsa (stale degil) 'lost'tan
        # recovery'e gec; ilk fresh frame state'i online'a yukseltir.
        if cache_state == "lost" and connected and not stale:
            cache.begin_recovery()
            cache_state = "recovering"
            logger.info(
                "yadnp3_device_relink device=%s ip=%s last_data_age=%ss — link acik "
                "ve veri taze; kalici 'lost' durumundan cikiliyor",
                device.code,
                device.ip_address,
                int(now - last_update) if last_update else "?",
            )
            # Cihaz yeniden konusuyor: yoklama/relink sayaclarini sifirla.
            self._lost_probe_sifirla(device.code)
            try:
                mm.request_integrity_poll()
            except Exception:  # noqa: BLE001
                logger.debug("yadnp3_relink_integrity_poll_error", exc_info=True)

        # KOPUK CIHAZI KENDILIGINDEN YOKLA
        # --------------------------------
        # Buraya gelindiginde cihaz 'lost' ve yukaridaki relink kosulu
        # tutmadi (yani veri hala gelmiyor). Eskiden bu noktada gateway
        # cihaza HICBIR SEY sormuyor, pasif bekliyordu; kilidi ancak
        # operatorun manuel refresh'i kiriyordu. Artik periyodik olarak
        # kendimiz soruyoruz, sonuc alamazsak oturumu yeniliyoruz.
        if cache_state == "lost":
            self._kopuk_cihazi_yokla(mm, device, connected=connected)

        # Cihaz "online" degil mi? comm_lost yayini.
        # Ilk edge'de quality=comm_lost, sonrakilerde no_change (mesaj flood'unu
        # onlemek icin).
        if cache_state != "online":
            # Bayrak BURADA set edilmez; yayin kalicilastiktan sonra
            # `commit_published` -> `confirm_stale_announced` ile isaretlenir.
            # Aksi halde kalicilastirilamayan (disk dolu) tek bir comm_lost
            # yayini cihazi SCADA'da sonsuza kadar canli gosterirdi.
            already_announced, stale_token = cache.begin_stale_announce()
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
                    read_token=None if already_announced else stale_token,
                )
                for s in signals
            ]

        # Buradan sonrasi: state == "online". Cihaz cevapliyor.
        # Recovery confirmed olduysa (set() flag'i tetiklemis) bir kerelik
        # mark_all_dirty + log. Boylece SCADA tum sinyalleri quality=good ile
        # gorur; cihazin son bilinen degerleri yayilanir.
        # Cihaz konusuyor: yoklama sayaclarini sifirla ki bir sonraki
        # kopmada relink esigi bastan baslasin.
        self._lost_probe_sifirla(device.code)

        if cache.consume_recovery_publish():
            cache.reset_stale_announce()
            redirty_count = cache.mark_all_dirty()
            logger.info(
                "yadnp3_device_recovered device=%s ip=%s redirty=%d (fresh frame ile dogrulandi)",
                device.code,
                device.ip_address,
                redirty_count,
            )

        readings: list[SignalReading] = []
        for s in signals:
            # TEK atomik cagri: dirty kontrolu + deger okuma + surum, hepsi ayni
            # lock altinda. Eski kod bunu uc ayri lock alimiyla yapiyordu
            # (get -> is_dirty -> clear_dirty) ve aralarda DNP3 IO thread'i
            # yazabildigi icin yeni olcumler KALICI olarak kayboluyordu.
            taken = cache.peek_if_dirty(s.dnp3_object_group, s.dnp3_index)
            if taken is None:
                # Ya henuz hic okunmadi (yeni bagi/yeni nokta) ya da bu cycle'da
                # degismedi: 'no_change' — frontend son iyi degeri korur,
                # poller bunu yayinlamaz.
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
            raw, value_string, dnp3_flags, version, cihaz_zamani, damga_kalitesi = taken
            # DAMGAYI DOGRULA. Cihaz saatine guvenilmez: RTC pili biten bir
            # gosterge 2000-01-01'den sayar ve o damgayi oldugu gibi kabul
            # etmek olay verisini 26 yil geriye tasirdi.
            # Reddedilen damga yalnizca DUSER; olcum yayinlanmaya devam eder.
            cihaz_zamani, damga_kalitesi = dogrula_cihaz_zamani(cihaz_zamani, damga_kalitesi, time.time())
            scaled = raw * s.scale + s.offset
            # Kalite: cihazin bildirdigi DNP3 bayraklarindan turetilir.
            # Bayrak byte'i TIPE GORE okunur — `s.dnp3_object_group` sart
            # (G3'te 0x40/0x80 kesici pozisyonu, G30'da REFERENCE_ERR).
            # `publish_dnp3_quality` KAPALIYKEN (varsayilan) geriye uyum icin
            # "good" yayinlanir. Backend sozlugu v2.28.0'dan beri taniyor;
            # bayrak kapali cunku acilis KADEMELI olacak (once tek test
            # gateway'i + gercek cihaz dogrulamasi). Bkz. docs/BACKEND_TODO.md#B1
            mapped_quality = map_dnp3_quality(dnp3_flags, s.dnp3_object_group)
            # FAIL-SAFE gorunur olsun: kalite tasimasi GEREKEN bir gruptan
            # bayraksiz olcum geldiyse deger `invalid` yayinlanir. Sessiz
            # kalmasi, bir binding regresyonunun sahada aylarca fark
            # edilmemesi demek olurdu. Nokta basina TEK satir (log spam yok).
            if dnp3_flags is None and s.dnp3_object_group in _KALITE_TASIYAN_GRUPLAR:
                if self._bayrak_uyarisi_ver(s.dnp3_object_group, s.dnp3_index):
                    logger.warning(
                        "dnp3_missing_quality_flags device=%s signal=%s group=%s index=%s — "
                        "bu tip kalite bayragi TASIMALI ama okunamadi; fail-safe olarak "
                        "quality=invalid degerlendiriliyor (yayin ancak "
                        "DNP3_PUBLISH_QUALITY_FLAGS=true iken etkilenir)",
                        device.code,
                        s.key,
                        s.dnp3_object_group,
                        s.dnp3_index,
                    )

            # ---- NaN / Inf KORUMASI ------------------------------------------
            # G30v5/v6 (float/double) noktalarda outstation olculemeyen bir
            # degeri IEEE-754 NaN veya Inf olarak raporlayabilir (arizali CT
            # kanali, akim=0 iken cos-fi, bolme hatasi...). Bu deger payload'a
            # girerse zincir su sekilde kiriliyordu:
            #   requests json.dumps(allow_nan=False) -> InvalidJSONError
            #     -> requests.RequestException -> HttpTelemetryNotReadyError
            #     -> is_transient()=True -> retrier `mark_retry` CAGIRMAZ
            #     -> next_attempt_at ILERLEMEZ -> satir kuyrugun basinda kalir
            # Yani 0.5.0'in tam da onlemek icin yazildigi HEAD-OF-LINE BLOCKING
            # bu yolda geri geliyor ve TUM outbox kalici olarak tikaniyordu.
            # Ustelik `nan != nan` oldugu icin nokta her cycle yeniden dirty
            # isaretleniyor ve kuyruga surekli yeni NaN satiri ekleniyordu.
            #
            # Deger yerine None yayinliyoruz: sahte bir sayi (0.0) gondermek
            # SCADA'da gercek bir olcum gibi gorunurdu.
            if not math.isfinite(scaled):
                readings.append(
                    SignalReading(
                        signal_key=s.key,
                        source=s.source,
                        data_type=s.data_type,
                        raw_value=0.0,
                        scaled_value=None,
                        quality="invalid",
                        value_string=None,
                        dnp3_flags=dnp3_flags,
                        device_event_at=cihaz_zamani,
                        timestamp_quality=damga_kalitesi,
                        read_token=(s.dnp3_object_group, s.dnp3_index, version),
                    )
                )
                if self._nan_uyarisi_ver(s.dnp3_object_group, s.dnp3_index):
                    logger.warning(
                        "dnp3_non_finite_value device=%s signal=%s group=%s index=%s "
                        "raw=%r scale=%s offset=%s — deger sayisal degil; "
                        "quality=invalid ile yayinlaniyor",
                        device.code,
                        s.key,
                        s.dnp3_object_group,
                        s.dnp3_index,
                        raw,
                        s.scale,
                        s.offset,
                    )
                continue

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
                    quality=mapped_quality if self._publish_dnp3_quality else "good",
                    value_string=value_string,
                    # Ham bayrak byte'i her durumda tasinir: teshis icin
                    # (/metrics, log) kullanilabilir ve backend hazir oldugunda
                    # yayina eklemek tek bayrak degisikligi olur.
                    dnp3_flags=dnp3_flags,
                    # Dirty bayragi BURADA TEMIZLENMEZ. Poller yayini
                    # kalicilastirdiktan sonra commit_published ile bu token'i
                    # geri verir; ancak o zaman ve surum hala ayniysa temizlenir.
                    read_token=(s.dnp3_object_group, s.dnp3_index, version),
                )
            )
        return readings

    def _nan_uyarisi_ver(self, group: int, index: int) -> bool:
        """Bu nokta icin NaN uyarisi DAHA ONCE verilmedi mi?

        Arizali bir nokta her cycle (1-2sn) yeniden okunur; uyariyi her
        seferinde basmak log'u doldurur ve gercek arizalari gizler.
        """
        anahtar = (group, index)
        with self._nan_lock:
            if anahtar in self._nan_uyarilan:
                return False
            self._nan_uyarilan.add(anahtar)
            return True

    def _bayrak_uyarisi_ver(self, group: int, index: int) -> bool:
        """Bu nokta icin "kalite bayragi eksik" uyarisi DAHA ONCE verilmedi mi?

        `_nan_uyarisi_ver` ile ayni gerekce: arizali nokta her cycle yeniden
        okunur, uyari her seferinde basilirsa log dolar.
        """
        anahtar = (group, index)
        with self._nan_lock:
            if anahtar in self._bayrak_uyarilan:
                return False
            self._bayrak_uyarilan.add(anahtar)
            return True

    @property
    def io_thread_count(self) -> int:
        """Secilen opendnp3 IO thread sayisi.

        Log'a YAZILMASI icin acildi: eskiden yalnizca "auto" yaziliyordu ve
        heuristigin gercekte ne sectigi hicbir yerde gorunmuyordu — operator
        olcegi buyuturken ayarin tuttugunu dogrulayamiyordu.
        """
        return self._manager_threads

    def device_health(self) -> dict[str, dict[str, Any]]:
        """Cihaz basina DNP3 haberlesme durumu (health/metrics icin).

        Lock yalnizca master listesinin anlik kopyasini almak icin tutulur;
        cache sorgulari lock disinda yapilir (health endpoint'i poll akisini
        bloke etmemeli).
        """
        with self._lock:
            masters = list(self._masters.items())
        out: dict[str, dict[str, Any]] = {}
        for code, mm in masters:
            try:
                # Health tuketicisi bunu `time.time()` ile karsilastiriyor,
                # dolayisiyla DUVAR saati karsiligi gonderilir. Bayatlik
                # KARARI ise monotonic damga uzerinden verilir (bkz.
                # `last_update_at`) — ikisi kasitli olarak ayri.
                last = mm.cache.last_update_wall()
                # KANIT ve VERI yaslarini AYRI raporla. Sahada "cihaz kopuk mu
                # yoksa sadece degeri mi degismiyor" sorusu tek bir yasa
                # bakarak cevaplanamiyordu; teshis ancak gateway log'una
                # girilerek yapilabiliyordu.
                kanit_yasi = mm.kanit_yasi()
                veri_yasi = -1.0
                son_veri = mm.cache.last_update_at()
                if son_veri:
                    veri_yasi = time.monotonic() - son_veri
                out[code] = {
                    "state": mm.cache.state(),
                    "connected": mm.cache.is_connected(),
                    "last_frame_epoch": last or None,
                    "pending_signals": mm.cache.dirty_count(),
                    "known_points": mm.cache.size(),
                    "evidence_age_sec": round(kanit_yasi, 1) if kanit_yasi >= 0 else None,
                    "data_age_sec": round(veri_yasi, 1) if veri_yasi >= 0 else None,
                    "reachable": mm.ulasilabilir(),
                }
            except Exception:  # noqa: BLE001
                out[code] = {"state": "unknown", "last_frame_epoch": None}
        return out

    def commit_published(
        self,
        *,
        device: DeviceConfig,
        readings: list[SignalReading],
    ) -> int:
        """Yayin kalicilastiktan sonra dirty bayraklarini temizle.

        Yalnizca `read_token` tasiyan (yani gercekten yayinlanan) okumalar
        dikkate alinir. Surum uyusmuyorsa — okuma ile yayin arasinda cihazdan
        yeni bir olcum geldiyse — bayrak KORUNUR ve yeni deger bir sonraki
        cycle'da yayinlanir.

        Iki jeton turu var:
          * `(group, index, surum)` — normal olcum; dirty bayragi temizlenir.
          * `("stale", kusak)`      — comm_lost duyurusu; duyuru bayragi
            ancak yayin kalicilastiktan SONRA set edilir.
        """
        mm = self._masters.get(device.code)
        if mm is None:
            return 0
        items: list[tuple[int, int, int]] = []
        stale_token: Any = None
        for r in readings:
            token = r.read_token
            if isinstance(token, tuple) and len(token) == 3:
                items.append(token)
            elif isinstance(token, tuple) and len(token) == 2 and token[0] == "stale":
                stale_token = token
        if stale_token is not None and mm.cache.confirm_stale_announced(stale_token):
            logger.info(
                "yadnp3_comm_lost_announced device=%s — comm_lost yayini kalicilasti",
                device.code,
            )
        if not items:
            return 0
        return mm.cache.commit_published(items)

    def operate_device(
        self,
        *,
        device: DeviceConfig,
        index: int,
        op_type: str = "latch_on",
        count: int = 1,
        on_time_ms: int = 0,
        off_time_ms: int = 0,
        timeout_sec: float = 10.0,
        mode: str = "direct",
    ) -> dict[str, Any]:
        """Var olan master uzerinden DNP3 CROB komutu gonderir.

        Okuma/recovery davranisina dokunmaz; _ensure_master ile mevcut master'i
        alir (yoksa read path ile ayni sekilde acilir) ve operate_crob cagirir.
        Default LATCH_ON + DirectOperate (Device Profile PULSE desteklemez; bu
        cihazda SBO SELECT fazinda NOT_SUPPORTED donuyor, DirectOperate calisiyor).

        Toplam islem suresi burada olculup sonuca eklenir.

        F6 — PARAMETRE DOGRULAMASI BURADA, `_ensure_master`'DAN ONCE
        ---------------------------------------------------------
        Bu metod fiziksel komutun TEK giris noktasidir: hem kuyruk yolu
        (`main._execute_pending_commands`) hem `POST /operate` buradan gecer.
        Dogrulama en basta yapilir ki gecersiz bir parametre DNP3 yiginina
        HIC dokunmasin — `_ensure_master` gerektiginde oturum acar.
        """
        import time as _t

        parametre = validate_command_parameters(
            op_type=op_type,
            count=count,
            on_time_ms=on_time_ms,
            off_time_ms=off_time_ms,
        )
        if not parametre.valid:
            logger.warning(
                "dnp3_command_parameters_rejected device=%s index=%s op_type=%r "
                "count=%r on_time_ms=%r off_time_ms=%r reason=%s detail=%s — "
                "CROB OLUSTURULMADI, DNP3 oturumuna dokunulmadi",
                device.code,
                index,
                op_type,
                count,
                on_time_ms,
                off_time_ms,
                parametre.status,
                parametre.detail,
            )
            return {
                "ok": False,
                "status": parametre.status,
                "error": parametre.detail,
                "device_code": device.code,
                "index": index,
                "control": op_type,
            }

        mm = self._ensure_master(device)
        t0 = _t.monotonic()
        result = mm.operate_crob(
            index=index,
            op_type=op_type,
            count=count,
            on_time_ms=on_time_ms,
            off_time_ms=off_time_ms,
            timeout_sec=timeout_sec,
            mode=mode,
        )
        elapsed_ms = int((_t.monotonic() - t0) * 1000)
        out = {
            "device_code": device.code,
            "index": int(index),
            "outstation": device.dnp3_address,
            "object_group": 12,
            "variation": 1,
            "duration_ms": elapsed_ms,
            **result,
        }
        # Detayli komut logu (basarili/basarisiz — gercek DNP3 status ile).
        logger.info(
            "dnp3_command_result device=%s outstation=%s object=12 variation=1 "
            "index=%s control=%s mode=%s ok=%s status=%s dnp3_task=%s dnp3_status=%s "
            "dnp3_state=%s duration_ms=%s error=%s",
            device.code,
            device.dnp3_address,
            index,
            out.get("control"),
            out.get("mode"),
            out.get("ok"),
            out.get("status"),
            out.get("dnp3_task"),
            out.get("dnp3_status"),
            out.get("dnp3_state"),
            elapsed_ms,
            out.get("error"),
        )
        return out

    def forget_devices(self, active_device_codes: set[str]) -> int:
        """Backend config'inden cikarilmis cihazlarin master/channel'larini kapat.

        Aksi halde silinen cihazlarin TCP baglanti deneme kanalı acik kalir;
        ayni IP+port'a baglanmaya calisip diger cihazlarla flap yapar (zombie
        master). Config refresh akisinda her seferinde cagirilir.
        """
        cleaned = 0
        with self._lock:
            stale_codes = [c for c in self._masters if c not in active_device_codes]
            for code in stale_codes:
                mm = self._masters.pop(code, None)
                if mm is None:
                    continue
                try:
                    mm.shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("yadnp3_master_shutdown_error device=%s", code, exc_info=True)
                cleaned += 1
                logger.info("yadnp3_master_forgotten device=%s reason=removed_from_config", code)
        return cleaned

    def refresh_all_devices(self) -> tuple[int, int]:
        """Aktif tum cihazlara ANINDA full integrity poll + son bilinen tum
        degerleri yeniden yayinlat.

        Kullanim: SCADA/operator "tum cihazlara sorgu at" tetiklerse, ya da
        backend tarafinda veri kaybolduysa (DB restore, tag-engine sifirlanmasi)
        hat boyunca tum sinyallerin guncel degerini DB'ye yazmak icin manuel
        tetik.

        ONEMLI — ESKI DAVRANIS NO-OP IDI:
        Bu fonksiyon eskiden SADECE `request_integrity_poll()` cagiriyordu.
        Integrity poll cevabi `_DeviceCache.set()`'e duser ve orada deger
        DEGISMEDIYSE dirty isaretlenmez; dolayisiyla degeri sabit olan tum
        sinyaller icin refresh-all HICBIR mesaj uretmiyordu. Yani delta-only
        yayinin TEK telafi mekanizmasi calismiyordu: backend'i restore eden
        operator "tum cihazlara sorgu at" dedikten sonra da eksik degerlerle
        kaliyordu ve durum ancak sahada deger fiziksel olarak degisirse
        duzeliyordu (kesici pozisyonu gibi statik sinyallerde: hic).

        Artik integrity poll ISTEGIYLE BIRLIKTE `mark_all_dirty()` cagirilir;
        cihaz cevap vermese bile son bilinen degerler bir sonraki cycle'da
        quality=good ile yayinlanir.

        Returns: (basarili_count, toplam_master_count).
        """
        ok = 0
        redirty_total = 0
        # Lock'u yalnizca master listesinin ANLIK kopyasini almak icin tut;
        # integrity poll / mark_all_dirty cagrilari lock DISINDA yapilir.
        # Aksi halde bu islemler (native IO dahil) adapter lock'unu saniyelerce
        # tutar ve o sirada _ensure_master bekleyen poll worker'lari + komut
        # thread'i bloke olur.
        with self._lock:
            masters = list(self._masters.items())
        total = len(masters)
        for code, mm in masters:
            try:
                # Once yeniden-yayin bayraklari: cihaz cevap vermese bile son
                # bilinen degerler yayinlanir (asil telafi budur).
                redirty_total += mm.cache.mark_all_dirty()
                if mm.request_integrity_poll():
                    ok += 1
                    logger.info("yadnp3_integrity_poll_requested device=%s", code)
                else:
                    logger.warning("yadnp3_integrity_poll_failed device=%s", code)
            except Exception:  # noqa: BLE001
                logger.exception("yadnp3_integrity_poll_error device=%s", code)
        logger.info(
            "yadnp3_refresh_all devices=%d poll_queued=%d redirty_signals=%d",
            total,
            ok,
            redirty_total,
        )
        return ok, total

    def close(self) -> None:
        with self._lock:
            for mm in list(self._masters.values()):
                try:
                    mm.shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("yadnp3_master_shutdown_error", exc_info=True)
            self._masters.clear()
        try:
            self._manager.Shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("yadnp3_manager_shutdown_error", exc_info=True)
