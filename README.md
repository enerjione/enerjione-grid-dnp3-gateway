# EnerjiOne DNP3 Gateway

**Version:** 0.5.0
**Hedef platform:** Windows Server / Windows 10+ (Linux Docker da desteklenir)
**Python:** 3.10 - 3.12

EnerjiOne Grid platformunun **saha gateway** servisidir. DNP3 protokolu uzerinden
saha outstation cihazlarini master rolunde poll eder, okudugu sinyalleri
normalize eder ve **backend HTTP ingest** ile cati backend'inin tag-engine
servisine iletir. NAT arkasindaki saha gateway'i icin outbound HTTPS yeterlidir.

> NATS JetStream legacy/rollback yolu olarak durur; `TELEMETRY_PUBLISHER=nats`
> ile acilir. **Varsayilan `http`'dir** — dokuman ve compose sablonu eskiden
> NATS'i tek yol gibi anlatiyordu ve telemetri gelmediginde ekip yanlis yerde
> ariza ariyordu.

```
+-------------------+         +----------------------+  HTTPS  +------------------+
| DNP3 outstation   |  TCP    |  EnerjiOne DNP3      | ingest  |  Backend tag-    |
| (saha cihazlari)  +-------->|  Gateway (bu proje)  +-------->|  engine + DB     |
|  100-300 cihaz/gw |  20000  |                      |   443   |                  |
+-------------------+         |  - config_client     |         +------------------+
                              |  - poller (yadnp3)   |
                              |  - outbox (SQLite)   |
                              |  - http publisher    |
                              +----------+-----------+
                                         |
                                         | health HTTP (8020)
                                         v
                                  /health  (auth-suz, minimum)
                                  /info, /metrics (Bearer auth)
                                  /refresh-all, /operate (Bearer auth, POST)
```

Sistem mimarisinde 6 gateway paralel calisir = 6 × 100 cihaz = 600 cihaz/site.

## Ozet ozellikler

- **Otonom konfigurasyon:** Backend `/gateways/{code}/config` endpoint'inden
  cihaz listesi + sinyal katalogu cekilir; `config_version` degisince yeni
  hali yansir (gateway restart gerektirmez).
- **At-least-once delivery:** SQLite tabanli persistent outbox + retrier
  thread. NATS bagi koparsa mesajlar diske yazilir, bagi gelince retrier
  bosaltir. Mesaj kaybi YOK; tag-engine idempotent islem.
- **DNP3 master, yadnp3 (OpenDNP3 native):** Tam DNP3 standardi, Group 110
  octet-string destegi, event-driven (Class 1/2/3 scan + periyodik Class 0
  baseline). Fallback adapter: nfm-dnp3 (saf Python, Group 110 yok).
- **Cihaz IP allowlist:** `DNP3_DEVICE_ALLOWED_SUBNETS` CIDR listesi.
  Backend kompromize olsa bile gateway sadece saha LAN'inda TCP acar.
- **Multi-instance lock:** Ayni `GATEWAY_CODE` ile ikinci kopya boot ederse
  SystemExit. Outbox corrupt + duplicate publish riski yok.
- **Production validator:** TLS zorunlulugu (public host), token min length
  + placeholder prefix reddi, deprecated alanlarda hata. Saha hatalarini
  boot'ta yakalar.
- **Per-IP rate-limit:** `/health` (120 req/min) ve `/refresh-all` (10 req/min)
  defansif limit; localhost muaf.
- **Recovery state machine:** TCP up + DNP3 dead durumunda sahte "online"
  yayini onler. 15sn grace icinde fresh frame gelmezse cihaz tekrar lost.
- **Rotating log + secret redaction:** RotatingFileHandler (20MB × 10
  backup) + token / NATS parolasi otomatik `***REDACTED***`.
- **Graceful shutdown:** SIGINT/SIGTERM + Windows SIGBREAK (NSSM stop).
  Tipik <8sn kaynak temizligi.

## Hizli Baslangic

### 1. Python 3.10+ ve bagimliliklar

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Production: yadnp3 wheel manuel kurulur (PyPI'de manylinux + win wheel'leri):
```powershell
py -m pip install "yadnp3==3.2.1.1"
```

Veya hizli kurulum scripti:
```powershell
./scripts/install.ps1
```

### 2. Ortami yapilandir

`.env.example` -> `.env` kopyalayin ve doldurun. Kritik alanlar:

| Key | Amac |
| --- | --- |
| `GATEWAY_CODE` | Backend'deki gateway kodu (orn. `GW-001`) |
| `GATEWAY_TOKEN` | Backend `gateways.token` ile birebir; production'da >=32 char rastgele |
| `APP_ENVIRONMENT` | `development` / `staging` / `production` (validator sertligi degisir) |
| `GATEWAY_MODE` | `mock` (test) veya `dnp3` (saha) |
| `BACKEND_API_URL` | EnerjiOne backend (`/api/v1` ile biter) |
| `NATS_URL` | NATS JetStream (`nats://` private ag / `tls://` public) |
| `WORKER_HEALTH_PORT` | Instance basina unique (8020, 8021, ...) |
| `DNP3_DEVICE_ALLOWED_SUBNETS` | Saha LAN CIDR'leri (production'da MUTLAKA set) |
| `LOG_FILE_PATH` | NSSM kurulumda zorunlu (rotation icin) |

Tam aciklamali: [.env.example](./.env.example) + [docs/SECURITY.md](./docs/SECURITY.md).

### 3. Gateway'i calistir

```powershell
# Tek gateway, default .env
py -3.10 -m dnp3_gateway

# veya setuptools ile kuruldu
enerjione-dnp3-gateway

# Coklu instance, ayri .env
py -3.10 -m dnp3_gateway --env-file .env.GW-002 --health-port 8021
```

Birden fazla gateway ayni makinede: `./scripts/new_gateway.ps1 -Code GW-002`
ile yeni `.env.GW-002` uretin (random token + NTFS ACL otomatik).

### 4. Saglik kontrolu

```powershell
curl http://127.0.0.1:8020/health
```

Yanit ornegi:

```json
{
  "status": "ok",
  "service": "dnp3-gateway",
  "version": "0.5.0",
  "gateway_code": "GW-001",
  "gateway_instance_id": "8f2b...",
  "app_environment": "production",
  "mode": "dnp3",
  "issues": [],
  "config": {
    "config_version": "ab12cd34ef56",
    "device_count": 100,
    "signal_count": 180,
    "active": true,
    "config_cache_age_sec": 12.3,
    "config_cache_stale": false
  },
  "outbox": {
    "outbox_pending": 0,
    "outbox_dead_letter": 0,
    "broker_ready": true,
    "broker_publish_successes": 14530,
    "broker_publish_failures": 0
  },
  "metrics": {
    "uptime_sec": 3210,
    "poll_cycles_total": 642,
    "signals_published_total": 14530
  }
}
```

Status semantigi: `ok` / `starting` / `degraded` / `unhealthy` (503).
Detayli incident response: [docs/RUNBOOK.md](./docs/RUNBOOK.md).

## Proje yapisi

```
EnerjiOne Grid DNP3 Gateway/
|-- README.md
|-- CHANGELOG.md
|-- VERSION                       <- semver; mevcut surum
|-- pyproject.toml                <- paket meta + setuptools
|-- requirements.txt              <- runtime bagimliliklar
|-- requirements-dev.txt          <- gelistirme/test
|-- .env.example
|-- Dockerfile
|-- src/
|   `-- dnp3_gateway/
|       |-- __init__.py
|       |-- __main__.py           <- py -m dnp3_gateway
|       |-- main.py               <- ana dongu / config refresh / shutdown
|       |-- config.py             <- Settings (pydantic-settings + validator)
|       |-- logging_setup.py      <- rotating + secret redaction
|       |-- state.py              <- GatewayState (thread-safe, disk cache)
|       |-- poller.py             <- run_poll_cycle (paralel okuma)
|       |-- health_server.py      <- /health, /info, /metrics, /refresh-all
|       |-- auth/                 <- identity, headers, instance_lock
|       |-- backend/
|       |   `-- config_client.py  <- BackendConfigClient (HMAC opt-in)
|       |-- messaging/
|       |   |-- jetstream_publisher.py
|       |   |-- outbox.py
|       |   `-- resilient_publisher.py
|       `-- adapters/
|           |-- base.py
|           |-- mock.py
|           |-- dnp3_master.py    <- fallback (nfm-dnp3)
|           `-- dnp3_yadnp3_master.py  <- primary (yadnp3/OpenDNP3)
|-- scripts/
|   |-- install.ps1
|   |-- run_gateway.ps1
|   |-- new_gateway.ps1
|   `-- render_compose.py
|-- docker/
|   `-- compose.template.yml
|-- tests/
`-- docs/
    |-- ARCHITECTURE.md
    |-- SECURITY.md
    |-- RUNBOOK.md
    `-- DOCKER.md
```

## Production checklist

Sahaya cikmadan once:

- [ ] `GATEWAY_TOKEN` >=32 char rastgele (`secrets.token_urlsafe(48)`)
- [ ] `APP_ENVIRONMENT=production`
- [ ] Backend ile token eslesmesi dogrulandi (smoke test: /health "ok")
- [ ] `DNP3_DEVICE_ALLOWED_SUBNETS` saha LAN'ina gore set
- [ ] `LOG_FILE_PATH` rotation icin set (NSSM kurulumda)
- [ ] `.env` ACL kisitlandi (install.ps1 otomatik yapar)
- [ ] (Public NATS) `NATS_URL=tls://` + `NATS_CREDENTIALS_PATH` + `NATS_TLS_CA_PATH`
- [ ] (Public backend) `BACKEND_API_URL=https://` + sertifika gecerli
- [ ] Multi-instance kullanilacaksa `WORKER_HEALTH_PORT` her gateway icin farkli
- [ ] `RABBITMQ_URL` ve `NATS_DUAL_PUBLISH_ENABLED` `.env`'den silindi (DEPRECATED)

Tam detay: [docs/SECURITY.md "Checklist"](./docs/SECURITY.md#checklist-yeni-sunucuya-gateway).

## Versiyonlama

Versiyon `MAJOR.MINOR.PATCH`:
- **Patch** (`0.4.6`): bugfix, kucuk yama, doc temizligi
- **Minor** (`0.5.0`): yeni ozellik, geri-uyumlu genisleme
- **Major** (`1.0.0`): kirici degisiklik

`VERSION` dosyasi + `pyproject.toml` daima birlikte guncellenir.

## Lisans

Proprietary - Form Elektrik Ins. Muh. A.S. Tum haklari saklidir.
