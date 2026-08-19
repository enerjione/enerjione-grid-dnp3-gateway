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
DeviceConfig.session_policy = "continuous"   (VARSAYILAN)  |  "smart"
```

* **`continuous`** — bugunku davranis, birebir. Periyodik Class 1/2/3 event
  scan + Class 0 integrity scan + baglantida acilis integrity poll'u. Her
  zaman bagli DNP3 ekipmani icin dogru olan budur.
* **`smart`** — hicbir tekrarlayan tarama gorevi kurulmaz, acilis integrity
  poll'u yapilmaz, beklenen kapanma `comm_lost` uretmez.

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

Esik cihaz basina cozulur:

1. `DeviceConfig.smart_max_silence_sec` (backend, cihaz bazli),
2. `DNP3_SMART_MAX_SILENCE_SEC` (kurulum geneli yedek),
3. yoksa **denetim KAPALI**.

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

## 6. Yapilandirma anahtarlari

| Anahtar | Yer | Varsayilan | Aciklama |
|---|---|---|---|
| `session_policy` | backend, cihaz basina | `continuous` | `continuous` \| `smart` |
| `smart_max_silence_sec` | backend, cihaz basina | `null` | Sessizlik esigi (60..2592000 sn) |
| `DNP3_SMART_MAX_SILENCE_SEC` | gateway `.env` | `0` (kapali) | Cihaz bazli deger yoksa yedek |

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
    "smart_silence_remaining_sec": 93180.0
  }
}
```

Ozet sayimlar (`/health` -> `devices`): `online`, `recovering`, `lost`,
**`smart_idle`**, `smart_lost`, `unknown` ve
`session_policies: {continuous, smart}`.

Sayaclar (`devices.recovery`): `smart_idle_wakeup_total`,
`smart_silence_lost_total`.

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
