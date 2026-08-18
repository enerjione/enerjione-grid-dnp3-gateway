# Guvenlik ve coklu gateway dagilimi

## Kimlik modeli (ozet)

| Bilesen | Amac | Nerede tutulur | Hassasiyet |
| --- | --- | --- | --- |
| `GATEWAY_CODE` | Is mantigi kimligi; backend `gateways.code` | `.env` / secret store | Sir degil |
| `GATEWAY_TOKEN` | Backend config endpoint'ine erisim (X-Gateway-Token) | `.env` / vault / **asla** git | **Sir** |
| `GATEWAY_REFRESH_TOKEN` | `POST /refresh-all`, `/info`, `/metrics` Bearer auth | `.env` / vault | **Sir** |
| `GATEWAY_COMMAND_TOKEN` | `POST /operate` — **fiziksel kesici manevrasi** Bearer auth | `.env` / vault | **Sir (en kritik)** |
| `GATEWAY_INSTANCE_ID` (opsiyonel) | Coklu kopya tespiti, log korelasyonu | Bos ise `GATEWAY_STATE_DIR/instance_{kod}.id` | Bilgi |
| `APP_ENVIRONMENT` | `development` / `staging` / `production` validator sertligi | `.env` | Sir degil |
| `NATS_CREDENTIALS_PATH` (legacy) | NATS NKEY/JWT credentials dosya yolu | `.env` (dosya icerigi sir) | **Sir** |

Production validator uc token'in de **birbirinden farkli** olmasini zorlar ve
placeholder degerleri (`change-me`, `test`, ...) reddeder. Gerekce: config
okuma yetkisi ile kesici acma yetkisi ayni sir olamaz — telemetri icin
paylasilan bir token, sahada enerji kesme yetkisine donusurdu.

Uc token da bos birakilabilir; o zaman ilgili endpoint **503** doner
(kapali). Normal NAT-arkasi kurulumda `GATEWAY_COMMAND_TOKEN` gereksizdir —
komutlar `GET /pending` pull kanaliyla gelir.

Sahada **N gateway** = **N ayri `gateways` satiri** + cihazlar `device.gateway_code`
ile bolusturulur. Ayni `GATEWAY_CODE` + farkli `GATEWAY_TOKEN` calismaz (backend
token satira baglidir). Ayni token'i iki farkli `GATEWAY_CODE` arasinda paylasmayin;
bir token sizilirse sadece o gateway'in config'ine ulasilir.

**Multi-instance lock:** Gateway bootta `GATEWAY_STATE_DIR/instance_{CODE}.lock`
dosyasi uzerinde exclusive lock alir (`msvcrt.locking` Windows / `fcntl.flock`
POSIX). Ayni `GATEWAY_CODE` ile ikinci kopya acmaya calisirsa SystemExit ile
reddedilir — outbox SQLite corrupt + duplicate publish riskini onler.

## HTTP: backend ile sozlesme

Her config isteginde su basliklar gider (bkz. `src/dnp3_gateway/auth/headers.py`):

- `X-Gateway-Token` — gizli, sadece bu header'da gider, URL'de tasınmaz
- `X-Gateway-Code` — path'teki kod ile ayni olmali (backend uyumsuzlukta 400 doner;
  yanlis proxy/template render kurulumlarini erken yakalar)
- `X-Gateway-Instance-Id` — observability; backend audit/log korelasyonu icin
- `X-Request-Id` — istek bazli UUID (Kibana/Loki/Grafana korelasyonu)
- `User-Agent: EnerjiOne-Dnp3Gateway/{versiyon} (env=...)`

## Backend yanit ozgunlugu — ZORUNLU HMAC

**v1.10.0'dan beri varsayilan `REQUIRE_BACKEND_RESPONSE_SIGNATURE=true`.**
Once opsiyoneldi; su an uretim varsayilani ZORUNLU ve fail-closed.

Kapsam: `GET /config` ve `GET /pending` **200** yanitlari. Header:
`X-Config-Signature: <hex sha256>`.

```
expected = HMAC-SHA256(imza_anahtari, body_bytes).hexdigest()
hmac.compare_digest(sig_header, expected)
```

### Karar tablosu (varsayilan yapilandirma)

| Yanit | Sonuc |
|---|---|
| Gecerli imza | **Kabul** — islenmeye devam |
| Imza header'i YOK | **Fail-closed** — reddedilir |
| Imza bozuk/hatali formatta | **Fail-closed** — reddedilir |
| Imza eslesmiyor | **Fail-closed** — reddedilir |

Reddedilen yanitta **JSON parse edilmez, config uygulanmaz, komut dagitimi
yapilmaz.** Yani reddedilen bir `/pending` yaniti icin `PendingPoll` bile
uretilmez: komut kuyruga girmez, nonce uygulanmaz, `ledger.start_dispatch`
ve `operate_device` cagrilmaz. `GatewayConfigError` yukselir ve refresh
thread exponential backoff'a duser.

### Imza anahtari

Iki duzlem AYNI anahtari kullanmak ZORUNDA DEGIL:

| Uc | Imza anahtari |
|---|---|
| `GET /config` | Gateway kimligi: `GATEWAY_TOKEN` |
| `GET /pending`, komut uclari | `GATEWAY_COMMAND_DELIVERY_TOKEN` **doluysa YALNIZCA o**; **bos ise** `GATEWAY_TOKEN` |

> **F5 TAMAMLANDI.** Backend F5A ve gateway F5B sahaya cikti, saha kabulu
> yapildi. `GATEWAY_COMMAND_DELIVERY_TOKEN` artik bir gecis alani degil,
> kuyruklanmis komut duzleminin kalici credential'idir ve **onerilen
> yapilandirmadir**.
>
> **Kati ayrim korunur:** dolu ise `/pending` yaniti YALNIZCA bu anahtarla
> dogrulanir, `GATEWAY_TOKEN`a geri DUSULMEZ.
>
> **Geriye donuk uyumluluk:** bos birakilirsa komut uclari `GATEWAY_TOKEN`
> kullanir (v1.10 davranisi). Bu yalnizca henuz provision edilmemis
> kurulumlarin calismaya devam etmesi icindir; yeni kurulumlarda ayri
> credential verilmelidir.

### Rollback

`REQUIRE_BACKEND_RESPONSE_SIGNATURE=false` **yalnizca** imza gondermeyen ESKI
bir backend'e kontrollu, GECICI rollback icindir ve boot'ta gorunur uyari
uretir.

**Bu modda bile bypass YOKTUR:** imza GELIYOR ama gecersizse (bozuk ya da
eslesmiyor) yanit yine **reddedilir**. Bayrak yalnizca "imza header'i hic
yok" durumunu tolere eder; "imza var ama yanlis" her zaman fail-closed'dir.
Aksi halde saldirgan bozuk bir imza gondererek dogrulamayi atlatabilirdi.

## TLS

- **Production:** `BACKEND_API_URL=https://...` zorunlu (public host icin
  validator zorlar); private/loopback ag (RFC1918, 127.x, *.local, *.lan,
  *.internal, localhost) icin http:// kabul.
- **NATS (legacy):** `NATS_URL=tls://...` zorunlu (public host icin); private
  host icin `nats://` kabul.
- **Custom CA:** `BACKEND_API_CA_PATH` ve `NATS_TLS_CA_PATH` ile PEM bundle.
- **Bilincli opt-out:** TLS kurulana kadar saha icin
  `GATEWAY_INSECURE_ALLOW_PLAINTEXT=true` (boot'ta loud WARN log).

## Token omru ve rotasyonu

1. Backend Installer rolu ile yeni `token` uretip kaydet (veya ayni endpoint'te
   guncelleme).
2. Ilgili sunucuda `.env` guncelle (veya secret manager deploy).
3. **Servisi yeniden baslat** — process env'i bir kere okunur. Kisa kesinti:
   yeni `CONFIG_REFRESH` cycle'ina kadar eski proses 401 alir; loglarda
   `config_auth_error` etiketini izleyin.
4. Eski token'i backend kayitlarindan silin (audit trail).

## Operator HTTP yuzeyi (health server)

Gateway'in dinledigi tek port `WORKER_HEALTH_PORT`'tur. Yuzey kasitli olarak
kucuk tutulmustur:

| Endpoint | Auth | Rate limit |
| --- | --- | --- |
| `GET /health`, `/healthz` | Yok (probe) | 120 req/dk/IP |
| `GET /info`, `/metrics` | `GATEWAY_REFRESH_TOKEN` Bearer | 120 req/dk/IP |
| `POST /refresh-all` | `GATEWAY_REFRESH_TOKEN` Bearer | 10 req/dk/IP |
| `POST /operate` | `GATEWAY_COMMAND_TOKEN` Bearer | 10 req/dk/IP |

- Token karsilastirmasi `hmac.compare_digest` ile **timing-safe**.
- Rate limit anahtari istemci IP'sidir. `X-Forwarded-For` **yalnizca**
  `HEALTH_TRUSTED_PROXIES` (CIDR listesi) icindeki bir adresten gelen istekte
  okunur; liste bos ise XFF tamamen yok sayilir. Aksi halde saldirgan basliga
  rastgele IP yazip rate-limit'i bypass ederdi.
- `/health` auth'suzdur ama **kirpilmis** ozet doner; cihaz listesi, config
  detayi, sayaclar yalnizca auth'lu `/info` ve `/metrics` uzerindedir.

`/operate` bir **fiziksel manevra** endpoint'idir. Kullanilacaksa:

- `GATEWAY_COMMAND_TOKEN` ayri ve guclu olmali (validator zorlar),
- istekte `command_id` gonderilmeli — idempotency anahtaridir; retry'da ayni
  kesicinin iki kez surulmesini onler,
- port saha LAN'i disina acilmamali (backend'e dogrudan erisim gerekmiyorsa
  token'i bos birakip endpoint'i tamamen kapatin).

## Hata mesajlarinda sir sizmasi

Konfigurasyon hatalari `format_config_error()`'dan gecirilir. Pydantic'in
ham hata metni `input_value={...}` kuyrugunda **tum ayar sozlugunu**
(`GATEWAY_TOKEN` dahil) tasir; bu metin stdout'a, NSSM log dosyasina ve
`docker logs` ciktisina duserdi. Kuyruk kirpilir, proses `EX_CONFIG` (78) ile
cikar. `tests/test_production_blockers.py` bu davranisi kilitler.

## NATS auth ve TLS (STANDART yol — `TELEMETRY_PUBLISHER=nats`)

> 1.1.0+ itibariyla varsayilan telemetri yolu NATS JetStream'dir; bu bolum
> her kurulum icin gecerlidir. HTTP ingest yalnizca bilincli rollback'tir.

Production'da NATS server **deny-all default** + per-user subject ACL ile
kurulur. Her gateway icin ayri NKEY/JWT credentials:

```bash
# nsc ile account + user uretimi (operator host'unda, tek seferlik)
nsc add account ENERJIONE
nsc add user --account ENERJIONE --name gateway-GW001 \
    --allow-pub e1.telemetry.raw.GW-001 \
    --allow-sub _INBOX.>

# Uretilen .creds dosyasini gateway host'una guvenli kanaldan kopyala
scp gateway-GW001.creds gateway-host:/etc/enerjione/gateway-GW001.creds
chmod 600 /etc/enerjione/gateway-GW001.creds
```

`.env`:
```
NATS_URL=tls://nats.kurum.local:4222
NATS_CREDENTIALS_PATH=/etc/enerjione/gateway-GW001.creds
NATS_TLS_CA_PATH=/etc/enerjione/nats-ca.pem
```

ACL: her gateway sadece kendi subject'ine pub yetkilidir (`e1.telemetry.raw.GW-001`).
Bir gateway credentials'i leak olsa baska gateway'in subject'ine pub yapamaz.

## DNP3 cihaz IP allowlist

`DNP3_DEVICE_ALLOWED_SUBNETS` (CIDR listesi) production'da MUTLAKA set
edilmeli. Backend kompromize olsa bile gateway sadece bu subnet'lerdeki
IP'lere TCP 20000 baglantisi acar:

```
DNP3_DEVICE_ALLOWED_SUBNETS=192.168.10.0/24,10.0.5.0/24,172.16.20.0/24
```

Bu olmadan backend `169.254.169.254` (cloud metadata), `8.8.8.8`, internal
host'lara TCP scan yapma kapisi acik kalir. Hostname (FQDN) gelen cihazlar
allowlist aktifken reddedilir — gateway DNS cozmez, operator IP olarak
yapilandirmali.

## Log redaction

`src/dnp3_gateway/logging_setup.py` icindeki `_RedactionFilter` 3 katmanli
maskeleme yapar:

1. **Manuel registry:** `register_secret(token)` ile gateway_token,
   gateway_refresh_token ve NATS URL parolasi runtime'da kayda alinir —
   `***REDACTED***` ile yer degistirir.
2. **Broker URL regex:** `(nats|tls|amqp|amqps)://user:PASSWORD@host` deseni
   automatic yakalar — operator unutsa bile parola sizmaz.
3. **3rd-party logger seviye:** `nats`, `urllib3` WARNING'e cekildi —
   default INFO'da baglanti retry detaylari gurultu yapardi.

## Checklist: yeni sunucuya gateway

- [ ] Backend'de `gateways` kaydi: `code`, `token` (>=32 char rastgele).
- [ ] Cihazlar bu gateway'e `gateway_code` ile atanmis.
- [ ] Bu makinede `.env`: `GATEWAY_CODE` + guc token + `APP_ENVIRONMENT=production`.
- [ ] `WORKER_HEALTH_PORT` bu makine uzerinde benzersiz.
- [ ] `BACKEND_API_URL=https://...` (public host ise zorunlu).
- [ ] `DNP3_DEVICE_ALLOWED_SUBNETS` saha LAN'ina gore set edildi.
- [ ] `LOG_FILE_PATH` rotation icin set edildi (Windows NSSM kurulumda zorunlu).
- [ ] (Opsiyonel) `GATEWAY_REFRESH_TOKEN` operator paneli / metrik erisimi icin.
- [ ] (Opsiyonel) `GATEWAY_COMMAND_TOKEN` **yalnizca** dogrudan HTTP push
      kullanilacaksa; aksi halde bos birak (endpoint kapali kalir).
- [ ] Uc token birbirinden farkli (production validator zaten zorlar).
- [ ] `HEALTH_TRUSTED_PROXIES` — health portu bir reverse proxy arkasindaysa
      set edildi; degilse bos birakildi.
- [ ] `.env` dosyasi ACL kisitlandi (Windows: `icacls`, POSIX: `chmod 600`).
- [ ] Multi-instance lock dosyasi icin `GATEWAY_STATE_DIR` yazilabilir.
- [ ] (Legacy/rollback) NATS'a donulecekse: `NATS_URL` +
      `NATS_CREDENTIALS_PATH` + (public ise) `NATS_TLS_CA_PATH`.
