# Grid entegrasyonu — cihaz basina calisma-zamani sagligi (`device_health_v1`)

Gateway **1.15.0**'dan itibaren cihaz basina calisma-zamani sagligini
backend'e **kendisi POST eder**. Bu dokuman backend tarafinin ihtiyac
duydugu **her seyi** icerir; gateway kaynak koduna bakmak GEREKMEZ.

> **Gateway 1.14.0:** Smart Listening calisir, ama bu tasiyici **YOKTUR**.
> **Gateway 1.15.0:** Smart Listening calisir **ve** bu tasiyici vardir.
>
> Yetenek tespiti: deployment sozlesmesinde
> `device_runtime_health_transport.supported == true` ve
> `min_gateway_version == "1.15.0"`.

---

## 1. Neden ayri bir uc

Mevcut `X-E1-Gateway-Health` **basligi** `/pending` isteklerine biner ve
backend ayristirma tavani ~2 KB'dir. 200+ cihazin cihaz-bazli durumu oraya
**sigmaz**.

> **Baslik BUYUTULMEMELIDIR.** `/pending` fiziksel kesici komutlarinin
> tasiyicisidir; bir proxy/backend baslik limitinde 400 donerse **komutlar
> durur**. Toplu baslik oldugu gibi kalir ve bu yeni kanal onu rahatlatir.

---

## 2. HTTP

| | |
|---|---|
| Metod | `POST` |
| Yol | `/gateways/{gateway_code}/device-health` |
| Content-Type | `application/json` |
| Yon | **Giden** (gateway → backend). NAT/ozel ag sorunu yok. |

### Kimlik dogrulama

Mevcut **kanonik gateway credential**'i. Yeni bir sistem **yok**:

| Baslik | Icerik |
|---|---|
| `X-Gateway-Token` | `GATEWAY_TOKEN` |
| `X-Gateway-Code` | gateway kodu — **yol ile ayni olmali** |
| `X-Gateway-Instance-Id` | kalici instance kimligi |
| `X-Request-Id` | UUID (korelasyon) |
| `User-Agent` / `X-Gateway-Client` | surum bilgisi |

> **`X-Gateway-Command-Token` BILEREK GONDERILMEZ.** Saglik telemetrisi
> komut yetkisi gerektirmez; o sirri bu uca da yaymak F5'te ayrilan iki
> duzlemi yeniden birlestirirdi. Backend bu ucta komut token'i **beklememeli**.

### Yanit

| Durum | Gateway davranisi |
|---|---|
| `2xx` | basarili; parti "gonderildi" isaretlenir |
| `4xx` / `5xx` | gecici sayilir, sinirli geri cekilmeyle yeniden denenir |
| ag hatasi | ayni |

Govde **okunmaz**; yalnizca durum kodu onemlidir. Backend bos govde donebilir.

---

## 3. Zarf (envelope)

```json
{
  "schema": "device_health_v1",
  "gateway_code": "GW-001",
  "gateway_instance_id": "3f2c1a9e-...",
  "boot_id": 12,
  "sequence": 34,
  "snapshot": true,
  "snapshot_id": "12-3",
  "snapshot_batch_index": 0,
  "snapshot_batch_count": 4,
  "device_total": 200,
  "devices": [ /* bkz. bolum 4 */ ]
}
```

| Alan | Tip | Anlam |
|---|---|---|
| `schema` | string | Sabit `"device_health_v1"`. Farkli bir deger gelirse **reddedin**. |
| `gateway_code` | string | Yoldaki kod ile ayni olmali. |
| `gateway_instance_id` | string | **Kalici** kimlik — restart'ta **degismez**. |
| `boot_id` | int ≥ 1 | Her proses baslangicinda **artar**. |
| `sequence` | int ≥ 1 | Proses ici **monotonik** sayac. |
| `snapshot` | bool | `true` = tam durum, `false` = yalnizca degisenler. |
| `snapshot_id` | string \| null | Ayni tam snapshot'in **tum** partilerinde ayni. Delta'da `null`. |
| `snapshot_batch_index` | int \| null | **0 tabanli** parti sirasi (`0..count-1`). Delta'da `null`. |
| `snapshot_batch_count` | int \| null | Bu snapshot'in toplam parti sayisi. Delta'da `null`. |
| `device_total` | int | Gateway'in tanidigi **toplam** cihaz sayisi. |
| `devices` | array | En fazla `batch_max` (varsayilan 50) kayit. |

---

## 4. Cihaz kaydi

```json
{
  "device_code": "SN2-001",
  "connection_state": "smart_idle",
  "connected": false,
  "reachable": false,

  "configured_session_policy": "auto",
  "effective_session_policy": "smart",
  "operation_mode": "smart",

  "dial_in_interval_min": 720,
  "next_expected_report_epoch": 1755643200.0,
  "report_overdue_sec": 0.0,
  "report_late": false,

  "last_valid_contact_epoch": 1755600000.0,
  "last_frame_epoch": 1755600000.0,

  "ip_probe_status": "unknown",
  "tcp_probe_status": "connecting",
  "last_probe_epoch": null,

  "ip_endpoint_type": "listening"
}
```

| Alan | Tip | Degerler / not |
|---|---|---|
| `device_code` | string | Backend'deki cihaz kodu. |
| `connection_state` | enum | `online` \| `smart_idle` \| `recovering` \| `lost` \| `listener_error` \| `unknown` |
| `connected` | bool | TCP link acik mi. |
| `reachable` | bool | Komut gonderilebilir mi (uyuyan cihaz `false`). |
| `configured_session_policy` | enum | `continuous` \| `smart` \| `auto` |
| `effective_session_policy` | enum | `continuous` \| `smart` \| `unknown` (`auto` henuz cozulmedi) |
| `operation_mode` | enum | `smart` \| `boost` \| `unknown` |
| `dial_in_interval_min` | int \| null | Beklenen rapor araligi (dk). `null` = tanimsiz. |
| `next_expected_report_epoch` | float \| null | Unix epoch (saniye, UTC). |
| `report_overdue_sec` | float \| null | Gecikme; `0.0` = gecikme yok. |
| `report_late` | bool | **Uyari bayragi** — durum degil. |
| `last_valid_contact_epoch` | float \| null | Son **gecerli DNP3 kaniti**. |
| `last_frame_epoch` | float \| null | Son frame. |
| `ip_probe_status` | enum | `reachable` \| `unreachable` \| `unsupported` \| `unknown` |
| `tcp_probe_status` | enum | `open` \| `connecting` \| `unknown` |
| `last_probe_epoch` | float \| null | Son tanilama ani. |
| `ip_endpoint_type` | enum | `listening` \| `initiating` |

`null` epoch **"hic olmadi"** demektir — `0` gonderilmez (panelde 1970
tarihleri cikmasin diye).

---

## 5. Semantik — **karistirilmamasi gerekenler**

### `smart_idle` ≠ offline

Horstmann Smart modda modemini **kapatir**. Bu **saglikli** bir durumdur.
`lost` ile ayni kovaya konursa saglikli uyuyan filo SCADA'da arizali gorunur.

### `report_late` ≠ `lost`

```
beklenen rapordan ONCE           -> smart_idle,  SAGLIKLI
rapor gecti, max_silence dolmadi -> report_late=true, DEGRADED, state HALA smart_idle
max_silence asildi               -> lost
```

`report_late` bir **durum degil, bayraktir**; `connection_state` degismez.

> **Alarm degil, uyari olarak ele alin.** Dial-In gecikmesi cok sik iyi
> huyludur (hucresel tikaniklik, rapor saatinde kucuk kayma). `lost`
> sayilirsa gunluk sahte alarm uretir.

### Sonda sonuclari durum **belirlemez**

`ip_probe_status` / `tcp_probe_status` **salt teshistir**.

> `ip_probe_status = "unreachable"` gormek **normaldir**: ICMP saha
> aglarinda/APN'lerde sikca engellidir ve Smart bir modem **mesru olarak**
> uykudadir. "ping dusuyor → cihaz oldu" kurali filonun yarisini sahte kopuk
> gosterir. Baglanti karari **yalnizca** `connection_state`indir.

### Operation Mode

`1 = Smart`, `0 = Boost`. Gateway bunu zaten cozup token'a cevirir.

* **Satellite** Operation Mode: **yok sayilir**, bu kanalda **gonderilmez**.
* **Boost Mode Enabled**: bir **yetenek** (konfigurasyon), calisma-zamani
  durumu **degildir**; bu kanalda **gonderilmez** ve siniflandirmaya girmez.

---

## 6. Sequence / snapshot semantigi

### Bayat yazma korumasi

Backend her gateway icin son gorulen `(boot_id, sequence)` ikilisini tutmali
ve **leksikografik** karsilastirmali:

```
gelen (boot_id, sequence) <= saklanan  ->  YOK SAY (bayat yeniden gonderim)
gelen (boot_id, sequence) >  saklanan  ->  UYGULA ve sakla
```

> **`gateway_instance_id` tek basina YETMEZ.** O kimlik gateway diskinde
> **kalicidir** ve restart'ta **ayni kalir**. Restart sonrasi `sequence`
> 1'den baslar; yalnizca instance kimligine bakan bir backend, yeni
> calismanin `sequence=1` partisini "eski" sanip **atardi**.
>
> `boot_id` her acilista arttigi icin eski calismanin `sequence=9999`
> partisi bile yeni calismanin `sequence=1` partisinden **kucuktur**.

> **Duvar saati kullanilmaz.** Sahada RTC'si bos acilan gateway'ler ve NTP
> siçramalari gercektir; saate bagli siralama tam da o anlarda tersine doner.

### Snapshot ve delta

| `snapshot` | Anlam | Backend davranisi |
|---|---|---|
| `true` | Bu parti **tam durumun bir parcasi** | Partideki cihazlari o anki gercek kabul et |
| `false` | Yalnizca **degisen** cihazlar | Yalnizca gelenleri guncelle |

* Acilista **her zaman** bir snapshot gelir.
* Durum degisiminde delta gelir (varsayilan 2sn toplama penceresi).
  Gateway her poll cycle'indan **sonra** yayinciyi uyarir, dolayisiyla
  gecikme **~poll araligi + debounce**tir (yayincinin 30sn'lik yedek
  uyanmasi **degil**).
* **Cihaz seti degisince** (config yenilemesi) otomatik olarak **tam
  snapshot** gonderilir — delta silinen bir cihazi **tasimaz**.
* Varsayilan **300 saniyede bir** uzlastirma snapshot'i gelir — delta
  kaybolsa bile backend en gec o surede gercekle hizalanir.

### Cok parcali snapshot — **`device_total` tek basina YETMEZ**

Bir snapshot birden fazla partiye bolunur ve hepsi `snapshot=true` tasir.
Partileri **`snapshot_id`** ile eslestirin:

```
snapshot_id = "12-3"   batch_index=0  batch_count=4   -> BASARILI
snapshot_id = "12-3"   batch_index=1  batch_count=4   -> BASARISIZ
    (gateway'de cihaz seti degisir; device_total YINE 200)
snapshot_id = "12-4"   batch_index=0  batch_count=4   -> yeni snapshot basladi
```

> **Neden `device_total` yetmez:** iki turda da `200`dur. Yalnizca ona bakan
> bir backend yarim kalan **eski** snapshot ile yenisini **ayirt edemez**;
> ikisini birlestirip **tutarsiz** bir tablo kurar — ya da "eksik kalanlari
> sil" mantigi varsa **var olan cihazlari siler**.

**Backend kurallari:**

1. Ayni `snapshot_id`li partileri **birlestir**.
2. Yeni bir `snapshot_id` baslayinca **tamamlanmamis eskisini at**.
3. Silinen cihazlari **yalnizca** `snapshot_batch_count` kadar parti
   geldikten **sonra** uzlastir.

`snapshot_id` formati `{boot_id}-{artan_sayac}` — **saatten bagimsiz**.
Kismi bir basarisizliktan sonraki yeniden deneme **her zaman yeni bir
`snapshot_id`** uretir: veri `health_source`tan yeniden okunur ve bu arada
cihaz seti degismis olabilir.

`(boot_id, sequence)` **istek basina** bayat siralama icin aynen kalir;
snapshot korelasyonu onun **yerine gecmez**, uzerine eklenir.

### Cihaz silme

Gateway ayrica **"cihaz silindi"** mesaji **gondermez**. Config'ten cikan bir
cihaz sonraki snapshot'ta **bulunmaz**; backend uzlastirmayi buradan yapar.

---

## 7. Yeniden deneme ve backpressure

| | |
|---|---|
| Geri cekilme | ustel, **2sn → 120sn tavan**, ±%20 jitter |
| Parti boyu | varsayilan **50** cihaz (`1..500`) |
| Bellek | cihaz basina **en son durum** (coalescing) — gecis basina **degil** |
| Disk | **hicbir sey yazilmaz** |

> Backend uzun sure erisilemezse gateway **ara gecisleri dusurur** ve yalnizca
> en son durumu saklar. Bu kanal **komut/olay denetim gecmisi degildir**;
> backend'in ihtiyaci "cihaz **su an** ne durumda"dir.

Jitter **onemlidir**: 50 gateway ayni backend'e baglidir ve senkron yeniden
denemeler backend geri geldigi anda ikinci bir yuk dalgasi uretir.

---

## 8. Izolasyon garantileri

Basarisiz bir saglik teslimi **hicbirini** etkilemez:

* DNP3 okuma dongusu
* komut duzlemi (`/pending`, CROB, SELECT/OPERATE, CommandLedger)
* telemetri yayini
* `X-E1-Gateway-Health` toplu basligi

Yayinci **kendi thread'inde** kosar; poll yolundan yapilan cagrilar
**bloklamaz**.

---

## 9. Gateway yapilandirmasi

| Env | Varsayilan | Aciklama |
|---|---|---|
| `DEVICE_HEALTH_PUBLISH_ENABLED` | `false` | **Kanali acar.** Grid ucu hazir olana kadar kapali. |
| `DEVICE_HEALTH_BATCH_MAX` | `50` | Parti basina azami cihaz (`1..500`). |
| `DEVICE_HEALTH_SNAPSHOT_INTERVAL_SEC` | `300` | Uzlastirma araligi (`30..86400`). |
| `DEVICE_HEALTH_CHANGE_DEBOUNCE_SEC` | `2.0` | Degisiklik toplama penceresi (`0..60`). |

> **Varsayilan kapali olmasi bilinclidir.** Backend ucu tanimadan acilirsa
> her turda 404 alinir ve log dolar. Kapaliyken **hicbir thread baslatilmaz**.

### Nasil acilir — compose **duzenlenmeden**

Render edilmis compose bu degiskenleri `${VAR:-varsayilan}` ile gecirir:

```yaml
DEVICE_HEALTH_PUBLISH_ENABLED: "${DEVICE_HEALTH_PUBLISH_ENABLED:-false}"
```

Yani compose dosyasinin **yanindaki `.env`e** yazmak yeterlidir:

```bash
echo "DEVICE_HEALTH_PUBLISH_ENABLED=true" >> .env
docker compose -f gw-001.yml up -d
```

> Render edilmis dosyayi **elle duzenlemeyin**: bir sonraki render'da
> sessizce geri alinir.

Grid renderer'i isterse varsayilani acik da uretebilir:
`render_compose.py --device-health-enabled`.

**Devreye alma sirasi:** once backend ucu yayina alin, sonra gateway'lerde
bayragi acin.

---

## 10. Backend uygulama kontrol listesi

- [ ] `POST /gateways/{code}/device-health` ucu, kanonik gateway auth ile
- [ ] `X-Gateway-Code` ile yol parametresi uyumu (defans derinligi)
- [ ] `schema != "device_health_v1"` → reddet
- [ ] `(boot_id, sequence)` sakla; **eski/esit** olani **yok say**
- [ ] `snapshot=true` partilerini **`snapshot_id`** ile birlestir
- [ ] Yeni `snapshot_id` basladiginda **tamamlanmamis eskisini at**
- [ ] Silinen cihazlari **yalnizca tam snapshot** (`snapshot_batch_count`
      kadar parti) geldikten sonra uzlastir
- [ ] `smart_idle` ve `report_late` icin **ayri** gosterim — `lost` **degil**
- [ ] Sonda alanlarini **teshis** olarak goster, durum olarak **degil**
- [ ] Bilinmeyen alanlari **yok say** (ileri uyumluluk: alan eklemek geriye
      uyumludur)
- [ ] Uca gelen istekleri komut duzleminden **ayri** oranlamaya/izlemeye al
