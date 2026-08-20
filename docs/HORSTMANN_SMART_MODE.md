# Horstmann Smart Navigator 2.0 — Smart Mode oturum yasam dongusu

Bu dokuman, gateway'in Horstmann Smart Navigator 2.0 **Smart Mode** /
**Initiating Endpoint** davranisini nasil desteklediginI anlatir (G-SMART-01).

> **Tek cumlelik ozet:** Smart Mode'da cihazin hucresel modemi normalde
> KAPALIDIR ve DNP3 oturumu **15 saniye** bosta kalinca kapanir. Gateway
> periyodik tarama gonderirse bu sayac hicbir zaman dolmaz; beklenen
> kapanmayi `comm_lost` sayarsa da saglikli bir sahayi arizali gosterir.

---

## 1. Boost ve Smart Mode

| | **Boost Mode** | **Smart Mode** |
|---|---|---|
| Hucresel modem | SUREKLI bagli | Normalde **KAPALI** |
| Ne zaman baglanir | Her zaman bagli | "Wake up Modem" isaretli bir DNP3 noktasi degistiginde; ve zamanlanmis raporda |
| Ariza noktalari | — | Normalde **Wake up Modem** etkin olmali |
| Izleme noktalari | Aninda | Saklanip **bir sonraki zamanlanmis raporda** gonderilebilir |
| Oturum sonu | Kalici baglanti | Hareketsizlik zaman asimi **15 saniye SABIT** (Initiating Endpoint) |
| Ad-hoc Class 0 yoklamasi | Mumkun | **YALNIZCA Boost Mode'da mumkun** |
| Dogru gateway davranisi | Surekli/olay tabanli DNP3 | Kabul et, dinle, **SUS**, modemin kapanmasina izin ver |

**Kritik ayrinti:** *hareketsizlik zaman asimini HER TCP/DNP trafigi
sifirlar.* Yani "az gonderelim" yetmez; **hic gondermemek** gerekir.

---

## 2. Politika ACIKCA yapilandirilir

Gateway rejimi cihazin DNP3 noktalarindan **CIKARMAZ**. Karar tek bir
alandan gelir:

```
DeviceConfig.session_policy = "continuous" (VARSAYILAN) | "smart" | "auto"
DeviceConfig.ip_endpoint_type = "listening" (VARSAYILAN) | "initiating"
```

* **`continuous`** — bugunku davranis, birebir. Periyodik Class 1/2/3 event
  scan + Class 0 integrity scan + baglantida acilis integrity poll'u. Her
  zaman bagli DNP3 ekipmani icin dogru olan budur.
* **`smart`** — hicbir tekrarlayan tarama gorevi kurulmaz, acilis integrity
  poll'u yapilmaz, beklenen kapanma `comm_lost` uretmez.
* **`auto`** (1.13.0) — rejim cihazin **Master `Operation Mode`** noktasindan
  CALISMA ANINDA turetilir. Mod gozlenene kadar gateway **sessiz** kalir.

### Gecerli kombinasyonlar (1.14.0 — HEPSI)

**Uc tipi ile Operation Mode BAGIMSIZ iki kavramdir:**

```
ip_endpoint_type  ->  TCP baglantisini KIM acar
operation mode    ->  cihaz modemini KAPATIR MI
```

| `ip_endpoint_type` | `session_policy` | Gecerli mi |
|---|---|---|
| `listening` | `continuous` (boost) | ✅ |
| `listening` | `smart` | ✅ (1.14.0) |
| `listening` | `auto` | ✅ (1.14.0) |
| `initiating` | `continuous` (boost) | ✅ |
| `initiating` | `smart` | ✅ |
| `initiating` | `auto` | ✅ |

> **1.13.0'dan davranis degisikligi.** 1.13.0 `listening` + `smart`/`auto`
> kombinasyonunu config seviyesinde REDDEDIYORDU. Gerekce "Smart Mode'da
> baglantiyi cihaz baslatir" idi ve bu **uc tipi ile modu birbirine
> karistiriyordu**. Sabit IP'li (ya da APN icinden erisilebilen) bir
> Horstmann Smart modda calisir: modemini kapatir. Dogru davranis surekli
> SYN gondermek degil, uykuyu KABUL ETMEKTIR.
>
> Reddetmenin somut zarari: ya kurulum hic yapilamiyordu, ya da cihaz
> `continuous` kosturulup gateway her tarama araliginda frame gonderiyor,
> 15sn'lik hareketsizlik sayaci HIC dolmuyor ve modem HICBIR ZAMAN
> kapanmiyordu — yani ozelligin tamami calismiyordu.

**`listening` + `smart` yasam dongusu** `initiating`ten SU NOKTADA AYRILIR:
orada baglantiyi gateway acar, dolayisiyla cihaz uykudayken TCP baglantisi
**kurulamaz** ve `OnOpen`/`OnClose` HIC tetiklenmez. `smart_idle`e giris
`set_connected(False)` yolundan DEGIL, okuma yolundan yapilir:

```
baglanti kurulamiyor + session_policy=smart + uc=listening
    -> smart_idle (BEKLENEN uyku)      -> comm_lost YOK
    -> denetim: dial_in + max_silence  -> gercek kopus YINE yakalanir
```

**Basarisiz TCP denemeleri TEK BASINA `comm_lost` URETMEZ.** Bu bir muafiyet
degildir: hic konusmamis bir cihaz icin sessizlik sayaci master'in kurulus
anindan baslar (`olusturuldu_wall` capasi), yani gercekten olu bir cihaz
`smart_max_silence_sec` dolunca yine `lost` olur.

Varsayilan `continuous` oldugu icin **mevcut kurulumlarin davranisi
degismez**. Backend sozlesmesi: [BACKEND_TODO.md](./BACKEND_TODO.md#b5)

> **Tanimsiz bir deger konfigurasyonu DUSURUR.** `"smrt"` yazim hatasi
> sessizce `continuous`a duserse, Smart moda alinmasi gereken bir cihaz
> periyodik taranmaya devam eder, modemi hicbir zaman kapanmaz ve bunu
> kimse fark etmez. Gateway bu durumda TUM config'i reddeder ve son iyi
> config'iyle calismaya devam eder.

### Otomatik tespit neden YOK

Smart Navigator, `Binary Input G1 index 15` uzerinde **Master Operation
Mode** raporlar (`0x01` = Boost, `0x81` = Smart). Bundan otomatik tespit
**bilincli olarak ayri bir istir** ve bu gorevin kapsaminda degildir:

* ilk uygulamanin davranisi ONGORULEBILIR olmali — operator ne
  yapilandirdiysa o calisir;
* index profile gore degisir (Pole Master farkli), yani genel adapter'a
  `G1/15` gomulmemelidir;
* `Boost Mode Enabled` (yetenek) ile `Operation Mode` (calisma anindaki
  durum) farkli seylerdir ve karistirilirsa pil dusukken fiilen Smart
  calisan bir cihaz Boost sanilir.

Bkz. bolum 10 — sonraki gorev.

---

## 2b. `auto` — Master Operation Mode'dan turetme

### Kaynak: YALNIZCA Master

Karar **yalnizca** hucresel Master/PoleMaster'in `Operation Mode` noktasindan
verilir. Su kaynaklar KATILMAZ:

* `Satellite N Operation Mode` — telemetri olarak yayinlanir, politikaya girmez
* `Boost Mode Enabled` — KONFIGURASYON (yetenek), calisma anindaki durum degil
* `Boost Mode` (G10 komut noktasi)
* uc tipi tek basina

Sinyal cihazin KENDI katalogundan **semantik** kimlikle bulunur:
`data_type=binary` **ve** `dnp3_object_group=1` **ve** anahtarin son bileseni
`operation_mode` **ve** `source` master. **DNP3 index SABITLENMEZ** (SN 2.0
G1/15, Pole Master farkli).

### Deger yorumu — DEGER mi BAYRAK mi

Dokumantasyon `0x01 = Boost`, `0x81 = Smart` der. **Bunlar noktanin degeri
DEGIL, tam DNP3 bayrak oktetidir.** Group 1 bayrak byte'inin 0x80 biti
DURUM (STATE) bitidir — yani degerin kendisi:

| Bayrak okteti | STATE biti | Deger | Mod |
|---|---|---|---|
| `0x01` | 0 | `0` | **Boost** |
| `0x81` | 1 | `1` | **Smart** |

Kutuphaneyle dogrulandi (yadnp3 3.2.1.1):
`Binary(True, Flags(0x01))` -> `flags=0x81`.

Naif "1 = Boost" varsayimi tersine cevirirdi. Esleme
`operation_mode.normalize_operation_mode(..., smart_raw_value=...)` ile ters
cevrilebilir ve `tests/test_auto_session_policy.py` turetmeyi adim adim pinler.

### Baslangic durumu (mod henuz bilinmiyor)

`auto` **sessiz** baslar: tarama yok, acilis integrity poll'u yok.

> Siniflandirma ugruna tarama kurmak, cihaz gercekten Smart ise 15 saniyelik
> idle sayacini surekli sifirlardi — yani duzeltmeye calistigimiz hata.

Ayrica **siniflandirma icin taze bir baglanti YIKILMAZ**.

`/health` bu belirsizligi gosterir: `effective_session_policy="unknown"`,
`operation_mode="unknown"`.

### Fallback (mod hic gelmezse)

**BAGLANTILI** gecen 120 saniyede mod gozlenemezse `continuous` uygulanir.
Sayac yalnizca cihaz BAGLIYKEN isler (baglanti yokken tarama kurmak anlamsiz
olurdu ve cihaz sonradan baglandiginda taramalar hazir beklerdi).

**Iki risk de gercek, secim bilincli:**

| Fallback | Risk |
|---|---|
| `continuous` (SECILEN) | Cihaz gercekten Smart ise modemi acik kalir — pil tuketimi, YAVAS zarar |
| `smart` | Cihaz gercekten Boost ise yoklama durur — telemetri bayatlar, VERI DOGRULUGU zarari; gercek kopma da gecikir |

Veri dogrulugunu pil tasarrufunun onune koyuyoruz; ayrica `continuous`
1.11.x'ten beri suregelen davranistir. Karar **sessiz degil**:
`auto_policy_fallback` WARNING loglanir ve `/health` `auto_fallback=true`
gosterir.

### Calisma anindaki gecisler

| Gecis | Yapilan | Oturum yikilir mi |
|---|---|---|
| (auto) ilk mod = Smart | zaten sessiz | **HAYIR** |
| (auto) ilk mod = Boost | `AddClassScan` calisma aninda | **HAYIR** |
| Smart -> Boost | `AddClassScan` calisma aninda | **HAYIR** |
| Boost -> Smart | **yalnizca o cihazin** master'i yeniden kurulur | evet (tek cihaz) |
| Ayni mod tekrar | hicbir sey | hayir (flap YOK) |

Boost -> Smart'ta rebuild ZORUNLU: pinlenmis yadnp3 3.2.1.1 bir taramayi
kaldirmayi/durdurmayi SUNMUYOR (`IMasterScan` yalnizca `Demand()` tasiyor).
`DNP3Manager` ya da diger cihazlarin master'lari ETKILENMEZ.

## 3. Durum makinesi

Mevcut uc durumlu makineye **`smart_idle`** eklendi:

```
                    TCP acildi
   [lost] ─────────────────────────────────► [recovering]
      ▲                                            │
      │                                            │ gecerli DNP3 kaniti
      │                                            ▼
      │  TCP kapandi + continuous              [online]
      ├────────────────────────────────────────────┤
      │  TCP kapandi + smart + KANIT YOK            │
      │                                            │ TCP kapandi + smart
      │  sessizlik esigi asildi                     │ + oturumda KANIT VAR
      └──────────────────── [smart_idle] ◄──────────┘
```

| Gecis | Kosul |
|---|---|
| `* -> recovering` | TCP acildi (`OnOpen`) |
| `recovering -> online` | Gecerli DNP3 kaniti geldi |
| `online/recovering -> lost` | TCP kapandi, politika `continuous` |
| `online -> smart_idle` | TCP kapandi, politika `smart`, **oturumda kanit gorulmus** |
| `* -> lost` | TCP kapandi, politika `smart`, **oturumda kanit YOK** |
| `smart_idle -> lost` | Sessizlik esigi asildi (bolum 5) |
| `smart_idle -> recovering` | Cihaz yeniden baglandi |

### "Gecerli DNP3 kaniti" nedir

Uc seyden biri:

* bir **olcum** (SOE handler'a dusen herhangi bir nokta),
* **gecerli bir IIN** (outstation'in her uygulama yaniti),
* **basarili bir DNP3 gorevi** (`OnTaskComplete` -> `SUCCESS`).

**Salt TCP baglantisi kanit DEGILDIR.** 4G'de soket kurulup DNP3 katmani hic
konusmadan da dusebilir; bunu "basarili oturum" sayip `smart_idle`e gecmek,
GERCEK bir arizayi saglikli uyku gibi gosterirdi. Bu yuzden kanitsiz bir
oturumun kapanmasi `lost` uretir ve normal `comm_lost` yolundan gecer.

---

## 4. `smart_idle` okuma davranisi

`smart_idle` durumundaki bir cihaz icin `read_device`:

* **cihaz seviyesinde `comm_lost` URETMEZ**,
* degismemis sinyaller icin `no_change` doner,
* **son bilinen degerleri KORUR** (comm_lost yolunun aksine 0.0'a cevirmez),
* cihazi komutlar icin **ulasilabilir SAYMAZ** (`reachable=false`),
* cihaza **tek bir DNP3 istegi bile gondermez** — yoklama, integrity poll ve
  zorla relink hepsi devre disidir.

Bekleyen bir yayin varsa (orn. operator `/refresh-all` tetiklediyse ya da
oturum kapanirken onaylanmamis degerler kaldiysa) o degerler yayinlanir.
Bu **yerel** bir istir; cihaza istek gitmez ve modem uyanmaz.

---

## 5. Sessizlik denetimi: `smart_idle -> lost`

**"TCP kopuk" ile "cihaz zamaninda haber vermedi" AYRI seylerdir.** Smart
modda modemin kapali olmasi beklenen durumdur; ama cihaz sonsuza kadar da
kaybolamaz.

```
son gecerli kanit + izin verilen sessizlik suresi
    -> hala smart_idle (saglikli)

esik asildi
    -> lost -> mevcut comm_lost mekanizmasi (TEK bir kenar yayini)
```

### Kanonik cozum sirasi

`smart_max_silence_sec` = `null` / eksik demek **"cihaz seviyesinde ezme yok"**
demektir — **"denetim kapali" DEMEK DEGILDIR**. Karar su sirayla cozulur:

1. **gecerli cihaz degeri** — `DeviceConfig.smart_max_silence_sec`, **60..2592000**
2. **`DNP3_SMART_MAX_SILENCE_SEC`** — kurulum geneli yedek (`0` = kapali,
   `60..2592000` = gecerli)
3. **kapali**

**Gecersiz cihaz degeri** (aralik disi — `0` ve negatifler dahil —, bozuk tip
ya da ayristirilamayan deger): cihaz ezmesi **yok sayilir** + WARNING, ve ayni
sirayla **2. adima** dusulur. Config DUSMEZ.

> Aralik kontrolu hem backend parser'inda hem adapter'da yapilir: `DeviceConfig`
> disk onbelleginden de gelebilir ve o yol parser'i atlar.

**Env yedegi fail-closed dogrulanir:** `1..59` ve `2592000` ustu degerler
**boot'ta config hatasi** uretir. O araliktaki bir esik normal bir Smart
uykusunu bile kopuk ilan ederdi.

> **Adapter'da gomulu bir sure YOKTUR** — bilincli. Dogru deger cihazin
> Dial-In rapor programina baglidir ve yalnizca kurulumu yapan bilir.
> "24 saat" gomulseydi, saatlik rapor veren bir cihaz saatlerce olu
> gorunmeden kalirdi; gunluk rapor veren bir cihaz ise her gun sahte
> comm_lost uretirdi.

Sure **monotonic** saatle olculur: duvar saatiyle olculse tek bir NTP
siçramasi uyuyan tum filoyu ayni anda `lost` yapardi.

Esik asildiktan sonra cihaz, **yeni ve kanitli bir oturum** kurmadan tekrar
`smart_idle`e donemez.

---

## 5b. Dial-In farkindali saglik: `late` (1.14.0)

`smart_max_silence_sec` **kopus** esigidir ve tipik olarak gunler
mertebesindedir. Tek basina kullanildiginda operator, gercekten bozulmus bir
cihazi gunler sonra ogrenir. `dial_in_interval_min` bu bosluga **erken
uyari** koyar.

Uc pencere vardir ve **karistirilmamalidirlar**:

| Pencere | Durum | comm_lost |
|---|---|---|
| beklenen rapordan ONCE | `smart_idle` — SAGLIKLI | ❌ |
| rapor gecti, `max_silence` dolmadi | **LATE / DEGRADED** | ❌ |
| `max_silence` asildi | `lost` | ✅ **tam bir kez** |

`late` bir **durum degil, bayraktir**: `state` `smart_idle` KALIR. Sebebi
somut — Dial-In gecikmesi cok sik iyi huyludur (hucresel ag tikanikligi,
cihazin rapor saatinde kucuk kayma) ve bunu `lost` saymak SCADA'da
gunluk sahte alarm uretirdi. `/health` bunu `report_late: true` ve fleet
ozetinde ayri bir `late` sayaciyla raporlar.

`dial_in_interval_min` **verilmezse `late` HIC uretilmez** — her kurulumda
zamanlanmis Dial-In tanimli olmak zorunda degildir.

### Tanilama: ICMP (aktif) + kanal durumu (pasif)

Cihaz `late` oldugunda gateway bir **teshis** uretir:

```
ip_probe  unreachable         -> modem / APN / yonlendirme suphesi
ip ok + kanal hic acilmiyor   -> dinleyici / guvenlik duvari / uyku
kanal OPEN + DNP3 kaniti yok  -> protokol / oturum sorunu (adres, link)
```

> **HICBIR TANILAMA CIKTISI `comm_lost` URETMEZ.** Bu bir tasarim kurali,
> bir ayrinti degil. ICMP saha aglarinda/APN'lerde sikca ENGELLIDIR ve Smart
> bir modem MESRU olarak uykudadir — ping'e cevap vermemesi BEKLENEN
> davranistir. "ping dusuyor -> cihaz oldu" kurali filonun yarisini sahte
> kopuk gosterirdi. Tanilama operatorun arizanin YERINI bulmasi icindir;
> durum makinesinin girdisi DEGILDIR.

#### TCP teshisi ham soketle OLCULMEZ

`tcp_probe_status` **yadnp3 kanalinin kendi durumundan** turetilir
(`IChannelListener.OnStateChange`) — cihazin DNP3 portuna tanilama amacli
ham soket **ACILMAZ**.

> Gerekce, ikinci bir yeniden baglanma dongusu kurmama gerekcesiyle AYNIDIR:
> **uretim DNP3 kanaliyla yarisan ikinci bir TCP baglantisi.** Smart moddaki
> bir Horstmann yalnizca sinirli bir Socket Listening Timeout penceresi
> boyunca uyanik kalir; ham tanilama soketi yarisi KAZANIP once baglanabilir,
> sonra hicbir DNP3 trafigi uretmeden kapanarak gercek oturumu ENGELLER.
> Olcum araci olctugu sistemi bozmus olur.

Kutuphane "reddedildi" ile "paket dustu" ayrimini VERMEZ; o ayrim
UYDURULMAZ:

| Kanal durumu | `tcp_probe_status` |
|---|---|
| `OPEN` | `open` |
| `OPENING` | `connecting` (deneniyor — ariza DEGIL) |
| `CLOSED` / `SHUTDOWN` / hic gozlenmedi | `unknown` |

#### ICMP her zaman ARKA PLANDA kosar

`ping` alt surec baslatir ve saniyelerce bloklayabilir. Poll thread'inden
dogrudan cagrilmasi **suru etkisi** uretirdi: 200 cihaz ayni anda `late`
olursa (APN kesintisi, saha elektrigi — gercek senaryolar) her biri ilk
sondasini AYNI cycle'da hak eder ve senkron cagrilarda bunlar SIRAYA girer:

```
200 cihaz x ~2sn ICMP zaman asimi = ~400 saniye HICBIR CIHAZ OKUNAMAZ
```

Yani tanilama, teshis etmeye calistigi kesintiyi GERCEK bir kesintiye
cevirirdi. Cihaz basina 300sn siklik siniri bunu TEK BASINA COZMEZ.

Bu yuzden ICMP **sinirli bir arka plan havuzunda** kosar
(`network_probe.DiagnosticExecutor`): sinirli isci, sinirli kuyruk, cihaz
basina en fazla bir ucus, kuyruk dolunca is **dusurulur** (telemetri ASLA
beklemez), istisnalar izole. `/health` sondanin bitmesini **beklemez**; son
bilinen degeri doner.

#### Kapanis: iptal et, sonra bekle

Tanilama isi **best-effort**tur. Kapanista biriken is **akitilmaz, iptal
edilir**:

1. yeni `submit` **derhal** reddedilir
2. kuyrukta **bekleyen** isler iptal edilir — bu ayni zamanda kapanis
   sinyaline **yer acar**
3. isci basina bir sinyal **guvenilir** sekilde yerlestirilir
4. yalnizca o an **calisan** isler biter
5. **tum** isci thread'leri cikana kadar beklenir
6. ancak ondan sonra master/kanal yikimina devam edilir

> **`daemon=True` bir mekanizma DEGILDIR.** Kuyruktaki kapanmalar
> `mm.ip_probe`, `mm.sonda_son_wall` ve `mm.kanal_durumu()` uzerinden
> **master nesnelerine** dokunur; isciler hala calisirken master/kanal
> yikilirsa bu erisimler yikilmis nesnelere gider. Siralama bir tercih
> degil, **dogruluk sartidir**.

`shutdown()` `bool` doner. `False` ise cagiran taraf **sessiz gecmez** —
`close()` ERROR loglar, cunku sonraki bir crash'in tek ipucu o satir olur.

Sayaclar ayri tutulur: `dropped_total` (kuyruk **doygunlugu**),
`cancelled_total` (**kapanista** iptal), `completed_total` (fiilen
calistirilan). Karistirilirlarsa operator yanlis yere bakar.

Tanilama yalnizca cihaz `late` iken ve `_PROBE_MIN_INTERVAL_SEC` (300sn)
siklik siniriyla tetiklenir.

---

---

## 5c. SMART SESSIZLIK DEGISMEZI (1.15.0)

Bir Smart oturumunun **acilis isi** bittikten sonra gateway **uygulama
katmaninda susar**:

| Trafik | Smart | Continuous |
|---|---|---|
| Class 1/2/3 event scan | ❌ KAPALI | ✅ `DNP3_EVENT_SCAN_INTERVAL_SEC` |
| Class 0+1+2+3 baseline/integrity scan | ❌ KAPALI | ✅ `DNP3_EVENT_BASELINE_INTERVAL_SEC` |
| **Link katmani keepalive** | ❌ **KAPALI** | ✅ varsayilan 60sn |
| G110 string okumasi | ⚠️ oturum basina **1 deneme** | ✅ 6 denemeye kadar backoff |
| Acilis integrity poll | ⚠️ tek atislik (kutuphane sinirli) | ✅ |
| Outstation unsolicited/olay yanitlari | ✅ normal kabul edilir | ✅ |

### Link keepalive — 2026-08-20 saha dersi

> **OLCULDU (yadnp3 3.2.1.1):** `cfg.link.KeepAliveTimeout` varsayilani
> **60000 ms** ve 1.14.0'a kadar gateway onu **hic ayarlamiyordu**. Tamamen
> sessiz bir master bile **60. saniyede** 10 baytlik bir `LINK_STATUS`
> gonderiyor.

Horstmann dokumantasyonu hareketsizlik sayacinin **her TCP/DNP trafigiyle**
sifirlandigini soyler. 60 saniyede bir keepalive, 600 saniyelik oturum
sayacini **sonsuza kadar** sifirlar — yani **taramalar kapatilsa bile modem
hicbir zaman uyuyamaz**.

Sahadaki 2026-08-20 yakalamasi yalnizca ~17 saniyelikti ve bu cerceveyi
**gormedi**; konfigurasyon duzeltilip taramalar sustuktan **sonra** isiracak
olan hata buydu.

1.15.0'da `smart` cihazlarda `TimeDuration.Max()` ile devre disi birakilir.
**`continuous`ta dokunulmaz** — orada olu link tespiti icin degerlidir ve
zaten 5 saniyede bir tarama gittigi icin hicbir zaman tetiklenmez.

### G110 string okumasi

Ayni fonksiyondaki diger tum yoklamalar `smart` kapisiyla korunuyordu; G110
yolu **korumasizdi**. Cihaz G110 dondurmezse 15/30/60/120/240 sn backoff ile
**6 `ScanRange`** gider — sessizlik penceresinin tam ortasina dusen
**tekrarlayan** uygulama istekleri.

1.15.0: `smart`ta **oturum basina tek deneme**. Seri no / IMEI / firmware
bilgisi degerlidir ve tek atislik acilis isi sozlesmece serbesttir; cihaz
cevap vermezse bir daha sorulmaz. **`continuous`ta backoff aynen korunur.**

### Acilis integrity poll — kutuphane siniri

`AssignClassDuringStartup=False` acilis **"assign class"** gorevini kapatir
ama opendnp3'un **acilis integrity poll'unu kapatmaz**: yadnp3 binding'i
`MasterParams.startupIntegrityClassMask` alanini **sunmuyor**.

Bu **tek atisliktir** (cihaz cevap verince tekrarlanmaz) ve 600 saniyelik
sayaci **engellemez**, dolayisiyla sozlesme geregi serbest birakilmistir.

---

## 2c. GOZLEM ile EYLEM AYRIDIR (1.15.0)

`Operation Mode` **her cihazda** okunur ve `/health` uzerinden raporlanir.
**Etkin politika ise yalnizca `configured_session_policy=auto` iken
degistirilir.**

> **Yapilandirilan politika her zaman otoriterdir.** Gateway, Operation Mode
> `smart` gorunce `continuous` bir cihazi **asla** susturmaz. Oyle yapsaydi
> Grid'in acik niyeti sessizce ezilirdi.

Once gozlem ve eylem **tek bir erken donuse** bagliydi:

```python
if mm.configured_session_policy != "auto":
    return False          # <-- modu OKUMA kodu da bunun ARDINDAYDI
```

Sonucu sinsiydi: `continuous` yapilandirilmis bir cihaz mod noktasini
durustce raporlasa bile gateway onu **hic yorumlamiyordu** ve
`operation_mode="unknown"` kaliyordu. Yani **"panel SMART diyor ama gateway
continuous kosuyor"** uyusmazligi gateway'in **hicbir yuzeyinde** (log,
`/health`, saglik basligi) gorunmuyordu — 2026-08-20'de teshis icin
**tcpdump acmak** gerekti.

Artik uyusmazlik kenar-tetikli bir WARNING uretir:

```
device_policy_mismatch device=SN2-1 observed_operation_mode=smart
  configured_policy=continuous effective_policy=continuous periodic_scans=true
```

**Politika degistirilmez**; operatore backend'de `session_policy` duzeltmesi
soylenir (bkz. [BACKEND_TODO.md](./BACKEND_TODO.md#b5)).

### Saha teshis komutu (tcpdump GEREKMEZ)

```bash
docker logs eg-gw-<kod> 2>&1 | grep -E "yadnp3_master_enabled|device_policy_mismatch|auto_policy_fallback|yadnp3_periodic_scans_enabled"
```

`yadnp3_master_enabled` satiri artik **acikca** basar:

```
device=SN2-1 mode=listening(client) endpoint=10.0.0.5:20001 ...
ip_endpoint_type=listening configured_policy=continuous effective_policy=continuous
operation_mode=unknown periodic_scans=true event_scan=5s baseline_scan=30s
```

`periodic_scans=true` gorulduyse cihaz **5 saniyede bir** Class 1/2/3 istegi
gonderiyor demektir ve Horstmann'in hareketsizlik sayaci **asla dolamaz**.

---

## 6. Yapilandirma anahtarlari

| Anahtar | Yer | Varsayilan | Aciklama |
|---|---|---|---|
| `session_policy` | backend, cihaz basina | `continuous` | `continuous` \| `smart` \| `auto` |
| `ip_endpoint_type` | backend, cihaz basina | `listening` | `listening` \| `initiating`. **Politikadan BAGIMSIZ** (1.14.0) — alti kombinasyon da gecerli |
| `master_ip_port` | backend, cihaz basina | — | `initiating` icin **ZORUNLU**, 1024..65535, gateway icinde TEKIL |
| `smart_max_silence_sec` | backend, cihaz basina | `null` (= ezme yok) | Sessizlik esigi, **60..2592000** sn. Gecersizse yok sayilir, env yedegine dusulur. |
| `dial_in_interval_min` | backend, cihaz basina | `null` (= `late` KAPALI) | Beklenen zamanlanmis rapor araligi, **60..1440** dk. `late` erken uyarisini besler; `comm_lost` URETMEZ. |
| `smart_listen_reconnect_max_sec` | backend, cihaz basina | `null` (= kutuphane varsayilani) | `listening` kanalda yeniden baglanma TAVANI, **5..600** sn. Cihazin uyanisinin en gec ne kadar sonra fark edilecegini belirler. |
| `DNP3_SMART_MAX_SILENCE_SEC` | gateway `.env` | `0` (kapali) | Cihaz ezmesi yokken kullanilan yedek. Kabul: **`0` veya `60..2592000`**; `1..59` boot'ta REDDEDILIR. |

Politikanin kendisi icin **env anahtari yoktur**: karar cihaz basinadir ve
kurulum geneli bir bayrakla verilemez.

---

## 7. `/health` ciktisi

Cihaz basina (`device_health()`):

```json
{
  "SN2-001": {
    "state": "smart_idle",
    "session_policy": "smart",
    "connected": false,
    "reachable": false,
    "last_frame_epoch": 1755600000.0,
    "last_valid_contact_epoch": 1755600000.0,
    "evidence_age_sec": null,
    "data_age_sec": 412.0,
    "smart_idle_age_sec": 400.0,
    "smart_max_silence_sec": 93600,
    "smart_silence_deadline_epoch": 1755693600.0,
    "smart_silence_remaining_sec": 93180.0,
    "configured_session_policy": "auto",
    "effective_session_policy": "smart",
    "operation_mode": "smart",
    "operation_mode_raw": 1.0,
    "operation_mode_last_seen_epoch": 1755600000.0,
    "auto_fallback": false,
    "ip_endpoint_type": "initiating",
    "master_ip_port": 20100,
    "listener_expected": true,
    "listener_port": 20100,

    "dial_in_interval_min": 720,
    "next_expected_report_epoch": 1755643200.0,
    "report_overdue_sec": 0.0,
    "report_late": false,

    "ip_probe_status": "unknown",
    "tcp_probe_status": "connecting",
    "last_probe_epoch": null
  }
}
```

`initiating` dinleyicisi acilamayan cihazlar `state="listener_error"` ile
raporlanir (port dolu/ayricalikli). Bu bir **kurulum** arizasidir, haberlesme
arizasi degil — `devices.listener_error` sayacinda ayri tutulur.

Ozet sayimlar (`/health` -> `devices`): `online`, `recovering`, `lost`,
**`smart_idle`**, `smart_lost`, **`late`**, `listener_error`, `unknown`,
`session_policies: {continuous, smart, auto}` ve
`effective_policies: {continuous, smart, unknown}`.

> **`late` TOPLAMA GIRMEZ.** Bir cihaz ayni anda hem `smart_idle` hem
> `late` olabilir; `total` hesabina eklenirse iki kez sayilir ve sahte
> `unknown` uretirdi. `late` bir DURUM degil, mevcut durumun uzerine binen
> bir BAYRAKTIR — `smart_lost` ile de karistirilmamalidir: `smart_lost`
> cihazlar GERCEKTEN kopuktur, `late` cihazlar hala `smart_idle`dir ve
> `comm_lost` URETMEMISTIR.

Sayaclar (`devices.recovery`): `smart_idle_wakeup_total`,
`smart_silence_lost_total`.

Backend'e giden saglik basligi (`X-E1-Gateway-Health`) `devices.smart_idle` ve
`devices.smart_lost` sayaclarini da tasir.

`smart_idle`, backend'e giden saglik basliginda **sorun olarak
raporlanmaz** — aksi halde saglikli uyuyan filo arizali gorunurdu.

---

## 8. Kalicilik (restart davranisi)

Smart cihazlar uykudayken container yeniden baslarsa, kalici kayit olmadan
hepsi bir sonraki raporlarina kadar `comm_lost` gorunurdu.

Kayit dosyasi: `<GATEWAY_STATE_DIR>/session_state_<GATEWAY_CODE>.json`

```json
{
  "version": 1,
  "written_at_unix": 1755600123.4,
  "devices": {
    "SN2-001": {
      "state": "smart_idle",
      "last_valid_contact_unix": 1755600000.0,
      "smart_idle_since_unix": 1755600010.0
    }
  }
}
```

* Atomik yazilir (tmp + `os.replace`), hiz sinirlidir (5 sn), kapanista
  zorla bosaltilir.
* **Surumlenmistir.** Surum uyusmazsa, JSON bozuksa ya da dosya yoksa
  kayitlar **sessizce yok sayilir** — sonuc, ozelligin hic olmadigi
  davranistir (cihaz `lost` ile baslar). Hata **guvenli tarafa** duser.
* Tanimsiz durum tokenleri ve sacma zaman damgalari (gelecek / 2000 oncesi)
  reddedilir.
* Sessizlik esigi coktan asilmissa `smart_idle` **geri yuklenmez**.

---

## 9. Neden periyodik tarama Smart'ta CALISMAMALI

Cihazin hareketsizlik sayaci **15 saniyedir** ve **her DNP3 frame'inde
sifirlanir**. Gateway varsayilan tempoda ~5 saniyede bir Class taramasi
gonderir:

```
0s  scan -> sayac sifirlanir
5s  scan -> sayac sifirlanir
10s scan -> sayac sifirlanir
...  sayac 15 saniyeye ASLA ULASMAZ -> modem HICBIR ZAMAN kapanmaz
```

### Uygulama (yadnp3 3.2.1.1)

Pinlenmis kutuphane bir taramayi **sonradan durdurmayi desteklemiyor**:
`AddClassScan` bir `IMasterScan` dondurur ve o nesnenin tek metodu
`Demand()`'dir. Bu yuzden kontrol "durdurmak" degil **"hic eklememek"**
uzerinden kuruldu. `session_policy` `connection_fingerprint`e dahildir:
politika degisirse **yalnizca o cihazin** master'i yeniden kurulur.

`smart` politikada:

* `AddClassScan` **hic cagrilmaz** (ne event ne integrity),
* `AssignClassDuringStartup` **False** doner — acilista Class 0/integrity
  poll'u yapilmaz (Smart Navigator'da ad-hoc Class 0 zaten yalnizca Boost'ta
  mumkundur),
* yoklama / veri-sessizligi poll'u / zorla relink devre disidir.

Cihaz raporunu **kendisi** (unsolicited) gonderir; gateway yalnizca dinler.

### Olculen kanit

`tests/test_smart_session_loopback.py`, gercek bir DNP3 outstation ile
gateway arasina seffaf bir TCP proxy koyup baytlari sayar:

* `smart` cihaz, baglantidan sonra **20 saniyede 0 bayt** (gateway -> cihaz),
* ayni kosulda `continuous` politikada trafik **devam eder** (kontrol testi).

---

## 10. Bilinen sinir ve sonraki gorev

### Otomatik mod tespiti (AYRI GOREV)

Su an politika elle yapilandirilir. Cihazin `Master Operation Mode`
noktasindan (`G1/15`: `0x01` Boost, `0x81` Smart) otomatik tespit ayri bir
gorevdir. O gorevde dikkat edilmesi gerekenler:

* index profile gore degisir (Pole Master farkli) — semantik kimlik
  kullanilmali, `G1/15` gomulmemeli;
* **Satellite** `Operation Mode` sinyalleri telemetri olarak yayinlanmaya
  devam etmeli ama haberlesme politikasina GIRMEMELI — hucresel oturum
  Master'a aittir;
* `Boost Mode Enabled` (yetenek) mod kaynagi DEGILDIR.

### Komut kuyruklama (AYRI GOREV)

Uyuyan bir cihaza komut gonderilemez; gateway bunu oldugu gibi raporlar
(`reachable=false`, `status="offline"`) ve **sahte basari uretmez**. Komutun
bir sonraki uyanmada calistirilmasi ayri bir gorevdir.

---

## 11. Ornek loglar

Tum gecisler **kenar-tetiklidir**; her poll cycle'inda tekrarlanmaz.

```
# master kurulumu
yadnp3_master_enabled device=SN2-001 mode=initiating(server) endpoint=0.0.0.0:20100 \
  remote=10 local=1 event_scan=-s integrity_scan=-s session_policy=smart

# cihaz uyandi ve geri geldi
smart_idle_wakeup device=SN2-001 idle_duration=3612s — cihaz kendi istegiyle baglandi \
  (ariza raporu ya da zamanlanmis rapor)

# izin verilen sessizlik penceresi asildi -> GERCEKTEN kopuk
smart_idle_silence_exceeded device=SN2-001 ip=10.20.5.11 silence=95210s limit=93600 — \
  cihaz izin verilen sessizlik penceresinde haber vermedi; GERCEKTEN kopuk sayiliyor (comm_lost)

# restart sonrasi
session_state_store_loaded path=/app/.gateway_state/session_state_GW-001.json devices=612 smart_idle=574
smart_idle_restored device=SN2-001 last_contact=2026-08-19T09:14:31Z — \
  kalici kayittan geri yuklendi; sahte comm_lost URETILMEDI
```

---

## 12. Sorun giderme

**"Cihaz Smart ama modem hala kapanmiyor"**
1. `/health` -> `session_policy` degerine bakin. `continuous` ise backend
   cihaz kaydinda `session_policy="smart"` ayarlanmamis demektir.
2. Gateway loglarinda `yadnp3_master_enabled ... session_policy=smart`
   satirini arayin.
3. Cihazin KENDISI Smart Mode'a alinmis mi? Gateway'de Smart secmek cihazi
   Smart yapmaz.

**"Cihaz uyuyor ama SCADA'da offline gorunuyor"**
* `state` `smart_idle` mi yoksa `lost` mu?
* `lost` ise `smart_silence_remaining_sec` sifirlanmistir: cihaz izin verilen
  pencerede haber vermemis. Dial-In programini ve `smart_max_silence_sec`
  degerini karsilastirin.

**"Smart cihaz baglaniyor ama hemen comm_lost oluyor"**
* Cihaz TCP kuruyor ama DNP3 katmani konusmuyor demektir (kanit yok). Bu
  BILINCLI davranistir — kanitsiz oturum `smart_idle`de saklanmaz.
* Link adreslerini (`dnp3_address`, `master_address`) ve cihazin unsolicited
  raporlama ayarini kontrol edin.

**Ozelligi geri almak**
* Cihazin `session_policy` degerini `continuous` yapin. Gateway master'i
  yeniden kurar ve 1.11.x davranisina doner.

---

## 13. Saha kabul proseduru (GERCEK CIHAZ GEREKTIRIR)

> **Tam prosedur, komutlar ve isaretleme sablonu:**
> [FIELD_ACCEPTANCE.md](./FIELD_ACCEPTANCE.md)

Birim ve loopback testleri bunu uretime hazir ilan etmez. Asagidakiler
**gercek bir Smart Navigator 2.0 ile** dogrulanmalidir:

1. Cihazi Smart Mode + Initiating Endpoint'e alin; ariza noktalarinda
   **Wake up Modem** etkin olsun.
2. Backend'de cihaza `session_policy="smart"` verin. Gateway logunda
   `session_policy=smart` gorulmeli.
3. **Ariza tetikleyin.** Beklenen zincir:
   modem ON -> TCP baglanti -> olay gateway'e ulasir -> gateway SUSAR ->
   ~15 sn hareketsizlik -> modem/oturum OFF -> cihaz `smart_idle`.
4. Sessizligi **paket seviyesinde** dogrulayin:
   `tcpdump -i any port <master_ip_port>` — aktarim bittikten sonra
   gateway -> cihaz yonunde **hicbir paket** olmamali.
5. `/health` -> `state=smart_idle`, `connected=false`, `reachable=false`;
   SCADA'da **comm_lost YOK**.
6. Gateway'i yeniden baslatin: `smart_idle_restored` logu gorulmeli, sahte
   comm_lost olmamali.
7. `continuous` bir cihazin ayni gateway'de surekli bagli kaldigini ve normal
   taranmaya devam ettigini dogrulayin.
8. Uyuyan cihaza komut gonderin: `status="offline"` donmeli, sahte basari
   olmamali.

> **En kritik dogrulama — uyanma sonrasi olay teslimi.** Simule outstation,
> pinlenmis wheel `AnalogConfig`i Python'a acmadigi icin noktalarini olay
> sinifina atayamaz ve bu yuzden uyanma aninda kendiliginden rapor
> GONDEREMEZ. Sahada Horstmann tam olarak bunu yapar. Bu yol **yalnizca
> gercek cihazla** dogrulanabilir (adim 3). Basarisizlik modu GUVENLIDIR:
> kanit gelmezse cihaz `smart_idle`de saklanmaz, `recovering -> lost` olur ve
> comm_lost GORUNUR — sessiz kalmaz.
