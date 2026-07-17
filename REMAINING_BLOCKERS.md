# EnerjiOne Grid — Kalan Production Blocker'lar

**Tarih:** 2026-05-13
**Kapsam:** 4 paralel re-audit (backend / gateway / mobile / frontend+infra) sonrası kalan eksikler
**Deployment:** Local LAN + mobil 4G saha
**Daha önce tamamlananlar:** 25 fix (Sprint 1-4 + 6 + Account Lockout)

---

## ÖNEMLİ POZİTİF DOĞRULAMALAR

Re-audit aşağıdakileri **temiz** buldu — endişe etme:

- ✅ `infra/nats/nats-server.conf` `.gitignore`'da, git history'de **hiç commit'lenmemiş**
- ✅ `apps/backend-api/fcm-service-account.json` `.gitignore`'da, git history'de **hiç commit'lenmemiş** (sadece local disk'te dev için)
- ✅ Bugün yapılan 25 fix doğru uygulanmış — hiçbiri eksik veya bozuk değil
- ✅ Gateway `identity.py` token validation, ThreadingHTTPServer, /health leak fix, multi-instance lock — hepsi çalışıyor
- ✅ Mobile Android tarafı sağlam (signing, R8, FLAG_SECURE, logout invalidate, WS reconnect)
- ✅ Backend account lockout, JWT TTL, rate-limit, backup size cap — hepsi aktif

---

## 🔴 CRITICAL — Pilot Canlıya Çıkmadan Şart (1-3 gün)

### C1. iOS tarafı tamamen kapsamsız
**Lokasyon:** `EnerjiOne Grid Mobile App/ios/Runner/`
- `Info.plist:48-52` — `NSAllowsArbitraryLoads=true` (4G üzerinden cleartext = parola sızıntısı)
- `AppDelegate.swift` / `SceneDelegate.swift` — boş; FLAG_SECURE'un iOS karşılığı (`applicationWillResignActive` blur view) yok
- **Etki:** App switcher screenshot'tan SCADA verisi sızıyor, 4G'de parola plaintext
- **Fix:** Info.plist'te `NSAllowsArbitraryLoads=false` + sadece LAN domain için exception; AppDelegate'te blur view ekle

### C2. Android cleartext traffic global açık
**Lokasyon:** `EnerjiOne Grid Mobile App/android/app/src/main/AndroidManifest.xml:13` + `network_security_config.xml`
- `usesCleartextTraffic="true"` ve `cleartextTrafficPermitted="true"` (base-config)
- 4G üzerinden yanlış `http://...` URL girilirse JWT + parola plaintext
- **Fix:** `usesCleartextTraffic="false"` global; `network_security_config.xml`'de sadece LAN range'lere (10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12) domain-config istisna ver

### C3. Mobile WS token URL'de leak ediyor
**Lokasyon:** `EnerjiOne Grid Mobile App/lib/src/services/live_telemetry_service.dart:71-77`
- `wss://.../ws/live-values?token=<JWT>` — token nginx access log + Referer + debug histogram + tarayıcı history'de
- Backend zaten `/auth/ws-ticket` endpoint sunuyor (30sn TTL, single-use)
- **Fix:** Önce `POST /auth/ws-ticket` çağır → dönen 30sn'lik ticket'i `?ticket=...` veya `Sec-WebSocket-Protocol` ile yolla

### C4. Backend worker'lar placeholder INTERNAL_SERVICE_TOKEN'a sessiz düşüyor
**Lokasyon:**
- `apps/alarm-service/alarm_service/main.py:44`
- `apps/notification-worker/notification_service/main.py:48`
- `apps/iec104-outbound/iec104_outbound/config.py:88-89`

```python
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "change-me-internal-token")
```
- Env unutulursa placeholder ile devam, backend 401 verir, worker sonsuz retry → log spam
- **Fix:** Her worker'ın başlangıcında `if token in PLACEHOLDERS or not token: sys.exit(1)` ile clear error mesajı

### C5. Backend placeholder set drift
**Lokasyon:** `apps/backend-api/app/core/config.py:9-13`
- `_PLACEHOLDER_SECRETS = {"change-me-in-production", "change-me-internal-token", ""}` ama `.env.example` `please-change-me-32-bytes-hex`, `change-me-strong-password` gönderiyor
- Operator install.sh atlayıp `.env.example` → `.env` kopyalarsa production validator yakalamıyor
- **Fix:** Pattern-based check:
```python
def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return (not v) or v.startswith(("change-me", "please-change-me", "change-this", "your-secret"))
```

### C6. Frontend `localStorage.accessToken` hâlâ aktif
**Lokasyon:** `apps/frontend-web/src/shared/api.ts:192-202, 246` + 90+ kullanım yeri
- Backend cookie auth hazır ama frontend hâlâ Bearer token + localStorage paralel akışı
- XSS sonrası token tamamen exfiltrate edilebilir
- **Fix:** `saveSession` yalnız `username/role` saklasın; tüm fetch'lerden `Authorization: Bearer` parametresini kaldır; `credentials: 'include'` ile cookie'ye güven

### C7. Mobile WS connection state UI'ya bağlı değil
**Lokasyon:** `EnerjiOne Grid Mobile App/lib/src/services/live_telemetry_service.dart` (reconnect logic var) + ekranlar
- `connectionState` stream'ini hiçbir ekran dinlemiyor
- Saha teknisyeni 4G/WiFi geçişinde "Canlı" rozet görmeyecek; reconnect sessizce olur ama UI son frozen değeri canlıymış gibi gösterir
- **Fix:** `device_detail_screen.dart`, `live_values_screen.dart`'ta `StreamBuilder<LiveTelemetryConnectionState>` ile rozet göster

---

## 🟠 HIGH — İlk Hafta İçinde Kapatılmalı

### H1. Backend `/health/ws-stats` ve `/health/dlq` auth'suz
**Lokasyon:** `apps/backend-api/app/api/health.py:161, 182`
- Anonim kullanıcılar subscriber count, NATS stream ismi, queue depth görüyor
- **Fix:** `Depends(require_roles([INSTALLER, ENGINEER]))` ekle

### H2. Backend idempotency service yazılmış ama çağrılmıyor
**Lokasyon:** `apps/backend-api/app/services/idempotency_service.py` (helpers var) + `apps/backend-api/app/api/internal.py:100` (çağırmıyor)
- `/internal/alarms` retry sonrası duplicate AlarmEvent satırı oluşabilir
- **Fix:** `ingest_alarm` başında `is_processed(message_id)` kontrol; success sonrası `mark_processed`

### H3. Gateway NATS auth/TLS context yok
**Lokasyon:** `EnerjiOne Grid DNP3 Gateway/src/dnp3_gateway/messaging/jetstream_publisher.py:189`
- `nats.connect()` çağrısında `tls=`, `user_credentials=`, `tls_hostname=` yok
- Backend production validator NATS auth zorluyor ama gateway tarafı paralel yok
- **Fix:** Settings'e `nats_credentials_path` + `nats_tls_ca_path` ekle; publisher'a ssl.SSLContext insa et + `user_credentials=` parametre geçir

### H4. Gateway `refresh_nonce` disk'e persist edilmiyor
**Lokasyon:** `EnerjiOne Grid DNP3 Gateway/src/dnp3_gateway/state.py:62, 186-201, 213-224`
- `load_from_cache()` `_refresh_nonce`'i restore etmiyor → restart sonrası gereksiz integrity poll
- **Fix:** `_persist_unsafe()` payload'ına `refresh_nonce` ekle + `load_from_cache` içinde restore

### H5. Gateway DNP3 cihaz IP allowlist yok
**Lokasyon:** `EnerjiOne Grid DNP3 Gateway/src/dnp3_gateway/backend/config_client.py:307-321`
- `_is_safe_device_ip` sadece scheme/slash/space yakaliyor; backend kompromize olursa `169.254.169.254` (cloud metadata), `8.8.8.8`, internal IP'lere TCP 20000 açılabilir
- **Fix:** `DNP3_DEVICE_ALLOWED_SUBNETS` config (CIDR listesi); `ipaddress.ip_network` ile her IP doğrula

### H6. Frontend 24 `window.confirm` callsite hâlâ native
**Lokasyon:** 11 dosyada 24 callsite (özellikle `GridManagementPanel.tsx`'te 7 tane)
- `ConfirmDialog` provider hazır, `useConfirm()` hook çalışıyor; sadece migrate işi
- **Fix:** Her dosyada `const { confirm } = useConfirm();` ekle, `window.confirm(...)` → `await confirm({message, danger})`

### H7. Frontend Material Symbols CDN (4G saha bloku riski)
**Lokasyon:** `apps/frontend-web/index.html:9-14`
- Google Fonts CDN; 4G saha proxy bloklarsa tüm ikonlar bozulur
- **Fix:** `@material-symbols/font-400` npm paketi kur, CDN linki kaldır

### H8. Infra: image `:latest` rollback imkânsız
**Lokasyon:** `EnerjiOne Grid/docker-compose.yml:145, 234, 263, 300, 331, 365`
- 6 servis `image: e1/<svc>:latest`; bozuk build push → tüm sistem down
- **Fix:** `:${E1_VERSION:-latest}` veya `:0.4.5` gibi semver tag

### H9. Infra: Windows installer yok
**Lokasyon:** Sadece `install.sh`, `update.sh`, `uninstall.sh` bash
- Local-LAN Windows müşterisi WSL2 + bash kurmadan kuramaz
- **Fix:** `install.bat` veya `install.ps1` — `docker compose pull && docker compose up -d` muadili

### H10. Backend Alembic baseline boş ama startup `ALTER TABLE` patten devam ediyor
**Lokasyon:** `apps/backend-api/app/main.py:108-673`
- 130+ idempotent ALTER startup'ta her boot çalışır; hata olursa partial schema
- **Fix:** main.py ALTER bloğunu freeze, yeni schema sadece `alembic_migrations/versions/`'da

### H11. Mobile inactivity timeout + auto-logout 401 handler yok
**Lokasyon:** `EnerjiOne Grid Mobile App/lib/src/core/api/api_client.dart:33-35`
- Telefon masada açık unutulursa SCADA verisi 8 saat görünür
- Backend token TTL bittiğinde her ekran "Ağ hatası" toast'ı verir (graceful logout yok)
- **Fix:** `AppLifecycleState.paused` → 10dk sonra logout; `Dio onError(401)` → auto logout + login redirect

### H12. Mobile HTTPS pinning yok
**Lokasyon:** `EnerjiOne Grid Mobile App/lib/src/core/api/api_client.dart`
- Mobil 4G üzerinden kurumsal CA / self-signed cert ile çalışıyorsa SSL handshake fail
- **Fix:** `dio_certificate_pinning` paketi veya `BadCertificateCallback` ile SPKI hash pin

---

## 🟡 MEDIUM — Pilot Sonrası 30 Gün

### M1-M15 (kısa liste)
- **Backend rate-limit** `GET /project-settings` (auth-free, base64 logo blob DoS riski) — `apps/backend-api/app/api/project_settings.py:131-137`
- **Backend pg_restore SUPERUSER yerine `e1_restore` rolü** — `apps/backend-api/app/services/backup_service.py:431`
- **Gateway backend config HMAC signature** — `apps/.../backend/config_client.py:190-269`
- **Gateway outbox SQLite chmod 600** — `apps/.../messaging/outbox.py:103`
- **Gateway `/refresh-all` rate-limit** — `apps/.../health_server.py:249-286`
- **Gateway `requirements.txt`'den `pika` legacy kaldır** — `requirements.txt:21`
- **Gateway `pip-compile`/`uv lock` lockfile** — repo root
- **Mobile polling pattern → FCM push** (7 ayrı `Timer.periodic`, 4G data israfı)
- **Mobile force-update gate** — backend `/health/min-version` çağrısı + block dialog
- **Mobile push tap deeplink routing** (`onMessageOpenedApp` / `getInitialMessage`)
- **Mobile root/jailbreak detection** (`flutter_jailbreak_detection`)
- **Frontend WS disconnect toast** — `App.tsx:735` `useEffect([liveSocket.connectionState])`
- **Frontend `tr-TR` hardcoded 80+ → `i18n.language`** (10 dosya)
- **Infra container hardening** — `read_only: true` + `cap_drop: [ALL]` + `no-new-privileges:true` worker'lara
- **Infra HTTPS terminator** (Caddy/Traefik) mobil 4G erişim varsa

---

## 🟢 LOW — Uzun Vade / Pilot Sonrası

- Backend `/health/dlq` audit eventi yok (WS connect/disconnect, manual telemetry ingest)
- Backend WS ticket + JWT revocation in-memory (multi-replica'ya geçince Redis)
- Backend manual telemetry message_id dedup (zaten gateway_ingest_batches UNIQUE var)
- Gateway log redaction NATS user/pass URL pattern
- Gateway `docs/SECURITY.md` güncellemesi (rebrand + RabbitMQ→NATS)
- Mobile yerelleştirme tam (`flutter_localizations` + ARB)
- Mobile offline mod (hive cache)
- Mobile Semantics erişilebilirlik
- Mobile splash Android 12+ uyumlu
- Mobile adaptive_icon_foreground safe-zone doğrula
- Mobile `secure_store.clearAllOnLogout` base_url + remember_me sil
- Frontend `<th scope>` tüm tablolarda
- Frontend dark mode
- Frontend empty state i18n tutarlılık
- Infra `docker-compose.prod.yml` override
- Infra backup disk-%85 proaktif alert
- Infra JetStream stream backup (`nats stream backup` cron)
- Infra `.env.example` development→production migration checklist
- Backend ENGINEER vs INSTALLER alarm_rules tutarlılık kararı

---

## ÖZET TABLO

| Kategori | Critical | High | Medium | Low |
|---|---|---|---|---|
| Mobile | 3 (iOS ATS, Android cleartext, WS token, WS state UI) | 2 (inactivity, pinning) | 4 | 7 |
| Backend | 3 (worker fail-fast, placeholder set drift, frontend localStorage) | 3 | 3 | 4 |
| Gateway | 0 | 3 (NATS auth, nonce persist, IP allowlist) | 4 | 2 |
| Frontend | 1 (localStorage token — backend C6 ile aynı) | 2 (confirm migrate, Material Symbols CDN) | 2 | 3 |
| Infra | 0 | 2 (image latest, Windows installer) | 2 | 3 |
| **Toplam** | **7 Critical** | **12 High** | **15 Medium** | **19 Low** |

---

## ÖNERİLEN SIRA (Pilot canlı için minimum)

**Sprint A — 1 hafta (Critical kapatma):**
1. Mobile iOS ATS + blur view + Android cleartext sıkılaştır
2. Mobile WS ticket akışına geç + connection state UI binding
3. Frontend `localStorage.accessToken` kaldır, tam cookie auth
4. Backend worker'lar fail-fast (INTERNAL_SERVICE_TOKEN placeholder reddi)
5. Backend `_PLACEHOLDER_SECRETS` startswith check'e çevir

**Sprint B — 1 hafta (High):**
6. Gateway NATS auth (creds + TLS context) + IP allowlist + nonce persist
7. Backend `/health/ws-stats` auth, idempotency wiring, image semver tag
8. Mobile inactivity timeout + HTTPS pinning
9. Frontend Material Symbols self-host + 24 confirm migrate
10. Infra Windows install.bat

**Pilot sonrası 30 gün: Medium maddeler**
**Sürekli iyileştirme: Low maddeler**

---

## VERDİĞİN KAPSAM DIŞI YENİ EKSİKLER YOK

- TLS / mTLS — ATLA (LAN içi plaintext kabul, sadece mobil 4G → backend HTTPS dış katmanda)
- Observability stack — ATLA (istemedin)
- CSRF — ATLA (cookie SameSite=Strict yeterli)
- Public expose hardening — ATLA (LAN dışı yok)
- NATS cluster HA — ATLA (single node OK)
