# Mimari

`dnp3_gateway`, EnerjiOne Grid platformunun **saha gateway** katmanidir. Her
instance tek bir lojik gateway'i temsil eder; backend tarafinda kayitli ve o
gateway'e atanmis DNP3 outstation cihazlari ile haberlesir.

Ayni veya farkli sunucularda **yuk paylasimli** coklu instance icin token,
multi-instance lock, TLS kurallari icin: [SECURITY.md](./SECURITY.md).

## Bilesen diyagrami

Gateway backend'e **yalnizca outbound HTTPS** ile baglanir. Saha kurulumlari
NAT/GSM arkasindadir; iceri dogru acilmis tek bir port yoktur.

```
                       +------------------------------+
                       |     backend-api (FastAPI)    |
                       |  Postgres + tag-engine       |
                       +--+---------+---------+-------+
                          ^         ^         ^
      GET /config         |         |         |  POST /telemetry/batch
      (+X-Config-Sig)     |         |         |  (cihaz basina TEK istek)
                          |         |         |
      GET /pending  ------+         |         +------ POST /commands/results
      (1sn, komut cekme)            |
      + X-E1-Gateway-Health --------+
        (saglik ozeti, ek istek YOK)
                          |
              +-----------+-----------------------------+
              |        outbound HTTPS (requests)         |
              |   ortak connection pool + AYRI session   |
              |   (komut-poll telemetriyi bloklamaz)     |
              +-----------+-----------------------------+
                          |
+--------------------+    |    +---------------+     +--------------------+
|   DNP3 adapter     |<---+---->  GatewayState  +---->| ResilientPublisher |
|   - yadnp3 (def.)  |  due    | (thread-safe, |     |  broker + outbox   |
|   - mock (test)    |  dev.   |  cache_path)  |     +---------+----------+
+----------+---------+         +-------+-------+               |
           |                           ^              +--------+--------+
           | DNP3 TCP (20000)          |              |                 |
           v                           |          basarili         basarisiz
+-------------------+          +-------+------+        |                 |
|  Outstation(s)    |          | health HTTP  |        v                 v
|  100-300 cihaz    |          | /health      |   HttpTelemetry     Outbox (SQLite)
|  (saha)           |          | /info        |   Publisher         + OutboxRetrier
+-------------------+          | /metrics     |        |            + dead-letter
                               | /refresh-all |        v
                               | /operate     |   backend ingest
                               +--------------+
```

Varsayilan ve STANDART yol NATS JetStream'dir (1.1.0+); telemetri
backend'e ugramaz. `TELEMETRY_PUBLISHER=http` ile backend HTTP ingest'e
rollback mumkundur (yuk backend'e biner, yalnizca bilincli geri donus).

## Veri akisi (basarili publish)

```
[DNP3 Master.read_device]   <- event-driven cache (SOE) + periyodik integrity
       |
       v
SignalReading(key, source, raw, scaled, quality, dnp3_flags, value_string?)
       |
       v
[poller.build_telemetry_payload]
       |
       v
+---------------------------------------------------+
| {                                                 |
|   "message_id": "<uuid>",                         |
|   "correlation_id": "<uuid>",                     |
|   "source_gateway": "GW-001",                     |
|   "device_code": "DEV-001",                       |
|   "signal_key": "master.actual_current",          |
|   "signal_source": "master",                      |
|   "signal_data_type": "analog",                   |
|   "value": 1234.5,                                |
|   "value_string": null,                           |
|   "quality": "good",                              |
|   "dnp3_flags": ["online"],                       |
|   "source_timestamp": "2026-05-13T08:00:00Z"      |
| }                                                 |
+---------------------------------------------------+
       |
       v
[ResilientPublisher.publish_batch]  — cihaz basina TEK POST
       |
       v
[HttpTelemetryPublisher] POST /gateways/{code}/telemetry/batch
       |
       v
[backend ingest -> tag-engine]   (message_id ile idempotent)
```

`dnp3_flags` alani **kalitenin kapsamini** belirler: dolu ise kalite NOKTA
seviyesindedir (tek bir noktanin `invalid` bayragi cihazi offline yapmaz),
`null` ise CIHAZ seviyesidir (0.4.x davranisi). Alan susturulursa
`DNP3_PUBLISH_QUALITY_FLAGS` acildigi anda tek bir hatali nokta tum cihazi
offline gosterirdi.

## Veri akisi (backend down — outbox at-least-once)

```
[poller] --publish_batch()--> [HttpTelemetryPublisher] -- TransientPublishError
                                            \             (timeout/5xx/429)
                                             +-> [ResilientPublisher catches]
                                                       |
                                                       v
                                            [Outbox.enqueue]
                                            SQLite: outbox_<CODE>.db
                                                       |
                                                       | backend gelince
                                                       v
                                            [OutboxRetrier (bg thread)]
                                        fetch_batch(now) -> publish_batch
                                                       |
                                            +---------+---------+
                                            |                   |
                                       basari (delete_many) fail (mark_retry)
                                                            |
                                                   next_attempt_at ileri
                                                   (head-of-line blocking yok)
                                                            |
                                                            v
                                                  retry_count >= MAX
                                                            v
                                                     dead_letter table
                                                     (prune: gun/satir tavani)
```

Hata siniflandirmasi `messaging/errors.py`'de tek yerde:

| Sinif | Ornek | Davranis |
| --- | --- | --- |
| `TransientPublishError` | timeout, 408/425/429/502/503/504 | retry_count ARTMAZ, `next_attempt_at` ileri alinir |
| `PermanentPublishError` | 400/401/403/422 | retry_count artar, MAX'ta dead-letter |

Gecici hatada sayacin artmamasi kritik: aksi halde uzun bir backend
kesintisinde yuz binlerce gecerli mesaj sessizce dead-letter'a duserdi.
`next_attempt_at` ise tek bir kotu satirin kuyrugun basini tikamasini
(head-of-line blocking) onler.

### Publish-sonrasi commit (TOCTOU korumasi)

Cache "dirty" bayragi publish'ten **once** degil **sonra** temizlenir:

```
peek_if_dirty() -> (readings, version)     # bayrak DURUYOR
        publish_batch(...)                 # basarisiz olabilir
commit_published(version)                  # sadece basarili ise, versiyon esitse
```

Publish sirasinda cihazdan yeni bir event gelirse versiyon sayaci artar ve
`commit_published` **hicbir seyi temizlemez** — o degisiklik bir sonraki
turda tekrar yayinlanir. Eski akista (once temizle, sonra yayinla) o pencerede
gelen deger sessizce kaybolurdu.

## Thread modeli

| Thread | Gorev |
| --- | --- |
| `main` | Poll dongusu (`DEFAULT_POLL_INTERVAL_SEC`) + periyodik bakim adimlari |
| `config-refresh` | Backend `GET /config` (fail'de exponential backoff) |
| `command-poll` | `GET /pending` (~1sn) — SCADA komut kanali, **ayri HTTP session** |
| `health-http` | `ThreadingHTTPServer.serve_forever` |
| `outbox-retrier` | Outbox SQLite drenaji, batch + backoff |
| `poll-worker-*` | `ThreadPoolExecutor` — cihazlar paralel yoklanir |
| `publish-worker-*` | Tek cihazin sinyalleri paralel yayinlanir (WAN gecikmesini gizler) |
| `signal handler` | SIGINT/SIGTERM/SIGBREAK -> `stop_event.set()` |

Komut-poll'un **ayri** `requests.Session` kullanmasi bilincli: telemetri
POST'lari havuzdaki tum baglantilari tuketirse komut cekme siraya girer ve
SCADA'dan verilen bir acma/kapama komutu gecikirdi.

`GatewayState` tek mutex ile korunur (kucuk lock'lar lock-free dict lookup'a
indirgenir; bkz. `_DeviceCache` double-check pattern).

### Thread canliligi (`ThreadLiveness`)

Arkaplan thread'leri sessizce olebilir — bir daemon thread'in olumu prosesi
dusurmez. `retrier` olurse telemetri teslimi durur, `command-poll` olurse
SCADA komut kanali kopar; ikisi de disaridan **normal** gozukurdu. Her thread
`ThreadLiveness`'a kayitli; olen thread `/health` uzerinde
`thread_dead:<ad>` olarak raporlanir ve durum `unhealthy` olur. Poll dongusu
ayrica bir watchdog ile izlenir (`poll_stalled`).

## Recovery state machine (DNP3 cihaz haberlesmesi)

`_DeviceCache._state`:

```
  +-----------+     baglanti acildi      +--------------+
  |   lost    +------------------------->|  recovering  |
  +-----+-----+   (begin_recovery)       +------+-------+
        ^                                       |
        | grace timeout                         | fresh frame
        | (15sn)                                | (cache.set)
        |                                       v
        |                                +--------------+
        +-(fail_recovery)----------------+   online     |
                                         +--------------+
                                                |
                                                | link kopuk (OnClose)
                                                v
                                          (lost'a geri)
```

* **lost:** SCADA "comm_lost" gorur; gateway comm_lost yayini surdurur.
* **recovering:** Link acik gozukuyor ama henuz fresh DNP3 frame gelmedi.
  SCADA hala comm_lost goruyor — sahte online onlenir. 15sn grace icinde
  frame gelirse `online`'a yukselir; gelmezse tekrar `lost`.
* **online:** Normal event-driven yayin. Recovery confirmed olunca tek
  seferlik `mark_all_dirty` ile son bilinen TUM degerler quality=good ile
  yayinlanir (SCADA tarafi cihazi tekrar canli gorur).

## Konfigurasyon versiyonlama

Backend `config_version` hash'i (sha1) doner. Gateway bu degeri takip eder;
degistiginde:

- Yeni cihaz eklemesi anlik yansir (en fazla `CONFIG_REFRESH_SEC` gecikme).
- Sinyal kataloğu degisikligi (DNP3 adresi, scale/offset) restart gerektirmez.
- Backend `is_active=False` set ederse poller calismaya devam eder ama
  publish durur.

Disk cache: backend down olsa bile gateway diskteki son config ile polling'e
devam eder. `CONFIG_CACHE_MAX_AGE_HOURS`'tan eski olursa `/health` issue
listesinde `config_cache_stale` raporlanir.

## Defansif sema validasyonu

`BackendConfigClient._parse_gateway_config` icindeki kontroller — backend
kompromize olsa bile gateway kontrolsuz buyume + DoS yememeli:

| Kontrol | Limit | Davranis |
| --- | --- | --- |
| `devices` listesi | 1000 hard cap | Asimi truncate + ERROR log |
| `signals` listesi | 5000 hard cap | Asimi truncate + ERROR log |
| String alanlar | code 64ch, label 256ch, unit 32ch | Truncate + WARN |
| Cihaz IP scheme | `://`, `/`, `\`, ` ` yasak | Cihaz reddedilir |
| Cihaz IP allowlist | `DNP3_DEVICE_ALLOWED_SUBNETS` | Disindaki IP reddi |
| `dnp3_address` | 0-65519 | Aralik disinda default'a clamp + WARN |
| `dnp3_index` | 0-65535 (16-bit) | Aralik disinda default 0 |
| `scale`, `offset` | finite float | inf/nan -> default + WARN |
| Response Content-Length | 10MB hard cap | Stream sirasinda raise |

## Sinyal katalogu cozumleme (profiller)

Bir DNP3 modelinin adres haritasi (object_group, index, scale, offset)
firmware'in ozelligidir — her kurulumda aynidir. Bu yuzden gateway icinde
yerlesik profiller bulunur (`profiles/<model>.json`, dosya adi backend'deki
`devices.model` ile birebir ayni).

Oncelik sirasi (`state.signals_for`):

```
1. backend profili (model icin dolu)  -> KAZANIR
2. yerlesik profil (profiles/*.json)  -> backend bos/yok ise
3. duz `signals` listesi              -> profil kavrami hic yoksa (eski backend)
```

Otorite **backend'dir**. Gerekce: kurulumcu sahada yanlis bir DNP3 index'ini
arayuzden duzeltebilmeli; yerlesik harita kazansaydi tek bir adres hatasi
icin yeni gateway imaji cikarmak gerekirdi. Yerlesik kopya yalnizca katalog
bos geldiginde devreye girer — yani "backend'e cihaz eklendi ama sinyalleri
girilmedi" durumunda gateway yine de dogru yoklar.

## Komut yolu (downlink)

```
[backend] --+-- GET /pending  (gateway ceker, ~1sn, NAT dostu)
            |
            +-- POST /operate (dogrudan push, GATEWAY_COMMAND_TOKEN)
                        |
                        v
              [CommandLedger] start_dispatch(command_id)
              SQLite: command_ledger_<CODE>.db
                 | zaten varsa -> CROB TEKRARLANMAZ, kayitli sonuc doner
                 v
              [DNP3 adapter] CROB (DirectOperate, varsayilan)
                        |
                        v
              [CommandLedger] record_result(...)
                        |
                        v
              POST /commands/results   (at-least-once, batch 50)
                 | gecici hata -> tekrar denenir
                 | kalici hata -> 3 kez sonra dead-letter
```

Ledger iki isi birden yapar: **idempotency** (ayni `command_id` ile gelen
tekrar istegi ikinci bir kesici manevrasi yapmaz) ve **at-least-once sonuc
teslimi** (gateway sonucu bildirmeden olurse, acilista
`recover_unknown_results` yarim kalmis komutlari `unknown` olarak bildirir —
operator "komut gitti mi?" sorusuyla bas basa kalmaz).

## Saglik kanali (`X-E1-Gateway-Health`)

Backend `communication_status`i yalnizca telemetri geldiginde
guncelleyebilir. Deger degismeyen bir gosterge saatlerce sessiz kalabilir —
"veri gelmiyor" ile "haberlesme yok" ayni sey degildir ve bu ayrimi yapabilen
tek yer gateway'dir.

Cozum: zaten saniyede bir atilan `GET /pending` istegine bir **baslik**
binmesi (ek istek maliyeti yok). Baslik yalnizca `online` OLMAYAN cihazlari
tasir — 600 cihazin tamamini gondermek ~9 KB eder ve nginx baslik tavanina
yaklasan bir istek **komut kanalini da** dusururdu. Liste yine de tavani
asarsa kirpilir ve `states_truncated` ile bildirilir; sessiz kirpma backend'e
"geri kalan her sey iyi" dedirtirdi.

Bu modulun hicbir hatasi `/pending` cagrisini dusuremez (tam savunmaci
uretim) — orasi SCADA komut kanali.

## Kaynak guard'lari

| Guard | Esik | Etki |
| --- | --- | --- |
| `DiskGuard` | 1 GB warn / 256 MB kritik | Kritikte outbox yazimi durur; `/health` -> `disk_critical`. Disk dolarsa SQLite bozulur — once kontrollu duraklat. |
| `ClockGuard` | 2 sn warn / 30 sn guvensiz | Backend HTTP `Date` basligindan sapma olculur. Guvensiz iken **DNP3 saat senkronu askiya alinir**: yanlis saatli bir gateway sahadaki 300 cihazin saatini birden bozabilirdi. |

## Kalici depolama (SQLite) surumleme

`outbox_<CODE>.db` ve `command_ledger_<CODE>.db` `PRAGMA user_version`
tabanli bir migration runner ile acilir (`messaging/sqlite_support.py`):

- **Yukseltme:** sema `CREATE TABLE IF NOT EXISTS` ile degil, sirali
  migration'larla kurulur. Aksi halde yeni surum bir kolon eklediginde
  sahadaki mevcut dosya eski semada kalir, ilk sorgu `no such column` atar ve
  `restart: unless-stopped` ile **tum saha ayni anda crash-loop'a** girer.
- **Downgrade koruma:** dosya koddan yeniyse sessizce devam etmek yerine hata
  verilir.
- **Bozulma:** acilista `PRAGMA quick_check`. Bozuk dosya
  `<ad>.corrupt.<zaman>` olarak **karantinaya** alinir (forensic icin
  saklanir) ve temiz bir DB ile devam edilir. Eskiden bozuk dosya yapicida
  raise edip kalici boot arizasi yapardi; health server hic acilmazdi.

## Konfigurasyon hatasi = cikis kodu 78

Ayarlar `argparse`'tan **sonra** yuklenir ve `load_settings()` pydantic
hatasini `ConfigError`'a cevirir; proses `EX_CONFIG` (78) ile ve cerceveli bir
mesajla cikar. Iki sebep:

1. Eskiden Settings import aninda dogrulaniyordu — `--help` bile calismiyordu
   ve hatali bir `.env` sonsuz servis crash-loop'u uretiyordu.
2. Pydantic'in hata metnindeki `input_value={...}` kuyrugu **tum ayar
   sozlugunu** (`GATEWAY_TOKEN` dahil) stdout'a / NSSM loguna dokuyordu.
   `format_config_error()` bu kuyrugu kirpar.

## Roadmap

- **mTLS** — `dnp3_gateway` -> backend arasinda kullanici sertifikalari
  (uzun vade).
- **Prometheus exporter** — `/metrics` JSON yerine `text/plain` Prometheus
  format opsiyonel.
- **Analog output komutlari** — su an yalnizca `binary_output` (CROB)
  destekleniyor.
- **Reproducible build** — `requirements-lock.txt` (`pip-compile
  --generate-hashes`) + CI'da hash dogrulamasi.
