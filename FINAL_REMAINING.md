# EnerjiOne Grid — Son Kontrol: Kalan Eksikler

**Tarih:** 2026-05-13 (sprint sonrası 2. re-audit)
**Bugün yapılan:** 40 fix tamamlandı, tüm Critical (C4-C7) + 11 High (H1-H11) doğrulandı çalışıyor.
**Atlanılan:** HTTPS, observability, mTLS (kullanıcı kapsam dışı).

---

## 🟢 DOĞRULANAN POZİTİFLER

| Servis | Tamam Olan |
|---|---|
| **Backend** | Cross-cutting cookie/Bearer dual auth, 401 lockout audit, WebSocket Origin allowlist, backup restore RBAC, Dockerfile non-root uid 10001, NATS deny-all + per-user ACL |
| **Gateway** | Multi-instance lock çalışıyor, ThreadingHTTPServer, refresh_nonce persist + monotonic, NATS auth params, IP allowlist parse, `BACKEND_API_CA_PATH` zaten mevcut |
| **Mobile** | Release signing config, R8/obfuscate, FLAG_SECURE, WS reconnect+backoff, logout invalidate, inactivity tracker, 401 SessionExpiredBus |
| **Frontend** | ErrorBoundary, ConfirmDialog 29 callsite migrate, Material Symbols self-host, cookie-only auth, sanitizeErrorDetail, resource limits |

---

## 🔴 KALAN — Production Öncesi

### CRITICAL (1)

#### CR1. Default admin password hardcoded + enforce-change yok
**Lokasyon:** [seed_installer.py:10,27-31](../EnerjiOne%20Grid/apps/backend-api/scripts/seed_installer.py)
`DEFAULT_PASSWORD = "ChangeMe123!"` her install.ps1/install.sh çalıştırmasında **var olan installer'ın şifresini resetliyor**. `User` modelinde `must_change_password` kolonu yok; login flow zorla değiştirme yapmıyor.
**Fix:** (a) `seed_installer.py` user zaten varsa şifreye dokunmasın; (b) `User.must_change_password BOOL` kolonu + main.py ALTER + frontend ilk login'de change-password modal.

---

### HIGH (10)

#### H1. Mobile push tap deeplink routing
**Lokasyon:** [push_service.dart:54](../EnerjiOne%20Grid%20Mobile%20App/lib/src/core/notifications/push_service.dart)
`onMessageOpenedApp` ve `getInitialMessage()` hiç yok. Saha teknisyeni alarm bildirimine tıklayınca dashboard'a düşüyor, alarma değil.

#### H2. Mobile `AppConfig.defaultBaseUrl = 'http://10.0.2.2:8000'`
**Lokasyon:** [app_config.dart:3](../EnerjiOne%20Grid%20Mobile%20App/lib/src/core/config/app_config.dart)
Android emülatör loopback'i. Gerçek cihazda backend'e ulaşmaz; müşteri ilk açılışta hatalı URL ile karşılaşır. Prod-flavor `--dart-define=BASE_URL=` veya boş string + zorunlu input.

#### H3. Mobile iOS push entitlements yok
**Lokasyon:** [ios/Runner/Info.plist](../EnerjiOne%20Grid%20Mobile%20App/ios/Runner/Info.plist), `Runner.entitlements`
`aps-environment` + `UIBackgroundModes (remote-notification, fetch)` yok. iOS'ta push çalışmaz.

#### H4. Mobile logout token cleanup eksik
**Lokasyon:** [auth_controller.dart:85-103](../EnerjiOne%20Grid%20Mobile%20App/lib/src/features/auth/auth_controller.dart)
`clearAllOnLogout()` sadece `remember=false` ise çağrılıyor. `remember=true` durumunda `clearSession()` çağrılmıyor; `access_token` SecureStore'da kalıyor. Logout'un başına unconditional `clearSession()`.

#### H5. Backend internal_service_token caller trace yok
**Lokasyon:** [internal.py:28-38](../EnerjiOne%20Grid/apps/backend-api/app/api/internal.py)
Tek paylaşılan token, hangi servis çağırdı log'da yok. Breach forensic imkânsız. `X-Service-Name` header bekle + `_require_service_token` içinde `logger.info("internal_call svc=%s path=%s", ...)`.

#### H6. Backend `/auth/me/fcm-token` rate-limit yok
**Lokasyon:** [auth.py:326-371](../EnerjiOne%20Grid/apps/backend-api/app/api/auth.py)
Mobile reconnect storm + compromised user → DB row spam. `@limiter.limit("20/minute")`.

#### H7. Backend `pg_restore` SUPERUSER ile çalışıyor
**Lokasyon:** [backup_service.py:432-444](../EnerjiOne%20Grid/apps/backend-api/app/services/backup_service.py)
`POSTGRES_USER=enerjione` superuser; `validate_dump_file` gzip TOC'a enjekte SQL'i göremeyebilir. `--no-superuser-statements` flag yok. Dedicated `e1_restore` rolü oluştur.

#### H8. Gateway config response HMAC signature yok
**Lokasyon:** [config_client.py:190-269](src/dnp3_gateway/backend/config_client.py)
HTTPS atlandığı için MITM riski. Backend `X-Config-Signature: hmac_sha256(token, body)` yollasın, gateway `hmac.compare_digest` ile doğrulasın.

#### H9. Gateway `pika` legacy + lock dosyası yok
**Lokasyon:** [requirements.txt:21](requirements.txt)
`pika>=1.3,<2.0` production'da kullanılmıyor (rollback için). Saldırı yüzeyi + `requirements-lock.txt` yok → reproducible build yok. `pip-compile`/`uv lock`.

#### H10. Frontend `<th scope>` 93 yerde eksik
**Lokasyon:** 17 dosyada tablolar
Hiçbirinde `scope="col"`/`scope="row"` yok. Screen reader tablo bağlamını kuramaz. WCAG 1.3.1 fail.

---

### MEDIUM (14)

#### Backend (4)
- **AlarmRule INSTALLER-only**, diğer admin endpoint'ler `[INSTALLER, ENGINEER]` — tutarsız ([alarm_rules.py:94,132,167](../EnerjiOne%20Grid/apps/backend-api/app/api/alarm_rules.py))
- **Dev CORS `*` + credentials** — `app_env != production` durumunda `*` regex yerine `http://localhost` ([config.py:63](../EnerjiOne%20Grid/apps/backend-api/app/core/config.py))
- **FCM placeholder boot log spam** — `fcm.py:_load` placeholder JSON için sessiz skip ([fcm.py:67-78](../EnerjiOne%20Grid/apps/backend-api/app/services/fcm.py))
- **`requirements.txt` floating ranges** — pip-compile --generate-hashes ile lock + hash

#### Gateway (3)
- **NATS user:pass URL log redaction yok** — [logging_setup.py:51](src/dnp3_gateway/logging_setup.py); `(amqp|nats)[s]?://` genişlet
- **Outbox SQLite chmod 600 yok** — [outbox.py:103](src/dnp3_gateway/messaging/outbox.py); POSIX `os.chmod(0o600)`
- **`/refresh-all` rate-limit yok** — [health_server.py:249-286](src/dnp3_gateway/health_server.py); per-IP sliding window

#### Mobile (4)
- **`_firebaseBackgroundHandler` boş** — [main.dart:13-18](../EnerjiOne%20Grid%20Mobile%20App/lib/main.dart); data-only payload + badge sync
- **Polling fırtınası** — 7 ayrı `Timer.periodic` (devices 5sn, vd.); AppLifecycle paused → durdur, FCM push-driven
- **Force-update gate yok** — `/health/min-version` çağrısı
- **Offline cache yok** — `hive`/`shared_preferences` ile son alarm/device listesi cache

#### Frontend (3)
- **WS disconnect toast yok** — App.tsx'te `useEffect([liveSocket.connectionState])` ile toast.warning
- **`tr-TR` hardcoded 44 callsite** — merkezi `formatDateTime(value, lang)` helper
- **Form HTML5 validation min/max yetersiz** — kritik sayı alanlarına (dnp3_address 0-65535) ekle

---

### LOW (15)

#### Backend (3)
- Logout endpoint cookie'den de jti revoke etmeli ([auth.py:288-301](../EnerjiOne%20Grid/apps/backend-api/app/api/auth.py))
- Backup download `record_event` audit eksik ([backups.py:152](../EnerjiOne%20Grid/apps/backend-api/app/api/backups.py))
- Audit eksikleri (WS connect/disconnect, manual telemetry ingest)

#### Gateway (5)
- `docs/SECURITY.md` rebrand/NATS güncellemesi (Horstmann user-agent referansı, RabbitMQ bölümü)
- `docs/RUNBOOK.md` yeni env'ler (`NATS_TLS_CA_PATH`, `NATS_CREDENTIALS_PATH`, `DNP3_DEVICE_ALLOWED_SUBNETS`)
- `docs/ARCHITECTURE.md` diyagram NATS/outbox/instance_lock
- `scripts/install.ps1` `.env` ACL kısıtlaması (`icacls`)
- Outbox dead_letter retention (haftalık prune)

#### Mobile (5)
- Splash Android 12+ (`flutter_native_splash`)
- `adaptive_icon_foreground` safe-zone'lu asset
- Root/jailbreak detection (`flutter_jailbreak_detection`)
- Erişilebilirlik (`Semantics` widget 0 kullanım)
- Yerelleştirme (`flutter_localizations` + ARB, 98 dosya TR hardcoded)

#### Frontend + Infra (2)
- Toast queue max-cap (5'ten fazla → slice)
- `React.memo` büyük tablolarda

---

## 📋 Pilot Canlıya Çıkmadan ÖNCE Yapılacaklar

**Minimum (1-2 gün):**

1. **CR1** — Default admin pwd + must_change_password (en kritik)
2. **H1** — Mobile push deeplink routing (saha UX için şart)
3. **H4** — Mobile logout cleanup (security)
4. **H6** — Backend fcm-token rate-limit (basit ek)
5. **H8** — Gateway config HMAC (HTTPS yoksa daha kritik)
6. **H9** — Gateway lock file + pika kaldır (supply-chain)

**Pilot sonrası 1 hafta:**
- H2, H3, H5, H7, H10 + 14 Medium maddesi

**Sürekli iyileştirme:**
- 15 Low maddesi (doc, accessibility, polish)

---

## Toplam Tablo

| Kategori | Critical | High | Medium | Low |
|---|---|---|---|---|
| Backend | 1 | 3 | 4 | 3 |
| Gateway | 0 | 2 | 3 | 5 |
| Mobile | 0 | 4 | 4 | 5 |
| Frontend | 0 | 1 | 3 | 2 |
| **Toplam** | **1** | **10** | **14** | **15** |

**Önceki audit'e göre değişim:**
- Bugün başında 7 Critical → şimdi **1** (kullanıcı 3 mobile HTTPS-related Critical'i atladı, 3 kapatıldı, 1 yeni keşif: default admin pwd)
- 12 High → şimdi **10** (11 fix kapatıldı, yeniler ortaya çıktı: caller trace, FCM RL, pg_restore privilege)

**Yorum:** Bugün çok ilerleme oldu. Kalan 1 Critical ufak iş (kolon + flag). 10 High'ın çoğu mobile (HTTPS atlandığı için bunlar göze daha çok battı). **Pilot sahaya çıkmak için minimum 6 madde kalıyor; 1-2 gün iş.**
