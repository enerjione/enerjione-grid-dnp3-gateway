# Backend Koordinasyonu Gereken Isler

**Durum:** B2 ACIK; B1 tek env bayragina indi; B3 ve B4 kapandi
**Olusturulma:** 2026-07-31 (production hardening calismasi)
**Son guncelleme:** 2026-08-02
**Neden ertelendi:** Bu maddeler gateway'i tek basina degistirerek cozulemez;
backend'in `/gateways/{code}/config`, `/gateways/{code}/pending` ve
`/telemetry/gateway/{code}` sozlesmelerini de degistirir. Gateway tarafindaki
diger tum production riskleri kapatildi (bkz. `CHANGELOG.md` 0.5.0).

> Bu dosya, gateway ve backend ayni anda deploy edilecegi zaman acilacak.
> Her maddede **kim once deploy edilmeli** bilgisi var — sira yanlis olursa
> saha sessizce bozulur.

---

## B1. DNP3 kalite bayraklari yayina eklenecek — ⚙️ TEK ENV BAYRAGI KALDI

> **2026-08-14 guncellemesi — bayragi acmadan once kapatilan IKINCI blocker.**
> Esleme fonksiyonu **tip-kordu**: bayrak byte'inin 5/6/7. bitleri object
> group'a gore farkli anlam tasidigi halde tek bir tabloyla okunuyordu.
> Kutuphaneyle dogrulandi — G3 double-bit'te `0x40/0x80` KESICI POZISYONUDUR
> (DETERMINED_OFF -> `0x41`), ama esleme onu `REFERENCE_ERR` sanip **her ACIK
> kesiciyi** `quality=invalid` yayinlayacakti. Ayni sekilde G1'de `0x20`
> CHATTER_FILTER, G20/G21'de ROLLOVER/DISCONTINUITY'dir; hicbiri OVER_RANGE
> degildir.
>
> `map_dnp3_quality(flags, object_group)` artik tipe gore okuyor ve bit
> tablosu gercek opendnp3 enum'larina karsi pinlendi. Ayrica bayrak
> yoklugu tipe gore degerlendiriliyor: G110 (OctetString) kalite byte'i
> TASIMAZ -> `good`; kalite tasimasi gereken bir gruptan bayraksiz olcum
> gelirse fail-safe `invalid` + WARNING log.
>
> **Yayinlanan sozluk DEGISMEDI** (`good | invalid | restart | forced |
> comm_lost`) ve `dnp3_flags` ham byte'i aynen gonderilmeye devam ediyor —
> backend sozlesmesi etkilenmedi.

**Backend durumu:** ✅ HAZIR — ve dusunuldugunden cok once. `invalid`,
`restart`, `forced` token'lari **v2.28.0**'dan beri taniniyor; dahasi
backend NOKTA/CIHAZ kapsam ayrimini da yapiyor
(`map_quality_to_status_scoped`, `_POINT_LEVEL_QUALITIES`,
`ALARM_BLOCKING_QUALITIES`). Bu maddenin "backend'de yapilacak" kismi
YAZILDIGINDA ZATEN YAPILMISTI.

**Gateway'de eksik olan neydi:** `poller.build_telemetry_payload` govdeye
`dnp3_flags` KOYMUYORDU. Adapter bayragi okuyup `SignalReading`de tasiyor,
govde onu dusuruyordu.

Bu, bayragi acmayi TEHLIKELI yapiyordu: backend kalitenin nokta mi cihaz
seviyesinde mi oldugunu `dnp3_flags`in VARLIGINDAN anliyor. Alan gitmeyince
tek bir noktanin `REFERENCE_ERR`i CIHAZ seviyesi sayilip TUM CIHAZI OFFLINE
yapardi — harita kirmizi, "son veri" sayaci donar.

Iki depo birlikte kosturularak dogrulandi:

| senaryo | kalite | cihaz (`dnp3_flags` VAR) | cihaz (YOKKEN) |
|---|---|---|---|
| CT referans hatasi | `invalid` | **online** ✅ | offline ❌ |
| operator zorlamis | `forced` | online ✅ | online |
| cihaz reboot etti | `restart` | **online** ✅ | offline ❌ |
| link koptu | `comm_lost` | **offline** ✅ | offline ✅ |

Dordunde de alarm degerlendirmesi BLOKE — olcume guvenilmiyor, alarm durumu
donuyor. `comm_lost` her iki kapsamda da cihaz seviyesi kaliyor (dogru).

**KALAN TEK IS:** `DNP3_PUBLISH_QUALITY_FLAGS=true`. Backend yillardir hazir,
govde dogru, esleme artik tipe gore okuyor.

**Ama saha genelinde HEMEN acilmayacak.** Karar (2026-08-14): once TEK bir
test gateway'inde acilip gercek Horstmann verisiyle su tablo dogrulanacak,
sonra kademeli yayilim yapilacak.

| senaryo | beklenen `quality` |
|---|---|
| normal analog | `good` |
| kesici ACIK (G3 DETERMINED_OFF) | `good` |
| kesici KAPALI (G3 DETERMINED_ON) | `good` |
| cihaz reboot | `restart` |
| operator zorlamis nokta | `forced` |
| CT / referans hatasi | `invalid` |
| haberlesme kopmasi | `comm_lost` |

<details><summary>Ozgun kayit</summary>

**Gateway durumu:** ✅ Adapter kalite bayraklarini artik OKUYOR ve tasiyor
(`SignalReading.dnp3_flags`), ancak yayinlanan `quality` alani geriye uyum icin
hala eski sozlugu kullaniyor.

**Sorun:** Gateway bugun yalnizca `good | no_change | comm_lost` uretiyor.
Outstation bir noktayi `ONLINE=0` (gecersiz), `RESTART`, `LOCAL_FORCED`
(operator elle zorlamis), `OVER_RANGE` veya `REFERENCE_ERR` ile raporladiginda
bu bilgi SCADA'ya **`quality: "good"`** olarak gidiyor.

Somut senaryo: outstation CT referansini kaybediyor ve analog noktayi
`value=0.0, flags=ONLINE|REFERENCE_ERR` olarak raporluyor. SCADA hat akimini
0 A olarak kabul ediyor → "hat enerjisiz" yorumu, yanlis alarm bastirma veya
yanlis manevra karari.

**Backend'de yapilacak:**
- Tag-engine `quality` sozlugune yeni degerleri ekleyecek:
  `invalid` (ONLINE bayragi yok / OVER_RANGE / REFERENCE_ERR),
  `restart` (RESTART), `forced` (LOCAL_FORCED / REMOTE_FORCED)
- Bu kaliteler icin davranis karari: alarm degerlendirmesine girsin mi,
  trend grafiginde nasil gosterilsin, historian'a nasil yazilsin
- Opsiyonel: ham `dnp3_flags` byte'i teshis icin saklansin

**Gateway'de yapilacak (backend hazir olunca):**
- `dnp3_yadnp3_master.read_device` icinde `_map_dnp3_quality()` cagrisini aktif et
  (fonksiyon yazildi, su an `DNP3_PUBLISH_QUALITY_FLAGS=false` ile kapali)

**Deploy sirasi:** ONCE BACKEND. Gateway yeni kalite degerlerini gonderdiginde
backend bunlari tanimiyorsa olcumleri reddedebilir veya yanlis isleyebilir.

**Gecis kolayligi:** `DNP3_PUBLISH_QUALITY_FLAGS` env bayragi eklendi
(default `false`). Backend hazir olunca saha genelinde tek tek acilabilir.

</details>

---

## B2. Cihazin DNP3 olay zaman damgasi — ⚙️ GATEWAY TARAFI BITTI, BACKEND BEKLIYOR

**Gateway durumu:** ✅ Cihaz damgasi artik yayinlaniyor. Govdede iki YENI alan
var (bkz. `poller.build_telemetry_payload`):

| Alan | Icerik |
| --- | --- |
| `device_event_at` | Cihazin KENDI olay damgasi (ISO-8601, UTC). Damga yoksa ya da makul araligin disindaysa `null`. |
| `timestamp_quality` | `synchronized` / `unsynchronized` / `invalid` / `null` |

**`source_timestamp`'in anlami DEGISMEDI** — hala gateway saatidir. Bu bilincli:
o alan backend'de historian birincil anahtarinin parcasi, TimescaleDB partition
kolonu ve retention silme kriteri. Anlamini "cihaz zamani" yapmak gecmise
damgali satirlarla INSERT'i patlatir, ileriye damgali satirlari ise
retention'a gorunmez kilardi (bkz. backend migration 0025). Yani ozgun plandaki
"`source_timestamp` anlamini degistir" maddesi **uygulanmadi ve uygulanmamali**;
yerine ayri bir alan eklendi.

**Cihaz saatine GUVENILMIYOR.** RTC pili biten bir gosterge 2000-01-01
damgalar; makul araligin disindaki damga adapter'da DUSURULUR ve
`timestamp_quality="invalid"` olur. **Olcumun kendisi her zaman yayinlanir** —
bozuk saat veriyi degil, yalnizca damgayi kaybettirir.

**Backend'de KALAN is:**
- Telemetri semasina `device_event_at` + `timestamp_quality` alanlarini ekle
  (su an gonderiliyor ama backend bunlari yok sayiyor)
- SOE / ariza analizi sorgularinda hangi alanin kullanilacagina karar ver:
  `device_event_at` doluysa o, degilse `source_timestamp`
- `timestamp_quality != "synchronized"` olan damgalarin arayuzde nasil
  isaretlenecegi (kullaniciya "bu zaman supheli" demek gerekir)

**Deploy sirasi:** SERBEST. Gateway alanlari zaten gonderiyor; eski backend
bunlari yok sayar (kirilma YOK). Backend hazir oldugunda veri otomatik anlam
kazanir.

**Onkosul zinciri KAPANDI:** zaman senkronizasyonu (`timeSyncMode`) eklendi,
yani outstation saatleri artik gateway tarafindan yaziliyor. Bu olmadan B2
"hepsi ayni yanlis saat" durumunu "hepsi FARKLI yanlis saat" haline getirirdi.
Ayrica `ClockGuard` gateway saati supheliyken saat yazmayi askiya alir —
yanlis saatli bir gateway 300 cihazin saatini birden bozamaz.

---

## B3. Per-device sinyal katalogu — ✅ GATEWAY TARAFI KAPANDI

**Durum:** (b) secenegi uygulandi. Gateway artik sinyal setini **cihaz basina**
secer (`state.signals_for(device)`), tek global liste kullanmaz.

Cozumleme sirasi:

```
1. backend `signals_by_profile[device.signal_profile]` (dolu)  -> KAZANIR
2. yerlesik profil `profiles/<model>.json`                     -> backend bos/yok ise
3. duz `signals` listesi                                       -> profil kavrami hic yoksa
```

Ozgun kayitta "gateway tarafinda cozulemez" denmisti; yanilmisiz. Iki ek
karar bunu mumkun kildi:

- **Yerlesik profiller (2. adim).** Bir DNP3 modelinin adres haritasi
  FIRMWARE'in ozelligidir — her kurulumda aynidir. Protokol surucusu zaten
  gateway'de oldugu icin haritanin dogal yeri de orasi. Bilinen bir model,
  backend katalogu bos olsa bile dogru yoklanir. Otorite yine **backend**:
  kurulumcu sahada yanlis bir index'i arayuzden duzeltebilmeli, yoksa tek bir
  adres hatasi icin yeni gateway imaji cikarmak gerekirdi.
- **Bos liste, yanlis listeden iyidir.** Profil bulunamaz ve yerlesik harita
  da yoksa cihaz yoklanmaz. Duz listeye dusmek komsu modelin adreslerini
  yoklamak olurdu; okunan deger yanlis `signal_key` ile yayinlanir ve alarm
  esigi sahte bir buyukluk uzerinden calisirdi. Sessiz yanlis veri, gorunur
  eksik veriden daha kotudur.

**Backend'de KALAN is (opsiyonel, oncelik dusuk):** `signals_by_profile`
alanini donmek. Gonderilmezse gateway yerlesik profillere duser — bilinen
modeller icin dogru calisir, ama sahadan index duzeltme yetenegi o model icin
kaybolur. Yani bu artik bir **blocker degil**, bir **esneklik** maddesi.

**Deploy sirasi:** serbest. Gateway her iki formati de anlar (alan yoksa
yerlesik/duz listeye duser), backend once ya da sonra deploy edilebilir.

**Performans notu da kapandi:** filtreleme cycle basina bir kez, PROFIL
basina onbelleklenir (`poller.run_poll_cycle._profil_onbellek`) — 300 cihaz x
265 sinyal = 79.500 iterasyon yerine profil sayisi kadar.

---

## B4. Gateway → backend saglik heartbeat'i — ✅ TAMAMLANDI

**Durum:** (a) secenegi uygulandi. Saglik ozeti `GET /gateways/{code}/pending`
istegine `X-E1-Gateway-Health` basligiyla biniyor (bkz.
`backend/health_header.py`). Ek istek YOK.

Baslik CONFIG client'a degil KOMUT client'ina bindi: config-refresh 5 dakikada
bir, komut-poll saniyede bir kosar. Govde `_build_health_body` ile ayni
kaynaktan uretiliyor (10 sn onbellekli) — `status` ve `issues` `/health` ile
BIREBIR ayni, elle yazilmiyor.

**Spesifikasyondan iki sapma (bilerek):**

1. `devices.states` EKLENDI — cihaz KODU bazinda link durumu. Sayimlar
   "hangi cihaz" sorusunu cevaplamiyordu; backend cihazi ancak telemetri
   gelince guncelleyebildigi icin sessiz ama saglikli bir gosterge ile kopmus
   bir gosterge ayirt edilemiyordu. Kodlar YALNIZCA bu kimlik dogrulamali
   baslikta gider; `/health` auth'suz oldugu icin orada hala sadece sayim var.

2. Yalnizca `online` OLMAYAN cihazlar gonderiliyor. 600 cihazin tamamini
   gondermek ~9 KB eder; nginx tavanina yaklasan bir baslik ISTEGIN TAMAMINI
   reddettirir, yani KOMUTLAR DA GITMEZ. Tavan asilirsa kademeli kuculuyor
   (cihaz listesi -> sorun metinleri -> yalnizca sayimlar) ve kirpma
   `states_truncated` ile bildiriliyor.

**Backend yarisi:** `gateway_health_service.record_health` (25 sn yazma
kisitiyla) + `device_link_states` + `gateway_staleness_watchdog.apply_link_states`.
`apply_link_states` v2.34.0'dan SONRA girdi — saha 2.34.0'da oldugu surece
`gateway_health` satiri dolar ama cihaz durumuna yansimaz.

**Alarm kurali da yazildi** (backend `gateway_fleet_alarm.py`): cihazlarin
`lost/total` orani esigi (varsayilan 0.5) 5 dakikadan uzun sure kesintisiz
asarsa muhendis + kurulumcuya bildirim gider. Filo duzelince isaret temizlenir,
yeni bozulma yeniden uyarir. `GATEWAY_FLEET_LOST_RATIO` /
`GATEWAY_FLEET_SUSTAIN_SEC` ile ayarlanir.

**B4 TAMAMEN KAPANDI.**

<details><summary>Ozgun kayit</summary>

**Gateway durumu:** ✅ Tum saglik verisi hazir ve `/health`'te yayinlaniyor;
sadece backend'e ULASTIRACAK kanal yok.

**Sorun:** Gateway NAT arkasinda; `WORKER_HEALTH_HOST` default `127.0.0.1` ve
compose sablonu portu `127.0.0.1:...` olarak baglar. Backend `/health`'i
uzaktan sorgulayamaz. Sonuc: outbox dolmaya baslasa, dead-letter birikse,
cihazlarin %80'i kopuk olsa bile bu bilgi **saha PC'sinin localhost'unda
kaliyor** ve cati panelinde hicbir alarm cikmiyor.

Somut senaryo: backend ingest 6 saat 500 donuyor; outbox 400K'ya cikiyor,
`/health` degraded oluyor. Kimse gormuyor. Outbox limite varinca poller tamamen
duruyor ve SCADA verisi kalici kesiliyor — ekip olayi musteri sikayetiyle
ogreniyor.

**Backend'de yapilacak (iki secenekten biri):**
- **(a)** Mevcut 1 sn'lik `GET /gateways/{code}/pending` istegine hafif bir
  heartbeat govdesi/basligi ekle (gateway zaten her saniye cagiriyor —
  **ek istek maliyeti YOK**, tercih edilen)
- **(b)** Yeni `POST /gateways/{code}/status` endpoint'i

**Gonderilecek alanlar (gateway'de hepsi hazir):**
```json
{
  "status": "ok|degraded|unhealthy",
  "issues": ["outbox_near_capacity", "some_devices_comm_lost"],
  "outbox_pending": 1234,
  "outbox_dead_letter": 0,
  "devices": {"total": 300, "online": 287, "recovering": 3, "lost": 10},
  "uptime_sec": 86400,
  "version": "0.5.0"
}
```

**Backend'de ayrica:** bu veriden alarm kurali (orn. `lost/total > 0.5`
5 dakikadan uzun surerse ENGINEER'a bildirim).

**Deploy sirasi:** ONCE BACKEND (endpoint/alan hazir olsun), sonra gateway.
Gateway tarafi 404/400'e toleransli yazilacak (eski backend'de sessizce atlar).

</details>

---

## Ozet tablo

| # | Konu | Gateway hazir mi | Deploy sirasi | Onceligi |
|---|---|---|---|---|
| B1 | Kalite bayraklari | ⚙️ **kod hazir; kademeli acilis bekliyor** | — (backend v2.28.0'dan hazir) | Yuksek — olcum dogrulugu |
| B2 | Cihaz zaman damgasi | ⚙️ **gateway gonderiyor, backend yok sayiyor** | serbest (kirilma yok) | Yuksek — SOE/ariza analizi |
| B3 | Per-device katalog | ✅ **GATEWAY TARAFI KAPANDI** | serbest | Dusuk — yalnizca esneklik kaldi |
| B4 | Saglik heartbeat + filo uyarisi | ✅ **TAMAMLANDI** | — | ~~Yuksek — kor nokta~~ |

**Gateway tarafinda kalan is YOK.** Dordunun de gateway yarisi bitti.

Kalan iki is BACKEND'de ve ikisi de **kirilma riski tasimiyor** — gateway
gerekli veriyi zaten gonderiyor, backend hazir oldugunda anlam kazaniyor:

1. **B2** — `device_event_at` + `timestamp_quality` alanlarini kabul et.
   SOE/ariza analizi icin en degerli kazanim.
2. **B1** — saha genelinde `DNP3_PUBLISH_QUALITY_FLAGS=true`. Backend
   v2.28.0'dan beri hazir; bu yalnizca bir `.env` degisikligi.

B3'un backend yarisi (`signals_by_profile`) istege bagli bir esneklik
maddesidir; yapilmazsa yerlesik profiller devrede kalir.
