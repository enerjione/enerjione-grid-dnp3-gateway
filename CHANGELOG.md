# Changelog

Semver'a gore tutulur. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.7.3] - 2026-08-14

Gateway, bekleyen komutlarin YASINI dogrulayabilecek hale getirildi (F3A —
gateway yarisi). **Mevcut backend ile davranis DEGISMEDI**; backend
`created_at` alanini pending payload'ina henuz koymuyor.

### Security

- **Komut zincirinde YAS kavrami yoktu.** `PendingCommand` zaman alani
  tasimiyordu, backend'in `/pending` sorgusunda yas filtresi yok (yalnizca
  `status='pending'`) ve gateway gelen komutu kac saat once uretildigine
  bakmadan calistiriyordu.

  Somut senaryo: backend'de komut olusturuluyor, gateway 30 dakika kapali
  kaliyor (bakim/deploy/elektrik), acildiginda komut hala `pending` ve OLDUGU
  GIBI calisiyor. Kuyrukta bekleyen `master.firmware_update` ya da
  `master.software_reset` icin bu kabul edilemez.

  Yeni `command_freshness.validate_command_freshness()` saf, yan etkisiz bir
  fonksiyon olarak tazeligi dogrular. Red sonuclari: `expired`,
  `command_timestamp_missing`, `command_timestamp_invalid`,
  `command_timestamp_future` — hepsinde `operate_device` CAGRILMAZ.

  Enforcement `_execute_pending_commands` icinde, `start_dispatch`ten SONRA
  ve F1/F2'den ONCE: tazelik istegin ICERIGINDEN bagimsiz bir ozelliktir,
  "bu istegi hic degerlendirmeli miyiz" sorusu "dogru noktayi mi gosteriyor"
  sorusundan once gelir. Red terminal sonuc uretir, ledger'a yazilir,
  backend'e teslim edilir ve ayni komut sonraki poll'larda YENIDEN
  degerlendirilmez.

### Added

- `PendingCommand.created_at` (opsiyonel). **Ham metin olarak tasinir**,
  parse EDILMEDEN: parser'da ayristirilsaydi bozuk bir damga komutu sessizce
  dusururdu ve backend sonucu hicbir zaman ogrenemezdi.
- `COMMAND_MAX_AGE_SEC` (varsayilan **120**). Sahada olculerek secildi:
  komutun kuyruktan gateway'e teslimi p95=0.92 sn, uctan uca en kotu gozlem
  10.1 sn (adapter CROB timeout'u). 120 sn bir deploy/restart penceresini de
  (30-60 sn) kapsar; daha kisa degerler deploy sirasinda mesru komutlari
  sahte `expired` yapardi.
- `COMMAND_REQUIRE_TIMESTAMP` (varsayilan **false**). Damgasiz komut
  reddedilsin mi. Bu bir **GECIS** bayragidir, TTL'yi kapatan bir feature
  flag DEGILDIR: `created_at` geldigi anda azami yas her kosulda uygulanir
  ve bayrak bunu bypass ETMEZ. Backend alani gondermeye basladiktan sonra
  `true` yapilacak; damgasiz komutu kalici olarak kabul etmek bir cozum
  degildir.

### Notes

- **Mevcut backend ile davranis degisikligi YOK.** `created_at` gelmedigi ve
  bayrak kapali oldugu icin komutlar bugunku gibi calisir; tek gozlemlenebilir
  fark, gercek bir komut geldiginde basilan
  `command_timestamp_missing_legacy_allowed` uyarisidir (bos poll'de
  basilmaz — log storm yok). Gecisin sessizce kalicilasmamasi icin kasitli.
- Damga GONDERILMIS ama bozuk/timezone'suz ise bu bir gecis durumu degil,
  bozuk veridir: bayraktan bagimsiz fail-closed.
- Tolerans disinda GELECEKTEKI damga reddedilir; kabul edilseydi komut
  olumsuz olurdu (yasi hicbir zaman TTL'yi asmazdi). Tolerans 60 sn.
- Saat karsilastirmasi gateway'in kendi UTC saatiyle yapiliyor. Backend'in
  HTTP `Date` basligindan skew-bagisik yas hesabi F3B'de degerlendirilecek;
  bu surumde config client'in yanit yolu DEGISTIRILMEDI.
- `POST /operate` TTL kapsam DISI (payload zaman damgasi tasimiyor);
  F1/F2 korumasi aynen yerinde.
- Backend deposunda hicbir degisiklik yok; DB migration gerekmez
  (`result_status` serbest `String(40)`, enum/CHECK kisiti yok).
- Testler 538 -> 576.

## [1.7.2] - 2026-08-14

Fiziksel DNP3 cikis komutlari artik cihazin KENDI sinyal katalogu ile
yetkilendiriliyor. Telemetri, kalite ve komut yurutme akislari DEGISMEDI.

### Security

- **Backend'den gelen `dnp3_index` hicbir yerde dogrulanmiyordu (F1).**
  Cihazlarin konfigure `binary_output` listesi elde OLDUGU HALDE
  yetkilendirmede kullanilmiyordu; gelen index oldugu gibi CROB'a
  ceviriliyordu. Reddi yalnizca outstation verebiliyordu.

- **`PendingCommand.command` parse edilip ATILIYORDU (F2)** — repoda tek bir
  okuyucusu yoktu, yani komut NIYETI ile index'in ayni noktayi gosterdigi
  hicbir yerde kontrol edilmiyordu.

  Ikisinin birlikte neyi mumkun kildigi somuttur; saha katalogundan:

  ```
  index  7 -> master.reset_all_fcis      (rutin, sahada kullanilan komut)
  index  2 -> master.firmware_update
  index 22 -> master.modem_firmware_ota
  index 23 -> master.software_reset
  ```

  "FCI sifirla" niyetiyle gonderilmis ama index'i 2 olan tek bir komut, saha
  cihazinda FIRMWARE GUNCELLEMESI baslatiyordu. Cihaz hata dondurmez;
  istenmeyen seyi YAPAR. Yalnizca index allowlist'i (F1) bunu DURDURMAZ —
  index 2 katalogda gecerli bir output'tur; durduran sey niyet
  dogrulamasidir (F2).

  Yeni `command_authorization.authorize_output_command()` her iki kontrolu
  tek, saf, yan etkisiz bir fonksiyonda yapar. Fail-closed sonuclar:
  `command_missing`, `catalog_unavailable`, `index_not_authorized`,
  `command_index_mismatch`. Hepsinde `operate_device` CAGRILMAZ.

  **Sozlesme uydurulmadi.** Backend ham index kabul etmiyor; index'i kendi
  katalogundan `SignalCatalog.key == f"master.{slug}"` ile turetiyor.
  Gateway artik ayni cozumlemeyi BAGIMSIZ dogruluyor (defense-in-depth).
  Karsilastirma literal — case-fold / trim / alias / fuzzy YOK, cunku gevsek
  eslestirme fail-OPEN yonunde calisir.

  **Model izolasyonu:** yetkilendirme cihazin KENDI setine karsi yapilir
  (`state.signals_for`), global index listesine karsi DEGIL. Olculdu:
  `master.boost_mode` SN 2.0'da index 26, Pole Master Kit'te 30 — global
  liste ikisini de kabul eder ve komutu yanlis noktaya gonderirdi.

  **Enforcement tek noktada:** `/pending` ve `config.pending_commands` ayni
  `_pending_commands` kuyrugundan gectigi icin tek cagri ikisini de kapsar.
  `POST /operate` de ayni fonksiyonu uygular.

  Reddedilen komut SESSIZCE DUSMEZ: kalici sonuc uretir, ledger'a yazilir,
  backend'e bildirilir ve `start_dispatch` tuketildigi icin sonraki
  poll'larda tekrar islenmez.

### Changed

- **`POST /operate` govdesinde `command` alani ZORUNLU.** Slug olmadan komut
  niyeti dogrulanamaz; bos birakilirsa `command_missing` ile reddedilir.
  Sozlesme kirilma riski olculdu: filodaki hicbir gateway'de
  `command_token` tanimli degil (endpoint her yerde 503) ve backend'de bu
  endpoint'i cagiran kod yok — canli cagiran olmadigi icin siki sozlesme
  ilk kullanicisi olmadan once konuldu.

### Notes

- Kapsam disi birakilanlar (ayri ele alinacak): komut TTL/`expires_at`,
  HMAC imzasinin zorunlu kilinmasi, `/pending` icin ayri credential,
  parametre bounds, Direct/SBO politikasi.
- Geriye uyum: uretimdeki komut gecmisine karsi dry-run yapildi;
  cozumlenebilen 8/8 komut AUTHORIZED kaldi, sifir regresyon. Testler
  503 -> 538.

## [1.7.1] - 2026-08-14

DNP3 kalite bayragi eslemesi **tip-kordu** ve bu haliyle yayina acilsaydi
sahayi bozacakti. Yayinlanan kalite sozlugu ve telemetri govdesi DEGISMEDI;
`DNP3_PUBLISH_QUALITY_FLAGS` varsayilani hala `false`.

### Fixed

- **Her ACIK kesici `quality=invalid` yayinlanacakti.** `map_dnp3_quality`
  bayrak byte'ini tek bir tabloyla okuyordu, oysa 5/6/7. bitler object
  group'a gore TAMAMEN farkli anlam tasir. Kutuphaneyle dogrulandi:
  G3 double-bit'te `0x40`=STATE1, `0x80`=STATE2 KESICI POZISYONUDUR —
  `DETERMINED_OFF -> 0x41`, `DETERMINED_ON -> 0x81`,
  `INDETERMINATE -> 0xC1`. Eski esleme `0x41`'i "REFERENCE_ERR" sanip
  acik kesiciyi gecersiz olcum ilan ediyordu. G3 bu depoda tam olarak
  kesici pozisyonu icin okunuyor.

  Ayni kok neden diger gruplarda da vardi: G1'de `0x20` OVER_RANGE degil
  CHATTER_FILTER, `0x40` RESERVED; G10'da ikisi de RESERVED; G20/G21'de
  ROLLOVER / DISCONTINUITY. Hicbiri "bu deger guvenilmez" demez — bunlar
  ham `dnp3_flags` byte'inda korunur, kalite token'ini bozmaz.
  `OVER_RANGE`/`REFERENCE_ERR` yorumu artik YALNIZCA analog gruplarda
  (G30/G40) yapiliyor.

  Imza `map_dnp3_quality(flags, object_group)` oldu; `object_group`
  ZORUNLU — varsayilan verilseydi tip-kor cagri sessizce geri gelirdi.
  Bit tablosu gercek opendnp3 enum'larina karsi pinlendi
  (`test_bayrak_tablosu_kutuphaneyle_uyumlu`), yani bir yadnp3
  yukseltmesi anlam degistirirse CI kirilir.

- **Bayrak okunamadiginda olcum sessizce `good` sayiliyordu.** Artik tipe
  gore degerlendiriliyor: kalite tasimasi GEREKEN gruplarda bayrak yoksa
  fail-safe `invalid` + nokta basina tek WARNING (`dnp3_missing_quality_flags`).
  G110 OctetString ise kalite byte'i TASIMAZ (SOE handler onu bayraksiz
  yazar) — bu tip icin bayrak yoklugu NORMALDIR ve `good` kalir. Kor bir
  "flags yoksa invalid" kurali, bayrak yayina acildigi anda seri no / IMEI /
  firmware / IP noktalarinin tamamini gecersiz yapardi.

- **Env degiskeni adi tekillestirildi.** Dokumanlar ve kod yorumlari
  var olmayan bir `GATEWAY_PUBLISH_DNP3_QUALITY` adini kullaniyordu;
  `Settings` prefix kullanmadigi icin gercek ad `DNP3_PUBLISH_QUALITY_FLAGS`.
  Dokumani takip eden operator etkisi olmayan bir degisken set ediyor,
  tek ipucu acilistaki `quality_flags_published=False` log satiri oluyordu.
  Tum referanslar canonical ada cekildi; **legacy alias EKLENMEDI** (iki ad
  tasimak ayni karisikligi geri getirirdi).

### Notes

- Telemetri **degeri** ve govde sozlesmesi degismedi: `dnp3_flags` ham byte'i
  aynen gonderiliyor, kalite sozlugu hala `good | invalid | restart | forced |
  comm_lost`. Yeni token EKLENMEDI.
- `DNP3_PUBLISH_QUALITY_FLAGS` varsayilani bilincli olarak `false` birakildi.
  Once tek bir test gateway'inde gercek cihaz verisiyle dogrulanacak, sonra
  kademeli yayilim (bkz. `docs/BACKEND_TODO.md#B1`).

## [1.7.0] - 2026-08-13

Sahadaki SN2.0 cihazlari "haberlesme koptu" veriyordu, alarmlar gec geliyordu
ve KESICI KOMUTU gonderilemiyordu. Ucunun de tek bir sebebi vardi: gateway
bir cihazin canli olup olmadigini "cache'e olcum dustu mu" ile olcuyordu.

### Fixed

- **Sessiz ama saglam cihaz "kopuk" ilan ediliyordu (sahte comm_lost).**
  Canlilik kaniti tek bir yerde uretiliyordu: `_DeviceCache.set()` icindeki
  `_last_update_at`. event-driven modda DEGERI DEGISMEYEN bir cihaz yayin
  URETMEZ; bir arıza gostergesi saatlerce ayni degerleri tasiyabilir. Bu
  yuzden ayakta olan cihaz esik dolunca `stale -> recovering -> lost`
  zincirine giriyordu.

  Oysa DNP3 canliligi ZATEN soyluyordu ve iki kanit da cope gidiyordu:
  `OnReceiveIIN` (outstation'in HER uygulama yanitinda gelir) yalnizca log
  basiyor, `OnTaskComplete` ise `pass` idi. Gercek DNP3 loopback'inde
  olculdu: sessiz bir outstation saniyede bir `USER_TASK/SUCCESS` + IIN
  uretiyor, SIFIR veri noktasiyla.

  Artik canlilik AYRI bir saatte tutuluyor (`_last_evidence_at`); kanit
  `OnReceiveIIN` ve `OnTaskComplete(result == SUCCESS)` ile besleniyor.
  Basarisiz gorevler (RESPONSE_TIMEOUT / NO_COMMS) ve cihazin desteklemedigi
  gorevler (ASSIGN_CLASS/ENABLE_UNSOLICITED -> BAD_RESPONSE) kanit SAYILMAZ.

  **Kopma tespiti KAPATILMADI.** `set()` kanit saatini de besledigi icin
  daima `kanit_yasi <= veri_yasi`: bu ayrim var olan bir tespiti geciktirmez,
  yalnizca sahte olani kaldirir. Gercek kopmada (yari-acik soket, yanlis
  master adresi) ne IIN gelir ne de gorev SUCCESS doner; mevcut
  lost/probe/relink kendiliginden isler. Regresyon testi bunu GERCEK trafikle
  kanitliyor (`test_susan_cihaz_comm_lost_OLUR`).

- **KESICI KOMUTU "deger degismedi" diye reddediliyordu.** `operate_crob`
  kapisi `state != "online"` idi; `state` cihazin VERI URETIP uretmedigine
  bagli oldugu icin sessiz cihazin komutu DNP3'e hic cikmadan
  `status: "offline"` ile geri ceviriliyordu (`yadnp3_operate_skipped_offline`).
  Fail-fast'in gercek gerekcesi "olu soket uzerinde 10 sn bloklama" idi;
  dogru olcut de ULASILABILIRLIK. Kanit tazeyse komut GONDERILIR; basarisiz
  olursa operator uydurulmus bir "offline" degil cihazin gercek DNP3 sonucunu
  gorur. Kanit yoksa davranis eskisi gibi (hic gonderme).

- **Alarm gec geliyordu.** Cihaz `lost`/`recovering` iken `read_device` TUM
  sinyalleri `raw_value=0.0, quality=comm_lost` ile donduruyor, gercek alarm
  degeri yayina hic cikmiyordu. Backend'de `comm_lost` alarm degerlendirmesini
  DONDURDUGU icin alarm ancak "haberlesme geri geldiginde" goruluyordu. Sahte
  kopma kalkinca bu yol da kendiliginden duzeldi.

### Added

- **Olcum sessizliginde gateway KENDISI SORUYOR.** Kanit taze ama olcum
  bayatsa (orn. Class 0 integrity poll dusuyorsa) cihaza periyodik integrity
  poll gonderilir — cihaz comm_lost ILAN EDILMEDEN. Eskiden ayni belirtinin
  cevabi "kopuk ilan et" idi. Aralik `_DATA_SILENCE_POLL_INTERVAL_SEC` (60 sn)
  ve hiz siniri epizotlar ARASINDA da gecerli. Calisan oturum YIKILMAZ
  (relink yok) — `_kopuk_cihazi_yokla`dan farki budur.

- **Teshis gorunurlugu.** `/health` cihaz basina `evidence_age_sec`,
  `data_age_sec` ve `reachable` raporluyor; "cihaz kopuk mu yoksa yalnizca
  degeri mi degismiyor" sorusu artik tek GET ile cevaplaniyor. Filo
  seviyesinde `devices.alive_no_data` sayimi ve `devices_alive_no_data` issue
  kodu backend'e tasiniyor (mevcut `issues` kolonu; migration GEREKMEZ).
  `recovery_stats()` icine `data_silence_poll_total` eklendi.

### Changed

- Bayatlik esigi tek kaynaktan uretiliyor (`_bayatlik_esigi_sn`); ayni deger
  hem bayatlik karari hem komut yolunun ulasilabilirlik olcutu icin
  kullaniliyor. Formul degismedi: `max(4*baseline, 10*scan, 60)`.

## [1.6.3] - 2026-08-11

Iki ayri saha bulgusu: G110 okumasi yanlis anda tetikleniyordu, ve zorla
relink yavas hatlarda toparlanmayi geciktiriyordu.

### Fixed

- **G110 (string) okumasi YANLIS ANDA tetikleniyordu — v1.6.2 eksik kaldi.**
  Aralik turetmesi dogruydu ama tarama `_ensure_master` icinden, `Enable()`in
  hemen ardindan cagriliyordu. `Enable()` ASENKRON: o noktada TCP oturumu
  ACIK DEGIL (listening modda cihaz kendi baglanana kadar dakikalar
  surebilir). opendnp3'te ad-hoc `ScanRange` master OFFLINE iken kuyruga
  ALINMAZ, aninda fail edilir — ama `ScanRange()` istisna atmadigi icin kod
  "queued" yazip True donuyordu: SAHTE BASARI.

  Sahadaki SN2'ler surekli flap ettigi icin recovery/relink yolundan
  `request_integrity_poll` -> `scan_g110_once` tetikleniyor ve sorun
  maskeleniyordu. KARARLI baglanan Pole Master Kit bu yola hic girmiyor ve
  string'leri hicbir zaman gelmiyordu.

  Artik tetikleme link ACILDIKTAN SONRA ve POLL THREAD'inden yapiliyor:
  `OnOpen` yalnizca bayrak kurar (`cache.g110_iste`) — o callback opendnp3'un
  IO thread'inden gelir ve oradan ScanRange cagirmak risklidir; gercek istegi
  `read_device` gonderir.

  **Varis dogrulanir, gerekirse tekrar denenir:** basari, ISTEGIN
  GONDERILMESIYLE degil cihazdan GERCEKTEN bir G110 degeri gelmesiyle
  olculur (`_DeviceCache.set`). Gelmezse 15/30/60/120/240 sn ustel backoff,
  6 deneme tavani. Tavana ulasilirsa cihaz basina BIR KEZ WARNING
  (`yadnp3_g110_okunamadi`). Link kopup acilirsa bayrak yeniden kurulur.

  PERIYODIK SCAN YOK: deger geldikten sonra bir daha istenmez.

- **Zorla relink yavas hatlarda toparlanmayi GECIKTIRIYORDU.** Sahada
  olculdu (2026-08-11): relink ACIK olan TCP oturumunu yikiyor; yerel agda
  yeniden baglanma milisaniyeler surerken 4G/GSM hatta ~3 DAKIKA surdu
  (11:26:37 master enable -> 11:29:23 link open). Olusan dongu:

      link acilir -> 15sn grace dolar -> lost -> 90sn yoklama -> relink ->
      3 dk yeniden baglanma -> ayni yerden basa

  Cihaz hicbir zaman kararli bir pencere bulamiyordu. Dort koruma eklendi:

  1. **Taze oturum yikilmaz** — link `_LOST_RELINK_MIN_LINK_AGE_SEC` (300 sn)
     dolmadan zorla kapatilmaz; o sureye kadar yalnizca yoklanir.
  2. **Komut ucustayken yikilmaz** — CROB gorevi master uzerinde asenkron
     kosar; ortasinda `shutdown()` cagrilirsa gorev cevap alamadan olur ve
     sonuc **`CommandStatus.UNDEFINED`** doner. Sahada tam olarak bu
     gorulmustu. Komuttan sonra 60 sn oturuma dokunulmaz.
  3. **Ustel bekleme** — ard arda relink'ler 300 sn'den baslayip iki katina
     cikar (tavan 1 saat). Relink ise yaramiyorsa surekli TCP acip kapatmak
     modem/hat uzerinde gereksiz yuk.
  4. **Cihaz ses verirse sayac ANINDA sifirlanir** — durum makinesinden
     BAGIMSIZ. Eskiden sifirlama yalnizca relink/online yollarina bagliydi;
     cihaz yoklama penceresinde geri gelip durum makinesi hala 'lost' iken
     sayac ilerlemeye devam edebiliyor ve calisan bir oturum yikilabiliyordu.

### Changed

- **Ilk basarili G110 okumasi INFO seviyesine cikti**
  (`yadnp3_g110_okundu device=... nokta=N`), cihaz basina TEK satir. Bu
  hatanin sessiz kalma sebebi tum G110 loglarinin DEBUG olmasiydi —
  varsayilan `LOG_LEVEL=INFO` ile hicbiri gorunmuyordu.
- `scan_g110_once` link KAPALIYKEN istek gondermeden False doner ve DEBUG
  basar; sahte basari uretmez.

### Notes

- Zorla relink artik belirgin sekilde daha MUHAFAZAKAR: pratikte yalnizca
  uzun suredir acik ama olu soketler icin calisir. Bu bilincli bir geri
  cekilme — sahadaki olcum agresif relink'in fayda degil zarar verdigini
  gosterdi. Yari-acik soket durumu yine kapaniyor, sadece daha sabirli.
- v1.6.2'deki `_g110_bloklari` aralik turetmesine DOKUNULMADI; SN2.0 ->
  ((3,23),(65000,65020)) ve PMK -> ((0,50),(65000,65003)) testleri aynen
  geciyor.
- Birim testler: 423 passed.

## [1.6.2] - 2026-08-07

G110 (Octet String) sinyalleri artik her cihaz modelinde okunuyor.

### Fixed

- **Okuma araligi SN2.0'ye gore ELLE SABITLENMISTI.**
  `_G110_RANGES = ((3, 23), (65000, 65020))` bir SINIF SABITIYDI ve yalnizca
  SN2.0'in string index haritasiydi. Ayni gateway'de baska bir model
  bulundugunda o cihazin string'leri kapsam disinda kaliyordu:

  | Model | String index'leri | Sabit ile istenen |
  |---|---|---|
  | SN2.0 | 3, 5-17, 19-23, 65000-65003, 65009-65014, 65020 (30 nokta) | hepsi |
  | Pole Master Kit | 0-3, 5-50, 65000-65003 (54 nokta) | yalnizca 24'u |

  Pole Master Kit'te SIM CCID / Network Operator / Network Type (0,1,2),
  Modem Dial-In / GPS Lat-Lon / Network RF Status (24,25,26) ve
  sat04-sat09'un TUM string'leri (27-50) HIC istenmiyordu.

  Artik aralilar her cihazin KENDI sinyal setinden turetiliyor
  (`_g110_bloklari`) ve `_ManagedMaster` uzerinde CIHAZ BASINA tutuluyor —
  ayni gateway'de SN2.0 ile Pole Master Kit birlikte bulunabilir.

- **Ilk baglantida string okumasi HIC tetiklenmiyordu.**
  `scan_g110_once` yalnizca `request_integrity_poll` icinden cagriliyordu;
  o da lost-probe / stale / relink / manuel refresh yollarindan geliyordu.
  Duzgun baglanip HIC KOPMAYAN bir cihazda string okumasi hicbir zaman
  calismiyordu — kapsam icindeki noktalar bile gelmiyordu. Sahadaki SN2'ler
  surekli kopup baglandigi icin bu eksik maskelenmisti.

  `_ensure_master` artik master kurulduktan (Enable) sonra bir kez
  `scan_g110_once` cagiriyor. Mevcut cagri yollari aynen korundu.

- Cihazla ILK TEMAS komut yoluyla olursa (`operate_crob` -> `_ensure_master`,
  sinyal listesi yok) master bos bloklarla kuruluyordu ve sonraki okuma
  bloklari doldursa bile tarama bir daha tetiklenmiyordu. Bloklar bostan
  doluya gecerse string okumasi bir kez tetikleniyor; `signals=None` gelen
  yollar bloklara DOKUNMUYOR.

### Notes

- **Davranis korumalari:** blok gruplama (bosluk <= 8) disinda iki baraj
  daha var — blok genisligi `_G110_BLOK_MAX_GENISLIK` (512) asarsa parcalanir,
  toplam blok sayisi `_G110_MAX_BLOK` (16) asarsa kirpilir; ikisi de WARNING
  basar. 0-65535 gibi genis tek aralik Mayis 2026'da cihazi bozmustu
  (revert 1302b83) ve asla uretilmiyor — birim testiyle kilitli.
- Tarama PERIYODIK DEGIL (one-shot) — string'ler statik.
- `dnp3_class` alanina dokunulmadi; statik octet string okumasi EVENT
  sinifindan bagimsizdir.
- SN2.0 icin turetilen bloklarin eski sabitle BIREBIR ayni oldugu birim
  testiyle kilitlendi (regresyon korumasi).
- Birim testler: 413 passed (14 yeni).

## [1.6.1] - 2026-08-06

Kopan cihaz artik KENDILIGINDEN geri geliyor — manuel refresh gerekmiyor.

### Fixed

- **`lost` durumundaki cihaza gateway hicbir sey sormuyordu.** Otomatik
  integrity poll yalnizca iki durumda tetikleniyordu: stale-edge'de
  (`online -> recovering`) TEK SEFER, ve relink'te (`lost -> recovering`)
  ama yalnizca veri ZATEN geliyorsa. Yani "cihaz lost + link acik gorunuyor
  + veri gelmiyor" durumunda gateway pasif bekliyordu; kilidi kiran tek sey
  operatorun elle tetikledigi `/refresh-all` idi.

  Sahada gozlenen: *"bazen haberlesme gidiyor, manuel refresh atinca
  geliyor."*

  Bu senaryo 4G/GSM hatlarda KURAL: modem RRC idle'a duser, TCP soketi
  FIN/RST uretmeden yari-acik kalir, opendnp3 `OnClose` gormez, outstation
  da unsolicited veri gondermeyi keser. Iki taraf da sessizdir ve kimse ilk
  hamleyi yapmaz — cihaz sonsuza kadar `lost` kalir.

  Cozum iki katmanli:
  1. **Periyodik kendiliginden yoklama** — `lost` + link acik iken
     `DNP3_LOST_PROBE_INTERVAL_SEC` (varsayilan 30 sn) araligiyla integrity
     poll gonderilir; manuel refresh'in yaptiginin aynisi.
  2. **Zorla oturum yenileme** — `DNP3_LOST_RELINK_AFTER_PROBES`
     (varsayilan 3) sonucsuz yoklamadan sonra TCP oturumu kapatilip yeniden
     kurulur. Yari-acik sokete integrity poll ise yaramaz; tek cikis budur.

  **Link KAPALIYKEN hicbir sey yapilmaz:** opendnp3 kanali zaten kendi TCP
  retry'ini suruyor. Araya girip master'i yeniden kurmak devam eden
  baglanti denemesini iptal eder ve toparlanmayi GECIKTIRIRDI.

### Added

- **`DNP3_LOST_PROBE_INTERVAL_SEC`** (varsayilan 30) ve
  **`DNP3_LOST_RELINK_AFTER_PROBES`** (varsayilan 3) ayarlari.

- **`/health` -> `devices.recovery`** — `lost_probe_total`,
  `forced_relink_total`, `devices_probing`. "Gateway acaba yeniden
  baglanmayi deniyor mu?" sorusu sahada yalnizca soket durumunu ornekleyerek
  (`SYN_SENT` saymak) cevaplanabiliyordu; artik tek GET yetiyor.

- Zorla relink olay uretir (`yadnp3_forced_relink`, WARNING). Tek tek
  yoklamalar DEBUG seviyesindedir — 500 cihazli bir sahada toplu kopmada
  her yoklamayi INFO basmak log'u bogar ve gercek olayi gizlerdi.

### Notes

- **Canli dogrulama** (izole container, gercek opendnp3 outstation): outstation
  prosesine `SIGSTOP` gonderilerek yari-acik soket birebir taklit edildi —
  TCP `ESTABLISHED` kalir, cekirdek ACK'lemeye devam eder, uygulama katmani
  hicbir sey cevaplamaz. Sonuc: cihaz `lost` oldu, gateway kendi integrity
  poll'unu gonderdi, cevapsiz kalinca oturumu zorla yeniledi ve `SIGCONT`
  sonrasi **manuel refresh olmadan** `online`'a dondu. 5/5 kontrol gecti.

## [1.6.0] - 2026-08-05

Telemetri tasima yolu artik KURULUM SENARYOSUNA gore davraniyor ve aktif yol
her zaman gorunur. **Varsayilan davranis mevcut kurulumlarda DEGISMEDI**
(`INSTALL_MODE` varsayilani `remote`; NATS calisirken hicbir sey degismez).

### Added

- **`INSTALL_MODE` (`local` | `remote`, varsayilan `remote`)** — NATS
  erisilemedigi zaman ne olacagini belirler.

  - `local` ("bu cihaza kur", backend ile ayni makine): **NATS ZORUNLU,
    HTTP yedegi YOK.** Ayni makinede NATS'a erisilememesi bir yapilandirma
    hatasidir; sessizce HTTP'ye dusmek bu hatayi GIZLER. Sistem "calisiyor"
    gorunur, panelde veri akar, ama her olcum backend HTTP + Postgres
    zincirinden gecer ve 500 cihaz hedefi tutmaz — ariza ancak yuk testinde,
    haftalar sonra fark edilir. Bu modda NATS dustugunde mesajlar outbox'ta
    birikir (kayip yok) ve `/health` acikca `telemetry_backend_unreachable`
    der.
  - `remote` ("baska cihaza kur", saha sunucusu): once NATS denenir,
    erisilemezse **HTTP ingest ile veri akmaya DEVAM eder**. Sahada 4222
    kapali/NAT arkasinda olabilir. NATS geri gelince birincil yola
    **kendiliginden donulur** — HTTP'de kalici takilma yok.

- **`TelemetryTransportRouter`** (`messaging/transport_router.py`) —
  `ResilientPublisher` ile gercek publisher'lar arasina giren broker
  cephesi. Bu konumlandirma iki seyi ayni anda saglar:
  - **fallback'te kayip yok**: NATS'a yazilamayan mesaj AYNI CAGRIDA HTTP'ye
    verilir; iki yol da coktugunde istisna yayilir ve mesaj outbox'a yazilir,
  - **outbox drenaji da yedek yolu kullanir**: retrier ayni router'dan gectigi
    icin NATS uzun sure kapali kalsa bile birikmis tampon HTTP'den bosalir.

- **Aktif tasima yolu `/health` govdesinde**: `outbox.telemetry_transport`
  altinda `active_transport`, `install_mode`, `fallback_enabled`,
  `transport_switches`, `fallback_delivered_total`, `nats_ready`,
  `http_ready`. Sahada en pahali sorulardan biri "su an veri hangi yoldan
  gidiyor?" idi ve log'a bakmadan cevaplanamiyordu.

- **`telemetry_transport_fallback_http` health sorunu** (degraded) — uzak
  kurulumda HTTP yedegine dusulunce panelde gorunur. Veri aktigi icin
  "unhealthy" degil, ama sessiz de kalmamali.

- **`TELEMETRY_FALLBACK_FAIL_THRESHOLD`** (varsayilan 3) ve
  **`TELEMETRY_FALLBACK_PROBE_INTERVAL_SEC`** (varsayilan 30) ayarlari.

- **Boot log + konsol banner'inda aktif yol**:
  `telemetry_transport_selected install_mode=... active=... fallback=...`
  ve `dnp3_gateway_starting ... install_mode=...`.

### Changed

- **Yol degisimi OLAY uretir, denemeler sessizdir.** Her basarisiz NATS
  denemesini loglamak 500 cihazli sahada saniyede binlerce satir uretir ve
  gercek olayi bogardi. Artik yalnizca `nats->http` (WARNING) ve `http->nats`
  (INFO) gecislerinde TEK satir atilir; deneme sayilari `/health`'te.

- **Yerel modda `NATS_URL` acikca verilmelidir** — varsayilana
  (`nats://localhost:4222`) sessizce dusmek yasak. Ajan `NATS_URL`'i compose'a
  yazmayi atlarsa gateway kendi container'ina baglanmaya calisir, hicbir zaman
  baglanamaz ve yerel modda yedegi de olmadigi icin telemetri tamamen durur.
  Kurulumun ilk saniyesinde acik hata vermek, sahada saatler suren teshisten
  iyidir. Kural HER ortamda gecerli (dev dahil).

### Notes

- **Tekrar (duplicate) riski:** NATS batch'i KISMEN basarisiz olursa tum
  batch HTTP'ye aktarilir ve giden mesajlar iki kez islenebilir. Bilincli
  tercih — at-least-once garantisini korumak, kayip vermemek. Baskin
  senaryoda (NATS tamamen erisilemez) `JetStreamNotReadyError` hicbir mesaj
  gonderilmeden firlatilir, dolayisiyla tekrar olusmaz.

- **Canli dogrulama** (192.168.2.99, izole NATS 2.10 container): gercek
  `docker stop` ile NATS durduruldu, 50 mesajin tamami HTTP'den teslim edildi
  (outbox 0, kayip 0); gercek `docker start` sonrasi otomatik NATS'a donuldu;
  yerel modda ayni kesintide HTTP'ye HIC dusulmedi ve 30 mesaj outbox'ta
  korundu. 16/16 kontrol gecti. Production NATS'a dokunulmadi.

## [1.5.0] - 2026-08-04

401 cihazli sahada olculen CPU yukunun kaynagi bulundu ve ayarlanabilir hale
getirildi. **Varsayilan davranis DEGISMEDI.**

### Added

- **`DNP3_EVENT_SCAN_INTERVAL_SEC`** — Class 1/2/3 event scan araligi artik
  poll araligindan AYRI ayarlanabiliyor. Varsayilan 0 = poll interval ile
  ayni (eski davranis; mevcut kurulumlar etkilenmez).

  NEDEN: `scan_interval_sec` dogrudan `default_poll_interval_sec`e
  baglanmisti, ama bunlar farkli isler — poll interval YAYIN turu, scan
  interval cihaza SORMA sikligidir. 401 cihazda `scan=1s` saniyede 401 DNP3
  istegi uretiyordu; her istek TCP round-trip + cerceve cozumleme +
  C++->Python callback zinciri demek.

  Bu, CPU'nun cihaz sayisiyla (mesaj sayisiyla DEGIL) artmasini acikliyor:
  300 cihaz %77.2 / 400 cihaz %108.6 CPU iken yayin yalnizca +%17 artmisti.

  Once yayin yolundan supheledim, OLCUM CURUTTU: json.dumps 3.5 us/mesaj
  (6500 msj/sn icin %2.3), payload kurma 4.6 us (%3.0) — serilestirme toplam
  ~%5. `orjson` eklemek ise yaramazdi.

  Cihazlar unsolicited modda calistigi icin (disableUnsolOnStartup=False)
  scan yalnizca yedek mekanizmadir; 3-5 sn tipik olarak veri tazeligini
  bozmadan yuku belirgin dusurur. Kazanc SAHADA OLCULMELI — bu yuzden
  varsayilan degistirilmedi.

### Fixed

- **Log iki yerde yaniltiyordu.** `manager_threads=auto` yaziyordu;
  heuristigin GERCEKTE kac thread sectigi hicbir yerde gorunmuyordu ve
  operator olcegi buyuturken ayarin tuttugunu dogrulayamiyordu. Artik
  `io_threads=20` gibi gercek deger yaziliyor (yeni `io_thread_count`).
  `scan=` alani da poll interval yerine etkin degeri ve kaynagini gosteriyor.

## [1.4.0] - 2026-08-04

400 cihazli olcumde bulunan iki SESSIZ sinir kapatildi. Hedef: 500 cihaz.

### Fixed

- **Dosya tanitici (fd) tavani — cihaz sayisinin gercek sinirlayicisi.**
  Her DNP3 cihazi bir TCP soketi tutuyor; 400 cihazda 420 fd acikti. Docker
  varsayilani 1024 -> ~950 cihazda mutlak duvar, ama pratik sinir cok daha
  erken: baglanti flap'inde eski soket kapanmadan yenisi acilir ve fd gecici
  olarak IKIYE katlanir. Limit dolunca hata "cihaz kopuk" gibi gorunur ve
  gercek sebep hicbir sayacta yazmaz.

  Compose sablonuna `ulimits.nofile = 65536` eklendi. MEVCUT kurulumlarin
  compose dosyasina ELLE eklenmeli — sablon yalnizca yeni kurulumlari etkiler.

- **DNP3 IO thread sayisi sabit 4 idi.** Kod icindeki yorum
  `max(4, ceil(device_count_hint / 25))` heuristigini TARIF EDIYOR ama
  UYGULAMIYORDU ("device_count_hint constructor'da bilinmiyor"). 400 cihazda
  sonuc 100 CIHAZ/THREAD. Sikisma cihazi offline yapmaz; yalnizca DNP3
  yanitini geciktirir ve veriyi bayatlatir — yani sessizce.

  Ipucu artik `MAX_PARALLEL_DEVICES`ten geliyor (reader boot'ta, config
  gelmeden kuruldugu icin gercek cihaz sayisi bilinemez). Hedef ~25
  cihaz/thread, taban 4, tavan 32: 100->4, 300->12, 500->20, 1000->32.
  `DNP3_MANAGER_THREADS` acikca set edilmisse operator karari korunur.

### Docs

- RUNBOOK "Tek gateway'e kac cihaz?" olculen rakamlarla yeniden yazildi
  (400 cihaz: CPU %108, RAM 375 MiB, 6.349 sinyal/sn, sifir hata) +
  sinirlayici tablosu. Simulator yukunun gercekciden 10-100 kat agir oldugu
  notu eklendi: sahadaki SN2 bir ariza gostergesidir, 193 sinyalin cogu
  sabittir ve delta-only yayin devrededir.

## [1.3.0] - 2026-08-04

300 cihazli sahada olculen yayin darbogazi kaldirildi. Hedef 500 cihaz.

### Added

- **`JetStreamPublisher.publish_batch` — toplu (paralel) yayin.** Bu metot
  YOKTU; `ResilientPublisher.publish_batch` "broker batch desteklemiyor"
  deyip mesajlari TEK TEK `publish()`e dusuruyor, her cagri da cagiran
  thread'i BLOKE eden bir JetStream ACK round-trip'i oluyordu. HTTP
  publisher'da batch vardi; NATS'a geciste bu kazanim sessizce kayboldu.

  Sahada olculdu (300 cihaz): bir cycle'da 30.696 mesaj, NATS round-trip
  0.064 ms -> yalnizca ack beklemesi ~2.0 sn; cycle ortalamasi 4.02 sn
  (hedef 1 sn, medyan 3.57, max 16.35), gateway CPU %94.8 (100 cihazda %5).
  Kapasite uyarilari SIFIRDI (poll_pool_starved/timeout yok) — yani darbogaz
  worker havuzu degil, yayin yoluydu.

  Artik tum publish'ler ayni loop turunda baslatilip ack'ler TOPLU bekleniyor
  (N sirali round-trip -> ~1 round-trip). Govdeler cagiran thread'de
  serilestiriliyor (JSON'u loop icine tasimak tek loop'u tum gateway icin
  darbogaz yapardi); paralellik `_BATCH_CHUNK`=256 ile sinirli (kontrolsuz
  gather istemci yazma tamponunu ve bekleyen-ack sayisini sisirirdi).

  Hata semantigi KORUNDU: en az bir mesaj basarisizsa istisna firlatilir,
  ResilientPublisher TUM batch'i outbox'a yazar. Duplicate uretebilir ama
  `Nats-Msg-Id` dedup'i (2dk) eler — at-least-once bozulmaz.

  8 yeni test (348 toplam). `test_yayinlar_paralel_yapilir` regresyonu
  olcerek yakalar: sirali davranista test duser.

## [1.2.0] - 2026-08-04

Kapasite: tek gateway hedefi 500 cihaz (ekstra gateway kurmadan).

### Changed

- **`MAX_PARALLEL_DEVICES` varsayilani 100 -> 500, ust sinir 500 -> 1000.**
  Havuz tembel buyudugu icin kucuk sahada ek maliyet yok; 500 cihazda cihaz
  basina worker, kopuk cihaz dalgasinda tek timeout dalgasi demek. Compose
  sablonu ve `.env.example` 500 set eder.
- **`DEVICE_POLL_TIMEOUT_SEC` varsayilani 30 -> 15.** Kopuk cihaz worker'i
  bu sure boyunca isgal ediyor; RUNBOOK ayar tablosundaki 300+ cihaz onerisi
  varsayilan yapildi. Integrity poll'u 15sn'i asan yavas WAN sahasi env ile
  yukseltebilir.
- RUNBOOK "Olcek" bolumu 500 cihaz kademesiyle guncellendi.

## [1.1.0] - 2026-08-04

Mimari karar: telemetrinin STANDART yolu dogrudan NATS JetStream.

### Changed

- **`TELEMETRY_PUBLISHER` varsayilani `http` -> `nats`.** Telemetri artik
  backend'e ugramadan `e1.telemetry.raw.<code>` subject'ine basilir. HTTP
  ingest yolu kaldirilmadi; yalnizca bilincli rollback icin durur
  (`TELEMETRY_PUBLISHER=http`). Komut/config kanali HTTP'de kalir.

  Gerekce (2026-08-04, 100 cihazlik yuk testi): HTTP yolunda her olcum
  backend HTTP ingest -> Postgres outbox -> NATS zincirinden geciyordu.
  Backend outbox drain'i 3.250 msj/sn basarken persist 1.100 msj/sn
  isleyebildi; 5M+ mesajlik backlog olustu, cihaz durum gecisleri (comm_lost)
  telemetri kuyrugunun arkasinda saatlerce gorunmez kaldi ve gateway outbox'i
  500K tavaninda olcum dusurdu. NATS-direkt yolda backend yalnizca tuketici.

  Grid tarafi v2.40.0 bununla uyumlu: panel "Guncelle" akisi gateway
  compose'unu guncel NATS URL'i ile tazeler; HTTP yedek yolundan telemetri
  basan gateway icin backend rate-limit'li uyari loglar.
- Compose sablonu ve `.env.example` `TELEMETRY_PUBLISHER: "nats"` set eder;
  README/ARCHITECTURE/RUNBOOK/SECURITY "legacy NATS" etiketleri kaldirildi.

## [1.0.2] - 2026-08-03

Saha arizasinin ortaya cikardigi iki gorunurluk/dayaniklilik eksigi.

### Fixed

- **Backend'e telemetri gitmezken `/health` "ok" diyebiliyordu.**
  `broker_ready` govdede vardi ama hicbir sorun kodu uretmiyordu; ariza
  ancak outbox dolmaya basladiginda (dakikalar sonra) gorunurdu. Yeni kod:
  `telemetry_backend_unreachable` (degraded). Veri outbox'a yazilmaya devam
  ettigi icin unhealthy DEGIL — surerse `outbox_full` zaten unhealthy yapar.

### Changed

- **Config periyodik refresh varsayilani 300sn -> 60sn**; compose sablonu
  acikca 30sn set ediyor. Cihaz eklendiginde anlik tetik `config_nonce` ile
  gelir, periyodik refresh onun YEDEGIDIR — sahada tetik olunce yeni cihaz
  5 dakika gorulmedi. Maliyet ~sifir: config istekleri ETag ile sartli
  (degismediyse 304, govde inmez).

  Not: `.env.example` zaten 30 diyordu ama compose sablonu hicbir sey set
  etmedigi icin saha kod varsayilani 300'u aliyordu — ornek konfigurasyon
  ile gercek davranis ayrismisti.

### Docs

- RUNBOOK: `command_channel_failing`, `command_channel_down` ve
  `telemetry_backend_unreachable` sorun kodlari mudahale adimlariyla eklendi.

## [1.0.1] - 2026-08-03

**Saha arizasi duzeltmesi.** GW-001'de SCADA komut kanali sessizce olmustu.

### Fixed

- **Saglik basligi `/pending`i dusuruyordu — komut kanali oldu.** Gateway'in
  `X-E1-Gateway-Health` basligi backend'de `gateway_health` tablosuna
  yaziliyor; o tablo sahadaki veritabaninda yoktu (migration eksik). INSERT
  patlayinca transaction bozuluyor ve AYNI transaction'daki komut sorgusu da
  patliyor -> `GET /pending` 500.

  Uc sonucu vardi: (1) SCADA komutlari gateway'e HIC ulasmiyordu, (2)
  `config_nonce` okunamadigi icin yeni eklenen cihaz anlik gorulmuyor,
  yalnizca 5 dakikalik periyodik refresh'te geliyordu, (3) saglik ozeti
  backend'e gitmiyordu.

  `health_header.py` "KOMUT KANALI KUTSAL — bu modulun hicbir hatasi
  /pending cagrisini dusurmemeli" diyordu ama savunma yalnizca basligi
  URETIRKEN cikan hatalari kapsiyordu; baslik uretilip gonderildiginde ve
  BACKEND onu isleyemediginde savunma yoktu. Yani bir teshis kolayligi,
  korumakla yukumlu oldugu seyi oldurdu.

  Artik `/pending` 5xx dondu ve baslik gonderildiyse ayni istek BASLIKSIZ bir
  kez daha denenir; basliksiz calisiyorsa baslik 10 dakika birakilir ve
  komutlar akmaya devam eder. Backend duzeltilince kendiliginden geri gelir.
  Baslikla ilgisi olmayan 5xx'ler yutulmaz.

- **Komut kanali olurken `/health` "ok" diyordu.** Thread yasiyordu, bu
  yuzden `thread_dead:` tetiklenmedi ve panel 660 ardisik hata boyunca
  saglikli gorundu. Operatorun arizayi fark etmesinin hicbir yolu yoktu.
  Yeni sorun kodlari: `command_channel_failing` (15 ardisik hata, degraded)
  ve `command_channel_down` (60 ardisik, unhealthy). `/health` govdesine
  `command_channel` ozeti eklendi.

## [1.0.0] - 2026-08-03

**Ilk uretim surumu.** Gateway 20 gercek cihazla sahada dogrulandi; protokol
yolu artik CI'da gercek DNP3 trafigiyle test ediliyor ve dagitim imaji her
PR'da build edilip icinde calistirilarak dogrulaniyor.

1.0 su anlama gelir: **gateway tarafinda planlanmis is kalmadi.** Backend
sozlesmesine bagli iki kalem (B1 kalite bayragi env'i, B2 cihaz zaman damgasi
alanlarinin kabulu) backend tarafinda duruyor ve ikisi de kirilma riski
tasimiyor — gateway gerekli veriyi zaten gonderiyor.

Sinirlar: olcek dogrulamasi 20 cihaza kadar yapildi; hedef 100-300 icin
kademeli cikis plani `docs/RUNBOOK.md` bolum 6'da.

### Added

- **Gercek DNP3 loopback entegrasyon testleri.** `dnp3_yadnp3_master` bu
  deponun en kritik modulu (tum olcumler ve tum kesici komutlari oradan
  geciyor) ama tek satiri bile gercek protokol trafigiyle test edilmemisti;
  regresyon ancak musteri sahasinda gorulurdu. Artik localhost'ta gercek bir
  DNP3 outstation ayaga kalkiyor ve gateway kendi master'iyla ona baglaniyor:
  okuma, kalite bayraklari, olcekleme, delta-only yayin, link kopmasi ->
  comm_lost -> toparlanma ve CROB'un cihaza ulasmasi dogrulaniyor.
  Adapter kapsami CI'da %64.
- **CI'da yadnp3** + `import opendnp3` dogrulamasi. Ikincisi sart: loopback
  testleri kutuphane yoksa kendini atlar, wheel sessizce kurulmazsa en kritik
  testler kosmadan CI yesil donerdi.
- **`docker imaji` CI isi.** Imaj artik her PR'da build ediliyor ve
  **calistirilarak** dogrulaniyor: icinde `import opendnp3`, `python -m
  dnp3_gateway --version`, imajdaki surumun `VERSION` ile esitligi ve
  entrypoint'in kimliksiz container'i cikis kodu 64 ile reddettigi. Dagitim
  Docker-only oldugu icin Dockerfile yardimci dosya degil urunun kendisi;
  eskiden yalnizca main'e push'ta build ediliyordu, yani kapinin yanlis
  tarafinda.
- **`requirements-dnp3.txt`** — DNP3 native surum pini icin tek kaynak.
  Pin uc yerde tekrarlaniyordu; birini yukseltip digerini unutmak CI'in test
  ettigi surumle sahanin calistirdigi surumun sessizce ayrismasi demekti.

### Fixed

- **Profili katalogda olmayan cihaz duz sinyal listesine dusuyordu.** Backend
  profil bazli katalog gonderdiginde duz `signals` listesi TUM profillerin
  BIRLESIMIdir. Komsu modelin `(30,12)` adresi bu cihazda baska bir
  buyukluktur; okunan deger yanlis `signal_key` ile yayinlanir ve esik alarmi
  sahte bir buyukluk uzerinden calisirdi. Telemetri akmaya devam ettigi icin
  fark edilmesi cok zordu.

  Artik duz listeye yalnizca **karisma ihtimali yokken** dusuluyor: tek profil
  grubu VE tek modelli filo. Aksi halde cihaz yoklanmiyor ve
  `signals_profile_unknown` ERROR'u basiliyor. Uyarilar config surumu basina
  bir kez cikiyor (cihaz basina her cycle degil).

- **`comm_lost` duyurusu yayin onayindan ONCE isaretleniyordu.** Disk dolup
  `OutboxFullError` verirse mesaj ne broker'a ne diske gidiyordu, ama bayrak
  "duyuruldu" oldugu icin sonraki her cycle `no_change` uretiyordu —
  `no_change` yayinlanmaz. Kopmus gosterge SCADA'da **sonsuza kadar** son iyi
  degeriyle CANLI gorunurdu; operator hattin enerjili oldugunu sanabilirdi.

  Bayrak artik `commit_published` ile, yayin kalicilastiktan sonra set
  ediliyor (olcum degerleriyle ayni commit-after-publish deseni). Kusak
  sayaci, onceki kopma epizoduna ait gecikmis bir onayin yeni epizodu
  susturmasini engelliyor.

- **Bozuk komut defterinin sifirlanmasi SESSIZDI.** Karantina, outbox kaybiyla
  ayni genel kodla (`state_db_quarantined`) raporlaniyordu. Oysa burada
  kaybedilen fiziksel komut gecmisidir: yarim kalmis bir CROB'un sonucu
  backend'e asla bildirilemez ve o komutlar icin tekrar-onleme garantisi
  kalkar. Artik ayri bir `command_journal_reset` sorunu, `command_ledger_reset`
  ERROR'u ve `/health` `command_ledger` ozeti var.

- **`/operate` duplicate cevabinda `ok:false`.** Kayitli sonuc bulunmadiginda
  (ilk deneme hala suruyor ya da sonuc teslim edilip kayittan dusuruldu)
  cevap "basarisiz" diyordu. Cagiran taraf bunu okuyup YENI bir `command_id`
  ile tekrar dener ve kesici **gercekten iki kez surulurdu** — defterin
  onlemek icin var oldugu sey. Artik `ok:null` + `status:"pending"` doner.
  Basari cevabi da ayni alanlari (`ok`/`status`/`duplicate`) tasiyor.

- **`/operate` komut defteri erisilemezken fail-open idi.** `start_dispatch`
  hata verirse CROB yine gonderiliyordu: ne tekrar-onleme ne sonuc kaydi
  kaliyordu. Fiziksel manevrada "belki iki kez surdum" kabul edilemez. Artik
  503 ile reddediliyor (`operate_ledger_unavailable`).

- **Kuyrukta bekleyen cihazlar "okundu" isaretleniyordu.** Per-device timeout
  SUBMIT zamanindan olculuyordu ve tum future'lar icin bu deger AYNIYDI. 300
  cihaz / 25 worker'da kuyrugun sonundaki cihaz daha ISE BASLAMADAN "timeout"
  sayilip iptal ediliyor, sonra `mark_read` ile okundu isaretleniyordu. Log da
  "cihaz yanit vermedi" diyordu — oysa istek hic gonderilmemisti.

  Baslama zamanini artik worker'in kendisi yaziyor; baslamamis cihazlara
  timeout uygulanmiyor ve `mark_read` YAPILMIYOR. `due_devices` bayatliga gore
  siralaniyor, boylece kacirilan cihaz bir sonraki cycle'da en one geciyor —
  eskiden config sirasi korundugu icin ayni cihazlar surekli aclikta kalirdi.
  Yeni `poll_pool_starved` ERROR'u durumu kapasite sorunu olarak isaretliyor.

- **Saat sicramasi toplu sahte `comm_lost` uretiyordu.** Bayatlik ve recovery
  grace hesaplari duvar saatiyle yapiliyordu. Bir NTP duzeltmesi — ya da RTC
  pili bos bir sahada acilis sonrasi saat ayari — `now - last_update`
  degerini birden devasa yapip O ANDA HABERLESEN tum cihazlari comm_lost
  ilan ederdi. Tum sure olcumleri `time.monotonic()`'e tasindi; duvar saati
  yalnizca gosterim (`last_frame_epoch`) icin tutuluyor.

- **Backend erisilemezken hizli-hata yoktu.** `_ready` bayragi tutuluyordu ama
  hicbir yerde okunmuyordu: her cihaz icin yeni bir POST denenip tam timeout
  kadar bekleniyordu. Kara delik olmus bir agda (4G kopmasi, firewall drop)
  cycle timeout duvarina toslar, worker havuzu dolar ve kuyruktaki cihazlar
  hic yoklanamazdi (yukaridaki aclik sorununu besliyordu). Devre kesici
  eklendi: 1sn'den baslayip 15sn'de tavanlanan bekleme, sonunda tek bir
  yarim-acik probe. Telemetri outbox'a yazilmaya devam ediyor — **veri kaybi
  yok**.

- **`install.ps1` stok Windows Server'da CALISMIYORDU.** Dosya BOM'suz UTF-8
  kaydedilmisti ve icinde em-dash geciyordu. Windows PowerShell 5.1 BOM'suz
  dosyalari cp1252 okur; U+2014'un son bayti `0x94` = U+201D'dir ve
  PowerShell akilli tirnaklari GERCEK tirnak sayar. String erken kapanir,
  ardindan gelen suslu parantezler yerini sasirir ve script hic calismaz —
  yalnizca bir YORUM satirindaki tire tum kurulumu bozuyordu. PowerShell 7
  (UTF-8 varsayilan) bunu maskeliyordu. Uc script ASCII'ye cevrildi;
  `tests/test_powershell_scripts.py` geri gelmesini engelliyor.

- **Siraya bagli test kirliligi.** `test_crob_command` sahte bir `opendnp3`
  enjekte edip `_ManagedMaster._OP_MAP_LAZY` SINIF niteligini duz atamayla
  sifirliyor ama geri yuklemiyordu. Icinde sahte enum'lar kalinca, ayni
  oturumda sonra kosan ve gercek opendnp3 kullanan her test CROB kurarken
  duser — hem de yalnizca belirli dosya siralamasinda.

- **`poll_pool_starved` log'u var olmayan bir ayari soyluyordu**
  (`POLL_MAX_PARALLEL`). Dogrusu `MAX_PARALLEL_DEVICES` — operator ariza
  aninda bulunmayan bir ayari aramaya yonlendiriliyordu.

- **CI'da iki farkli is ayni adla gorunuyordu** (`pytest (py3.12)` hem ubuntu
  hem windows). Windows'un matriste olma sebebi YALNIZCA Windows'ta cikan bir
  hataydi; o job kirmizi yandiginda platformu ad'dan okuyamamak, tam da onu
  yakalamak icin kurulan kapiyi korlestiriyordu.

### Changed

- **Compose sablonuna `stop_grace_period: 30s`.** `docker stop` varsayilani
  10sn ve gateway'in tipik kapanisi (<8sn) buna ANCAK sigar. Pencere yetmezse
  proses SIGKILL ile olur ve in-flight bir CROB'un sonucu komut defterine
  yazilamaz; o komut sonraki acilista `unknown` bildirilir.

### Docs

- `RUNBOOK.md`: `POST /operate` tam dokumantasyonu (cevap sekli, HTTP kodlari,
  `ok:null` uyarisi), `command_journal_reset` / `poll_pool_starved` /
  `http_publisher_breaker_open` mudahale bolumleri, yeni log etiketleri,
  gercek shutdown sirasi + NSSM `AppStopMethodConsole` notu.
- `ARCHITECTURE.md`: 0.5.x mimarisi — HTTP-first akis, hata siniflandirmasi,
  commit-after-publish, thread canliligi, profil cozumleme, komut yolu,
  saglik basligi, kaynak guard'lari, SQLite surumleme, cikis kodu 78.
- `SECURITY.md`: `GATEWAY_COMMAND_TOKEN`, operator HTTP yuzeyi tablosu,
  hata mesajlarinda sir sizmasi, NATS bolumu legacy olarak isaretlendi.
- `RUNBOOK.md`: **Docker-first** yeniden duzenlendi — uretim yolu ilk sirada
  (imaj etiket politikasi, volume ve kimlik zorunlulugu, yukseltme/rollback),
  Windows/venv "yalnizca gelistirme" olarak isaretlendi. Yeni "Olcek — 20
  cihazdan 300'e" bolumu: kademeli cikis plani, ayar tablosu ve darbogazin
  gateway'de mi backend'de mi oldugunu ayirt etme rehberi.
- `BACKEND_TODO.md`: B3 (per-device katalog) gateway tarafinda kapandi —
  ozgun kayittaki "gateway tarafinda cozulemez" degerlendirmesi yanlisti.
  B2'nin gateway yarisi da bitti (`device_event_at` + `timestamp_quality`
  yayinlaniyor). **Gateway tarafinda kalan is yok.**

## [0.6.0] - 2026-08-02

Gateway artik backend'e CIHAZ BAZINDA link durumu bildiriyor (BACKEND_TODO
B4 kapandi).

### Added

- **`X-E1-Gateway-Health` basligi — cihaz bazinda link durumu.** Backend bir
  cihazin haberlesip haberlesmedigini yalnizca telemetri geldiginde
  anlayabiliyordu. Ariza bekleyen bir gosterge saatlerce sessiz kalabilir
  (deger degismezse gateway hicbir sey yayinlamaz) ve o sure boyunca cihazin
  canli mi olu mu oldugu BILINMIYORDU. "Veri gelmiyor" ile "haberlesme koptu"
  ayni sey degil; bu ayrimi yapabilecek tek yer gateway, cunku DNP3 link
  durumu burada tutuluyor.

  Ozet, saniyede bir zaten atilan `GET /gateways/{code}/pending` istegine
  baslik olarak biniyor — **ek istek, ek baglanti, ek CPU yok**. Config
  client'a degil KOMUT client'ina bindi: config-refresh 5 dakikada bir kosar,
  cihaz kaybini 5 dakika gec ogrenmek bu mekanizmanin amacini bosa cikarirdi.

  **Yalnizca `online` OLMAYAN cihazlar gonderiliyor.** 600 cihazin tamamini
  gondermek ~9 KB eder; nginx tavanina yaklasan bir baslik ISTEGIN TAMAMINI
  reddettirir, yani KOMUTLAR DA GITMEZ. Tavan asilirsa kademeli kuculuyor
  (cihaz listesi -> sorun metinleri -> yalnizca sayimlar) ve kirpma
  `states_truncated` ile bildiriliyor; sessiz kirpma backend'e "geri kalan
  her sey iyi" dedirtirdi.

  Cihaz KODLARI yalnizca bu kimlik dogrulamali baslikta gider. `/health`
  auth'suz oldugu icin orada eskisi gibi sadece sayim var.

  Saglik saglayicisinin patlamasi, sacma deger dondurmesi ya da basligin
  uretilememesi `/pending` cagrisini DUSURMEZ — komut kanali SCADA'nin
  kendisi, ozellik yalnizca teshis kolayligi.

### Fixed

- **Baslik govdesinde `status` sabit yaziliyordu.** Outbox dolarken, bir
  thread olmusken, disk biterken ya da saat kayarken bile backend'e "iyiyim"
  derdi. Artik `status` ve `issues` `/health` ile ayni kaynaktan
  (`_build_health_body`, 10 sn onbellekli) geliyor.

- **`sync-docs.ps1` sessizce basarisiz oluyordu** (DOCS deposu): eksik
  kaynagi yalnizca uyariyla geciyor, "basarili" donuyordu.

### Changed

- Platform geneli tek-seferlik denetim raporlari (`FINAL_PREPROD_AUDIT`,
  `FINAL_REMAINING`, `PRODUCTION_READINESS_PLAN`, `REMAINING_BLOCKERS`)
  `DOCS/planning/reports/`'a tasindi. Icerikleri gateway'i degil tum
  platformu anlatiyordu; yanlis depodaydilar.

- **Testler 200 -> 239.**

## [0.5.0] - 2026-07-31

Production-oncesi kapsamli denetim (9 boyut, karsit dogrulama) sonrasi
sertlestirme surumu. **Testler 93 -> 200.** Backend sozlesmesini degistiren
4 kalem bilincli olarak ertelendi: [docs/BACKEND_TODO.md](docs/BACKEND_TODO.md).

### Fixed — Uretimi durduran hatalar

- **`config_client.py`'de modul-seviye `import logging` yoktu.** Uc kod yolunda
  `NameError` uretiyordu: (a) `/pending` yanitinda tek bozuk komut TUM SCADA
  komut kanalini sessizce olduruyor, (b) `DNP3_DEVICE_ALLOWED_SUBNETS` icinde
  tek CIDR yazim hatasi gateway'i KALICI olarak config-cekemez yapiyor,
  (c) allowlist tanimliyken ilk config fetch dusuyordu.
- **Windows multi-instance lock ETKISIZDI.** `msvcrt.locking` konum-bagimli
  byte-range kilidi kullanir; dosya append modunda acildigi ve lock alindiktan
  sonra icine yazildigi icin ikinci proses FARKLI bir araligi kilitleyip
  basariyla basliyordu. Sonuc: ayni outbox/ledger'a iki proses yazimi ve
  `CommandLedger` proses-yerel oldugu icin **ayni cihaza cift CROB**.
- **CI'da test kapisi yoktu.** main'e giden her commit test edilmeden
  `:latest` olarak yayinlaniyordu.

### Fixed — Kalici veri kaybi

- **Yayin onayi artik kalicilastirmadan SONRA veriliyor** (commit-after-publish).
  Dirty bayragi okuma aninda temizlendigi icin (a) okuma-yayin arasinda gelen
  yeni olcum kalici kayboluyor (acilan kesici saatlerce kapali gorunuyordu),
  (b) outbox dolunca yayinlanmayan deger "yayinlanmis" sayiliyordu.
- **Head-of-line blocking kaldirildi.** Gecici/kalici ayrimi metin eslesmesiyle
  yapiliyordu; kalici bir HTTP 500 "gecici" sayilip ayni satir sonsuza kadar
  kuyrugun basinda kaliyor, arkasindaki TUM telemetri teslim edilemiyordu.
  Artik tip + HTTP status ile siniflandirma, kalici satir `next_attempt_at`
  ile erteleniyor.
- **Outbox drenaj hizi.** Mesaj basina POST + commit + her batch sonrasi 2sn
  uyku (~32 msg/sn) birikmis 500K mesaji saatlerce bosaltiyor, uretim hizini
  yakalayamiyordu. Artik toplu POST + tek DELETE transaction + kuyruk doluyken
  uyku atlama.
- **`refresh_all_devices()` no-op'tu** — delta-only yayinin TEK telafi
  mekanizmasi, degeri degismeyen sinyaller icin hicbir mesaj uretmiyordu.
- **Outbox-full breaker histerezisi** (ac-kapa dongusu her turda okunan
  degerleri bosa harciyordu).
- **`publish_batch`** hata durumunda kalan item'lari sessizce dusuruyordu.

### Fixed — Olcum dogrulugu (DNP3)

- **Kalite bayraklari artik okunuyor.** `grep flags src/` daha once TAM SIFIR
  sonuc veriyordu; outstation `RESTART` / `LOCAL_FORCED` / `OVER_RANGE` /
  `REFERENCE_ERR` raporlasa bile SCADA'ya `quality="good"` gidiyordu.
  Yayina baglanmasi `DNP3_PUBLISH_QUALITY_FLAGS` ile backend hazir olunca.
- **DNP3 zaman senkronizasyonu eklendi** (`DNP3_TIME_SYNC=lan`). `timeSyncMode`
  hicbir yerde set edilmiyordu; outstation saatleri serbest surukleniyordu.
- **`OnReceiveIIN` bostu** — `EVENT_BUFFER_OVERFLOW` (outstation olay tamponu
  doldu ve olaylari DUSURDU), `DEVICE_RESTART`, `NEED_TIME` artik loglaniyor.
- **G3 DoubleBitBinary / G21 FrozenCounter** sessizce dusuruluyordu.
- **Cihaz IP/port/DNP3 adresi degisince master yeniden kuruluyor.** Eskiden
  yalnizca `device.code` ile anahtarlaniyordu: RTU IP'si degistirilince gateway
  ESKI IP'ye baglanmaya devam ediyor, eski IP baska cihaza atanmissa O CIHAZDAN
  okuyup degerleri ESKI `device_code` ile yayinliyordu.
- **Kalici `lost` kilidi.** TCP hic kopmazsa cihaz DNP3'te saglikli konussa bile
  gateway sonsuza kadar `comm_lost` yayinliyor ve komut gondermeyi reddediyordu.
- **Production'da `GATEWAY_MODE=mock` artik reddediliyor.** Validator bunu
  kontrol etmiyordu; operator satiri degistirmeyi unutursa uydurma telemetri
  SCADA'ya akiyor ve mock adapter her komuta `ok=True` donduruyordu.

### Added — Gozlemlenebilirlik

- `/health`'e **cihaz haberlesme ozeti** (online/recovering/lost/unknown).
  Eskiden 300 cihazin tamami kopukken bile `{"status":"ok"}` donuyordu.
  Ozet SAYIMDIR; auth'suz endpoint cihaz kodu/IP sizdirmaz.
- **Poll dongusu watchdog'u** (`poll_loop_stalled`) ve **thread canliligi**
  (`thread_dead:<ad>`) — ikisi de `unhealthy`.
- **Disk alani ve saat sapmasi denetimi** (`resource_guard.py`). "Disk-full
  breaker" mesaj sayiyordu, BAYT saymiyordu; gateway saati tum arsivin tek
  zaman referansiyken hicbir yerde dogrulanmiyordu. Sapma > 30sn ise DNP3
  zaman yazimi durur.
- **Olu metrik sayaclari gercekten besleniyor**; `signals_outboxed_total`
  ayrildi (outbox'a dusen mesaj artik "published" sayilmiyor).

### Added — Dayaniklilik

- **SQLite sema surumleme** (`PRAGMA user_version` migration runner),
  **butunluk kontrolu** (`quick_check`) ve **bozuk-DB karantinasi**. Yeni kolon
  eklemek sahadaki mevcut `.db` dosyalarini crash-loop'a sokardi; bozuk dosya
  kalici boot arizasiydi.
- **`POST /operate` artik CommandLedger'a yaziliyor** (`command_id` ile
  idempotency). Backend'in HTTP timeout'u cihazin CROB suresinden kisa oldugu
  icin retry'da ayni kesici IKI KEZ surulebiliyordu.
- **Ana poll dongusu beklenmedik hatada olmuyor** (eskiden yalnizca
  `KeyboardInterrupt` yakalaniyordu; disk dolunca proses oluyordu).
- **Sinirsiz buyume budandi**: `_seen_command_ids` (FIFO pencere),
  `command_ledger` ve `outbox_dead_letter` (yas + adet retention).

### Changed — Kurulum / dagitim

- `.env.example` **30 eksik ayarla senkronlandi**; senkronu koruyan bir test
  eklendi (yeni ayar dokumante edilmezse CI kirmizi).
- `install.ps1` artik **yadnp3'u kuruyor** — eskiden dokumante edilen kurulum
  varsayilan konfigurasyonla boot edemiyordu.
- `new_gateway.ps1` artik `LOG_FILE_PATH` ve per-instance `GATEWAY_STATE_DIR`
  uretiyor ("NSSM'de zorunlu" denilen rotation sahada yoktu).
- Compose sablonu `TELEMETRY_PUBLISHER: "http"` set ediyor ve 300-cihaz
  varsayilanlarini geri almiyor. Sablon NATS'i isaret edip HTTP kullaniyordu.
- `:latest` **yalnizca surum tag'inde** uretiliyor; main push -> `:main` +
  `:sha-*` (rollback mumkun).
- Olu `DNP3_INTEGRITY_POLL_MIN` alani kaldirildi; yadnp3'un yok saydigi
  legacy ayarlar set edilmisse boot'ta uyariliyor.

## [0.4.6] - 2026-05-13

### Security — Audit follow-up (sprint sonrasi 2. pas)

Cross-reference auditi sonucu kapatilan ek bulgular:

- **Rate-limit memory leak FIX (B1)**: `_SlidingWindowRateLimiter._buckets`
  dict'i unbounded buyuyebilirdi (saldirgan farkli IP'lerden spam yaparsa).
  `start_health_server` artik daemon `rate-limit-cleanup` thread'i baslatir;
  her 120sn'de bir `cleanup_stale()` cagrir ve eskimis IP entry'lerini siler.
- **X-Forwarded-For trust boundary FIX (H1)**: Onceki impl XFF header'ini
  her zaman okuyordu — saldirgan dogrudan gateway'e baglanip
  `X-Forwarded-For: 127.0.0.1` set ederek localhost-muafiyetinden rate-limit
  bypass yapabilirdi. Yeni model nginx `real_ip` mantigi:
    * `HEALTH_TRUSTED_PROXIES` (CIDR listesi, default BOS) — bos ise XFF
      TAMAMEN yok sayilir (en guvenli default).
    * Set edilirse SADECE bu subnet'lerden gelen istekte XFF okunur.
- **429 / 401 / 404 / 503 Content-Length=0 (H2)**: HTTP/1.1 framing spec
  uyumu. Bazi client'lar Content-Length yoksa connection'i half-close gorur
  ve keep-alive bozulur.
- **404 fast-path rate-limit bypass FIX (M1)**: `do_POST` artik 404 dispatch
  oncesinde rate-limit check yapar. Saldirgan `POST /random` spam'le
  sunucuyu yoramaz.
- **LOG_LEVEL=DEBUG production WARN (M5)**: Boot'ta loud uyari log. DEBUG
  seviyesinde 3rd-party kutuphaneler (requests, urllib3) hassas detay
  loglayabilir; redaction filter cogu kapatir ama garanti yok.

### Added — Konfigurasyon

- **`HEALTH_TRUSTED_PROXIES`** (.env / config.py): Reverse proxy CIDR
  allowlist. Bos = XFF yok sayilir (default, en guvenli). Set edilirse
  sadece bu subnet'lerden gelen XFF okunur. Ornek:
  `HEALTH_TRUSTED_PROXIES=10.0.0.0/8,192.168.1.5/32`.

### Removed — Legacy kod (CR-3 follow-up)

- **`src/dnp3_gateway/messaging/rabbit_publisher.py`** tamamen silindi.
  0.4.3 cutover'da rollback amaciyla duruyordu; 0.4.6 audit sonrasi
  production'a guven geldigi icin saldiri yuzeyi azaltma amaciyla
  kaldirildi. Eski `pika` ile rollback gerekirse `git revert` ile geri
  alinabilir.

### Tests

- **`tests/test_health_server.py`** (16 yeni test):
  - `_SlidingWindowRateLimiter`: allow + localhost muafiyet + farkli IP
    bucket'lar + window slide + cleanup_stale.
  - `_parse_trusted_proxies`: bos/gecersiz/IPv6 CIDR.
  - `_ip_in_networks`: tek/coklu CIDR + bos liste + gecersiz IP.
- **`tests/test_outbox.py`** (11 yeni test):
  - enqueue / fetch_batch / delete / pending estimate.
  - `OutboxFullError` raise senaryosu.
  - `mark_retry` + `move_to_dead_letter`.
  - Restart sonrasi DB tutarliligi.
  - Unicode payload serileme.
- Toplam test coverage: 45 -> 72 test (sprintten sonra +27).

### Security — Production validator genisletildi

- **GATEWAY_TOKEN placeholder prefix reddi (PROD)**: `config._PLACEHOLDER_TOKEN_PREFIXES`
  listesi (orn. `change-me`, `gw-default`, `gw-001-token`, `please-change`,
  `example-token`) ile baslayan token'lar production'da SystemExit eder.
  `.env.example`'dan kopyalanip token guncellenmeden boot edilirse erken
  yakalanir. Auth katmanindaki literal `_PLACEHOLDER_TOKENS` set'i (frozenset)
  exact-match icin staging + production'da hala aktif.
- **RABBITMQ_URL production'da reddedildi**: Gateway 0.4.x'te RabbitMQ
  telemetri akisindan kaldirilmisti; bu validator eski .env'lerin sessizce
  prod'a deploy edilmesini engeller. Operator satiri silmek zorunda.
- **NATS_DUAL_PUBLISH_ENABLED=true production'da reddedildi**: DEPRECATED
  bayrak. Onceden no-op + WARN log; simdi prod'da hata vererek operator
  beklediginden farkli davranisla karsilasmasin.

### Security — Health server rate-limit (H-8/H-9)

- **Per-IP sliding window** her endpoint'te aktif:
  - `/health`, `/healthz`, `/info`, `/metrics`: 120 req/min/IP
  - `POST /refresh-all`: 10 req/min/IP (token leak senaryosunda saha cihazi
    DoS koruma).
- Localhost (`127.0.0.1`, `::1`) muaf — cati paneli/backend ayni host'tan
  sinirsiz probe yapabilir.
- `X-Forwarded-For` ilk degeri dikkate alinir (reverse proxy arkasinda gercek
  client IP'sini yakala).
- Rate-limit ihlali: HTTP 429 + `Retry-After: 60` header + WARN log.

### Removed — RabbitMQ legacy temizligi (CR-3)

- **`src/dnp3_gateway/messaging/rabbit_publisher.py` silindi**. 0.4.3'te
  cutover yapilmisti, modul rollback amaciyla duruyordu; production'a guven
  geldigi icin saldiri yuzeyi azaltma amaciyla kaldirildi.
- **`pyproject.toml` `legacy-rabbit` extra'si silindi** (`pika>=1.3` opsiyonel
  dependency).
- **`requirements.txt`** "pika ROLLBACK" yorumlari + Horstmann basligi
  temizlendi; production-friendly modern format.
- `messaging/__init__.py` docstring guncellendi — mimari diyagrami eklendi.
- `main.py` RabbitMQ password redaction blogu silindi (artik gerek yok).

### Changed — Tutarlilik (CR-1, CR-2)

- **`NATS_PUBLISH_TIMEOUT_SEC` doc/kod senkronizasyonu**: kod default `0.5sn`'den
  `2.0sn`'ye yukseltildi; `.env.example` ile uyumlu. 100 cihaz x 30 sinyal
  paralel cycle yuk senaryosunda agresif outbox dusurmesinin onune gecer
  (yerel/LAN icin makul, 4G/WAN icin de uygun).
- **`config.py` validator docstring** yeniden yazildi — staging-only vs
  production-only kurallar ayri listelendi.
- **`config.rabbitmq_exchange` ve `rabbitmq_routing_key` default'lari bos**
  (eskiden `hsl.events` / `telemetry.raw_received`). Field'lar sadece pydantic
  schema compat icin tutuluyor, hicbir yerde okunmuyor.

### Changed — Logging

- **3rd-party logger seviyeleri**: `nats`, `urllib3`, `urllib3.connectionpool`
  WARNING'e cekildi. Onceden INFO seviyesinde baglanti retry log'lari gurultu
  yapardi. `pika` logger silindi (paket artik yuklu degil).
- **Log redaction broker regex** halen `(amqp|amqps|nats|tls)://user:pass@`
  patternini yakalar (eski .env'lerden gelen amqp URL'leri parolasini
  redact eder).

### Changed — Rebrand temizligi (L-1..L-4)

- Tum source/docs/scripts'te "Horstmann SN2", "Horstmann Smart Logger",
  "hsl.events", "hsl-gw-*" referanslari **EnerjiOne** / **eg-gw-*** /
  **enerjione-gateway-*** ile degistirildi. Saha cihazlari artik
  uretici-agnostik "DNP3 outstation" olarak adlandiriliyor.
- `config.LOG_FILE_PATH` default ornek: `C:/ProgramData/EnerjiOne/...`
- `DeviceConfig.signal_profile` default `"default"` (eskiden
  `"horstmann_sn2_fixed"`); backend ne gonderirse o tasinmaya devam eder.
- `docker/compose.template.yml` container ismi `eg-gw-{code}`, network adi
  `enerjione`, volume `eg-gw-{code}-state`.
- User-Agent zaten `EnerjiOne-Dnp3Gateway/{ver}` (0.2.0'dan beri).

### Added — Operasyonel

- **`scripts/install.ps1`**:
  - `.env` artik UTF-8 BOM-siz yazilir (`.env.example`'dan kopyalama). Docker
    Compose `env_file` ve Python pydantic-settings BOM karakterini ilk
    anahtarin parcasi sanmasin.
  - `.env` olusturulduktan sonra NTFS ACL kisitlanir (sadece mevcut kullanici
    FullControl). Token PowerShell history / yedek vol'lara sizmasin.
- **`scripts/run_gateway.ps1`**: `-RabbitmqUrl` parametresi `-NatsUrl` ile
  degistirildi.

### Docs — Tam yeniden yazim

- **`docs/SECURITY.md`**: Kimlik modeli tablosu + multi-instance lock
  bolumu + NATS auth (NKEY/JWT credentials) detayi + DNP3 cihaz IP
  allowlist + log redaction katmanlari + production checklist.
- **`docs/RUNBOOK.md`**: NSSM kurulumu + `LOG_FILE_PATH` zorunlulugu +
  yeni log etiketleri + incident response (8 sik sorun) + operator
  endpoint'leri + graceful shutdown sirasi.
- **`docs/ARCHITECTURE.md`**: Bilesen diyagrami JetStream + outbox akisi +
  recovery state machine (lost -> recovering -> online) + defansif sema
  validasyonu tablosu + roadmap.
- **`docs/DOCKER.md`**: Image tagging (`:latest` yerine semver), NATS
  networking ornekleri, persistent state yedekleme.
- **`README.md`**: 0.4.x mimari diyagrami + production checklist + ozet
  ozellik listesi (yadnp3, outbox, allowlist, multi-instance lock,
  rate-limit). Eski RabbitMQ akis diyagrami silindi.
- **`scripts/README.md`**: install / run / new_gateway her birinin
  parametre ornekleri + NSSM ozetli yonlendirme.

### Tests

- `test_config_settings.py`:
  - `rabbitmq_exchange` / `rabbitmq_routing_key` default'lari bos kabulu.
  - `nats_publish_timeout_sec == 2.0` default kabulu.
  - 4 yeni prod validator testi:
    - `test_prod_rejects_placeholder_token`
    - `test_prod_rejects_change_me_prefix`
    - `test_prod_rejects_rabbitmq_url`
    - `test_prod_rejects_dual_publish_enabled`
  - `test_dev_allows_rabbitmq_url_and_placeholder` — development'da hepsi
    kabul edildigini dogrular.
- `tests/conftest.py`: `signal_profile="default"` (rebrand).

### Migration notes (0.4.5 -> 0.4.6)

Sahada `.env` icinde su satirlar varsa boot SystemExit eder (production):

```diff
- RABBITMQ_URL=amqp://...      # Sil, gateway artik kullanmiyor
- NATS_DUAL_PUBLISH_ENABLED=true   # Sil veya false yap
- GATEWAY_TOKEN=gw-001-token   # Yenisini uret: secrets.token_urlsafe(48)
- GATEWAY_TOKEN=change-me-...  # Aynisi: gercek token at
```

Eski deploy'lar `APP_ENVIRONMENT=staging` veya `development` ile bu kurallari
atlatabilir (gecici); kalici cozum yukaridakileri temizlemek.

## [0.4.5] - 2026-05-12

### Changed — production validator esnetildi (private network HTTP)
- **BACKEND_API_URL/NATS_URL production validator'u** artik host bazli karar
  veriyor: private/loopback ag (RFC1918, 127.x, *.local, *.lan, *.internal,
  localhost) icin clear-text http://+nats:// kabul; public host icin TLS
  hala zorunlu. Onceki halde "production" ortaminda her http:// reddedildigi
  icin internal IP'de calisan saha deploylari APP_ENVIRONMENT=staging'e
  dusmek zorunda kaliyordu.

### Added — bilincli plaintext opt-out
- **`GATEWAY_INSECURE_ALLOW_PLAINTEXT`** bayragi (default FALSE). Public
  host'a clear-text HTTP/nats:// gecici izin verir; boot'ta loud WARN log
  atilir. Saha senaryosu: backend henuz Caddy/LE ile TLS'lenmeden public
  IP'de calisirken gateway'i ayaga kaldirmak icin. Plan: TLS kurulunca
  bayragi kaldir.

## [0.4.4] - 2026-05-12

### Fixed — render_compose.py + saha template'leri (cutover follow-up)
- **`scripts/render_compose.py`**: `--rabbitmq-url` argumani `--nats-url`
  olarak yenilendi; `replacements` sozlugu `{{NATS_URL}}` yer tutucusu
  kullanir (compose template'i ile uyumlu). Onceki halde template
  `{{NATS_URL}}` istiyor ama renderer `RABBITMQ_URL` veriyordu — render
  `RenderError` ile crash ediyordu. Backend "yeni gateway" akisi ve CLI
  artik calisir.
- **`docker/.env.template`**: `RABBITMQ_URL` blogu `NATS_URL` ile
  degistirildi; `DNP3_LIBRARY` default `dnp3py` (legacy) -> `yadnp3`
  (onerilen). Production validator `NATS_URL` bos olmasini reddediyor.
- **`scripts/new_gateway.ps1`**: `-RabbitUrl` parametresi `-NatsUrl` ile
  degistirildi; uretilen .env dosyasi `NATS_URL` + `NATS_SUBJECT_PREFIX`
  yazar, `RABBITMQ_URL` artik yazilmaz.
- **`pyproject.toml`**: `nats-py>=2.6,<3` zorunlu dependency olarak
  eklendi; `pika` legacy-rabbit optional-dependency'sine tasindi.
  `requirements.txt`'nin runtime davranisi degismedi (oradaki pika
  satiri rollback amaciyla durmaya devam ediyor).

### Notes
- Saha gateway'leri `:latest` image cektikleri icin bu sürumun GHCR'de
  build edilmesi otomatik distribution saglar. Sahada `docker pull` +
  `docker compose up -d --force-recreate` ile alinir.

## [0.4.3] - 2026-05-11

### Security (BLOCKER seviye duzeltmeler — production hazirlik)
- **`/refresh-all` timing-safe auth + rol ayrimi**: `hmac.compare_digest` ile
  karsilastirma; ayri `GATEWAY_REFRESH_TOKEN` (bos ise endpoint devre disi).
  Eski "boyle local ise auth bypass" davranisi kaldirildi — container icinde
  `client_ip=127.0.0.1` yaniltici.
- **Token konsol/stderr leak'i kapatildi**: `new_gateway.ps1` artik token'i
  konsola yazmiyor (PSReadLine history sizmasi); sadece dosyaya yaziyor + NTFS
  ACL ile dosya izinlerini kisitliyor. `render_compose.py` token'i `--output`
  yoksa stdout'a sadece uyari ile birlikte aktariyor. `.gitignore` `.env.*`
  pattern ile genisletildi.
- **Production validator genisletildi**: prod'da `BACKEND_API_URL` https://
  zorunlu (clear-text token MITM koruma); `NATS_URL` bos olamaz + tls:// veya
  nats:// scheme; `GATEWAY_REFRESH_TOKEN` != `GATEWAY_TOKEN` (token leak
  cap'i sinirla).
- **Backend config response schema validation**: cihaz/sinyal listesi hard
  limit (1000 cihaz / 5000 sinyal), string field truncate, IP field URL/path
  injection reddi. Backend kompromize olursa gateway kontrolsuz buyume +
  log injection korur.

### Changed — CUTOVER: RabbitMQ → NATS JetStream
- **Telemetri akisi NATS JetStream'e tasindi.** Gateway artik RabbitMQ'ya
  baglanmaz; tum telemetri `e1.telemetry.raw.<gateway_code>` subject'ine
  basilir. Backend tarafindaki alarm/notification akisi RabbitMQ'da kalmaya
  devam ediyor — gateway onunla ilgilenmez.
- `JetStreamPublisher` artik primary publisher. `RabbitPublisher` modulu
  rollback senaryosu icin dosyada duruyor ama `messaging/__init__.py`'den
  export edilmiyor (explicit import gerek).
- `pika` paketi `requirements.txt`'te legacy-marked olarak duruyor; cutover'a
  guven gelince kaldirilacak.
- `nats-py>=2.6,<3` artik zorunlu runtime dependency.
- `RABBITMQ_URL` LEGACY/DEPRECATED — default bos; eski .env'lerden bozulma
  olmasin diye field tutuluyor.

### Added — DNP3 + Operasyonel
- **Recovery state machine** (yadnp3): fresh-frame onayli haberlesme
  dogrulamasi; comm_lost flap'larini onler, geri donus aninda 175 sinyalli
  full integrity poll publish'i.
- **Refresh-all endpoint**: operator tetikli "tum cihazlara sorgu at"
  (`POST /refresh-all` Bearer auth).
- **Rotating file log handler**: `LOG_FILE_PATH={gateway_code}.log` ile
  per-instance disk log; 20MB x 10 backup default (NSSM rotation eksikligini
  kapatir).
- **JetStream resilience**: thread-safe counter, background reconnect ile
  resource leak fix, ready=False olunca explicit raise (sessiz drop yok →
  outbox at-least-once).
- **Cycle ortasinda graceful shutdown**: `run_poll_cycle` artik `stop_event`
  argumani aliyor; seri ve paralel yolda 2sn quantum ile check eder.
- **Windows SIGBREAK handler**: NSSM stop'tan tetiklenen Ctrl+Break sinyali
  yakalanir.
- **Config refresh thread defansif Exception yakalama**: `ValidationError`,
  `SSLError`, `ConnectionResetError` artik thread'i sessizce oldurmuyor;
  state'e hata yazip backoff loop'a devam eder.
- **`Dnp3TelemetryReader.forget_devices` override**: legacy adapter'da silinen
  cihazlarin DNP3 session + cache temizligi.

### Fixed
- `poller.py:275` `getattr(...) and X or -1` antipattern — pending=0 durumunda
  yanlislikla -1 donuyordu. `pending_count()` public API ResilientPublisher'a
  eklendi.
- Class 0 yerine FULL integrity poll (0+1+2+3) — eksik baseline tazelemesini
  cozer (commit `2af0792`).
- `ScanClasses` imzasinda SOE handler eksikligi (commit `d65b525`).
- Recovery confirm aninda `mark_all_dirty` — gecikme azalma (commit `94c822b`).

### Tests
- `test_config_settings.py:15` default uyumlu (artik
  `show_gateway_token_on_start is False`).
- `test_auth_identity.py` production safeguard'a uyumlu test URL'leri.
- `test_config_client.py` `_DummySession` stream/raw/iter_content desteklior.
- `test_poller.py` Group 110 (string sinyal) yayinlanir kabulu.

## [0.3.5] - 2026-04-24

### Added
- `run_poll_cycle(max_parallel=...)` — `MAX_PARALLEL_DEVICES` artik poll
  dongusunde thread pool ile kullaniliyor. 100 cihazlik gateway'de seri okuma
  cycle suresini saniyeler/dakikalara cikarabiliyordu; paralel okumayla
  toplam cycle suresi okuma gecikmesinin en yavas cihazi kadar kaliyor.
- `test_run_poll_cycle_parallel_reads_all_due_devices` — 6 cihaz + 4 worker
  senaryosunda tum cihazlarin okunup `mark_read` edildigini dogrular.

### Changed
- `main.run()` artik `cfg.max_parallel_devices` degerini poll cycle'a aktariyor.
- `poller.run_poll_cycle` docstring'i paralel davranis ve publisher thread-safety
  varsayimini acikliyor.

## [0.2.2] - 2026-04-24

### Fixed / Changed
- `run_gateway.ps1` artik bos `gateway_code=` yazdirmiyor; once `scripts/show_env_summary.py`
  ile `.env` ozeti (kod, saglik portu, backend config URL).
- Baslangicta konsol banner: saglik URL, DNP3 TCP portu, coklu proses uyarisi.
- `404/401` config hatalarinda loga kisa cozum metni.
- `/health` JSON: `worker_health_port` alani.

## [0.2.1] - 2026-04-24

### Added
- Baslangicta konsola `GATEWAY_TOKEN` satiri (varsayilan tam metin);
  `SHOW_GATEWAY_TOKEN_ON_START=false` ile maskeli gosterim.

## [0.2.0] - 2026-04-24

### Added
- `dnp3_gateway.auth` — `GatewayIdentity`, kalıcı `GATEWAY_INSTANCE_ID` (veya
  `GATEWAY_STATE_DIR` altında dosya), `APP_ENVIRONMENT` ile üretim token
  uzunluğu + placeholder kontrolü.
- Her config isteğinde: `X-Gateway-Code`, `X-Gateway-Instance-Id`,
  `X-Request-Id`, `User-Agent`, `X-Gateway-Client` başlıkları.
- `BACKEND_API_VERIFY_SSL` / `BACKEND_API_CA_PATH` ile TLS doğrulama.
- `docs/SECURITY.md` — çoklu sunucu / token / RabbitMQ checklist.
- Sağlık JSON: `gateway_instance_id`, `app_environment`.

### Changed
- `BackendConfigClient` artık `GatewayIdentity` kullanır (eski token-only ctor kaldırıldı).

### Catı backend (Horstman Smart Logger)
- `GET /gateways/{code}/config`: isteğe bağlı `X-Gateway-Code` path ile
  uyuşmazsa 400 (yanlış yapılandırma / proxy erken tespiti).

## [0.1.0] - 2026-04-24

### Added
- Proje iskeleti (`src/dnp3_gateway/`, `tests/`, `scripts/`, `docs/`).
- `Settings` - pydantic-settings tabanli env + .env konfigurasyonu.
- `BackendConfigClient` - backend `/gateways/{code}/config` endpoint'i uzerinden
  cihaz listesi + standart Horstmann SN 2.0 sinyal katalogunu ceker.
- `GatewayState` - thread-safe calisma anı durumu + poll scheduler.
- `RabbitPublisher` - topic exchange + publisher-confirms + auto reconnect.
- `TelemetryReader` arayuzu + `MockTelemetryReader` (gercekci degerli).
- `Dnp3TelemetryReader` iskeleti (opendnp3 / `dnp3-python` tabanli) - 30/1/20
  object group'lari icin okuma destekli.
- `poller` - okunabilir sinyalleri filtreleyen, cihaz bazli telemetri mesaji
  uretip yayinlayan cekirdek dongu.
- `health_server` - `/health` JSON endpoint'i (status, config_version, versions).
- `main.run()` - config-refresh thread + polling loop + graceful shutdown.
- PowerShell scripts (`install.ps1`, `run_gateway.ps1`).
- 18 unit test (state, config_client, settings, mock adapter, poller).
- `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md`, `README.md`.
