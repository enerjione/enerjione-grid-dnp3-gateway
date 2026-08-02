# Backend Koordinasyonu Gereken Isler

**Durum:** ACIK — henuz yapilmadi
**Olusturulma:** 2026-07-31 (production hardening calismasi)
**Neden ertelendi:** Bu maddeler gateway'i tek basina degistirerek cozulemez;
backend'in `/gateways/{code}/config`, `/gateways/{code}/pending` ve
`/telemetry/gateway/{code}` sozlesmelerini de degistirir. Gateway tarafindaki
diger tum production riskleri kapatildi (bkz. `CHANGELOG.md` 0.5.0).

> Bu dosya, gateway ve backend ayni anda deploy edilecegi zaman acilacak.
> Her maddede **kim once deploy edilmeli** bilgisi var — sira yanlis olursa
> saha sessizce bozulur.

---

## B1. DNP3 kalite bayraklari yayina eklenecek — ⚙️ TEK ENV BAYRAGI KALDI

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

**KALAN TEK IS:** `GATEWAY_PUBLISH_DNP3_QUALITY=true`. Saha genelinde ya da
gateway bazinda acilabilir; backend yillardir hazir, govde artik dogru.

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
  (fonksiyon yazildi, su an `GATEWAY_PUBLISH_DNP3_QUALITY=false` ile kapali)

**Deploy sirasi:** ONCE BACKEND. Gateway yeni kalite degerlerini gonderdiginde
backend bunlari tanimiyorsa olcumleri reddedebilir veya yanlis isleyebilir.

**Gecis kolayligi:** `GATEWAY_PUBLISH_DNP3_QUALITY` env bayragi eklendi
(default `false`). Backend hazir olunca saha genelinde tek tek acilabilir.

</details>

---

## B2. Cihazin DNP3 olay zaman damgasi

**Gateway durumu:** ⚠️ Kismi — DNP3 zaman senkronizasyonu (`timeSyncMode`)
eklendi, ancak olay damgasi hala yayina girmiyor.

**Sorun:** `poller.build_telemetry_payload` icinde `source_timestamp`, cihazdan
okuma yapilmadan ONCE uretilen gateway saatidir ve o cihazin TUM sinyallerine
ayni deger basilir. Cihazin kendi DNP3 zaman damgasi (`it.value.time`) atiliyor.

Somut senaryo: 4G link 10 dakika kopuyor. Outstation event buffer'inda
`08:00:03` (ariza gecti), `08:00:04` (ariza kalkti), `08:07:12` (yeniden kapama)
olaylarini kendi damgalariyla biriktiriyor. Link gelince Class 1/2/3 scan
hepsini tek fragment'ta bosaltiyor; gateway hepsini `08:10:00.123` ile ayni
saniyeye damgaliyor. **Ariza suresi ve olay sirasi kalici olarak kayboluyor.**

**Backend'de yapilacak:**
- Telemetri semasina `gateway_received_at` alani eklenecek
- `source_timestamp` anlami degisecek: artik CIHAZIN olay zamani (varsa),
  yoksa gateway zamani (fallback)
- Historian/SOE sorgularinda hangi alanin kullanilacagi kararlastirilacak
- Opsiyonel: `timestamp_quality` (DNP3 SYNCHRONIZED / UNSYNCHRONIZED / INVALID)

**⚠️ SIRA KRITIK — tek basina yapilirsa DURUM KOTULESIR:**
```
B1 (kalite bayraklari)  →  DNP3 zaman senkronizasyonu  →  B2  →  B4
```
Neden: zaman-senk olmadan outstation saatleri serbest surukleniyor (ayda
10-60 sn drift, guc kesintisinde RTC 2000-01-01'e reset). B2'yi tek basina
acmak "hepsi ayni yanlis saat" durumunu **"hepsi FARKLI yanlis saat"** haline
getirir — ariza analizi icin daha kotu. Ayrica zaman kalitesi bitlerini
okuyabilmek B1'e bagli.

**Deploy sirasi:** ONCE BACKEND (yeni alani kabul etsin), sonra gateway.

---

## B3. Per-device sinyal katalogu

**Gateway durumu:** ❌ Gateway tarafinda cozulemez — kontrat degisikligi sart.

**Sorun:** `SignalConfig` dataclass'inda `device_code` veya `signal_profile`
bagi **YOK**. `poller.run_poll_cycle` cycle basinda TEK bir sinyal listesi
hesaplayip AYNI listeyi her cihaza uyguluyor. `DeviceConfig.signal_profile`
alani backend'den geliyor ve parse ediliyor ama gateway kodunda **hicbir yerde
okunmuyor**.

Tek marka filoda zararsiz (cihazda olmayan index → `no_change` → yayin yok).
Ama ikinci marka girdigi gun:

| | Profil A (Horstmann SN2) | Profil B (baska uretici) |
|---|---|---|
| `(30, 12)` | `conductor_temperature` | `battery_voltage` |

Backend iki profilin BIRLESIMINI donduruyor. B cihazi poll edildiginde onun
`(30,12)` degeri **`conductor_temperature` etiketiyle** yayinlaniyor ve alarm
esikleri bu sahte deger uzerinden tetikleniyor.

Ayrica performans: 300 cihaz x 265 sinyal = cycle basina 79.500 iterasyon.

**Backend'de yapilacak (iki secenekten biri):**
- **(a)** `SignalConfig`'e `device_code` alani ekle — sinyal dogrudan cihaza bagli
- **(b)** `signals` yanitini `signals_by_profile: {profil_adi: [...]}` seklinde
  grupla; gateway `device.signal_profile` ile eslestirsin

**(b) tercih edilir:** payload buyumez (ayni sinyal N cihaz icin tekrarlanmaz)
ve mevcut `signal_profile` alani anlamli hale gelir.

**Gateway'de yapilacak (backend hazir olunca):**
- `run_poll_cycle` icinde cycle basina bir kez `signals_by_profile` on-hesapla
- `poll_device`'a `signals_by_profile[device.signal_profile]` gecir
- Bilinmeyen profil → WARNING + bos liste (sessizce yanlis veri yayinlama)

**Deploy sirasi:** ONCE BACKEND (yeni alan/grup opsiyonel olarak eklensin,
gateway eski formati da anlamaya devam etsin), sonra gateway.

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

**Kalan:** bu veriden alarm kurali (orn. `lost/total > 0.5` 5 dakikadan uzun
surerse ENGINEER'a bildirim) — backend isi.

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
| B1 | Kalite bayraklari | ⚙️ **tek env bayragi kaldi** | — (backend v2.28.0'dan hazir) | Yuksek — olcum dogrulugu |
| B2 | Cihaz zaman damgasi | ⚠️ zaman-senk var | Backend → Gateway (**B1'den SONRA**) | Yuksek — SOE/ariza analizi |
| B3 | Per-device katalog | ❌ kontrat sart | Backend → Gateway | Orta — cok markali filoda kritik |
| B4 | Saglik heartbeat | ✅ **TAMAMLANDI** | — | ~~Yuksek — kor nokta~~ |

**Onerilen calisma sirasi:** ~~B4~~ (tamamlandi) → ~~B1~~ (bayrak acilinca biter) → B2 → B3.
