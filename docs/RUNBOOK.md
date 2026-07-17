# Runbook — EnerjiOne DNP3 Gateway

Gunluk operasyon kontrol listesi + incident response.

## 1. Baslatma

### Windows (gelistirme veya tek-host saha)

```powershell
cd "C:\...\EnerjiOne Grid DNP3 Gateway"
./scripts/install.ps1                # ilk kez ya da -Recreate
./scripts/run_gateway.ps1            # .env uzerinden
```

### Windows (NSSM servisi)

```powershell
nssm install EnerjiOneDnp3Gateway `
    "C:\Projeler\EnerjiOne Grid DNP3 Gateway\.venv\Scripts\python.exe" `
    "-m dnp3_gateway"
nssm set EnerjiOneDnp3Gateway AppDirectory "C:\Projeler\EnerjiOne Grid DNP3 Gateway"
nssm set EnerjiOneDnp3Gateway Start SERVICE_AUTO_START

# NSSM stdout rotasyonu YOK — .env'de LOG_FILE_PATH zorunlu:
#   LOG_FILE_PATH=C:/ProgramData/EnerjiOne/dnp3-gateway/{gateway_code}.log
# Gateway kendi RotatingFileHandler'i acar (20MB x 10 backup default).

nssm start EnerjiOneDnp3Gateway
```

### Ayni makinede coklu gateway

```powershell
./scripts/new_gateway.ps1 -Code GW-002 -HealthPort 8021
# -> .env.GW-002 uretir, GATEWAY_TOKEN dosyaya guvenli yazar

py -m dnp3_gateway --env-file .env.GW-002
```

Her instance kendi `instance_GW-002.lock` dosyasi alir; ayni kod ile ikinci
proses SystemExit eder.

### Linux / Docker

```bash
docker compose -f gateways/gw-001.yml up -d
```

Compose template: `docker/compose.template.yml`. Backend "Yeni gateway ekle"
akisi bu sablonu render eder.

## 2. Saglik dogrulama

```powershell
Invoke-RestMethod http://127.0.0.1:8020/health | ConvertTo-Json -Depth 5
```

Ciktidaki kritik alanlar:

| Alan | Beklenen | Eylem |
| --- | --- | --- |
| `status` | `ok` | — |
| `status` | `starting` | Ilk config bekleniyor. 15-30sn icinde `ok`'e gecmeli. |
| `status` | `degraded` | `issues` arrayine bak; cache_stale veya config_refresh_failing. |
| `status` | `unhealthy` | HTTP 503 doner. Outbox dolu veya kritik hata. **Mudahale.** |
| `config.active` | `true` | False ise backend'de `is_active=False` set edilmis demek. |
| `config.device_count` | >=1 | 0 ise backend cihaz atamamis. |
| `config.config_version` | 12 karakterli hash | Loglarda ayni hash'i gormeli. |
| `outbox.outbox_pending` | <100 saglikli | >>1000 ise broker'a yetisemiyor. |
| `outbox.broker_ready` | `true` | False ise NATS baglantisi yok. |

Detayli operasyonel metrikler `/info` ve `/metrics` endpoint'leri uzerinden
**Bearer auth ile** (`GATEWAY_REFRESH_TOKEN`). Auth-suz `/health` sadece minimum
status + issues raporlar (recon malzemesi sizmasin).

## 3. Log izleme

```powershell
# Rotating dosya (LOG_FILE_PATH set ise)
Get-Content "$env:ProgramData\EnerjiOne\dnp3-gateway\GW-001.log" -Tail 200 -Wait

# Servis stdout (NSSM AppStdout yonlendirmesi)
Get-Content "$env:ProgramData\EnerjiOne\dnp3-gateway\current.log" -Tail 200 -Wait
```

### Anahtar log etiketleri

| Etiket | Anlam | Seviye |
| --- | --- | --- |
| `dnp3_gateway_starting` | Servis acildi | INFO |
| `dnp3_gateway_running` | Polling loop'a girdi | INFO |
| `config_refresh gateway=.. version=..` | Backend yeni config dondurdu | INFO |
| `config_refresh_recovered` | Yeniden baglandi (n consecutive failures sonra) | INFO |
| `config_auth_error` | 401/403 — token uyumsuz, manuel mudahale gerek | ERROR |
| `config_404_error` | Backend'de gateway kaydi yok | ERROR |
| `gateway_polling_suspended` | `is_active=False` set edildi | WARNING |
| `gateway_polling_resumed` | `is_active=True` geri geldi | INFO |
| `poll_cycle gateway=.. published=..` | Cycle tamamlandi, X mesaj yayinlandi | INFO |
| `jetstream_publisher_ready` | NATS connect OK | INFO |
| `jetstream_disconnected` | NATS bagi koptu — outbox dolmaya basliyor | WARNING |
| `jetstream_reconnected` | NATS bagi geri geldi, outbox drenaj baslar | INFO |
| `outbox_drained sent=N` | Retrier mesaj drenaj etti | INFO |
| `outbox_backoff` | Retrier exponential backoff'a girdi | WARNING |
| `outbox_dead_letter` | Mesaj poison kabul edildi, manuel inceleme | ERROR |
| `yadnp3_device_recovered` | Cihaz haberlesmesi geri geldi | INFO |
| `yadnp3_device_stale` | Link acik ama frame gelmiyor | WARNING |
| `manual_refresh_all_requested` | `/refresh-all` tetiklendi | INFO |
| `signal_received signum=..` | SIGINT/SIGTERM/SIGBREAK alindi, shutdown | INFO |

## 4. Sik sorunlar

### "401 Invalid gateway token" + `config_auth_error`

```
config_auth_error gateway=GW-001 consecutive=3 error=config request returned 401: ...
```

**Sebep:** `.env` icindeki `GATEWAY_TOKEN`, backend'deki `gateways.token` ile
uyumsuz.

**Cozum:**
1. Backend Engineering paneli -> Gateway Yonetimi -> `GW-001` -> token kopyala.
2. `.env`'de `GATEWAY_TOKEN=` satirini guncelle.
3. `Restart-Service EnerjiOneDnp3Gateway` (veya `docker compose restart`).

### "404 — Backendde gateway yok"

```
config_404_error gateway=GW-001 — Backendde 'GW-001' kodlu gateway yok.
```

**Cozum:** Backend panelinden ayni `GATEWAY_CODE` + ayni `GATEWAY_TOKEN` ile
kayit ac.

### NATS baglanti yok / outbox dolmaya basladi

```
jetstream_disconnected url=nats://nats.local:4222
outbox_backoff sent=0 wait=15.00s current_cap=30s
```

**Sebep:** NATS server down, network, credentials, TLS handshake.

**Cozum:**
1. `nats-server` calisiyor mu? `nats stream ls`
2. Gateway'den `nc -zv nats.local 4222` (TCP reach)
3. `NATS_CREDENTIALS_PATH` dosyasi var mi ve `chmod 600`?
4. `NATS_TLS_CA_PATH` PEM gecerli mi? `openssl x509 -in nats-ca.pem -noout -text`

Outbox `pending` sayisi 500K (default `OUTBOX_MAX_PENDING`) yaklasınca
publisher disk-full circuit-breaker tetikler ve `/health` `unhealthy`
doner (HTTP 503). Mesaj kaybi YOK — NATS geri gelince retrier hizla bosaltir.

### Outbox `outbox_dead_letter` mesajlari birikti

```bash
docker exec -it eg-gw-001 \
    sqlite3 /app/.gateway_state/outbox_GW-001.db \
    "SELECT message_id, last_error FROM outbox_dead_letter LIMIT 10;"
```

100 retry × 60sn (max backoff) sonrasi hala fail eden mesajlar buraya tasinir.
Genelde payload-spesifik sorun (broker subject mismatch, ACL deny). `last_error`
icerigi inceleyin; gerekirse `DELETE FROM outbox_dead_letter` ile temizleyin.

### "Ayni GATEWAY_CODE icin baska bir proses zaten calisiyor"

```
SystemExit: Ayni GATEWAY_CODE='GW-001' icin baska bir proses zaten
calisiyor (lock: .gateway_state/instance_GW-001.lock).
```

**Sebep:** Multi-instance lock — ayni kodla 2. kopya acmaya calistiniz.

**Cozum:** Ya digerini durdurun, ya yeni `GATEWAY_CODE` + `GATEWAY_STATE_DIR` +
`WORKER_HEALTH_PORT` ile baslatip backend'de ayri gateway kaydi acin.

### `is_active=False`, yayin durmus

Operator dashboard'dan gateway'i enable'layin. Bir sonraki `CONFIG_REFRESH_SEC`
cevriminde (default 30sn) yayina geri doner.

### Production validator boot'ta SystemExit

Saha cikisinda yaygin hatalar:

| Hata | Cozum |
| --- | --- |
| `GATEWAY_TOKEN placeholder degerle baslayamaz` | `.env.example`'dan kopyaladiniz ama token degistirmediniz. >=32 char rastgele uretin. |
| `BACKEND_API_URL public host icin https:// olmali` | Caddy/LE ile TLS kurun veya `GATEWAY_INSECURE_ALLOW_PLAINTEXT=true` (gecici). |
| `RABBITMQ_URL set edilemez` | Eski .env'den kalmis. Satiri silin. |
| `NATS_DUAL_PUBLISH_ENABLED=true olamaz` | DEPRECATED. Satiri silin. |
| `GATEWAY_REFRESH_TOKEN, GATEWAY_TOKEN ile AYNI` | Ikisini farkli uretin (`secrets.token_urlsafe(32)`). |

## 5. Operator endpoints

### `POST /refresh-all` — tum cihazlara anlik integrity poll

Backend operator panelindeki "Tum cihazlari yenile" butonu icin. Bearer auth:

```bash
curl -X POST http://gateway-host:8020/refresh-all \
    -H "Authorization: Bearer $GATEWAY_REFRESH_TOKEN"
```

Yanit:
```json
{"ok": true, "requested": 100, "total_devices": 100}
```

`GATEWAY_REFRESH_TOKEN` bos ise endpoint 503 doner (tamamen devre disi).

### `GET /info` ve `/metrics` — Bearer auth

`/health` minimum status doner; detayli metrikler (outbox sayisi, broker
counters, config_version, cycle metrics) `/info` ve `/metrics` uzerinde.
Ayni `GATEWAY_REFRESH_TOKEN` ile Bearer auth.

## 6. Shutdown sırasi

Graceful shutdown (`SIGINT` / `SIGTERM` / Windows `SIGBREAK` NSSM stop):

1. `stop_event.set()` — config refresh thread + poller cycle araliklarini kirar
2. Reader (`reader.close()`) — DNP3 master/channel kapanir, TCP RST
3. OutboxRetrier — bg thread durur, in-flight publish tamamlanir (timeout 3sn)
4. JetStream publisher — drain + connection kapanir (timeout 2sn cap)
5. Health HTTP server — yeni baglanti reddi, in-flight bekleme
6. Outbox pending + dead-letter sayim raporu logla
7. Instance lock dosyasi OS tarafindan otomatik release edilir

Toplam tipik shutdown: <8 saniye.
