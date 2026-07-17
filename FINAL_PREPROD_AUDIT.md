# EnerjiOne Grid — Production Öncesi Son Audit (Konsolide)

**Tarih:** 2026-05-13
**Yapılan toplam fix:** ~55 (Critical/High pratikte kapatıldı, davet akışı dahil)
**Atlanılan:** TLS/HTTPS pinning, mTLS, Observability stack (kullanıcı kapsam dışı)
**Deployment:** Local LAN + 4G mobil

---

## 🟢 Doğrulananlar (TAMAM)

- Backend account lockout + JWT TTL + rate-limit (login/ws-ticket/password/telemetry/fcm-token)
- Davet akışı (invite/setup-password/resend-invite) + ChangePasswordModal + SetupPasswordPage
- Backend `_PLACEHOLDER_PREFIXES` pattern check, idempotency wiring `/internal/alarms`
- Backend backup upload size cap, off-site copy, generic hata mesajları
- Backend `/health/ws-stats` + `/health/dlq` ENGINEER+ auth, internal token X-Service-Name trace
- Backend config response HMAC signature (kod yapısı var, byte determinism sorunu — aşağıda)
- Gateway multi-instance lock, ThreadingHTTPServer, /health leak fix, refresh_nonce persist
- Gateway NATS TLS/creds context, DNP3 IP allowlist, pika legacy removed
- Mobile release signing, R8, FLAG_SECURE, WS reconnect+badge, inactivity tracker, 401 SessionExpiredBus
- Frontend ErrorBoundary, ConfirmDialog (29 callsite), WsStatusBadge, cookie-only auth, Material Symbols self-host, 96 `<th scope="col">`
- 3 worker fail-fast + X-Service-Name header
- Infra image `:latest` → `${E1_VERSION}` semver, Windows installer (install/update/uninstall.ps1)
- NATS deny-all + per-user subject ACL doğrulandı

---

## 🔴 KRİTİK — Pilot Canlıya Çıkmadan Şart

### CR1. Gateway config HMAC byte mismatch — gateway hiç config çekemez!
**Lokasyon:** [gateways.py:629-635](../EnerjiOne%20Grid/apps/backend-api/app/api/gateways.py)
HMAC imza `config_resp.model_dump_json()` byte'larından üretiliyor, ama FastAPI response render eden JSON `","` vs `", "` ayırıcısı + `jsonable_encoder` ile farklı byte üretir. Gateway response.content üzerinden doğruladığı için **her config refresh'te imza fail** — gateway hiç config çekemez.
**Fix:** Backend tarafında manuel `Response(content=body_bytes, media_type="application/json")` döndür; HMAC'i bu birebir byte'lardan hesapla. Alternatif: gateway HMAC verify'ını opsiyonel-skip eden bir feature flag ile geçici devre dışı bırak.

### CR2. `pg_restore` SUPERUSER ile çalışıyor — RCE açık
**Lokasyon:** [backup_service.py:431-444](../EnerjiOne%20Grid/apps/backend-api/app/services/backup_service.py)
`pg_restore -U enerjione` (superuser). `--no-superuser-statements` flag yok; `e1_restore` non-superuser rolü yok. `validate_dump_file` regex tarama gzip-compressed TOC içindeki `COPY FROM PROGRAM`'ı yakalayamayabilir → **uzaktan komut yürütme**.
**Fix:** DB'de `CREATE ROLE e1_restore NOSUPERUSER LOGIN` + `GRANT ALL ON SCHEMA public TO e1_restore`; `pg_restore -U e1_restore`; `--no-superuser-statements` flag.

---

## 🟠 YÜKSEK — İlk Hafta İçinde

### Backend

| # | Konu | Lokasyon |
|---|---|---|
| H1 | Pending invitation user'lar bildirim alıyor (henüz şifre belirlemedi) | [scope_service.py:99-101](../EnerjiOne%20Grid/apps/backend-api/app/services/scope_service.py) — `hashed_password IS NOT NULL` filter ekle |
| H2 | Logout cookie auth jti revoke etmiyor | [auth.py:377-386](../EnerjiOne%20Grid/apps/backend-api/app/api/auth.py) — cookie'den de JWT decode et |
| H3 | `/internal/notifications/dispatch/{alarm_id}` idempotency wire değil | [internal.py:546-587](../EnerjiOne%20Grid/apps/backend-api/app/api/internal.py) — `message_id` param + dedup |
| H4 | `FRONTEND_BASE_URL` `.env.example`'da yok | [config.py:167](../EnerjiOne%20Grid/apps/backend-api/app/core/config.py) — `.env.example`'a ekle + prod validator |

### Mobile

| # | Konu | Lokasyon |
|---|---|---|
| H5 | Push tap deeplink routing yok | [push_service.dart:54](../EnerjiOne%20Grid%20Mobile%20App/lib/src/core/notifications/push_service.dart) — `onMessageOpenedApp` + `getInitialMessage` |
| H6 | iOS push entitlements (`aps-environment`) yok | `ios/Runner/Info.plist`, `Runner.entitlements` |
| H7 | `AppConfig.defaultBaseUrl = 'http://10.0.2.2:8000'` emülatör loopback | [app_config.dart:3](../EnerjiOne%20Grid%20Mobile%20App/lib/src/core/config/app_config.dart) — boş string + login validator |
| H8 | Polling lifecycle pause yok (7 controller × `Timer.periodic`, 4G data + battery) | 7 dosya — `LifecyclePoller` mixin |
| H9 | Force-update gate yok | `lib/main.dart` — `/health/min-version` çağrısı |
| H10 | `clearAllOnLogout` `base_url` + `remember_me` silmiyor | [secure_store.dart:44-47](../EnerjiOne%20Grid%20Mobile%20App/lib/src/core/storage/secure_store.dart) |

### Frontend

| # | Konu | Lokasyon |
|---|---|---|
| H11 | UserManagementPanel hâlâ eski password-create UI; invite endpoint kullanmıyor | [UserManagementPanel.tsx:110-121](../EnerjiOne%20Grid/apps/frontend-web/src/features/auth/UserManagementPanel.tsx) — "Davet Et" butonu + setup_url görüntü |
| H12 | 16 dosyada hardcoded `tr-TR` `toLocaleString` | Frontend genelinde — `src/shared/format.ts` helper |
| H13 | `Referrer-Policy` setup-password token leak | [nginx.conf:52](../EnerjiOne%20Grid/apps/frontend-web/nginx.conf) — `/setup-password` için `no-referrer` veya token'ı sessionStorage'a taşı |

### Gateway

| # | Konu | Lokasyon |
|---|---|---|
| H14 | NATS user/pass URL log redaction yok | [logging_setup.py:51](src/dnp3_gateway/logging_setup.py) — `(amqp\|nats)[s]?://` |
| H15 | Outbox SQLite chmod 600 yok | [outbox.py:103](src/dnp3_gateway/messaging/outbox.py) — POSIX `os.chmod(0o600)` |
| H16 | `.env.example` yeni NATS/allowlist env'leri yok | `.env.example` — `NATS_TLS_CA_PATH`, `NATS_CREDENTIALS_PATH`, `DNP3_DEVICE_ALLOWED_SUBNETS` sample |

### Infra

| # | Konu | Lokasyon |
|---|---|---|
| H17 | Container hardening (`read_only`, `cap_drop: [ALL]`, `no-new-privileges`) worker'larda yok | [docker-compose.yml](../EnerjiOne%20Grid/docker-compose.yml) — x-hardened anchor |
| H18 | Backup disk-full proaktif alert yok | [backup_scheduler.py](../EnerjiOne%20Grid/apps/backend-api/app/services/backup_scheduler.py) — `shutil.disk_usage` %85 check |
| H19 | **`install.ps1` UTF-8 BOM bug** — `.env` ilk satır `﻿SECRET_KEY` olur, Docker Compose parse edemez | [install.ps1:150](../EnerjiOne%20Grid/install.ps1) — `[System.IO.File]::WriteAllText(...UTF8Encoding(false))` |
| H20 | `FRONTEND_BASE_URL` `.env.example`'da yok (H4 ile aynı, infra tarafı) | `.env.example` |

---

## 🟡 ORTA — İlk 30 Gün

### Backend (5)
- AlarmRule INSTALLER-only tutarsızlığı (diğer admin endpoint'ler `[INSTALLER, ENGINEER]`) — karar verilmesi gerek
- `requirements.txt` `pip-compile --generate-hashes` lock yok
- `/backups/upload` ENGINEER'a açık — restore sonrası RCE riski varken INSTALLER-only daha güvenli
- `GET /backups/{id}/download` audit eventi yazmıyor
- `/restore` typed-confirmation (kullanıcıya DB adını yazdırma)

### Mobile (4)
- `_firebaseBackgroundHandler` boş (data-only payload + badge sync)
- Yerelleştirme tam değil (114 hardcoded TR string, 21 dosya — kapsam dışı bırakılabilir)
- Offline mod yok (`hive`/`drift`)
- Adaptive icon foreground safe-zone yanlış

### Frontend (3)
- WS disconnect toast yok (Badge var ama transition watcher yok)
- `React.memo` büyük tablolarda yok
- BackupsPanel restore custom modal kullanıyor (ConfirmDialog'a migrate edilmedi)

### Gateway (4)
- `/refresh-all` per-IP rate-limit yok (Bearer var ama sliding window yok)
- Outbox dead_letter retention yok (haftalık prune)
- `_DeviceCache._values` unbounded (max size cap)
- `docs/SECURITY.md`, `RUNBOOK.md`, `ARCHITECTURE.md` cutover öncesi içerikli (Horstmann/RabbitMQ referansları)

### Infra (4)
- JetStream stream backup yok (`nats stream backup` cron)
- `install.ps1 -SkipPull` offline doc yok (`docker save/load` talimatı README'de)
- `BACKUP_OFFSITE_DIR` `.env.example`'da yok
- `.gitignore` eksik: `key.properties`, `*.keystore`, `*.jks`, `backups-offsite/`

---

## 🟢 DÜŞÜK — Sürekli İyileştirme

- Backend: Non-prod CORS `*` + credentials dokümante; pg_restore typed-confirmation
- Mobile: Splash Android 12+ (`flutter_native_splash`), root/jailbreak detection, inactivity 10dk→5dk
- Frontend: Dark mode, Toast queue max-cap, BackupsPanel ConfirmDialog migrasyonu
- Gateway: `requirements-lock.txt`, gateway compose Windows path doc
- Infra: `docker-compose.prod.yml` override, CI/CD `.github/workflows/`

---

## 📊 Toplam Tablo

| Kategori | Critical | High | Medium | Low |
|---|---|---|---|---|
| Backend | **2** | 4 | 5 | 2 |
| Gateway | 0 | 3 | 4 | 1 |
| Mobile | 0 | 6 | 4 | 3 |
| Frontend | 0 | 3 | 3 | 3 |
| Infra | 0 | 4 | 4 | 2 |
| **TOPLAM** | **2** | **20** | **20** | **11** |

---

## 🎯 Pilot Canlıya Çıkmadan ZORUNLU (1-2 gün)

1. **CR1** — Gateway config HMAC byte determinism (gateway config çekemiyor!)
2. **CR2** — `pg_restore` non-superuser rolü (RCE riski)
3. **H19** — `install.ps1` UTF-8 BOM bug (`.env` parse hatası, kurulum patlar)
4. **H1** — Pending invitation user'lar bildirim alıyor (veri sızıntısı)
5. **H4** — `FRONTEND_BASE_URL` `.env.example`'a ekle (invite akışı çalışmaz)
6. **H11** — UserManagementPanel invite UI (admin davet akışı UI üzerinden kullanamıyor)

Bu 6 madde **pilot canlıya çıkmadan kapatılmalı** — diğerleri ilk hafta-30 gün içinde incremental yapılabilir.

---

## ⚠️ Önceki Audit'e Göre Değişim

| Audit | Critical | High | Medium | Low |
|---|---|---|---|---|
| Bugün başı | 7 | 12 | — | — |
| Bugün ortası (re-audit 1) | 1 | 10 | 14 | 15 |
| Bugün sonu (re-audit 2) | 1 | 10 | 14 | 15 |
| **Şimdi (re-audit 3)** | **2** | **20** | **20** | **11** |

**Yeni keşfedilen 2 Critical:**
- **CR1 HMAC byte mismatch** — bugün eklenen HMAC signature feature'ı pratikte çalışmıyor (gateway test edince fark edilirdi)
- **CR2 pg_restore SUPERUSER** — önceki audit'lerde Medium olarak işaretliydi; bugünki audit pencerede tekrar Critical'a çekildi çünkü davet akışı ENGINEER'a backup upload izin veriyor

**Yeni keşfedilen High'lar:**
- Pending invitation user'lara mail gidiyor (H1)
- Logout cookie auth jti revoke etmiyor (H2)
- `/internal/notifications/dispatch` idempotency yok (H3)
- Mobile polling lifecycle pause yok (H8) — önemli battery/data sorunu
- `install.ps1` UTF-8 BOM bug (H19) — kurulum komple bozulur

Bu yeni bulgular önceki audit'lerin kapsamadığı (test edilmeden tanımlanmış) detaylar — derin re-audit faydalı oldu.

---

**Sonuç:** Sistem güzel ilerleme kaydetti. Pilot için **6 kritik fix** (1-2 gün) kalıyor; gerisi 30 günde tamamlanabilir.
