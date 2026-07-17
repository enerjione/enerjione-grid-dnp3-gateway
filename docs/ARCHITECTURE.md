# Mimari

`dnp3_gateway`, EnerjiOne Grid platformunun **saha gateway** katmanidir. Her
instance tek bir lojik gateway'i temsil eder; backend tarafinda kayitli ve o
gateway'e atanmis DNP3 outstation cihazlari ile haberlesir.

Ayni veya farkli sunucularda **yuk paylasimli** coklu instance icin token,
multi-instance lock, TLS kurallari icin: [SECURITY.md](./SECURITY.md).

## Bilesen diyagrami

```
                          +------------------------+
                          |  backend-api (FastAPI) |
                          |  Postgres + JetStream  |
                          +-----+----+-------------+
                                ^    |
   X-Gateway-Token, Code,       |    | GatewayConfigResponse
   Instance-Id, Request-Id,     |    | (+ X-Config-Signature)
   User-Agent                   |    |
                                |    v
                       +----------------------+
                       | BackendConfigClient  |
                       | (requests, HMAC opt) |
                       +----------+-----------+
                                  |
                                  v
+--------------------+     +---------------+        +-------------------+
|                    |     |               |        |                   |
|   DNP3 adapter     |<----+ GatewayState  +------->| ResilientPublisher|
|   - yadnp3 (def.)  | due | (thread-safe, |payload | broker + outbox   |
|   - nfm-dnp3       |dev. |  cache_path)  |        |                   |
|   - mock           |     |               |        +---------+---------+
+----------+---------+     +-------+-------+                  |
           |                       ^                          |
           | DNP3 TCP              |                          v
           v                       |                +-------------------+
+-------------------+         +----+-------+        |  JetStreamPub.    |
|  Outstation(s)    |         | health HTTP|        |  (NATS, async)    |
|  (saha cihazlari) |         | /health    |        +---------+---------+
+-------------------+         | /info      |                  |
                              | /metrics   |                  | publish
                              | /refresh-  |                  v
                              |   all      |        +-------------------+
                              +------------+        |  NATS JetStream   |
                                                    |  e1.telemetry.    |
                                                    |  raw.<GW_CODE>    |
                                                    +---------+---------+
                                                              |
                                                              v
                                                    tag-engine / alarm-svc
                                                    iec104-outbound vs.
```

## Veri akisi (basarili publish)

```
[DNP3 Master.read_device]
       |
       v
SignalReading(key, source, raw, scaled, quality, value_string?)
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
|   "quality": "good",                              |
|   "source_timestamp": "2026-05-13T08:00:00Z"      |
| }                                                 |
+---------------------------------------------------+
       |
       v
[ResilientPublisher.publish]
       |
       v
[JetStreamPublisher.publish]
       headers: {"Nats-Msg-Id": message_id, "X-Correlation-Id": ...}
       subject: "e1.telemetry.raw.GW-001"
       |
       v
[NATS JetStream Stream TELEMETRY_RAW]   <-- 2dk dedup window
       (Nats-Msg-Id ile broker-side de-duplication)
       |
       v
[tag-engine consumer]   (idempotent islem)
```

## Veri akisi (broker down — outbox at-least-once)

```
[poller] --publish()--> [JetStreamPublisher] -- JetStreamNotReadyError
                                            \
                                             +-> [ResilientPublisher catches]
                                                       |
                                                       v
                                            [Outbox.enqueue]
                                            SQLite: outbox_<CODE>.db
                                                       |
                                                       | NATS gelince
                                                       v
                                            [OutboxRetrier (bg thread)]
                                            fetch_batch -> publish
                                                       |
                                            +---------+---------+
                                            |                   |
                                       basari (delete)    fail (retry++)
                                                            |
                                                            v
                                                  retry_count >= MAX
                                                            |
                                                            v
                                                     dead_letter table
                                                     (manuel inceleme)
```

`JetStreamNotReadyError` TRANSIENT istisna — retry_count ARTIRMAZ. Aksi
takdirde uzun NATS outage'inda 500K mesaj sessizce dead-letter'a duserdi.

## Thread modeli

| Thread | Gorev |
| --- | --- |
| `main` | Poll dongusu (her `DEFAULT_POLL_INTERVAL_SEC`'de bir uyanir) |
| `config-refresh` | Backend config endpoint cagrisi (exponential backoff fail durumunda) |
| `health-http` | `ThreadingHTTPServer.serve_forever` (yavas client diger probe'lari bloklamaz) |
| `outbox-retrier` | Outbox SQLite drenajı, exponential backoff |
| `jetstream-<code>` | nats-py async event loop (sync facade) |
| `signal handler` | SIGINT/SIGTERM/SIGBREAK -> `stop_event.set()` |

`GatewayState` tek mutex ile korunur (kucuk lock'lar lock-free dict lookup'a
indirgenir; bkz. `_DeviceCache` double-check pattern).

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

## Roadmap

- **JetStream cluster HA** — 3-node + `replicas: 3` stream config (operasyon
  takimi karari).
- **mTLS** — `dnp3_gateway` -> backend ve -> NATS arasinda kullanici
  sertifikalari (uzun vade).
- **Prometheus exporter** — `/metrics` JSON yerine `text/plain` Prometheus
  format opsiyonel.
- **Command downlink** — backend'den gelen `binary_output` / `analog_output`
  komutlarini DNP3 outstation'a yazma akisi.
- **Reproducible build** — `requirements-lock.txt` (`pip-compile
  --generate-hashes`) + CI'da hash dogrulamasi.
