# Saha kabulu — Horstmann Smart Navigator 2.0 (FAT/SAT)

Bu dokuman **gercek cihazla** yapilmasi gereken kabul adimlarini tarif eder.
Otomatik testler bu adimlarin YERINE GECMEZ: gateway'in sessiz kalmasi
ancak gercek bir modemin kapandigini gormekle dogrulanir.

> **DURUM: FIELD_PENDING.** Bu depoya fiziksel bir Smart Navigator bagli
> DEGIL. Asagidaki adimlarin hicbiri PASS olarak isaretlenmemistir.
>
> **2026-08-20 SAHA GOZLEMI: FAIL / RETEST GEREKLI** — bkz. bolum 3G.
> Smart uyku dongusu gerceklesmedi; gateway ~5.73sn'de bir Class 1/2/3
> event scan gonderiyordu ve 600sn'lik hareketsizlik sayaci dolamadi.

---

## 0. Kurallar (ihlal edilemez)

| | |
|---|---|
| CROB / SELECT / OPERATE | **YASAK** — ayrica yetkilendirilmedikce |
| `reset_all_fcis`, cikis komutlari, firmware islemleri | **YASAK** |
| Konfigurasyon yazma (cihaza) | **YASAK** |
| Izin verilen | TCP gozlemi, telemetri, olay alimi, pasif paket yakalama, `/health` okuma |

Bir olayi tetiklemek icin saha ekipmanini fiziksel olarak degistirmek
gerekiyorsa **otomatik yapilmaz**; adim `REQUIRES_OPERATOR_FIELD_ACTION`
olarak isaretlenir.

---

## 1. On kosullar

```bash
# 1) Gateway surumu ve etkin ayarlar
curl -s http://127.0.0.1:8020/health | jq '{version, status}'

# 2) Cihaz sozlesmesi (backend'de)
#    ip_endpoint_type = initiating
#    master_ip_port   = <blok icinde bir port>
#    session_policy   = smart | auto
#
# 3) Host port blogu compose'da YAYINLANMIS olmali
docker inspect eg-gw-gw-001 --format '{{json .HostConfig.PortBindings}}' | jq
#    -> "20100/tcp" ... gorunmeli. Gorunmuyorsa cihaz HICBIR ZAMAN baglanamaz.

# 4) Dinleyici gercekten acik mi
ss -lntp | grep -E ':(20100|20101)'
```

`/health` -> cihaz ozetinde `listener_error` sayaci **0** olmali. Degilse
port baska bir surec/gateway tarafindan kullaniliyordur.

---

## 2. Paket yakalama — NE SAYILIR, NE SAYILMAZ

On-prem Ubuntu icin `tcpdump` yeterlidir. Wireshark GEREKMEZ.

### KRITIK AYRIM: TCP baglanti denemesi ≠ DNP3 uygulama yuku

Bu dokumanin ilk hali "gateway -> cihaz **0 bayt**" ariyordu. Bu olcut
`listening` uc icin **YANLISTIR** ve saglikli bir kurulumu FAIL gosterir:

* `listening`te baglantiyi **gateway** acar. Cihaz uyurken opendnp3
  `ChannelRetry` ustel geri cekilmeyle SYN gondermeye **DEVAM EDER** — bu
  BEKLENEN ve GEREKLI davranistir; cihazin uyandigini boyle fark ederiz.
  SYN sayisi 0 ise cihaz uyandiginda **hic yakalanamaz**.
* Bir SYN **uygulama yuku tasimaz**. Cihazin 15 saniyelik DNP3 hareketsizlik
  sayacini sifirlayan sey uygulama katmani trafigidir; karsi taraf kapaliyken
  TCP kurulum denemesi cihazin DNP3 yiginina hic ulasmaz.

Uykuda BEKLENEN ve BEKLENMEYEN:

| Trafik | `listening` | `initiating` |
|---|---|---|
| TCP SYN / yeniden baglanma denemesi | ✅ **BEKLENIR** | — (baglantiyi cihaz acar) |
| Periyodik Class 1/2/3 scan | ❌ OLMAMALI | ❌ OLMAMALI |
| Class 0 / integrity scan | ❌ OLMAMALI | ❌ OLMAMALI |
| DNP3 keepalive / uygulama istegi | ❌ OLMAMALI | ❌ OLMAMALI |
| ICMP (yalnizca `late` iken, sinirli) | bilgi | bilgi |

**GECER OLCUTU: DNP3 uygulama yuku = 0.** SYN sayisi bilgilendirmedir.

`scripts/field_capture.sh` bunu sarmalar ve iki sayiyi AYRI raporlar
(`--endpoint listening|initiating`).

> **`greater 1` KULLANMAYIN.** O filtre paketin TOPLAM uzunlugunu olcer ve
> bir SYN (~60 byte) ondan GECER — yani saglikli bir listening kurulumunu
> "gateway susmuyor" diye FAIL gosterirdi. Yuk uzunlugu hesaplanmalidir:
> `IP toplam - IP baslik - TCP baslik > 0`, ve DNP3 icin ilk iki yuk
> sekizlisi `0x05 0x64` olmalidir.

---

## 3A. SMART + INITIATING yasam dongusu

**Amac:** cihaz uyanir, veriyi verir, gateway SUSAR, modem kapanir ve bu
`comm_lost` URETMEZ.

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihazda ariza/olay tetikle (ya da zamanlanmis raporu bekle) | modem acilir | `REQUIRES_OPERATOR_FIELD_ACTION` |
| 2 | Cihaz gateway host/master portuna TCP acar | `ss` ile ESTABLISHED gorulur | FIELD_PENDING |
| 3 | Docker/host dogru container'a yonlendirir | `yadnp3_master_link_open device=...` logu | FIELD_PENDING |
| 4 | DNP3 oturumu acilir, gecerli kanit gelir | `/health` -> `state=online` | FIELD_PENDING |
| 5 | Unsolicited/olay verisi yayinlanir | telemetri backend'e ulasir | FIELD_PENDING |
| 6 | **Gateway susar** | `>= 15 sn` boyunca gateway->cihaz **0 DNP3 uygulama paketi** | FIELD_PENDING |
| 7 | Cihaz TCP'yi kapatir / modem uyur | `yadnp3_master_link_close` | FIELD_PENDING |
| 8 | Gateway `smart_idle`e gecer | `/health` -> `state=smart_idle` | FIELD_PENDING |
| 9 | `connected=false`, `reachable=false` | `/health` | FIELD_PENDING |
| 10 | **comm_lost YOK** | SCADA'da cihaz kopuk GORUNMEZ | FIELD_PENDING |

Sessizlik kaniti (adim 6) icin:

```bash
./scripts/field_capture.sh --device-ip <CIHAZ_IP> --port <MASTER_IP_PORT> \
    --gateway-ip <GATEWAY_IP> --window 20
# Beklenen cikti: "gateway -> cihaz  DNP3 uygulama paketi (20s): 0"
```

---

## 3B. `smart_idle` iken RESTART

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihazi dogrulanmis `smart_idle`e getir (3A) | — | FIELD_PENDING |
| 2 | `docker compose restart` (volume KORUNUR) | — | FIELD_PENDING |
| 3 | `smart_idle` geri yuklenir | log: `smart_idle_restored device=...` | FIELD_PENDING |
| 4 | Filo capinda comm_lost firtinasi YOK | `/health` -> `devices.lost` artmaz | FIELD_PENDING |
| 5 | Son gecerli temas korunur | `/health` -> `last_valid_contact_epoch` | FIELD_PENDING |
| 6 | Cihaza istek URETILMEZ | capture: DNP3 uygulama paketi 0 | FIELD_PENDING |

> Volume silinirse (`docker compose down -v`) bu test ANLAMSIZDIR — kalici
> kayit da silinmis olur.

---

## 3C. BOOST kontrol testi (ayni gateway)

`MASTER-A = Smart`, `MASTER-B = Boost` ayni gateway'de:

| # | Beklenen | Sonuc |
|---|---|---|
| 1 | A: Smart yasam dongusu (sessiz + uyku) | FIELD_PENDING |
| 2 | B: surekli baglanti, normal taramalar | FIELD_PENDING |
| 3 | A uyurken B'nin taramasi DEVAM eder | FIELD_PENDING |
| 4 | B'nin trafigi A'yi uyandirmaz | FIELD_PENDING |
| 5 | `/health` -> `session_policies` ikisini de dogru sayar | FIELD_PENDING |

---

## 3D. AUTO testi

`session_policy=auto` ile:

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihaz Smart bildirir | `operation_mode=smart`, `effective_session_policy=smart` | FIELD_PENDING |
| 2 | Cihaz Boost bildirir | `operation_mode=boost`, `effective=continuous` | FIELD_PENDING |
| 3 | Smart -> Boost gecisi | log `device_operation_mode_changed`, taramalar baslar, **oturum yikilmaz** | FIELD_PENDING |
| 4 | Boost -> Smart gecisi | YALNIZCA o cihazin master'i yeniden kurulur | FIELD_PENDING |
| 5 | Komsu cihazlarin master'i yeniden kurulmaz | log'da baska `device=` yok | FIELD_PENDING |

**Mod polaritesi dogrulamasi (KRITIK):** ilk gecerli gozlemde log satirini
okuyun:

```
device_operation_mode_changed device=SN2-1 old=unknown new=smart raw=1.0 ...
```

Cihazin PANELDE gosterdigi mod ile `new=` esesmiyorsa polarite terstir.
Kod degisikligi GEREKMEZ — `operation_mode.normalize_operation_mode`
`smart_raw_value` parametresiyle ters cevrilebilir (bkz. Acik Riskler).

---

## 3E. Hata senaryolari

| # | Senaryo | Beklenen | Sonuc |
|---|---|---|---|
| 1 | TCP acilir ama DNP3 kaniti YOK -> kapanir | `smart_idle` DEGIL, `lost` | FIELD_PENDING |
| 2 | Yanlis `dnp3_address` | `recovering` -> `lost`, log'da gorunur | FIELD_PENDING |
| 3 | Host portu kapali/yanlis | cihaz baglanamaz; `ss` bos, `/health` `listener_expected=true` | FIELD_PENDING |
| 4 | Ayni gateway'de cakisan `master_ip_port` | **config REDDEDILIR** (calisma zamanina hic gelmez) | ✅ otomatik test |
| 5 | Sessizlik esigi asilir | `smart_idle` -> `lost`, comm_lost **TAM BIR KEZ** | FIELD_PENDING |
| 6 | Taze kanitla yeniden baglanma | `recovering` -> `online` | FIELD_PENDING |
| 7 | `smart_idle` iken komut | `status=offline`, fiziksel islem 0 | ✅ otomatik test |

---

## 3F. LISTENING + SMART yasam dongusu (1.14.0) — A..E

**Neden ayri:** `initiating`te (3A) baglantiyi cihaz acar ve uyku `OnClose`
ile bildirilir. `listening`te baglantiyi GATEWAY acar; cihaz uyudugunda
**hicbir olay tetiklenmez** — sadece TCP denemeleri basarisiz olur. Bu
yolun sahada dogrulanmasi 3A'nin yerine GECMEZ.

**On kosul:** cihaz `ip_endpoint_type=listening`, `session_policy=smart`
(ya da `auto`), `smart_max_silence_sec` cihazin rapor programina uygun,
tercihen `dial_in_interval_min` tanimli.

### A — Uyanik cihaz normal calisir

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihaz uyanikken gateway baglanir | `yadnp3_master_link_open` | FIELD_PENDING |
| 2 | Gecerli DNP3 kaniti gelir | `/health` -> `state=online` | FIELD_PENDING |
| 3 | Telemetri backend'e ulasir | tag-engine'de olcum | FIELD_PENDING |

### B — Uyku KABUL EDILIR, comm_lost URETILMEZ

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihaz modemini kapatir (rapor sonrasi) | — | `REQUIRES_OPERATOR_FIELD_ACTION` |
| 2 | Gateway'in TCP denemeleri basarisiz olur | `ss` -> ESTABLISHED YOK | FIELD_PENDING |
| 3 | Gateway `smart_idle`e gecer | log: `smart_idle_entered ... reason=listening_unreachable` | FIELD_PENDING |
| 4 | **comm_lost YOK** | SCADA'da cihaz kopuk GORUNMEZ | FIELD_PENDING |
| 5 | `reachable=false`, `connected=false` | `/health` | FIELD_PENDING |
| 6 | Gateway DNP3 istegi URETMEZ | capture: DNP3 uygulama paketi **0**; SYN denemeleri BEKLENIR | FIELD_PENDING |

```bash
./scripts/field_capture.sh --endpoint listening \
    --device-ip <CIHAZ_IP> --gateway-ip <GATEWAY_IP> \
    --port <CIHAZIN_DNP3_PORTU> --window 20
```

> **`--port` degeri `listening`te cihazin DNP3 portudur** (`dnp3_tcp_port`,
> varsayilan 20000), `master_ip_port` DEGIL.
>
> **SYN denemeleri gormek DOGRUDUR, FAIL DEGILDIR.** Baglantiyi gateway
> acar ve cihazin uyandigini ancak denemeye devam ederek fark eder. SYN
> sayisi **0** ise asil sorun odur: cihaz uyandiginda yakalanamaz.
> Gecer olcutu **DNP3 uygulama paketi = 0**.
>
> ICMP paketleri de gorulebilir — yalnizca cihaz `late` iken, 300sn
> siklik siniriyla ve **hicbir saglik karari uretmeden** (bkz. adim D5).

### C — Uyanma yakalanir

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihaz rapor icin modemini acar | — | `REQUIRES_OPERATOR_FIELD_ACTION` |
| 2 | Gateway yeniden baglanir | log: `smart_idle_wakeup` | FIELD_PENDING |
| 3 | Yakalama gecikmesi olculur | `<= smart_listen_reconnect_max_sec` (ya da <=60sn varsayilanda) | FIELD_PENDING |
| 4 | `state=online`, veri akar | `/health` | FIELD_PENDING |

> **Gecikme neden onemli:** Horstmann'in Socket Listening Timeout'u ~600sn.
> Yeniden baglanma tavani bunun altinda kalmali; aksi halde cihaz penceresi
> kapanmadan once gateway ona ulasamaz. `ChannelRetry` varsayilani
> (1sn→60sn ustel) 600sn icinde >=10 deneme uretir — **kutuphane ile
> olculmustur**, sahada yalnizca dogrulanmasi gerekir.

### D — Dial-In gecikmesi `late` uretir, `lost` URETMEZ

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Beklenen rapor saati gecer, cihaz susar | log: `smart_report_overdue` | FIELD_PENDING |
| 2 | `state` HALA `smart_idle` | `/health` | FIELD_PENDING |
| 3 | `report_late=true`, `report_overdue_sec>0` | `/health` | FIELD_PENDING |
| 4 | **comm_lost YOK** | SCADA'da kopuk GORUNMEZ | FIELD_PENDING |
| 5 | Tanilama calisir (ARKA PLANDA) | log: `device_probe ... ip_probe=... tcp_state=...` | FIELD_PENDING |
| 6 | Cihaz gec de olsa haber verir | `report_late=false`, `state=online` | FIELD_PENDING |

### E — Gercek kopus YINE yakalanir (fail-safe kontrolu)

Bu adim **en kritigidir**: B ve D'nin comm_lost'u bastirmasi, gercek bir
arizanin da gizlendigi anlamina GELMEMELIDIR.

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihaz kalici olarak devre disi (APN/guc) | — | `REQUIRES_OPERATOR_FIELD_ACTION` |
| 2 | `smart_max_silence_sec` dolar | log: `smart_idle_silence_exceeded` | FIELD_PENDING |
| 3 | `state=lost`, comm_lost **TAM BIR KEZ** | SCADA'da kopuk gorunur | FIELD_PENDING |
| 4 | Sonraki cycle'larda `smart_idle`e GERI DONMEZ | `/health` -> `state` `lost` kalir | ✅ otomatik test |
| 5 | Tanilama teshis tasir | `ip_probe_status` / `tcp_probe_status` + log yorumu | FIELD_PENDING |

> **Tanilama ciktilari HICBIR ZAMAN tek basina comm_lost uretmez.** Adim 5'te
> `ip_probe=unreachable` gormek beklenendir ve karari VERMEZ; karar
> `smart_max_silence_sec` esiginindir. Bu ayrimi bozan bir davranis
> gorulurse adim **FAIL** isaretlenmelidir.

---

---

## 3G. SAHA GOZLEMI 2026-08-20 — **NOT PASS**

> **DURUM: FAIL / RETEST GEREKLI.** Smart uyku dongusu bu gozlemde
> GERCEKLESMEDI. Asagidaki hicbir madde PASS isaretli DEGILDIR.

### Gozlenen

| | |
|---|---|
| Tarih | 2026-08-20, ~15:10 yerel |
| Gateway | 1.14.0 |
| Grid | 2.105.x |
| Cihaz | Horstmann Smart Navigator 2.0 |
| Uc tipi | `listening` |
| Cihaz panelinde Operation Mode | **SMART** |
| Horstmann oturum hareketsizlik zaman asimi | **600 saniye** |
| Cihaz | `188.59.29.74:20001` |
| Gateway (container) | `172.19.0.3` |

```
15:10:42.835  gateway -> cihaz   Flags [P.]  length 24
15:10:43.568  cihaz -> gateway   Flags [P.]  length 17
15:10:48.568  gateway -> cihaz   Flags [P.]  length 24
15:10:49.296  cihaz -> gateway   Flags [P.]  length 17
15:10:54.297  gateway -> cihaz   Flags [P.]  length 24
15:10:55.129  cihaz -> gateway   Flags [P.]  length 17
15:11:00.130  gateway -> cihaz   Flags [P.]  length 24
15:11:00.834  cihaz -> gateway   Flags [P.]  length 17
```

Gateway **~5.73 saniyede bir** uygulama yuku gonderiyor. Bu **saf ACK
degildir** (`Flags [P.]`, `length > 0`). Trafik surdugu surece Horstmann'in
600 saniyelik hareketsizlik sayaci **hicbir zaman dolamaz** ve modem Smart
uykusuna **giremez**.

### Cerceve cozumlemesi (yadnp3 3.2.1.1 ile OLCULDU)

| Cerceve | Yapı | TCP yuk |
|---|---|---|
| 3 sinif obje basligi — `60/2, 60/3, 60/4` = **Class 1,2,3 event scan** | READ | **24 bayt** |
| 4 sinif obje basligi — `+60/1` = **Class 0+1+2+3 integrity** | READ | 27 bayt |
| Bos (NULL) yanit, veri yok | RESPONSE | **17 bayt** |

Sahada gozlenen **24/17** ikilisi tam olarak *"Class 1/2/3 event scan +
olay yok yaniti"* imzasidir. Integrity (27) **degildir**.

`DNP3_EVENT_SCAN_INTERVAL_SEC=5` ve gozlenen aralik 5.73s = 5s + ~0.73s
yanit suresi — opendnp3 tarama gorevini yanit tamamlandiktan sonra yeniden
zamanlar.

### Ne anlama geliyor

Class 1/2/3 event scan'i kuran **tek** yer `_periyodik_scan_ekle()`
(`dnp3_yadnp3_master.py`), ve o **yalnizca** `session_policy != "smart"`
iken cagrilir. Yani bu cihaz **etkin `smart` politikayla kosmuyordu**.

> **Cihaz panelindeki Operation Mode = SMART bunu DEGISTIRMEZ.** Gateway
> mimarisinde yapilandirilan politika otoriterdir; Operation Mode etkin
> politikayi **yalnizca** `session_policy=auto` iken belirler. Panel Smart
> gosterirken gateway `continuous` kosuyorsa sorun **yapilandirmadadir**.

### RETEST — sirasiyla

**Adim 0 — kok nedeni TESPIT ET (tcpdump GEREKMEZ):**

```bash
docker logs eg-gw-<kod> 2>&1 | grep -E "yadnp3_master_enabled|auto_policy_fallback|yadnp3_periodic_scans_enabled|device_operation_mode_changed"
```

| Gorulen | Teshis | Yapilacak |
|---|---|---|
| `configured_policy=continuous ... periodic_scans=true` | **Grid `session_policy` gondermedi/`continuous` gonderdi** | Grid tarafinda cihazi `smart` ya da `auto` yap (bkz. BACKEND_TODO B5/B6) |
| `configured_policy=auto effective_policy=smart` + sonra `auto_policy_fallback` | **`operation_mode` noktasi katalogda yok** | Cihaz sinyal katalogune `master.operation_mode` (G1) ekle |
| `configured_policy=auto` + `device_operation_mode_changed ... new=boost` | Mod noktasi **BOOST** okunuyor | Nokta index'i/polaritesi kontrol et |
| `configured_policy=smart ... periodic_scans=false` ama trafik SURUYOR | **GATEWAY BUGU** | Gateway ekibine bildir |

**Adim 1 — dogrulama capture'i (EN AZ 90 saniye):**

```bash
./scripts/field_capture.sh --endpoint listening \
    --device-ip 188.59.29.74 --gateway-ip <GATEWAY_IP> \
    --port 20001 --window 90
```

> **90 saniye SART.** Ilk gozlem yalnizca ~17 saniyeydi ve
> `DNP3_EVENT_BASELINE_INTERVAL_SEC=30` ile kurulan **27 baytlik** integrity
> taramasini kacirdi. Teshis dogruysa 90 saniyelik pencerede ~30sn'de bir
> **27 baytlik** cerceve de gorulmelidir — bu, tanının **yanlislanabilir**
> testidir. Gorulmezse analiz eksiktir ve yeniden bakilmalidir.

### PASS olcutu — HEPSI saglanmadan Smart uyku PASS ISARETLENMEZ

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Gateway oturumu kurar | `yadnp3_master_link_open` | FIELD_PENDING |
| 2 | Gerekli ilk DNP3 alisverisi tamamlanir | telemetri gelir, `state=online` | FIELD_PENDING |
| 3 | **Tekrarlayan uygulama yuku YOK** | capture: DNP3 uygulama paketi **0** (SYN denemeleri BEKLENIR) | FIELD_PENDING |
| 4 | ~600 saniye hareketsizlik gecer | — | FIELD_PENDING |
| 5 | Horstmann oturumu kapatir / modem uyur | `yadnp3_master_link_close` | FIELD_PENDING |
| 6 | Gateway `smart_idle` raporlar | `/health` -> `state=smart_idle` | FIELD_PENDING |
| 7 | **comm_lost / offline alarmi YOK** | SCADA'da kopuk GORUNMEZ | FIELD_PENDING |
| 8 | Sonraki gecerli uyanmada `online`a doner | `/health` | FIELD_PENDING |


---

## 3H. SAHA GOZLEMI 2026-08-20 (ikinci tur) — **NOT PASS**

> **DURUM: FAIL / RETEST GEREKLI.** Grid v2.106.0 politikayi dogru sekilde
> `continuous` -> `smart` yapti ve **ilk Smart uykusu BASARILI oldu**
> (`smart_idle_entered reason=listening_unreachable`, comm_lost YOK).
> Uyanma sonrasi uc ayri hata gorulduu; hepsi 1.15.0'da duzeltildi ama
> **fiziksel retest yapilmadi**.

**Ortam:** cihaz SN2_0, `listening`, `session_policy=smart`,
`dial_in_interval_min=60`, `communication_grace_min=15`,
Horstmann Listening Session Timeout **120 sn** (test degeri).

### Gozlenen hatalar ve durumlari

| # | Bulgu | Kanit | 1.15.0 |
|---|---|---|---|
| 1 | Uyanmadan 3 sn sonra sahte `comm_lost` | `13:08:53 link_open` -> `13:08:56 comm_lost_announced` -> `13:09:12 g110_okundu` | ✅ duzeltildi (grace kadar ERTELEME) |
| 2 | Sessiz ama saglikli oturum kopuk ilan edildi | `13:11:16 device_stale last_data_age=120s` | ✅ duzeltildi (smart'ta olcum yasi comm_lost URETMEZ) |
| 3 | Deterministik ~15 sn `lost`/`relink` salinimi | `age=3,18,33,48,63,78,93,108` | ✅ duzeltildi (kanit nesli siniri) |
| 4 | ~60 sn'de bir 10 baytlik cift yonlu trafik | `16:27:29` / `16:27:30` | ✅ duzeltildi (link keepalive KAPALI) |

### 4 numaranin cozumu (varsayim DEGIL)

```
056405c90a000100feda
  CTRL = 0xc9 -> DIR=1 PRM=1 FC=9 = REQUEST_LINK_STATUS
```

**Gateway'in kendi** opendnp3 master link keepalive'i
(`cfg.link.KeepAliveTimeout`, olculen varsayilan **60000 ms**).

> **Grid'deki "DNP3 link status period = 0" ayari bunu ACIKLAMAZ**: o ayar
> **cihaz** tarafinin periyodudur. Bu cerceveleri gateway uretiyordu.

Ayni yakalamadaki `61 bayt (cihaz->gateway)` + `15 bayt (gateway->cihaz)`
**mesrudur**: cihazin unsolicited raporu ve ona protokol geregi verilen
uygulama katmani CONFIRM'i.

### RETEST — PASS olcutu

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | `smart_idle` -> disaridan uyandirma | `link_open`, `smart_idle_wakeup` | FIELD_PENDING |
| 2 | Uyanma pazarligi | **comm_lost YOK** (grace dolmadan) | FIELD_PENDING |
| 3 | Ilk gecerli DNP3 kaniti | `device_recovered`, `state=online` | FIELD_PENDING |
| 4 | Oturum sessizlesir | **hicbir tekrarlayan DNP3/link yuku yok** (10 baytlik keepalive DAHIL) | FIELD_PENDING |
| 5 | Fiziksel Session Timeout dolar | Horstmann oturumu KENDISI kapatir | FIELD_PENDING |
| 6 | `online -> link_close -> smart_idle` | comm_lost **YOK** | FIELD_PENDING |
| 7 | `online -> stale -> comm_lost` **GORULMEMELI** | — | FIELD_PENDING |
| 8 | `lost -> relink -> timeout -> lost` dongusu **GORULMEMELI** | — | FIELD_PENDING |
| 9 | Sonraki uyanma normal `online` yapar | — | FIELD_PENDING |
| 10 | Kacirilmis Dial-In -> `late` -> `lost` -> ertesi gun kurtarma | — | FIELD_PENDING |

**Capture suresi fiziksel Session Timeout'un EN AZ 2 KATI olmali** (120 sn
test degeri icin >= 240 sn), yoksa 60 saniyelik keepalive'in kapandigi
dogrulanamaz.

```bash
./scripts/field_capture.sh --endpoint listening     --device-ip <CIHAZ_IP> --gateway-ip <GATEWAY_IP>     --port <CIHAZ_DNP3_PORTU> --window 300
```

---

## 4. Kalite bayragi pilotu (G-QUALITY-PILOT)

**Ayar (dogrulanmis kanonik ad):** `DNP3_PUBLISH_QUALITY_FLAGS`
(`config.py` -> `dnp3_publish_quality_flags`). Varsayilan `false`.

Bu bayrak **TEK BIR gateway'de** acilir; filo geneline saha dogrulamasi
yapilmadan ACILMAZ.

```bash
# Pilot gateway'de
DNP3_PUBLISH_QUALITY_FLAGS=true
```

| Durum | Nasil uretilir | Guvenli mi | Sonuc |
|---|---|---|---|
| normal analog -> `good` | dogal isletme | ✅ | FIELD_PENDING |
| normal binary -> `good` | dogal isletme | ✅ | FIELD_PENDING |
| normal double-bit -> `good` | kesici pozisyonu | ✅ | FIELD_PENDING |
| `restart` | cihaz yeniden baslatilir | ⚠ operator karari | `REQUIRES_OPERATOR_FIELD_ACTION` |
| `forced` (local/remote) | noktayi elle zorlama | ⚠ operator karari | `REQUIRES_OPERATOR_FIELD_ACTION` |
| analog `invalid` / referans hatasi | CT/referans kaybi | ❌ guvenli uretilemez | `NOT_TESTED_PHYSICAL` |
| `comm_lost` | link kopmasi (kablo/APN) | ✅ | FIELD_PENDING |

> **Cift-bit (G3) durum bitleri analog referans hatasiyla KARISTIRILAMAZ.**
> Ayni bayrak byte'i (`0x41`) G3'te "kesici ACIK", G30'da "REFERENCE_ERR"
> demektir. Bu ayrim `map_dnp3_quality(flags, object_group)` icinde tipe
> gore yapilir ve `tests/test_dnp3_quality_and_resilience.py` ile gercek
> DNP3 trafigi uzerinde pinlenir — **otomatik dogrulanmistir**, sahada
> yeniden uretilmesi GEREKMEZ.

Guvenli sekilde uretilemeyen durumlar icin **fabrikasyon PASS yazilmaz**;
`NOT_TESTED_PHYSICAL` isaretlenir.

---

## 5. Kabul ozeti sablonu

Raporlarken her satiri su dortluden biriyle isaretleyin:

```
PASS                            (kanit ekli)
FAIL                            (kanit ekli)
FIELD_PENDING                   (fiziksel cihaz yok)
REQUIRES_OPERATOR_FIELD_ACTION  (saha mudahalesi gerekiyor)
NOT_TESTED_PHYSICAL             (guvenli uretilemez)
```

Kanit = `pcap` dosyasi, `/health` ciktisi ya da log satiri. Kanitsiz PASS
KABUL EDILMEZ.
