# Docker ile Coklu Gateway Yonetimi

Bu dokuman EnerjiOne DNP3 Gateway'in Docker container'i olarak nasil kurulup
ayni host uzerinde N tane instance halinde yonetildigini anlatir. Hedef:
**bir Linux sunucusunda 5-10 farkli sahaya bagi gateway'leri tek docker engine
altinda calistirmak**.

## Mimari ozeti

```
+-----------------+     +-----------------+
|  Frontend (UI)  | --> |  backend-api    |
|  Yeni Gateway   |     |  POST /gateways |
+-----------------+     +-----------------+
                              |
                              v
+-----------------+     +-----------------------------+
|  Gateway YAML   | <-- |  Backend renderlar:         |
|  gw-001.yml     |     |  - GATEWAY_CODE/TOKEN       |
+-----------------+     |  - BACKEND_URL/NATS_URL     |
        |               +-----------------------------+
        | docker compose -f gw-001.yml up -d
        v
+----------------------+
| Linux sunucu         |
|  +----------------+  |   - container 1: GW-001  port 8020
|  | docker engine  |  |   - container 2: GW-002  port 8021
|  +----------------+  |   - container N: GW-NNN  port 80NN
+----------------------+
       outbound DNP3
       tum sahalara
```

Her container:

- **Kendi `.env` ile**: GATEWAY_CODE, GATEWAY_TOKEN, BACKEND_API_URL, NATS_URL
- **Sabit container portu**: 8020 (health/metrics)
- **Farkli host portu**: 8020, 8021, 8022, ...
- **Persistent volume**: `eg-gw-<code>-state` — instance_id + outbox SQLite
  + multi-instance lock dosyasi
- **Outbound TCP**: 100+ cihaza kadar DNP3 baglanti

## 1. Image

Image GitHub Container Registry'de yayinlanir:

```
ghcr.io/enerjione/enerjione-grid-dnp3-gateway:1.13.0    # surume kilitli (URETIM)
ghcr.io/enerjione/enerjione-grid-dnp3-gateway:main      # main'in son hali (DENEME)
```

Production deploy'larda **semver tag** kullanin (`:1.13.0`). Etiket politikasi
(`.github/workflows/release-image.yml`):

| Trigger             | Etiketler                        | Kullanim          |
|---------------------|----------------------------------|-------------------|
| `main` push         | `:main`, `:sha-<short>`          | deneme / CI       |
| `git tag v1.13.0`   | `:1.13.0`, `:1.13`, `:latest`    | **uretim**        |

> **`:latest` artik main push'unda GUNCELLENMEZ.** Eskiden her main commit'i
> `:latest`'i eziyordu ve test kapisi da yoktu; `docker compose pull` yapan
> operator test edilmemis bir imaji uretime aliyordu. Artik `:latest` yalnizca
> bilincli bir surum tag'iyle olusur ve her surumun kendi semver etiketi
> bulundugu icin rollback mumkundur.

Hicbir imaj testler gecmeden yayinlanmaz: `release-image.yml` icindeki
`needs: ci` adimi `ruff` + `pytest` (Linux ve Windows) kapisini calistirir.

Mimari: `linux/amd64`. yadnp3 (OpenDNP3 native) ile derlenir, Group 110 string
sinyaller dahil tam DNP3 destegi. (arm64 yok — yadnp3 PyPI'de arm64 wheel
saglamiyor.)

### Kaynaktan build (gelistirme)

```bash
git clone https://github.com/enerjione/enerjione-grid-dnp3-gateway.git
cd enerjione-grid-dnp3-gateway

docker build \
    --build-arg DNP3_LIBRARY=yadnp3 \
    -t ghcr.io/enerjione/enerjione-grid-dnp3-gateway:dev .
```

## 2. Yeni gateway ekle (frontend akisi)

Operator/installer panelinde "Yeni Gateway Ekle":

1. Frontend `POST /api/v1/gateways` ile kayit acar; backend rastgele 48 karakter
   token uretir.
2. Frontend `GET /api/v1/gateways/<code>/docker-compose?...` ile YAML indirir.
   Sorgu parametreleri:
   - `backend_url` (zorunlu): Gateway'in backend URL'i (orn.
     `https://api.enerjione.local/api/v1`).
   - `nats_url` (zorunlu): NATS JetStream URL (orn. `nats://nats.local:4222`).
   - `host_port` (varsayilan 8020): Bu instance icin host portu.
   - `image` (varsayilan: `VERSION` dosyasindan EXACT semver turetilir).
   - `app_environment` (varsayilan `production`).
3. Inen dosya `gw-<code>.yml`.
4. Sunucuya kopyalanir + `docker compose -f gw-<code>.yml up -d`.

### Manuel renderleme (frontend olmadan)

Gateway repo'sunda CLI:

```bash
python scripts/render_compose.py \
    --code GW-002 \
    --token "$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
    --name "Saha B" \
    --backend-url https://api.enerjione.local/api/v1 \
    --nats-url nats://nats.local:4222 \
    --host-port 8021 \
    --install-mode remote \
    --output ./gateways/gw-002.yml
```

CLI `--token` verilmezse rastgele uretip stderr'a yazar; bu degeri backend
veritabanina ayni kod altinda eklemek operator sorumlulugundadir.

**`--install-mode` ZORUNLU** (`local` | `remote`). Varsayilani yoktur, bilerek:
bu deger NATS erisilemedigi anda ne olacagini belirler ve yanlis secim
yalnizca ARIZA aninda gorunur.

* `local` — gateway backend ile **ayni makinede**. NATS zorunlu, HTTP yedegi
  **YOK**: ayni makinede NATS'a erisememek bir yapilandirma hatasidir ve
  sessizce HTTP'ye dusmek onu gizler.
* `remote` — **uzak saha** kurulumu. NATS birincil, erisilemezse HTTP yedegi.

**`--image` verilmezse `VERSION` dosyasindan EXACT semver turetilir**
(orn. `ghcr.io/enerjione/enerjione-grid-dnp3-gateway:1.11.4`). Uretim
ciktisi bilerek `:latest`e **baglanmaz**: `:latest` ile dosyaya bakarak
"hangi surum kurulu" sorusu cevaplanamaz ve siradan bir `docker compose pull`
gateway'i sessizce baska bir surume gecirebilir. (`:latest` etiketi YAYINDA
basilmaya devam eder; degisen yalnizca tuketim tarafi.)

Daha da kati bir pin isteniyorsa `--image` ile digest verilebilir:

```bash
    --image ghcr.io/enerjione/enerjione-grid-dnp3-gateway@sha256:<digest>
```

## 3. Calistirma

Ayni host'ta 5 gateway:

```bash
gateways/
  gw-001.yml
  gw-002.yml
  gw-003.yml
  gw-004.yml
  gw-005.yml

for f in gateways/*.yml; do
    docker compose -f "$f" up -d
done
```

Liste:

```bash
docker ps --filter "label=org.opencontainers.image.title=enerjione-dnp3-gateway"
```

Tek bir gateway'i durdur / yeniden baslat:

```bash
docker compose -f gateways/gw-001.yml stop
docker compose -f gateways/gw-001.yml restart
docker compose -f gateways/gw-001.yml down            # container siler, volume kalir
docker compose -f gateways/gw-001.yml down -v         # volume da siler (DIKKAT)
```

Logs:

```bash
docker logs -f eg-gw-001
docker compose -f gateways/gw-001.yml logs -f
```

Health endpoint:

```bash
curl http://127.0.0.1:8020/health   # gw-001
curl http://127.0.0.1:8021/health   # gw-002
```

## 4. Networking

### NATS ve backend ayni host'ta ise

Compose `networks: enerjione` external degil — gateway compose kendi ag'ini
olusturur. NATS ve backend baska bir compose project'inde ise:

```bash
docker network create enerjione

# compose'da external: true olarak guncelle
```

### NATS uzakta ise

`NATS_URL=nats://user:pass@nats.example.com:4222` (veya `tls://`). Ek ag
yapilandirmasi gerekmez. TLS icin `tls://` + `NATS_TLS_CA_PATH` mount.

### DNP3 outbound TCP

Container default bridge ag'inda remote IP'lere outbound baglanti kurabilir.
**Saha cihazlari container'larin Docker bridge'ine erisemez** ve gerek yok —
gateway master role outbound.

Saha cihazlarinin gateway IP'sini kabul etmesi icin sunucu IP'si firewall'da
whitelist'te olmali (DNP3 standart 20000/tcp).

## 5. Persistent state

Volume kaybolursa:

- **instance_id** yeniden uretilir -> backend log'unda "yeni baglanti" gorulur.
- **Outbox SQLite** kaybolur -> NATS'a gonderilemeyen mesajlar gider (acil
  durumda kabul edilebilir; tag-engine zaten idempotent).
- **Multi-instance lock dosyasi** silinir -> sorun yok, bir sonraki bootta
  yeniden olusturulur.

Yedekleme:

```bash
docker run --rm \
    -v eg-gw-001-state:/src \
    -v $(pwd)/backup:/dst \
    busybox tar czf /dst/gw-001-state-$(date +%F).tgz -C /src .
```

## 6. Upgrade / image yeni surum

```bash
# Tek tek (zero-downtime: cihaz polling 1sn, kabul edilebilir):
sed -i 's/enerjione-grid-dnp3-gateway:1.12.0/enerjione-grid-dnp3-gateway:1.13.0/' gateways/gw-001.yml
docker compose -f gateways/gw-001.yml up -d
```

Tum host'ta dongu:

```bash
for f in gateways/*.yml; do
    docker compose -f "$f" up -d
    sleep 5   # bir gateway recover etmeden digeri restart'a girmesin
done
```

## 7. Tipik kurulum boyutlari

| Sahalar | Cihaz dagilimi | Container sayisi | RAM/proses |
|---------|----------------|------------------|------------|
| Tek site | 100 cihaz | 1 container | ~150MB |
| 2-3 site | 600 cihaz | 3-6 container (100 cihaz/ea.) | ~150MB each |
| Coklu yedeklilik | 600 cihaz + standby | 3+3 farkli host | ayni |

100 cihaz/gateway senaryosunda: `MAX_PARALLEL_DEVICES=100`, paralel okumayla
cycle suresi <1sn. yadnp3 manager 4 IO thread (default heuristic).

## 8. Sorun giderme

### Container baslarken "GATEWAY_CODE bos olamaz"

`.env` dosyasi compose'a yuklenmemis. Compose `environment:` blogu render
edilmeden kullanildi demek. `gw-001.yml`'i kontrol edin.

### Health 8020 portu cevap vermiyor

```bash
docker exec -it eg-gw-001 curl http://127.0.0.1:8020/health
docker logs eg-gw-001 --tail 50
```

Container ic erisilebiliyor, host'tan erisilemiyor: `ports:` mapping bozuk.
`127.0.0.1:8020:8020` sadece localhost; uzak erisim icin `0.0.0.0:8020:8020`
veya reverse proxy.

### Backend 401 dönuyor

`GATEWAY_TOKEN` backend DB'deki `gateways.token` ile birebir ayni olmali.
Compose icindeki tirnak/escape karakterleri sorun cikarabilir; render edilmis
dosyayi acip token'i kontrol edin.

### Outbox surekli buyuyor

```bash
docker exec -it eg-gw-001 \
    sqlite3 /app/.gateway_state/outbox_GW-001.db \
    "SELECT COUNT(*), MAX(retry_count) FROM outbox; SELECT COUNT(*) FROM outbox_dead_letter;"
```

NATS server kapali ya da URL/credentials yanlis. `NATS_URL` + creds duzelt +
container restart -> retrier 2 saniye icinde drenaj baslar. Outbox 500K'a
yaklasırsa /health unhealthy doner (HTTP 503).

### "Ayni GATEWAY_CODE icin baska bir proses zaten calisiyor"

Multi-instance lock — volume eskimisken ayni kodla 2. container kalkti.
Onceki container'i down + volume'u ya temizleyin ya yeni kodla baslatin.
