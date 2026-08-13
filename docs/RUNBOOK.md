# Runbook — EnerjiOne DNP3 Gateway

Gunluk operasyon kontrol listesi + incident response.

## 1. Baslatma

> **URETIM = DOCKER.** Saha kurulumlari container ile yapilir; asagidaki
> Docker bolumu tek gecerli uretim yoludur. Windows/venv yolu YALNIZCA
> gelistirme icindir ve sahada kullanilmaz.

### Docker (URETIM)

```bash
docker compose -f gateways/gw-001.yml up -d
docker compose -f gateways/gw-001.yml logs -f
```

Compose sablonu: `docker/compose.template.yml`. Backend'in "Yeni gateway ekle"
akisi bu sablonu render eder.

**Imaj etiketleri** (bkz. `.github/workflows/release-image.yml`):

| Etiket | Ne zaman olusur | Kullanim |
| --- | --- | --- |
| `:latest` | YALNIZCA `v*.*.*` tag'i atildiginda | Uretim |
| `:<surum>` (or. `:0.6.0`) | Ayni tag ile | Uretim — **rollback icin bunu kullanin** |
| `:main` / `:sha-<kisa>` | main'e her push'ta | Deneme/CI; **sahaya almayin** |

`:latest` bilincli bir surum etiketi olmadan OLUSMAZ. Eskiden main'e giden her
push `:latest`i eziyordu ve sahada `docker compose pull` yapan operator test
edilmemis bir imaji uretime aliyordu.

**Zorunlu iki sey:**

1. `GATEWAY_CODE` + `GATEWAY_TOKEN` env — verilmezse container **cikis kodu
   64** ile durur (entrypoint kimlik dogrulamasi). Kimliksiz bir gateway'in
   sessizce ayaga kalkmasi istenmez.
2. `GATEWAY_STATE_DIR` (varsayilan `/app/.gateway_state`) bir **volume**
   olmali. Olmazsa restart'ta outbox kuyrugu, komut defteri ve instance_id
   kaybolur; log'da `UYARI: ... bir volume degil` satirini gorursunuz.

**Kurulum dogrulama:**

```bash
docker compose -f gateways/gw-001.yml ps            # healthy mi
curl -s http://127.0.0.1:8020/health | jq .status   # "ok" bekleniyor
```

**Yukseltme:**

```bash
docker compose -f gateways/gw-001.yml pull
docker compose -f gateways/gw-001.yml up -d
```

Compose dosyasindaki etiketi yeni surume cekmeyi unutmayin — `:latest` ile
calisiyorsaniz `pull` zaten yeni surumu getirir, ama **hangi surumun
kostugunu** `/health` `version` alanindan dogrulayin.

**Rollback:** compose'daki etiketi onceki semver'a cevirip `up -d`. SQLite
semasi ileri surumden geri donerse gateway acikca hata verir (sessizce yanlis
calismaz) — bkz. bolum 4 "Surum yukseltme".

### Windows / venv (YALNIZCA GELISTIRME)

> Sahada kullanilmaz. Bu yol gelistirme ve hizli deneme icindir.

```powershell
cd "C:\...\EnerjiOne Grid DNP3 Gateway"
./scripts/install.ps1                # ilk kez ya da -Recreate
./scripts/run_gateway.ps1            # .env uzerinden
```

`install.ps1` sunlari yapar: venv + `requirements.txt` + **yadnp3**
(`requirements-dnp3.txt` pininden) + **paketin kendisi** (`pip install -e .`)
ve sonunda kurulumu **dogrular**.

```powershell
.\.venv\Scripts\python.exe -c "import dnp3_gateway; print(dnp3_gateway.__version__)"
.\.venv\Scripts\python.exe -m dnp3_gateway --version
```

Ikisi de calismiyorsa once bunu duzeltin.

**Ayni makinede coklu gateway (gelistirme):**

```powershell
./scripts/new_gateway.ps1 -Code GW-002 -HealthPort 8021
py -m dnp3_gateway --env-file .env.GW-002
```

Her instance kendi `instance_GW-002.lock` dosyasi alir; ayni kod ile ikinci
proses SystemExit eder.

**Servis acilmiyorsa** (`service-stdout.log` / container log'u):

| Gorulen | Anlami |
| --- | --- |
| `KONFIGURASYON HATASI` cercevesi | `.env`'de gecersiz ayar. Mesaj hangi ayar oldugunu soyler. Cikis kodu **78**. |
| `[entrypoint] HATA: GATEWAY_CODE ve GATEWAY_TOKEN` | Container'a kimlik verilmemis. Cikis kodu **64**. |
| `ModuleNotFoundError: dnp3_gateway` | Paket kurulmamis (venv yolu). `install.ps1`'i tekrar calistirin. |
| `Yadnp3AdapterError` | yadnp3 wheel'i yok. `GATEWAY_MODE=dnp3` icin sart. Docker imajinda hazir gelir. |
| `Ayni GATEWAY_CODE icin baska bir proses` | Ayni kodla ikinci instance. |
| `Missing closing '}'` (install.ps1) | Scriptte ASCII-disi karakter var; Windows PowerShell 5.1 BOM'suz dosyalari cp1252 okur. `tests/test_powershell_scripts.py` bunu kilitliyor. |

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
| `outbox.outbox_pending` | <100 saglikli | >>1000 ise backend'e yetisemiyor. |
| `outbox.broker_ready` | `true` | False ise telemetri hedefine ulasilamiyor. |
| `devices.lost` | `0` | >0 ise asagidaki cihaz sagligi bolumune bak. |
| `threads.alive.*` | hepsi `true` | Biri `false` ise **derhal mudahale** (asagida). |
| `metrics.seconds_since_last_cycle` | < 5 x poll araligi | Buyukse poll dongusu donmus. |
| `metrics.signals_outboxed_total` | artmiyor | Artiyorsa telemetri backend'e DEGIL diske gidiyor. |
| `disk.level` | `ok` | `low`/`critical` ise disk temizligi gerek. |
| `clock.safe_for_time_sync` | `true` | False ise sunucu saati sapmis; NTP kontrol. |

Detayli operasyonel metrikler `/info` ve `/metrics` endpoint'leri uzerinden
**Bearer auth ile** (`GATEWAY_REFRESH_TOKEN`). Auth-suz `/health` sadece minimum
status + issues raporlar (recon malzemesi sizmasin).

> `GATEWAY_REFRESH_TOKEN` bos ise `/info` ve `/metrics` **503 doner** — bu
> bilincli bir kapatmadir. Metrik erisimi istiyorsaniz `.env`'de bu token'i
> set edin.

### 2.1 `issues[]` sorun kodlari — ne anlama gelir, ne yapilir

`/health` govdesindeki `issues` dizisi arizanin NE oldugunu soyler. Asagidaki
tablo sahadaki tek basvuru kaynagidir; bir kod burada yoksa surum notlarina
bakin.

| Kod | Durum | Anlami | Ilk mudahale |
| --- | --- | --- | --- |
| `all_devices_comm_lost` | **unhealthy** | TUM cihazlar kopuk. Saha switch'i / VPN / besleme. | Saha agini kontrol et. Tek bir cihaza `ping` + `telnet <ip> 20000`. Gateway degil, ag arizasi olmasi cok olasi. |
| `majority_devices_comm_lost` | degraded | Cihazlarin >=%50'si kopuk. | Kopan cihazlarin ortak noktasini bul (ayni switch? ayni fider? ayni model?). `/metrics` cihaz basina durum verir. |
| `some_devices_comm_lost` | degraded | Bir kismi kopuk. | Tek tek cihaz arizasi. Ilgili RTU'ya fiziksel erisim gerekebilir. |
| `poll_loop_stalled` | **unhealthy** | Ana dongu >5x poll araligidir tur atmadi. | Log'da son `poll_cycle` satirina bak. Cozulmezse **servisi yeniden baslat**; bu durum kendiliginden duzelmez. |
| `thread_dead:<ad>` | **unhealthy** | Arkaplan thread'i olmus. `outbox-retrier` -> telemetri teslimi durdu. `command-poll` -> **SCADA komut kanali durdu**. `config-refresh` -> config guncellenmiyor. | **Servisi yeniden baslat.** Once log'da o thread'in son hatasini al (`outbox_retrier_cycle_failed` vb.) ve sakla. |
| `outbox_full` | **unhealthy** | Outbox limitte; poller yeni okuma yayinlamiyor. | Backend ingest'i kontrol et. Duzelince kuyruk kendiliginden bosalir ve breaker acilir (restart GEREKMEZ). |
| `outbox_near_capacity` | degraded | Outbox %80 dolu. | Backend yavas/erisilemez. Su an veri kaybi YOK ama limite yaklasiyor. |
| `dead_letter_messages_present` | degraded | Teslim edilemeyen mesaj var. | `outbox_dead_letter` bolumune bak (asagida). Kalici bir sema/kabul hatasina isaret eder. |
| `config_cache_stale` | degraded | Config `CONFIG_CACHE_MAX_AGE_HOURS`'tan eski. | Backend'e ulasilamiyor. Cihaz IP/sinyal listesi guncel olmayabilir. |
| `config_refresh_failing` | degraded | Son config cekimi basarisiz. | `config_auth_error` / `config_404_error` log'una bak. |
| `command_channel_failing` | degraded | `/pending` ~15sn'dir hata veriyor. | Backend restart penceresi olabilir; surerse asagi bak. |
| `command_channel_down` | **unhealthy** | `/pending` ~1dk+ hata veriyor. **SCADA komutlari gateway'e ULASMIYOR** ve `config_nonce` okunamadigi icin yeni cihazlar anlik gorulmuyor. | Backend `/pending` endpoint'ini kontrol et. `health_header_disabled` log'u varsa gateway saglik basligini birakip komut kanalini kurtarmistir — o zaman backend'de `gateway_health` tablosu/migration eksiktir. |
| `telemetry_backend_unreachable` | degraded | Telemetri hedefi (backend ingest) su an erisilemez. | Veri outbox'a yaziliyor, KAYIP YOK. `http_publisher_breaker_open` log'una ve backend ingest'e bak. Surerse outbox dolar ve unhealthy'ye cikar. |
| `no_poll_cycles_yet` | degraded | 60sn+ gecti, hic cycle calismadi. | `config.active` false mu? Cihaz sayisi 0 mi? |
| `disk_space_low` | degraded | State dizininde <1 GB. | Log/dead-letter temizligi. `.gateway_state` altini kontrol et. |
| `disk_space_critical` | **unhealthy** | <256 MB. Telemetri diske yazilamayabilir. | **Acil** temizlik. Dolan disk retrier'i oldurur ve log rotation'i bozar. |
| `state_db_quarantined` | degraded | Bozuk SQLite dosyasi karantinaya alindi, temiz DB ile devam ediliyor. | `quarantined_state_files` alanindaki `*.corrupt.*` dosyalari sakla. Icindeki birikmis telemetri KAYIP. Genelde ani guc kesintisi sonrasi. |
| `command_journal_reset` | degraded | **Komut defteri** bozuktu ve sifirlandi. Bu, telemetri kaybi DEGIL. | Bolum 4 → "`command_journal_reset` — komut defteri sifirlandi". Bekleyen komutlarin durumu operator ile **dogrulanmali**. |
| `clock_skew_detected` | degraded | Gateway saati backend'den >=2sn sapmis. | NTP/w32time servisini kontrol et. |
| `clock_skew_unsafe` | degraded | Sapma >=30sn. **Outstation'lara saat YAZILMIYOR.** | NTP'yi duzelt. Duzelene kadar cihaz saatleri senkronize edilmez (bilincli koruma). |

**Coklu sorun:** `issues` birden fazla kod tasiyabilir. Genel `status`, en agir
olanin seviyesidir.

### 2.2 Cihaz haberlesme durumu

```powershell
(Invoke-RestMethod http://127.0.0.1:8020/health).devices
# total / online / recovering / lost / unknown / oldest_frame_age_sec
```

| Durum | Anlami |
| --- | --- |
| `online` | DNP3 frame'leri taze geliyor. |
| `recovering` | Link acildi ama henuz frame gelmedi. 15sn icinde `online` olmali; olmazsa `lost`. |
| `lost` | Haberlesme yok. SCADA `comm_lost` goruyor. |
| `unknown` | Config'te tanimli ama adapter henuz master acmamis (ilk baglanti) **veya** sinyal profili eslesmedi. |

> `/health` cihaz KODU ve IP'si icermez (auth-suz endpoint, recon malzemesi
> vermiyoruz). Cihaz bazinda detay icin auth'lu `/metrics` kullanin.

**`unknown` uzun sure kaliyorsa:** cihazin `model`/`signal_profile` degeri
backend katalogunda ve `profiles/` altinda yoksa o cihaz **hic yoklanmaz**.
Log'da `signals_empty device=... profil=...` satirini arayin.

## 3. Log izleme

```bash
# URETIM (Docker)
docker compose -f gateways/gw-001.yml logs -f --tail 200
docker compose -f gateways/gw-001.yml logs --since 1h | grep -E "ERROR|WARNING"
```

```powershell
# Gelistirme (venv) — LOG_FILE_PATH set ise rotating dosya
Get-Content "$env:ProgramData\EnerjiOne\dnp3-gateway\GW-001.log" -Tail 200 -Wait
```

> Docker'da `LOG_FILE_PATH` **set etmeyin**: loglar stdout'a aksin ve
> `docker logs` / log driver toplasin. Dosyaya yazmak container icinde
> rotasyon ve disk yonetimi sorunu yaratir, ayrica `docker logs` bos kalir.

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
| `poll_cycle_failed consecutive=N` | Cycle govdesi hata verdi; dongu DEVAM ediyor | ERROR |
| `outbox_drained sent=N` | Retrier mesaj drenaj etti | INFO |
| `outbox_backoff` | Retrier exponential backoff'a girdi | WARNING |
| `outbox_dead_letter` | Mesaj poison kabul edildi, manuel inceleme | ERROR |
| `outbox_full_cleared pending=..` | Breaker kapandi, poller devam ediyor | INFO |
| `outbox_retrier_cycle_failed` | Retrier turu hata verdi; thread YASIYOR | ERROR |
| `outbox_dead_letter_pruned removed=N` | Retention ile eski dead-letter silindi | INFO |
| `http_publisher_breaker_open url=.. bekleme=..` | Backend ingest erisilemiyor; denemeler timeout beklemeden hizli hata verecek (telemetri outbox'a yaziliyor, **kayip yok**) | WARNING |
| `http_publisher_breaker_closed url=..` | Backend ingest tekrar erisilebilir; outbox bosalmaya baslar | INFO |
| `poll_pool_starved baslamayan=.. due=.. workers=..` | **Worker havuzu doldu; bu cihazlara HIC istek gonderilemedi.** Cihaz arizasi degil, kapasite sorunu. Cihazlar okundu SAYILMADI | ERROR |
| `poll_device_per_device_timeout device=..` | Cihaz basladi ama sure asti; iptal + `mark_read` | WARNING |
| `manual_refresh_all_triggered ok=.. total=..` | `/refresh-all` tetiklendi | INFO |
| `signal_received signum=..` | SIGINT/SIGTERM/SIGBREAK alindi, shutdown | INFO |

**Cihaz / DNP3 (0.5.x)**

| Etiket | Anlam | Seviye |
| --- | --- | --- |
| `yadnp3_master_enabled device=..` | Cihaz icin DNP3 oturumu acildi | INFO |
| `yadnp3_master_rebuild device=.. eski=.. yeni=..` | Cihazin IP/port/DNP3 adresi degisti, oturum yeniden kuruldu | WARNING |
| `yadnp3_device_stale device=..` | Link acik ama frame gelmiyor | WARNING |
| `yadnp3_device_recovery_timeout` | Grace doldu, cihaz `lost` | WARNING |
| `yadnp3_device_relink device=..` | Kalici `lost` durumundan cikildi (link acik + veri taze) | INFO |
| `yadnp3_device_recovered device=..` | Haberlesme geri geldi, tum degerler yeniden yayinlanacak | INFO |
| `dnp3_event_buffer_overflow device=..` | **Outstation olay tamponu doldu ve OLAYLARI DUSURDU** — telemetride aciklik olusmus olabilir | ERROR |
| `dnp3_device_restart device=..` | Outstation yeniden basladi | WARNING |
| `dnp3_need_time device=..` | Cihaz saat yazilmasini bekliyor | INFO |
| `dnp3_time_sync_suspended device=..` | Gateway saati guvenilmez; cihaza saat YAZILMIYOR | ERROR |
| `dnp3_non_finite_value device=.. signal=..` | Cihaz sayisal olmayan deger (NaN/Inf) raporladi; `quality=invalid` ile yayinlandi | WARNING |
| `dnp3_command_result device=.. ok=.. dnp3_status=..` | CROB sonucu (detayli DNP3 durumu) | INFO |
| `signals_builtin_used device=.. profil=..` | Backend bu model icin sinyal gondermedi, yerlesik harita kullanildi | INFO |
| `signals_empty device=.. profil=..` | **Bu model icin sinyal YOK — cihaz yoklanmayacak** | WARNING |
| `signals_profile_missing device=.. profil=..` | Profil katalogda yok; filo tek modelli oldugu icin duz listeye dusuldu | WARNING |
| `signals_profile_unknown device=.. profil=..` | **Profil katalogda yok ve filo cok modelli — cihaz yoklanmayacak.** Duz liste birden fazla modelin birlesimi oldugu icin kullanilmadi (yanlis `signal_key` riski). Backend katalogunda profili tanimlayin | ERROR |
| `yadnp3_comm_lost_announced device=..` | `comm_lost` yayini kalicilasti; sonraki cycle'lar `no_change` uretecek | INFO |

**Komut kanali**

| Etiket | Anlam | Seviye |
| --- | --- | --- |
| `pending_command_executed id=.. ok=..` | Komut cihaza gonderildi | INFO |
| `operate_requested device=.. control=..` | HTTP push komutu (`/operate`) | INFO |
| `operate_duplicate_suppressed command_id=.. kayitli_sonuc=..` | Ayni komut tekrar geldi, CROB TEKRARLANMADI. `kayitli_sonuc=yok` ise cevap `status:"pending"` doner — komut kabul edilmis, sonucu henuz bilinmiyor | INFO |
| `operate_ledger_unavailable device=..` | **Komut defterine erisilemedi; komut REDDEDILDI (503).** Tekrar-onleme garanti edilemedigi icin CROB gonderilmedi | ERROR |
| `operate_without_command_id device=..` | Idempotency anahtari gonderilmedi; istek tekrarlanirsa CROB DA TEKRARLANIR | WARNING |
| `command_ledger_reset path=..` | **Komut defteri bozuktu ve sifirlandi** — bekleyen komutlar dogrulanmali | ERROR |
| `command_result_delivery_failed` | Sonuc teslimi GECICI hata; tekrar denenecek | WARNING |
| `command_result_delivery_permanent` | Sonuc teslimi KALICI hata; kuyruk ilerletildi | WARNING |
| `command_result_dead_letter id=..` | **Sonuc backend'e teslim edilemedi.** Komut cihaza gonderilmis olabilir; ledger'da kayitli | ERROR |
| `command_poll_error` / `command_poll_recovered` | Komut kanali kesildi / geri geldi | WARNING / INFO |

**Kaynak ve altyapi**

| Etiket | Anlam | Seviye |
| --- | --- | --- |
| `disk_space_low` / `disk_space_critical` | State dizininde yer azaldi | WARNING / ERROR |
| `clock_skew_detected` / `clock_skew_unsafe` | Sunucu saati sapmis | WARNING / ERROR |
| `sqlite_migration_applied db=.. version=..` | Sema yukseltildi (surum gecisinde normal) | INFO |
| `sqlite_db_quarantined path=..` | Bozuk DB karantinaya alindi, temiz DB ile devam | ERROR |
| `dnp3_settings_ignored_by_yadnp3 ..` | Set edilen bazi DNP3 ayarlari aktif adapter'da ETKISIZ | WARNING |

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

### Cihaz kopuk kaldi / "manuel refresh atinca geliyor"

**1.6.1'den once bu gercek bir hataydi.** Cihaz `lost` ve link'i ACIK
gorunuyorken gateway ona hicbir sey sormuyordu; kilidi ancak operatorun
`/refresh-all`'u kiriyordu. 4G/GSM hatlarda kural olan senaryo: modem RRC
idle'a duser, TCP soketi FIN/RST uretmeden yari-acik kalir, opendnp3
`OnClose` gormez, outstation da veri gondermeyi keser — iki taraf da sessiz
kalir.

Artik gateway kendisi yokluyor. Durumu tek GET ile gorun:

```bash
curl -s http://127.0.0.1:8020/health | jq .devices.recovery
```

```json
{ "lost_probe_total": 12, "forced_relink_total": 2, "devices_probing": 1 }
```

| Alan | Anlami |
|---|---|
| `lost_probe_total` | Kopuk cihazlara gonderilen kendiliginden integrity poll sayisi |
| `forced_relink_total` | Yoklama sonuc vermeyince zorla yenilenen TCP oturumu sayisi |
| `devices_probing` | SU AN yoklanan (kopuk ama link'i acik) cihaz sayisi |

Ilgili log — yoklamalar DEBUG, yalnizca zorla yenileme olay uretir:

```
yadnp3_forced_relink device=d3 ip=10.0.0.5 — 3 integrity poll cevapsiz kaldi,
TCP oturumu zorla yeniden kuruluyor (yari-acik soket suphesi)
```

**Ayarlar:** `DNP3_LOST_PROBE_INTERVAL_SEC` (varsayilan 30) ve
`DNP3_LOST_RELINK_AFTER_PROBES` (varsayilan 3). Cok kararsiz 4G hatlarda
araligi kisaltabilirsiniz; her cihaz basina saatte `3600/aralik` istek eder.

**Hala kopuk kaliyorsa** sorun buyuk olasilikla gateway'de degil:
`devices_probing` cihazi sayiyorsa gateway aktif olarak deniyor demektir.
Sunucudan `nc -zv <cihaz-ip> 20000` ile hattı dogrulayin; kapaliysa saha
tarafinda modem/anten/besleme kontrolu gerekir.

### "Cihaz kopuk gorunuyor ama aslinda sadece degeri degismiyor" (1.7.0'dan once)

**1.7.0'dan once bu gercek bir hataydi ve UC belirtisi vardi:** SCADA'da sahte
"haberlesme koptu", alarmin gec gelmesi, ve **kesici komutunun hic
gonderilememesi**. Sebep tekti: gateway canliligi "cache'e olcum dustu mu" ile
olcuyordu. event-driven modda degeri degismeyen bir cihaz yayin uretmez; bir
arıza gostergesi saatlerce ayni degerleri tasiyabilir.

Belirtiler tanidik geliyorsa once **surumu dogrulayin** (`/health` -> `version`).
1.7.0 ve sonrasinda canlilik DNP3 yanitlarindan uretilir (`OnReceiveIIN` +
`OnTaskComplete(SUCCESS)`); degerin degismesi GEREKMEZ.

Ayrimi tek GET ile gorun. **`/metrics` kullanin, `/health` DEGIL**: `/health`
auth'suz oldugu icin bilerek yalnizca SAYIM doner (cihaz kodu/IP sizdirmaz),
cihaz bazinda teshis Bearer ile korunan `/metrics` ucundadir (bkz. bolum 5).

```bash
curl -s -H "Authorization: Bearer $GATEWAY_TOKEN" \
  http://127.0.0.1:8020/metrics | jq .devices_detail
```

```json
{
  "d7": { "state": "online", "reachable": true,
          "evidence_age_sec": 1.2, "data_age_sec": 184.0 }
}
```

Filo seviyesi ozet auth'suz uctan alinabilir:

```bash
curl -s http://127.0.0.1:8020/health | jq '.devices.alive_no_data, .devices.recovery'
```

Cihaz basina uc alan belirleyici:

| Alan | Anlami |
|---|---|
| `evidence_age_sec` | Cihaz en son NE ZAMAN cevap verdi (canlilik) |
| `data_age_sec` | Olcum en son NE ZAMAN tazelendi (veri) |
| `reachable` | Komut yolunun kullandigi olcut — `false` ise komut GONDERILMEZ |

Okuma kilavuzu:

* `evidence_age_sec` kucuk + `data_age_sec` buyuk -> **cihaz saglikli, sadece
  sessiz.** Kopuk DEGILDIR, komut kabul eder. Gateway bu durumda kendiliginden
  integrity poll gonderir (`data_silence_poll_total` artar) ve filo
  seviyesinde `devices_alive_no_data` issue kodu cikar. Durgun bir fiderde bu
  NORMALDIR; KALICI ise Class 0 integrity poll'unun dustugunu dusundurur —
  link kalitesi / fragment boyutu / cihaz tarafi kontrol edilmeli.
* Ikisi de buyuk -> **gercek kopma.** `lost` durumu, yoklama ve zorla relink
  devreye girer (bkz. bir onceki bolum).

Ilgili log etiketleri:

```
yadnp3_veri_sessizligi device=d7 ip=10.0.0.9 veri_yasi=180s — cihaz CEVAP
VERIYOR (kanit taze) ama olcum tazelenmiyor; kopuk SAYILMADI, integrity poll
gonderiliyor
```

Komut reddedildiyse log su etiketi basar; `kanit_yasi` alanina bakin:

```
yadnp3_operate_skipped_offline device=d7 index=2 state=lost kanit_yasi=240s
```

`kanit_yasi` gercekten buyukse cihaza ulasilamiyordur (komut dogru olarak
gonderilmedi). Kucukse — ve komut yine reddediliyorsa — surum eskidir.

### "Su an veri hangi yoldan gidiyor?" — aktif tasima yolu

Telemetri iki yoldan gidebilir ve **hangisinde oldugunuzu bilmeden ariza
aramak cok pahali**. Tek GET ile cevaplayin:

```bash
curl -s http://127.0.0.1:8020/health | jq .outbox.telemetry_transport
```

```json
{
  "install_mode": "remote",
  "active_transport": "nats",
  "fallback_enabled": true,
  "transport_switches": 0,
  "fallback_delivered_total": 0,
  "nats_ready": true,
  "http_ready": true
}
```

Boot log'unda da tek satirda gorunur:

```
telemetry_transport_selected install_mode=remote active=nats fallback=http ...
```

**Kurulum moduna gore davranis (1.6.0+):**

| `INSTALL_MODE` | Ne zaman | NATS erisilemezse |
|---|---|---|
| `local` | "bu cihaza kur" — backend ile ayni makine | **HTTP'ye DUSULMEZ.** Mesajlar outbox'ta birikir, `/health` -> `telemetry_backend_unreachable`. Ayni makinede NATS'a erisilememesi bir yapilandirma hatasidir; gizlenmez. |
| `remote` (varsayilan) | "baska cihaza kur" — saha sunucusu | **HTTP yedegine duser**, veri akmaya devam eder. `/health` -> `telemetry_transport_fallback_http` (degraded). NATS geri gelince **otomatik doner**. |

Yerel modda `NATS_URL` **acikca verilmelidir**; verilmezse gateway boot'ta
acik hatayla durur (varsayilan `nats://localhost:4222`'ye sessizce dusmez).

**Yedege dusuldugunu nereden anlarsiniz:** log'da TEK bir olay satiri —
tekrarlayan deneme uyarisi degil:

```
telemetry_transport_switched from=nats to=http mode=remote sebep=nats_failed_x3: ...
telemetry_transport_switched from=http to=nats mode=remote sebep=nats_reachable_again
```

**HTTP yedegindeyken ne yapmali:** veri kaybi yok ama throughput NATS
yoluna gore kat kat dusuktur. NATS erisimini duzeltin (port 4222 acik mi,
kimlik bilgileri gecerli mi); gateway restart GEREKMEZ, en gec
`TELEMETRY_FALLBACK_PROBE_INTERVAL_SEC` (varsayilan 30sn) sonra
kendiliginden doner.

### Telemetri gitmiyor / outbox dolmaya basladi

> **ONCE SUNU KONTROL EDIN:** 1.1.0+ itibariyla telemetri varsayilan olarak
> **dogrudan NATS JetStream'e** gider (`TELEMETRY_PUBLISHER=nats`).
> `TELEMETRY_PUBLISHER=http` set edilmisse rollback yolundasiniz: telemetri
> backend HTTP ingest'e gider ve yuk backend'e biner. Once hangi yolda
> oldugunuzu `dnp3_gateway_starting ... publisher=... install_mode=...` log
> satirindan ya da yukaridaki `/health` alanindan teyit edin.

```
publish_batch_failed_outboxed count=175 error=... consecutive=1
outbox_backoff sent=0 wait=15.00s
```

**Teshis sirasi:**

1. `/health` -> `outbox.broker_ready` false mu? `metrics.signals_outboxed_total`
   artiyor mu? Artiyorsa telemetri **backend'e degil diske** gidiyor.
2. Backend ingest'i dogrudan dene:
   ```powershell
   curl -X POST "$BACKEND_API_URL/telemetry/gateway/GW-001" `
        -H "X-Gateway-Token: <token>" -H "Content-Type: application/json" -d "[]"
   ```
3. 401/404 -> token / gateway kaydi. 5xx -> backend tarafi.
4. `TELEMETRY_PUBLISHER=nats` ise: `nats-server` ayakta mi, `NATS_CREDENTIALS_PATH`
   var mi, `NATS_TLS_CA_PATH` PEM gecerli mi?

Outbox `pending` sayisi `OUTBOX_MAX_PENDING`'e (default 500K) yaklasinca
breaker tetiklenir ve `/health` `unhealthy` doner (HTTP 503). **Mesaj kaybi
YOK** — backend geri gelince retrier toplu POST ile bosaltir ve breaker
kendiliginden acilir. Kuyruk dead-letter yoluyla bosalsa bile breaker acilir
(0.5.1); **restart gerekmez**.

### Outbox `outbox_dead_letter` mesajlari birikti

```bash
# Docker
docker exec -it eg-gw-001 sqlite3 /app/.gateway_state/outbox_GW-001.db \
    "SELECT message_id, retry_count, last_error FROM outbox_dead_letter LIMIT 10;"
```
```powershell
# Windows
sqlite3 .\.gateway_state\outbox_GW-001.db `
    "SELECT message_id, retry_count, last_error FROM outbox_dead_letter LIMIT 10;"
```

`OUTBOX_MAX_RETRIES` (default 100) kez denenip hala **kalici** hata veren
mesajlar buraya tasinir. Gecici hatalar (ag, 408/429/502/503/504) retry
sayacini ARTIRMAZ — buraya dusen bir mesaj gercekten reddedilmis demektir.

`last_error` tipik sebepler:

| Hata | Anlami |
| --- | --- |
| `HTTP 422` / `400` | Backend payload'i reddediyor (sema uyumsuzlugu, bilinmeyen `signal_key`). |
| `HTTP 404` | `device_code` backend'de yok (cihaz silinmis). |
| `HTTP 413` | Payload cok buyuk (proxy limiti). |

Retention otomatiktir: `OUTBOX_DEAD_LETTER_RETAIN_DAYS` (30) ve
`OUTBOX_DEAD_LETTER_MAX_ROWS` (50000). Elle temizlik icin
`DELETE FROM outbox_dead_letter;`

### Komut sonuclari backend'e ulasmiyor

```
command_result_delivery_permanent count=1 error=... HTTP 422
command_result_dead_letter id=1234 failures=3
```

**Bu ciddi:** komut cihaza GONDERILMIS olabilir ama backend sonucu bilmiyor;
operator panelinde komut "bekliyor" gorunur.

```powershell
sqlite3 .\.gateway_state\command_ledger_GW-001.db `
  "SELECT command_id, delivery_state, delivery_failures, delivery_error
   FROM command_ledger WHERE delivery_state='dead_letter';"
```

`result_json` kolonu komutun GERCEK sonucunu tasir — operatore bu bilgi elle
iletilmelidir. Kalici hata 3 denemeden sonra dead-letter'a alinir ve kuyruk
ilerler (0.5.1); oncesinde tek bir kalici redd TUM sonuc kuyrugunu sonsuza
kadar bloke ediyordu.

### Bir cihaz hic veri gondermiyor

```powershell
(Invoke-RestMethod http://127.0.0.1:8020/health).devices
```

| Bulgu | Sonraki adim |
| --- | --- |
| Cihaz `lost` | Ag/cihaz arizasi. `telnet <ip> 20000`. Log'da `yadnp3_device_stale`. |
| Cihaz `unknown` kaliyor | Sinyal profili eslesmiyor olabilir — log'da `signals_empty device=..`. Backend'de cihazin `model` alanini kontrol edin. |
| Cihaz `online` ama deger gelmiyor | **Delta-only yayin**: deger degismediyse mesaj gitmez, bu NORMAL. Zorlamak icin `POST /refresh-all`. |
| `dnp3_event_buffer_overflow` | Outstation olaylari dusurdu; telemetride aciklik var. Scan araligini kisaltmayi degerlendirin. |

### Sunucu saati sapmis (`clock_skew_unsafe`)

```
clock_skew_unsafe skew_sec=45.2 — ... DNP3 zaman senkronizasyonu DURDURULDU
dnp3_time_sync_suspended device=DEV-042
```

Gateway saati backend'den >=30sn sapinca outstation'lara saat **yazilmaz**
(yanlis saati 300 cihaza yazmaktansa hic yazmamak yeglenir).

```powershell
w32tm /query /status
w32tm /resync
```

Duzeldikten sonra otomatik devam eder; restart gerekmez.

### `command_journal_reset` — komut defteri sifirlandi

```powershell
(Invoke-RestMethod http://127.0.0.1:8020/health).command_ledger
# journal_reset: True  -> defter bozuktu ve sifirlandi
```

Bu, `state_db_quarantined` ile **ayni sey degil**. Orada kaybedilen birikmis
telemetridir. Burada kaybedilen **fiziksel komut gecmisi**dir ve iki somut
sonucu vardir:

1. Onceki prosesin yarim biraktigi bir CROB'un sonucu backend'e **hicbir
   zaman bildirilemez** — backend o komutu sonsuza kadar "bekliyor" gorur.
2. O komutlar icin **tekrar-onleme garantisi yoktur**: backend bekleyen bir
   komutu yeniden gonderirse gateway onu yeni sanip CROB'u tekrarlayabilir.

**Mudahale:**

1. Log'da `command_ledger_reset` satirini bul; karantina dosyasinin yolunu al
   (`*.corrupt.<ts>`) ve **sakla** — denetim izidir.
2. Backend'de bu gateway icin **bekleyen (sonucu gelmemis) komutlari** listele.
3. Her biri icin sahadan/SCADA'dan **gercek cihaz durumunu dogrula** (kesici
   acik mi kapali mi). Komutu koru veya iptal et — **korumadan yeniden
   gonderme**.
4. Genelde ani guc kesintisi sonrasi gorulur. Tekrarliyorsa diskin
   saglik durumunu (SMART) ve UPS'i kontrol edin.

Gateway bu durumda calismaya devam eder; yeni komutlar normal sekilde
defterlenir ve tekrar-onleme yeniden gecerlidir.

### `poll_pool_starved` — cihazlara hic istek gitmiyor

```
poll_pool_starved gateway=GW-001 baslamayan=47 due=300 workers=25 — ...
```

**Sebep:** worker havuzu doldu; kuyruktaki cihazlara o cycle'da **hic istek
gonderilemedi**. Bu bir cihaz/hat arizasi DEGIL, kapasite sorunudur.

O cihazlar "okundu" sayilmaz; bir sonraki cycle'da bayatlik siralamasinin
**en onune** gecerler — yani veri kaybi yok, gecikme var. Ama tekrarliyorsa:

| Cozum | Ne zaman |
| --- | --- |
| `MAX_PARALLEL_DEVICES` artir | CPU ve ag bant genisligi musaitse. Varsayilan 100; 300 cihazda ~150 uygun (bkz. bolum 6) |
| Poll araligini uzat (`DEFAULT_POLL_INTERVAL_SEC`) | Cihazlar zaten event-driven yayin yapiyorsa |
| Cihaz sayisini bol | Tek gateway'e 300'den fazla cihaz dusuyorsa ikinci gateway ac |
| `DNP3_RESPONSE_TIMEOUT_SEC` dusur | Kopuk cihazlar worker'lari uzun sure tutuyorsa |

Once `/metrics` uzerinden kac cihazin `lost` oldugunu kontrol edin: kopuk
cihazlar timeout suresince worker isgal eder ve havuzu saglikli cihazlara
kapatir.

### `http_publisher_breaker_open` — hizli-hata modu

```
http_publisher_breaker_open url=https://... bekleme=8.0sn — backend ingest erisilemiyor
```

Backend ingest'e ulasilamadiginda gateway her cihaz icin yeni bir POST deneyip
tam timeout kadar beklemez; devreyi acar ve denemeler **aninda** gecici hata
verir. Telemetri outbox'a yazilmaya devam eder — **veri kaybi yoktur**.

Bekleme 1sn'den baslar, ikiye katlanarak 15sn'de tavanlanir. Her bekleme
sonunda **tek bir** istek probe olarak gecirilir; basarirsa
`http_publisher_breaker_closed` gorulur ve outbox retrier birikmis mesajlari
bosaltir.

Bu satiri gorunce bakilacak yer gateway degil **backend/ag**tir: `curl -sS -o
/dev/null -w "%{http_code}" $BACKEND_API_URL/health` ile disaridan dogrulayin.

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

### Konfigurasyon hatasi — cikis kodu 78

Gateway bozuk bir `.env` ile aciliyorsa **cerceveli bir mesaj** basip
`EXIT_CONFIG_ERROR (78)` ile durur:

```
========================================================================
  KONFIGURASYON HATASI — gateway baslatilamadi
========================================================================

GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_MODE=mock olamaz. ...

  Kontrol edilecek dosya : .env
  Ayarlarin tam listesi  : .env.example
  Kurulum rehberi        : docs/RUNBOOK.md
========================================================================
```

Bu mesaj **stdout ve stderr'e birlikte** yazilir; NSSM'de `AppStdout`
yonlendirmesi yoksa gormezsiniz (bkz. bolum 1). Docker'da `docker logs`
yeterlidir.

Saha cikisinda yaygin hatalar:

| Hata | Cozum |
| --- | --- |
| `GATEWAY_TOKEN placeholder degerle baslayamaz` | `.env.example`'dan kopyaladiniz ama token degistirmediniz. >=32 char rastgele uretin. |
| `GATEWAY_MODE=mock olamaz` | **En sik hata.** `.env.example` mock ile gelir; sahada `GATEWAY_MODE=dnp3` yapin. Mock adapter uydurma deger uretir ve her komuta "basarili" doner. |
| `BACKEND_API_URL public host icin https:// olmali` | Caddy/LE ile TLS kurun veya `GATEWAY_INSECURE_ALLOW_PLAINTEXT=true` (gecici). |
| `RABBITMQ_URL set edilemez` | Eski .env'den kalmis. Satiri silin. |
| `NATS_DUAL_PUBLISH_ENABLED=true olamaz` | DEPRECATED. Satiri silin. |
| `GATEWAY_REFRESH_TOKEN, GATEWAY_TOKEN ile AYNI` | Ikisini farkli uretin (`secrets.token_urlsafe(32)`). |
| `DNP3_DEVICE_ALLOWED_SUBNETS ... tek gecerli CIDR yok` | Yazim hatasi (orn. `/33`). Duzeltin; fail-closed davranis geregi TUM cihazlar reddedilirdi. |

> Cikis kodu **78** yalnizca konfigurasyon hatasini gosterir. Diger cikislar
> (cokme, sinyal) farkli kod doner — NSSM/Docker tarafinda ayirt edilebilir.

### Surum yukseltme

**Docker (uretim):** yukseltme `pull` + `up -d` (bkz. bolum 1). Surum imajin
icine build aninda yazilir, dolayisiyla asagidaki bayat-surum tuzagi Docker'da
YOKTUR. Yine de her yukseltmeden sonra dogrulayin:

```bash
curl -s http://127.0.0.1:8020/health | jq -r .version
```

> **Gelistirme (venv) yolunda dikkat: yalnizca `git pull` YETMEZ —
> `install.ps1`'i tekrar calistirin.**
>
> Surum bilgisi once **kurulu paket metadata**'sindan okunur (`pip install -e .`
> ile yazilir), yalnizca o yoksa `VERSION` dosyasina duser. Kodu guncelleyip
> kurulumu yenilemezseniz kod yeni, `/health` `version` alani ESKI olur ve bir
> olay incelemesinde "hangi surum kosuyordu" sorusu yanlis cevaplanir.
>
> Docker'da bu olamaz: imaj her build'de temiz kurulur ve CI
> (`docker imaji` isi) imajdaki surumun `VERSION` ile ayni oldugunu dogrular.

#### SQLite semasi

```
sqlite_migration_applied db=outbox[outbox_GW-001.db] version=2 ...
```

Bu satir **normaldir** — sema otomatik yukseltilir, birikmis telemetri korunur.

`sqlite_db_quarantined` gorurseniz dosya bozulmustur (genelde ani guc
kesintisi). Gateway temiz bir DB ile devam eder; bozuk dosya
`*.corrupt.<ts>` olarak saklanir ve icindeki birikmis telemetri **kayiptir**.
`/health` bunu `state_db_quarantined` ile raporlar.

**Geri donus (rollback):** eski bir surume donerseniz `PRAGMA user_version`
koddan yeni kalir ve gateway acikca hata verir (sessizce yanlis calismaz).
Bu durumda ilgili `.db` dosyasini tasiyip temiz baslatin.

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

> Bu cagri **hem** cihazlara integrity poll ister **hem de** son bilinen tum
> degerleri yeniden yayinlatir. Delta-only yayinin tek telafi mekanizmasidir:
> backend tarafinda veri kaybolduysa (DB restore, tag-engine sifirlanmasi)
> bunu kullanin.

### `POST /operate` — tek cihaza DNP3 komutu (CROB)

Backend'in cihaz komut butonlarini proxy ettigi yol. **Ayri** Bearer token:
`GATEWAY_COMMAND_TOKEN` (bos ise 503).

```bash
curl -X POST http://gateway-host:8020/operate \
    -H "Authorization: Bearer $GATEWAY_COMMAND_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"device_code":"RMU-17","index":3,"op_type":"latch_on","command_id":9911}'
```

| Alan | Varsayilan | Not |
| --- | --- | --- |
| `device_code` | — | zorunlu |
| `index` | — | zorunlu, binary output index'i |
| `op_type` | **`latch_on`** | `latch_on` cikisi KALICI enerjili birakir; `pulse_on` kendiliginden acilir. Ikisi DNP3'te farkli davranistir. |
| `count` / `on_time_ms` / `off_time_ms` | 1 / 100 / 100 | |
| `command_id` | yok | **Verilmesi ONERILIR.** Idempotency anahtari: ayni id ikinci kez gelirse CROB TEKRARLANMAZ, kayitli sonuc doner (`duplicate: true`). Verilmezse retry'da ayni kesici IKI KEZ surulebilir (log: `operate_without_command_id`). |

**Cevap sekli** — basari ve duplicate AYNI alanlari tasir:

```jsonc
{ "ok": true,  "status": "success", "duplicate": false, "result": {...} }  // calisti
{ "ok": false, "status": "timeout", "duplicate": false, "result": {...} }  // cihaz reddetti
{ "ok": true,  "status": "success", "duplicate": true,  "result": {...} }  // tekrar; sonuc BILINIYOR
{ "ok": null,  "status": "pending", "duplicate": true,  "result": null  }  // tekrar; sonuc BILINMIYOR
```

> **`ok: null` gorunce YENI bir `command_id` ile tekrar DENEMEYIN.** Komut
> kabul edilmistir; sonucu ya hala uretiliyor ya da backend'e teslim edilip
> kayittan dusurulmustur. Yeni bir id ile denemek kesiciyi GERCEKTEN iki kez
> surer. Sonucu bekleyin.

| HTTP | Anlami |
| --- | --- |
| 200 | Istek islendi (komutun kendisi basarili olmayabilir — `ok`'a bakin) |
| 400 | Govde hatali (`device_code`/`index` eksik, `command_id` tamsayi degil) |
| 401 | Token yanlis |
| 404 | `device_code` bu gateway'de tanimli degil |
| 503 | Token bos (endpoint kapali) **veya** komut defterine erisilemiyor — tekrar-onleme garanti edilemedigi icin CROB **gonderilmedi** |

> Komutlar NAT arkasindaki kurulumlarda **pull kanaliyla** da gelir
> (`GET /pending`, 1sn). Bu endpoint yalnizca dogrudan HTTP push isteniyorsa
> gerekir; normal kurulumda `GATEWAY_COMMAND_TOKEN` bos birakilabilir.

### `GET /info` ve `/metrics` — Bearer auth

`/health` minimum status doner; detayli metrikler (outbox sayisi, broker
counters, config_version, cycle metrics, **cihaz bazinda durum**) `/info` ve
`/metrics` uzerinde. Ayni `GATEWAY_REFRESH_TOKEN` ile Bearer auth.

> Token bos ise bu iki endpoint **503** doner. Sahada metrik erisimi
> istiyorsaniz `.env`'de `GATEWAY_REFRESH_TOKEN` set edin — aksi halde elinizde
> yalnizca auth'suz `/health`'in kirpilmis ozeti kalir.

## 6. Olcek — 20 cihazdan 500'e

Pilot (20 cihaz) ile hedef (gateway basina 500, 1.2.0+ varsayilan kapasite)
arasindaki fark yalnizca
sayi degil: darbogaz **worker havuzu** ve **WAN gecikmesi**dir. Asagidaki yol
her adimda tek bir seyi degistirir, boylece bir sey bozulursa sebebi bellidir.

### Kademe kademe cikis

| Adim | Cihaz | Bakilacak | Sorun isareti |
| --- | --- | --- | --- |
| 1 | 20 | `devices.online` = cihaz sayisi | `lost` > 0 -> once o cihazi coz, ilerleme |
| 2 | 50 | `metrics.seconds_since_last_cycle` | Poll araliginin 5 katini asiyorsa cycle yetismiyor |
| 3 | 100 | `poll_pool_starved` log'u | Ciktiysa `MAX_PARALLEL_DEVICES` artir |
| 4 | 200 | `outbox.outbox_pending` | Surekli artiyorsa backend ingest yetismiyor (gateway degil) |
| 5 | 300 | `signals_outboxed_total` | Artiyorsa telemetri diske gidiyor — NATS/broker darbogazi |
| 6 | 500 | `poll_pool_starved` + `metrics.seconds_since_last_cycle` | Cikiyorsa MAX_PARALLEL_DEVICES < cihaz sayisi veya kopuk cihaz dalgasi; DEVICE_POLL_TIMEOUT_SEC'i dusurun |

Her adimda **en az bir tam poll araligi + 5 dakika** bekleyin. Gateway
delta-only yayin yaptigi icin ilk dakikada trafik yuksek (integrity poll ile
tum degerler bir kez yayinlanir), sonra normale duser — erken karar vermeyin.

### Ayar tablosu

| Ayar | Varsayilan (1.2.0+) | 500 cihaz | Neden |
| --- | --- | --- | --- |
| `MAX_PARALLEL_DEVICES` | 500 | 500 | Havuz tembel buyur; cihaz basina worker = kopuk cihaz dalgasinda tek timeout dalgasi. `poll_pool_starved` cikarsa cihaz sayisinin altinda kalmis demektir |
| `DEFAULT_POLL_INTERVAL_SEC` | 1 | 1-2 | Cihazlar zaten event-driven yayin yapiyor; siklik CPU'ya mal olur, veriye degil |
| `DEVICE_POLL_TIMEOUT_SEC` | 15 | 15 | Kopuk cihazin worker'i yarim surede birakmasi havuzu saglikli cihazlara acar (1.2.0'da varsayilan oldu; yavas WAN sahasi env ile yukseltir) |
| `CYCLE_TIMEOUT_SEC` | 120 | 120 | Degistirmeyin; asilmasi zaten bir ariza isaretidir |

> **Once olcun, sonra degistirin.** Bu degerler baslangic noktasi; gercek
> sinir sahanin WAN gecikmesine ve cihazlarin yanit suresine bagli.
> `poll_pool_starved` ciktiysa artirin, cikmadiysa dokunmayin.

### Darbogaz gateway'de mi backend'de mi?

Karistirilmasi kolay ve mudahaleyi tamamen yanlis yone cevirir:

| Belirti | Darbogaz | Cozum yonu |
| --- | --- | --- |
| `poll_pool_starved` | **Gateway** — cihaza istek gidemiyor | `MAX_PARALLEL_DEVICES` / poll araligi |
| `outbox_pending` artiyor, `signals_outboxed_total` artiyor | **Backend** — telemetri kabul edilemiyor | Backend ingest kapasitesi; gateway ayarlari FAYDA ETMEZ |
| `http_publisher_breaker_open` | **Ag veya backend** erisilemiyor | Once `curl` ile disaridan dogrulayin |
| `devices.lost` yuksek, digerleri temiz | **Saha agi** | Gateway degil; switch/fider/besleme |

Gateway darbogazinda veri **gecikir**; backend darbogazinda veri **outbox'ta
birikir**. Ikisinde de kayip yoktur — ama cozum yerleri tamamen farklidir.

### Tek gateway'e kac cihaz? — OLCULDU

400 cihazla olculdu (2026-08-04, v1.3.0, simulator yuku):

| Olcum | Deger |
| --- | --- |
| Gateway CPU | %108 |
| RAM | 375 MiB (~0.94 MiB/cihaz) |
| Thread | 413 (~1/cihaz) |
| Acik fd | 420 (~1/cihaz) |
| Yayin | 6.349 sinyal/sn |
| Outbox / hata | 0 / 0 |

Olcekleme dogrusal: 300 -> 400 cihazda CPU %77 -> %108.

**Desteklenen: 500 cihaz** (v1.3.0 hedefi). Sinirlayicilar sirayla:

| Sinir | Deger | Not |
| --- | --- | --- |
| **fd (dosya tanitici)** | compose'da **65536** | Docker varsayilani 1024 idi -> ~950 cihazda duvar. Baglanti flap'inde fd gecici olarak IKIYE katlanir, o yuzden pratik sinir cok daha erkendi. Hata "cihaz kopuk" gibi gorunur, gercek sebep hicbir sayacta yazmaz. |
| **DNP3 IO thread** | otomatik, ~25 cihaz/thread | 1.3.0 oncesi SABIT 4 idi (400 cihazda 100 cihaz/thread). `DNP3_MANAGER_THREADS` ile ezilebilir. |
| CPU | ~%136 @500 (projeksiyon) | Host 14 cekirdekte sorun degil |
| `devices` sert tavani | 1000 | Backend ayristiricisinda; asilirsa liste kirpilir |

> **Simulator yuku GERCEKCIDEN AGIR.** Olcumde cihaz basina 15.9 sinyal/sn
> yayinlandi. Sahadaki Horstmann SN2 bir ariza gostergesidir; 193 sinyalin
> cogu sabittir ve delta-only yayin sayesinde gercek yuk 10-100 kat dusuktur.
> Yani bu rakamlar guvenli taraftan.

**500'un otesi olculmedi.** Daha fazla cihaz varsa ikinci bir gateway acin
(`new_gateway.ps1`) — cihazlari backend'de `gateway_code` ile bolusturun.
Her instance kendi outbox'i, kendi kilidi ve kendi prosesiyle calisir; bu
ayni zamanda tek prosesin GIL tavanini da asmanin dogal yoludur.

## 7. Shutdown sırasi

Graceful shutdown (`SIGTERM` — `docker stop` / compose `down`; gelistirmede
`SIGINT` / Windows `SIGBREAK`):

1. `stop_event.set()` — tum thread'lerin bekleme araliklarini kirar
2. Komut-poll thread'i join (5sn) + `command_ledger` kapanir
3. Config-refresh thread'i join (5sn)
4. Reader (`reader.close()`) — DNP3 master/channel kapanir, TCP RST
5. OutboxRetrier — bg thread durur, in-flight publish tamamlanir (timeout 3sn)
6. Telemetri publisher — baglanti kapanir
7. Health HTTP server — yeni baglanti reddi, in-flight bekleme
8. Outbox pending + dead-letter sayim raporu loglanir
9. Instance lock dosyasi OS tarafindan otomatik release edilir

Toplam tipik shutdown: <8 saniye.

> **Docker notu:** `docker stop` varsayilan olarak 10sn sonra SIGKILL gonderir
> — tipik shutdown (<8sn) buna sigar ama pay dardir. Compose'da
> `stop_grace_period: 30s` verin; aksi halde in-flight bir CROB sonucu
> ledger'a yazilamadan proses oldurulebilir (sonraki acilista `unknown` olarak
> bildirilir — kayip degil ama gurultudur).
>
> Gelistirmede NSSM kullaniyorsaniz karsiligi:
> `nssm set <svc> AppStopMethodConsole 10000`.
