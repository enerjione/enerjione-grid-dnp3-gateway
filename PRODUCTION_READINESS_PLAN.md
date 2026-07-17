# EnerjiOne Grid — Production Hazırlık Planı

**Tarih:** 2026-05-12
**Kapsam:** DNP3 Gateway · Backend (backend-api, alarm-service, tag-engine, iec104-outbound, notification-worker) · Frontend-web · Mobile (Flutter) · Infra (Docker, NATS, CI/CD)
**Yöntem:** 5 paralel kaynak-kod audit (siber güvenlik + UI/UX + operability)

---

## Yönetici Özeti

Sistem mimari olarak sağlam: rol bazlı auth, NATS deny-all default, gateway token hash + timing-safe karşılaştırma, bcrypt cost 12, JWT jti revocation, gateway tarafında production validator, backup magic-byte/SVG redd, vault. **Ancak production internet'e açılmadan önce 14 kritik bulgu kapatılmalı**, çoğu konfigürasyon/küçük kod değişikliği seviyesinde.

**En büyük 3 sistemik risk:**
1. **TLS hiçbir L7 hattında yok** — backend HTTP, NATS plaintext, AMQP plaintext, mobil cleartext açık. Public expose'da MITM.
2. **Auth model parçalı** — JWT 30 gün TTL + in-memory revoke + CSRF yok + cookie/header çift yol + IP spoofing açık.
3. **Observability sıfır** — Prometheus/Grafana/Alertmanager yok, image versioning yok (`:latest`), rollback prosedürü yok.

---

## 1. KRİTİK (deploy öncesi — 2 hafta)

> Bunlar olmadan production'a çıkmayın.

### 1.1 Network güvenliği — TLS her yerde
| # | Madde | Konum | Önerilen değişiklik |
|---|---|---|---|
| K1 | NATS TLS + auth zorla | [infra/nats/nats-server.conf:99](../EnerjiOne%20Grid/infra/nats/nats-server.conf) | TLS server cert + `verify_and_map`; gateway → JWT/NKEY auth; `tls://` URL + cert dosyaları gateway env'ine ekle |
| K2 | NATS Gateway client TLS context geç | [src/dnp3_gateway/messaging/jetstream_publisher.py:189](src/dnp3_gateway/messaging/jetstream_publisher.py) | `NATS_TLS_CA_PATH`, `NATS_CREDS_PATH` config + `ssl.SSLContext` inşa |
| K3 | RabbitMQ TLS (5671) zorla, 5672 expose kaldır | [docker-compose.yml:96](../EnerjiOne%20Grid/docker-compose.yml) | AMQPS + cert; public port-map yalnızca 5671 + management UI internal |
| K4 | Backend HTTPS terminator zorunlu | nginx/Caddy önünde | Compose'da reverse proxy servisi; backend yalnızca internal port |
| K5 | Mobile cleartext + ATS kapat | [AndroidManifest.xml:13](../EnerjiOne%20Grid%20Mobile%20App/android/app/src/main/AndroidManifest.xml), [Info.plist:48](../EnerjiOne%20Grid%20Mobile%20App/ios/Runner/Info.plist) | Prod flavor → `usesCleartextTraffic="false"`, `NSAllowsArbitraryLoads=false` |
| K6 | Mobile HTTPS pinning | [api_client.dart](../EnerjiOne%20Grid%20Mobile%20App/lib/src/core/api/api_client.dart) | `dio_certificate_pinning` SPKI hash + kurumsal CA fallback |

### 1.2 Auth & Authorization
| # | Madde | Konum | Önerilen değişiklik |
|---|---|---|---|
| K7 | CSRF koruması | [backend-api/app/api/auth.py:78](../EnerjiOne%20Grid/apps/backend-api/app/api/auth.py) | Double-submit cookie veya per-session `X-CSRF-Token`; mutating endpoint'lerde zorunlu |
| K8 | JWT TTL ≤15dk + refresh-token DB | [backend-api/app/core/config.py:25](../EnerjiOne%20Grid/apps/backend-api/app/core/config.py) | Access 15dk; refresh token opaque hash DB + rotation; revocation DB/Redis (multi-replica + restart safe) |
| K9 | `forwarded-allow-ips` daralt | [backend-api/Dockerfile:75](../EnerjiOne%20Grid/apps/backend-api/Dockerfile) | Docker compose subnet'i; nginx `set_real_ip_from` + `real_ip_recursive on` |
| K10 | Auth header yolunu prod'da kapat | [backend-api/app/api/deps.py:23](../EnerjiOne%20Grid/apps/backend-api/app/api/deps.py) | Tek canonical auth yolu (cookie); legacy header sadece dev flag arkasında |
| K11 | Frontend token → HttpOnly cookie tam migrasyon | [frontend-web/src/shared/api.ts:147](../EnerjiOne%20Grid/apps/frontend-web/src/shared/api.ts) | `localStorage.accessToken` kaldır; sadece username/role kalsın |

### 1.3 Mobile production gateway
| # | Madde | Konum | Önerilen değişiklik |
|---|---|---|---|
| K12 | Release signing config | [android/app/build.gradle.kts:38](../EnerjiOne%20Grid%20Mobile%20App/android/app/build.gradle.kts) | Upload keystore + `key.properties` (CI secret); Play App Signing |
| K13 | R8 + obfuscate | aynı dosya | `isMinifyEnabled=true`, `isShrinkResources=true`, `flutter build --obfuscate --split-debug-info=` |
| K14 | FLAG_SECURE + iOS resignActive blur | MainActivity, SceneDelegate | App switcher snapshot'tan veri sızıntısı kapat |

### 1.4 Secret hijyeni & supply chain
| # | Madde | Konum | Önerilen değişiklik |
|---|---|---|---|
| K15 | `infra/nats/nats-server.conf` repo'dan kaldır | [infra/nats/nats-server.conf](../EnerjiOne%20Grid/infra/nats/nats-server.conf) | `.gitignore`'a al; sadece `.template` repo'da; install.sh render etsin (bcrypt hash leak vektörü) |
| K16 | `fcm-service-account.json` doğrula | [apps/backend-api/](../EnerjiOne%20Grid/apps/backend-api/) | `.gitignore`'da var ✓ — git history'de hiç commit edilmediğini doğrula (`git log --all -- fcm-service-account.json`); şüphe varsa Firebase service account rotate |
| K17 | Gateway `.env.example` `SHOW_GATEWAY_TOKEN_ON_START=true` | [.env.example:77](.env.example) | Default `false` (dev/staging token leak) |
| K18 | Image versioning `:latest` kaldır | [docker-compose.yml:145+](../EnerjiOne%20Grid/docker-compose.yml) | Semver tag (`e1/backend-api:0.4.5`); update.sh git tag → image tag map |

### 1.5 Veri bütünlüğü
| # | Madde | Konum | Önerilen değişiklik |
|---|---|---|---|
| K19 | Gateway multi-instance lock | [src/dnp3_gateway/auth/identity.py:53](src/dnp3_gateway/auth/identity.py) | `fcntl.flock` / `msvcrt.locking` outbox SQLite üzerinde; iki proses aynı GATEWAY_CODE ile başlayamasın |
| K20 | Backup upload size + streaming cutoff | [backend-api/app/api/backups.py:235](../EnerjiOne%20Grid/apps/backend-api/app/api/backups.py) | Content-Length pre-check (2 GB cap), tmpfs'e atomic rename |

**Tahmini efor:** 1 senior backend dev + 1 mobile dev + 1 DevOps × 2 hafta tam zaman.

---

## 2. YÜKSEK (ilk 30 gün)

### 2.1 Saldırı yüzeyi
| Madde | Konum |
|---|---|
| Telemetry direct-ingest endpoint RBAC + rate-limit | [backend-api/app/api/telemetry.py:28](../EnerjiOne%20Grid/apps/backend-api/app/api/telemetry.py) |
| Rate limit tüm hassas endpoint'lere (ws-ticket, password change, public API) | [backend-api/app/core/rate_limit.py:32](../EnerjiOne%20Grid/apps/backend-api/app/core/rate_limit.py) |
| INTERNAL_SERVICE_TOKEN → servis bazlı + scope | [backend-api/app/api/internal.py:28](../EnerjiOne%20Grid/apps/backend-api/app/api/internal.py) |
| Gateway /health'ten `devices_detail` kaldır (auth'lu /info'ya) | [src/dnp3_gateway/health_server.py:330](src/dnp3_gateway/health_server.py) |
| Gateway ThreadingHTTPServer | [src/dnp3_gateway/health_server.py:452](src/dnp3_gateway/health_server.py) |
| Gateway DNP3 cihaz IP allowlist (`DNP3_DEVICE_ALLOWED_SUBNETS`) | [src/dnp3_gateway/backend/config_client.py:307](src/dnp3_gateway/backend/config_client.py) |
| Backend config payload HMAC imza (replay/downgrade) | gateway + backend |
| Frontend CSP `connect-src 'self'` | [frontend-web/nginx.conf:64](../EnerjiOne%20Grid/apps/frontend-web/nginx.conf) |
| Backend hata yanıtlarında generic + correlation_id | [backend-api/app/api/internal.py:483](../EnerjiOne%20Grid/apps/backend-api/app/api/internal.py) |
| Mobile WS token query → header/protocol | [live_telemetry_service.dart:27](../EnerjiOne%20Grid%20Mobile%20App/lib/src/services/live_telemetry_service.dart) |
| Mobile inactivity timeout + biyometrik re-auth | `auth_controller.dart` |
| CI: cosign image sign + Trivy scan + SHA-pinned actions | [.github/workflows/release-image.yml](.github/workflows/release-image.yml) |
| Container hardening (read_only, cap_drop, no-new-privileges) | [docker-compose.yml](../EnerjiOne%20Grid/docker-compose.yml) |
| Backend Dockerfile multi-stage (build tooling runtime'dan çıkar) | [backend-api/Dockerfile](../EnerjiOne%20Grid/apps/backend-api/Dockerfile) |
| Dependency lock + `pip-audit` CI'da | requirements.txt'ler |

### 2.2 UX & Operability
| Madde | Konum |
|---|---|
| Frontend ErrorBoundary + retry | `apps/frontend-web/src/app/App.tsx` |
| Frontend tek `<ConfirmDialog>` — 18 `window.confirm` migrate | birden fazla |
| Frontend WS bağlı/kopuk header rozeti | sabit konum |
| Frontend Vite `esbuild.drop: ['console','debugger']` + `sourcemap: false` | [vite.config.ts](../EnerjiOne%20Grid/apps/frontend-web/vite.config.ts) |
| Mobile WS reconnect + connectivity göstergesi | `live_telemetry_service.dart` |
| Mobile "Şifremi unuttum" + force-update gate | `login_screen.dart` |
| Mobile push tap → deeplink routing (`onMessageOpenedApp`) | `push_service.dart` |
| Mobile logout'ta tüm Riverpod provider invalidate | `auth_controller.dart:65` |
| Mobile polling pattern → WS push-only | `notifications_controller.dart:39` |
| Gateway Prometheus exporter (`/metrics` text format) | `src/dnp3_gateway/health_server.py:233` |
| Backend structured logging + correlation_id everywhere | tüm servisler |

---

## 3. ORTA (ilk 60 gün)

### 3.1 Observability stack (komple eksik — eklenecek)
- **Prometheus** + **Grafana** + **Loki + Promtail** + **Alertmanager** compose'a ekle
- Backend, gateway, worker'lara `prometheus_client` instrumentation
- OpenTelemetry tracing (FastAPI + nats-py auto-instrument)
- Alert kuralları: disk %95, NATS down, postgres replication lag, JetStream consumer lag, gateway last_cycle stale, broker_publish_failures spike
- Alertmanager → Slack/Email/PagerDuty

### 3.2 NATS HA & backup
- 3-node cluster veya leaf node mimarisi
- JetStream stream `replicas: 3`
- `nats stream backup` cron + retention
- JS disk dolma alerti (%85 threshold)

### 3.3 Backup & DR
- Off-site backup (rclone → S3/Backblaze)
- Backup encryption at rest (age/gpg)
- Quarterly restore drill prosedürü dokümante
- Backup retention policy (14 gün veya N-son)

### 3.4 Mobile (operasyonel)
- Yerelleştirme tamamla (`flutter_localizations` + ARB)
- Offline mod (hive read-cache + write-queue)
- Skeleton/shimmer yükleme
- Erişilebilirlik (Semantics) PR'ı
- Root/jailbreak detection (`freerasp`)
- App switcher screenshot blur (iOS) + Android FLAG_SECURE — K14'te yapıldıysa atla
- App icon adaptive_icon_foreground düzelt

### 3.5 Frontend (cilalama)
- Dark mode (CSS variables)
- Material Symbols self-host (CSP daralt)
- Tarih lokalizasyon helper'ı (`formatDateTime(value, lang)`)
- Logout'ta tüm `hsl.*` localStorage temizle
- Tablo erişilebilirliği (`<th scope>`, modal focus trap)
- Empty state i18n standardize

### 3.6 Backend (uzun vade)
- Alembic'e tam migrasyon (raw ALTER zincirini kapat)
- `organization_id` multi-tenant (sahibi olunacaksa)
- Argon2id'e geç (bcrypt 3.x → 4.x veya argon2-cffi)
- Password policy (min 12 char, kompleksite)
- WebSocket Origin allowlist (Origin-less reddi)
- Worker INTERNAL_SERVICE_TOKEN scope ayrımı

### 3.7 Infra
- `compose.prod.yml` override (read_only, cap_drop, monitoring stack)
- `install.sh` SHA-256 publish + minisign imza
- Default password ilk login'de zorunlu değiştir
- Vault/Doppler/sops entegrasyonu (uzun vade)
- arm64 image build (yadnp3 wheel gelince)

---

## 4. DÜŞÜK (ilk 90 gün — sürekli iyileştirme)

- Log redaction'ı genişlet (generic credential regex)
- Worker hata mesajı sanitization
- `state.py` refresh_nonce disk cache'e yaz
- Gateway `GATEWAY_STATE_DIR` default'u `%PROGRAMDATA%`
- `update.sh` backup retention (14 gün)
- `uninstall.sh` `--keep-data` flag
- `.dockerignore` template standardize
- Tag-engine pydantic schema + boyut limit
- Notification-worker poison message log redact
- FCM badge clear (iOS) + sync
- Mobile image caching (`cached_network_image`, flutter_map tile cache)

---

## 5. Önerilen Sprint Planı

| Sprint (2 hafta) | Konu | Sahip |
|---|---|---|
| **Sprint 1** | TLS her yerde (K1-K6) + Gateway lock (K19) + `:latest` → semver (K18) | DevOps + Backend |
| **Sprint 2** | Auth model (K7-K11) + mobile prod (K12-K14) + secret hijyeni (K15-K17, K20) | Backend + Mobile |
| **Sprint 3** | YÜKSEK güvenlik (rate limit, telemetry RBAC, INTERNAL_SERVICE_TOKEN scope, CI sign+scan, container hardening) | Backend + DevOps |
| **Sprint 4** | YÜKSEK UX (Frontend ErrorBoundary/ConfirmDialog/WS göstergesi + Mobile WS reconnect/force-update/deeplink/biyometrik) | Frontend + Mobile |
| **Sprint 5** | Observability stack (Prometheus + Grafana + Loki + Alertmanager + OTel) | DevOps + Backend |
| **Sprint 6** | NATS HA + backup off-site + DR drill + Alembic migrasyon | DevOps + Backend |
| **Sprint 7-8** | Mobile lokalizasyon + offline + erişilebilirlik + frontend dark mode/cilalama | Mobile + Frontend |

---

## 6. Hızlı Karar Noktaları

Aşağıdaki kararlar plana başlamadan netleşmeli (1 saatlik review yeterli):

1. **Multi-tenant gerekecek mi?** Şu an `organization_id` modeli yok. Tek-kurumsal-deploy olacaksa dokümante et; aksi halde data model değişikliği 60 günde yapılmalı.
2. **Public expose mu, VPN içi mi?** Backend + NATS + IEC104 public IP'de mi olacak? VPN içi ise TLS hâlâ önerilir ama urgency düşer.
3. **Mobil flavor stratejisi:** dev/stg/prod ayrı flavor mu (recommended) yoksa tek build + runtime config mi?
4. **Observability hosting:** self-host (Prometheus stack) mi, SaaS (Grafana Cloud / Datadog) mi?
5. **Refresh token strategy:** opaque token + DB mi, JWT refresh mi? (DB önerilir — revocation kolay.)

---

## 7. Audit Kapsamına Girmemiş Bölgeler

- **Penetration test yapılmadı** — yukarıdaki bulgular statik analiz; gerçek pentest (OWASP ZAP, Burp) ile dinamik bulgular eklenecek.
- **DPI/protokol fuzzing** — DNP3 ve IEC104 parser'ları malformed input'a karşı test edilmedi.
- **Load test** — gateway 100 cihaz × 6 instance + NATS JetStream + backend throughput ölçülmedi.
- **Yetki matrisi tablosu** — her endpoint × rol RBAC matrisinin tam denetimi yapılmadı (örnek bulgular var).
- **GDPR/KVKK uyumluluk** — kişisel veri envanteri + retention policy + DSAR (data subject access request) akışı denetlenmedi.

---

**Sonuç:** Sistem **production-ready değil ama production-attainable**. 14 kritik bulguyu 2 sprint içinde kapatarak güvenli pilot sahaya çıkılabilir; yüksek+orta öncelikler 60 günlük yol haritası ile tamamlanabilir. Observability stack eklenmeden hiçbir koşulda canlıya çıkılmamalı — sorunları göremezsiniz.
