"""Gateway konfigurasyonu - environment degiskenleri + .env dosyasindan okunur."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Production validator placeholder-token tespiti icin prefix listesi.
#
# .env.example ve eski seed scriptlerinden gelen jenerik degerler buraya
# alinmis. Operator gercek backend `gateways.token` ile eslesen token'i
# atamadiysa boot SystemExit ile reddedilir — saha kurulumlarinda en sik
# yapilan hata, .env.example'dan .env'i kopyalayip token'i degistirmeyi
# unutmak. Karsilastirma case-insensitive ve trimmed olarak yapilir.
_PLACEHOLDER_TOKEN_PREFIXES: tuple[str, ...] = (
    "change-me",
    "change_me",
    "changeme",
    "please-change",
    "please_change",
    "your-secret",
    "your_secret",
    "gw-default",
    "gw_default",
    "gw-001-token",  # .env.example default
    "gw-002-token",
    "default-token",
    "example-token",
    "secret-token",
    "test-token",
)


#: Smart sessizlik esiginin env yedegi icin kabul edilen aralik.
#: `config_client._SMART_SILENCE_MIN/MAX_SEC` ile AYNI olmak ZORUNDA
#: (tests/test_smart_session_contract.py parity testi kilitler): cihaz alani
#: ile env yedegi farkli sinirlar kabul etseydi ayni deger bir yoldan gecip
#: digerinden reddedilirdi.
_SMART_SILENCE_ENV_MIN = 60
_SMART_SILENCE_ENV_MAX = 30 * 24 * 3600


def _is_placeholder_token(token: str) -> bool:
    """Token, bilinen bir placeholder prefix ile basliyor mu?

    Production validator'inda kullanilir. Trim + lower + startswith mantigi;
    bos/None token False doner (caller ayrica zorunlulugu kontrol eder).
    """
    if not token:
        return False
    t = token.strip().lower()
    if not t:
        return False
    return t.startswith(_PLACEHOLDER_TOKEN_PREFIXES)


def _host_is_private(host: str | None) -> bool:
    """Host private/loopback ag uzerinde mi? (RFC1918, link-local, loopback, ULA).

    True donerse clear-text HTTP/nats:// MITM riski "kontrollu ag" altinda
    kalir — production validator'da bu host'lara izin verilebilir. Public
    hostname (DNS adi, hicbir IP'ye cevrilemez) veya routable IP icin
    False doneriz; o durumda HTTPS/TLS zorunlu olur.

    Iceriye `localhost` ve `*.local` (mDNS) da private kabul edilir.
    Hostname icin DNS lookup yapmiyoruz — sahada lookup yan etkili olabilir
    (timeout, MITM); operator'in IP veya isim tercihi yeterli sinyal.
    """
    if not host:
        return False
    h = host.strip().lower()
    if not h:
        return False
    if h in ("localhost", "localhost.localdomain"):
        return True
    if h.endswith(".local") or h.endswith(".lan") or h.endswith(".internal"):
        return True
    # IPv6 brackets ([::1]) ayikla
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        # DNS adi — private suffix kontrolu yukarida; geri kalan public
        # kabul.
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


class Settings(BaseSettings):
    """Tum gateway ayarlarini tek yerde toplayan pydantic modeli.

    Oncelik sirasi:
      1. process env degiskenleri
      2. `.env` dosyasi
      3. asagidaki varsayilanlar
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- Gateway kimligi -----------------------------------------------------
    gateway_code: str = Field(default="GW-001", description="Backend 'gateways.code' ile eslesen kimlik")
    gateway_token: str = Field(default="gw-default-token", description="Backend 'gateways.token' degeri")
    gateway_refresh_token: str = Field(
        default="",
        description=(
            "POST /refresh-all icin ayri token (rol ayrimi). Bos birakilirsa "
            "endpoint devre disi kalir. GATEWAY_TOKEN'dan FARKLI olmali; ayni "
            "ise backend tokeni leak olunca tum gateway'leri uzaktan yorma "
            "(DoS) acilir."
        ),
    )
    gateway_command_token: str = Field(
        default="",
        description=(
            "POST /operate icin ayri Bearer token. Backend cihaz komutlarini "
            "(CROB) bu token ile gateway'e proxy eder. Bos ise komut endpoint'i "
            "devre disi kalir (503). GATEWAY_TOKEN'dan FARKLI olmali."
        ),
    )
    gateway_command_delivery_token: str = Field(
        default="",
        description=(
            "KUYRUKLANMIS KOMUT DUZLEMI icin ayri credential (F5). "
            "`GET /pending`, `POST /command-delivery-acks` ve "
            "`POST /command-results` isteklerinde `X-Gateway-Command-Token` "
            "basligiyla gonderilir ve `/pending` yanit imzasinin HMAC "
            "ANAHTARI olur.\n"
            "\n"
            "NEDEN: bugun `/config` ile `/pending` ayni `GATEWAY_TOKEN` ile "
            "korunuyor. O token sizarsa yalnizca konfigurasyon degil FIZIKSEL "
            "KOMUT duzlemi de ele gecer. Ayri sir, komut duzlemini config "
            "duzleminden ayirir.\n"
            "\n"
            "BOS = mevcut (v1.10) davranis: komut uclari da `GATEWAY_TOKEN` "
            "kullanir. F5 rollout TAMAMLANDI (backend F5A + gateway F5B); bu "
            "yalnizca henuz provision edilmemis kurulumlarin calismaya devam "
            "etmesi icin korunan GERIYE DONUK UYUMLULUK yoludur, onerilen "
            "yapilandirma degildir.\n"
            "\n"
            "DOLU ise `/pending` yaniti YALNIZCA bu anahtarla dogrulanir — "
            "`GATEWAY_TOKEN`a geri dusulmez. `GATEWAY_TOKEN` ve "
            "`GATEWAY_COMMAND_TOKEN` ile AYNI olamaz. `/operate` bu token'i "
            "KULLANMAZ; o uc `GATEWAY_COMMAND_TOKEN` ile korunmaya devam eder."
        ),
    )
    gateway_name: str = Field(default="EnerjiOne DNP3 Gateway", description="Log/health icin insan-okur isim")

    # Ornek/destek: bos ise `GATEWAY_STATE_DIR` altinda kalici uuid dosyasina yazilir
    gateway_instance_id: str = Field(default="", description="Benzersiz proses ornek id (log/baglanti)")
    gateway_state_dir: str = Field(
        default=".gateway_state",
        description="instance_id dosyasinin tutulacagi dizin (coklu gateway icin ayri kod)",
    )

    # Gelistirme: development. Staging/production: token min uzunluk + placeholder yasak
    app_environment: str = Field(
        default="development",
        description="development | staging | production (kisa: dev, stg, prod)",
    )
    gateway_token_min_length_staging: int = Field(default=16, ge=8, le=256)
    gateway_token_min_length_production: int = Field(default=32, ge=16, le=256)

    # ----- Insecure plaintext opt-out (geçici, bilincli) -----------------------
    # Saha senaryosu: backend henuz TLS sertifikasi olmayan public IP'de
    # (orn. http://77.83.37.44:8000/...) ve gateway prod ortaminda calisiyor.
    # Default validator clear-text HTTP'yi public host'a yasakliyor — dogru
    # karar. Operator gectigine ZORUNDA kalirsa bu bayragi `true` set edip
    # opt-out edebilir. Boot'ta loud WARN log atilir; saklamak/maskelemek YOK.
    # Uretim defacto kullanim: gecici, plan = Caddy/Let's Encrypt ile TLS ekle.
    gateway_insecure_allow_plaintext: bool = Field(
        default=False,
        description=(
            "DEFAULT FALSE. True yapilirsa production ortaminda public host'a "
            "clear-text http:// + nats:// (TLS-siz) izin verilir. Gateway "
            "token + tum telemetri MITM riski altinda olur. Sadece TLS "
            "kurulana kadar gecici saha calistirma icin."
        ),
    )

    # ----- Calisma modu --------------------------------------------------------
    gateway_mode: str = Field(default="mock", description="mock | dnp3")

    # DNP3 master kutuphane secimi:
    #   yadnp3 (varsayilan) = OpenDNP3 reference; full DNP3 standardi,
    #     Group 110 (Octet String) destekler, event-driven (AddClassScan), tum
    #     outstation'larla %100 uyumlu (cunku ayni outstation kutuphanesi).
    #   dnp3py = nfm-dnp3 saf python; daha hafif ama Group 110 yok ve OpenDNP3
    #     outstation'lar ile tutarsiz davranis (TCP RST, transport segment).
    dnp3_library: str = Field(
        default="yadnp3",
        description="DNP3 master kutuphanesi: yadnp3 (onerilen) | dnp3py (legacy)",
    )

    # ----- Backend API ---------------------------------------------------------
    backend_api_url: str = Field(default="http://127.0.0.1:8000/api/v1")
    backend_api_verify_ssl: bool = Field(default=True, description="False sadece dev/test (MITM riski)")
    require_backend_response_signature: bool = Field(
        default=True,
        description=(
            "Backend'in `/config` ve `/pending` 200 yanitlarinda "
            "`X-Config-Signature` (HMAC-SHA256) ZORUNLU olsun mu. "
            "VARSAYILAN ACIK.\n"
            "\n"
            "NEDEN: bu iki uc cihaz katalogunu (F1/F2 yetkilendirmesinin "
            "girdisi) ve FIZIKSEL KOMUT niyetini tasiyor. Saha gateway'leri "
            "backend'e duz HTTP ile baglaniyor, yani imza bu iki uc icin TEK "
            "authenticity kontrolu. Basligi dusurebilen bir saldirgan katalogu "
            "degistirip F1/F2'yi etkisiz kilabilir ya da komut enjekte "
            "edebilirdi.\n"
            "\n"
            "`false` YALNIZCA imza GONDERMEYEN eski bir backend'e kontrollu "
            "rollback icindir ve GECICIDIR. Baslik GELDIYSE her kosulda "
            "dogrulanir — bu bayrak gecersiz bir imzayi ASLA bypass etmez."
        ),
    )
    backend_api_ca_path: str | None = Field(
        default=None,
        description="TLS icin ozel CA bundle yolu; bos = sistem varsayilani + verify_ssl",
    )
    # Config nadir degisir -> seyrek cek (5dk). Config degisince backend
    # config_nonce'u artirir; komut-poll bunu gorup config'i HEMEN ceker (5dk
    # beklemez). Komut artik AYRI command_poll_sec kanaliyla gelir.
    # 300 -> 60: cihaz eklendiginde ANLIK tetik `config_nonce` ile gelir
    # (komut-poll 1sn'de bir okur), ama o tetik herhangi bir sebeple
    # calismazsa periyodik refresh tek yedektir. Sahada tam bu oldu: backend
    # `/pending`e 500 dondu, nonce okunamadi ve yeni eklenen cihaz 5 DAKIKA
    # gorulmedi. 60sn ile en kotu durumda 1 dakika beklenir.
    #
    # Maliyet ~sifir: config istekleri ETag ile SARTLI; degismediyse backend
    # 304 doner ve govde HIC inmez.
    config_refresh_sec: int = Field(default=60, ge=5, le=3600)
    # Hafif komut-poll araligi: pending komutlar + nonce'lar. Komut anlik gelsin
    # diye kisa (1sn). GET /gateways/{code}/pending — agir config serialize yok.
    command_poll_sec: int = Field(default=1, ge=1, le=60)
    command_max_age_sec: float = Field(
        default=120.0,
        ge=5.0,
        le=86400.0,
        description=(
            "Bekleyen bir DNP3 komutunun AZAMI YASI (sn). Backend'in bildirdigi "
            "`created_at` bu sureden eskiyse komut CALISTIRILMAZ ve `expired` "
            "olarak bildirilir. Gerekce: gateway 30 dakika kapali kaldiktan "
            "sonra acildiginda kuyrukta bekleyen bir komut (orn. firmware "
            "update / software reset) oldugu gibi calisiyordu.\n"
            "\n"
            "120 sn olculerek secildi: sahada komutun kuyruktan gateway'e "
            "teslimi p95=0.92 sn, uctan uca en kotu gozlem 10.1 sn (adapter "
            "CROB timeout'u). 120 sn hem bu dagilimin cok uzerinde hem de bir "
            "deploy/restart penceresini (30-60 sn) kapsar. Daha kisa degerler "
            "deploy sirasinda mesru komutlari sahte `expired` yapar."
        ),
    )
    command_clock_skew_tolerance_sec: float = Field(
        default=5.0,
        gt=0.0,
        le=3600.0,
        description=(
            "Backend saatinin gateway saatinden ne kadar ILERIDE olmasina izin "
            "verilir (sn). Bu kadar gelecekteki `created_at` normal saat "
            "sapmasi sayilir ve yas 0 kabul edilir; DAHA fazlasi "
            "`command_timestamp_future` ile REDDEDILIR, fiziksel komut "
            "CALISTIRILMAZ.\n"
            "\n"
            "5 sn olculerek secildi: sahada backend-gateway saat farki ~67 ms. "
            "Onceki deger 60 sn idi — olculen sapmanin ~900 kati. O kadar genis "
            "bir pencere, saati ileri kaymis ya da damgasi bozulmus bir komutun "
            "gercek yasini gizleyebilirdi.\n"
            "\n"
            "SINIR DETERMINISTIK: `created_at <= now + tolerans` KABUL, "
            "`created_at > now + tolerans` RED.\n"
            "\n"
            "SAAT GERI ADIMI: NTP duzeltmesi sistem saatini geri alirsa komut "
            "gateway'e gore GELECEKTE gorunur ve fail-closed reddedilir. Bu "
            "BILINCLI bir takas: mesru bir komutu yanlislikla reddetmek, bayat "
            "bir fiziksel komutu calistirmaktan iyidir — operator durumu "
            "kontrol edip yeni komut verebilir.\n"
            "\n"
            "KAPATILAMAZ: 0/negatif deger acilista reddedilir (gt=0). Sifir "
            "tolerans mikrosaniyelik sapmada bile mesru komutlari keserdi; "
            "'0 = kapali' gibi bir kacis yolu ise gelecek damgali komutlari "
            "sinirsiz kabul etmek olurdu."
        ),
    )
    command_require_timestamp: bool = Field(
        default=True,
        description=(
            "Zaman damgasi TASIMAYAN komut reddedilsin mi. VARSAYILAN ACIK.\n"
            "\n"
            "Backend F3B ve sonrasi `/pending` komutlarinda timezone-aware UTC "
            "`created_at` tasir. Damgasiz bir fiziksel komut artik normal "
            "calisma sozlesmesinin DISINDADIR ve fail-closed reddedilir "
            "(`command_timestamp_missing`); yasini bilemedigimiz bir komutu "
            "cihaza gondermek, tam da bu kontrolun onlemek icin var oldugu sey.\n"
            "\n"
            "TTL'yi kapatan bir feature flag DEGILDIR: `created_at` geldiginde "
            "azami yas her kosulda uygulanir ve bu bayrak bunu bypass ETMEZ.\n"
            "\n"
            "`false` YALNIZCA gecici rollback / `created_at` gondermeyen eski "
            "bir backend'e karsi kontrollu uyumluluk icindir. Normal "
            "production/development dagitiminda KULLANILMAMALIDIR."
        ),
    )
    config_timeout_sec: int = Field(default=5, ge=1, le=60)
    # Command-poll READ timeout (sn). Config fetch'ten AYRI: poll kisa read
    # timeout ile hizli hata verip bir sonraki turda yeniden dener, uzun
    # takilmaz. Connect timeout sabit 3sn (bkz main.py command_client).
    command_poll_timeout_sec: float = Field(default=4.0, ge=1.0, le=30.0)
    config_cache_max_age_hours: float = Field(
        default=24.0,
        ge=1.0,
        le=720.0,
        description=(
            "Disk'teki config cache'i kac saatten daha eski olunca 'stale' "
            "kabul edilir. Backend down kalsa bile gateway eski config ile "
            "polling'e devam eder, ancak /health endpoint'i 'cache_stale' "
            "raporlar. Operator backend baglantisini cozmesi icin alarm."
        ),
    )

    # ----- Telemetri yayinlama -----------------------------------------------
    # STANDART yol: NATS JetStream (`<prefix>.<gateway_code>` subject'i).
    # Telemetri backend'e HIC ugramaz — HTTP ingest yolunda her olcum backend
    # HTTP + Postgres outbox + NATS zincirinden geciyordu ve yuk backend'e
    # biniyordu (2026-08-04 yuk testi: outbox drain 3.250 msj/sn'e karsi
    # persist 1.100 msj/sn — backlog kendiliginden erimiyordu). HTTP ingest
    # yalnizca bilincli rollback icin tutulur: `TELEMETRY_PUBLISHER=http`.
    # Komut/config kanali bundan bagimsizdir, HTTP uzerinden calismaya devam
    # eder.
    telemetry_publisher: str = Field(
        default="nats",
        description="Telemetri yayin yolu: nats (JetStream, STANDART) | http (backend ingest, rollback)",
    )

    # ----- Kurulum modu: tasima yolu davranisini BELIRLER ---------------------
    # Bu ayar `telemetry_publisher=nats` iken NATS erisilemedigi zaman ne
    # olacagini belirler ve iki kurulum senaryosunun DOGRU davranisi farklidir:
    #
    #   local  ("bu cihaza kur" — backend ile ayni makine)
    #          NATS ZORUNLU, yedek yol YOK. Ayni makinede NATS'a erisilememesi
    #          bir yapilandirma hatasidir; sessizce HTTP'ye dusmek bu hatayi
    #          GIZLER. Sistem "calisiyor" gorunur ama her olcum backend HTTP +
    #          Postgres zincirinden gecer ve 500 cihaz hedefi tutmaz — ariza
    #          ancak yuk testinde, haftalar sonra fark edilir. Bu modda NATS
    #          dustugunde mesajlar outbox'ta birikir (kayip yok) ve /health
    #          `telemetry_backend_unreachable` der.
    #
    #   remote ("baska cihaza kur" — sahadaki ayri sunucu)
    #          Once NATS denenir; erisilemezse HTTP ile veri akmaya DEVAM eder.
    #          Sahada 4222 kapali/NAT arkasinda olabilir ve operatorun elinde
    #          yalnizca backend'in HTTPS ucu olabilir. NATS geri gelince
    #          gateway birincil yola KENDILIGINDEN doner.
    #
    # Varsayilan `remote`: mevcut kurulumlar (INSTALL_MODE gondermeyenler)
    # davranis degistirmesin, yalnizca yedek yol kazansin.
    install_mode: str = Field(
        default="remote",
        description=(
            "Kurulum senaryosu: local (backend ile ayni makine — NATS zorunlu, "
            "HTTP yedegi YOK) | remote (uzak saha kurulumu — NATS birincil, "
            "erisilemezse HTTP yedegine duser ve geri doner)"
        ),
    )
    telemetry_fallback_fail_threshold: int = Field(
        default=3,
        ge=1,
        le=100,
        description=(
            "Uzak kurulumda kac ARDISIK NATS hatasindan sonra aktif yol HTTP'ye "
            "cevrilir. 1 tek bir timeout'ta yol degistirir (gereksiz salinim); "
            "cok buyuk deger her mesajda NATS timeout'u odemek demektir."
        ),
    )
    telemetry_fallback_probe_interval_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=3600.0,
        description=(
            "HTTP yedegine dustukten sonra NATS'a donmek icin yoklama araligi "
            "(sn). Yoklama gercek bir publish degil, NATS istemcisinin baglanti "
            "durumudur; maliyeti sifira yakindir."
        ),
    )

    # ----- NATS JetStream (STANDART TELEMETRI YOLU) ---------------------------
    # Publish down olunca: publish hatasi -> outbox'a yazilir -> retrier broker
    # gelince bosaltir (at-least-once, kayip yok).
    nats_url: str = Field(
        default="nats://localhost:4222",
        description=(
            "NATS JetStream server adresi (ZORUNLU). Compose icinden "
            "nats://nats:4222; ayri host'tan nats://<host>:4222."
        ),
    )
    nats_subject_prefix: str = Field(
        default="e1.telemetry.raw",
        description=(
            "JetStream subject prefix. Konkre subject `<prefix>.<gateway_code>` "
            "seklinde olusturulur (orn. e1.telemetry.raw.GW-001). Backend "
            "stream TELEMETRY_RAW bu prefix'i `e1.telemetry.raw.>` wildcard "
            "ile yakalar."
        ),
    )
    nats_connect_timeout_sec: int = Field(
        default=5,
        ge=1,
        le=60,
        description=(
            "NATS connect timeout. Kisa tutun ki gateway startup'i NATS yokken "
            "bloklanmasin — connect basarisiz olsa bile gateway ayaga kalkar, "
            "mesajlar outbox'a yazilir, baglanti gelince retrier bosaltir."
        ),
    )
    nats_tls_ca_path: str = Field(
        default="",
        description=(
            "NATS sunucusunun sundugu sertifikayi dogrulamak icin kullanilan CA "
            "bundle dosyasinin (PEM) tam yolu. Bos birakilirsa system trust "
            "store kullanilir. Kurumsal/self-signed CA varsa bu yolu doldurun; "
            "aksi halde tls:// baglantisinda SSL handshake fail eder."
        ),
    )
    nats_credentials_path: str = Field(
        default="",
        description=(
            "NATS NKEY/JWT credentials dosyasinin (.creds) tam yolu. "
            "`nsc generate user --account ... --name gateway-GW001` ile uretilen "
            "dosyayi gateway host'una koyup bu yolu set edin. Bos ise URL'deki "
            "user:password (nats://user:pass@host) kullanilir; ikisi de bos "
            "ise NATS server anonim baglanti red eder (deny-all)."
        ),
    )
    nats_publish_timeout_sec: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        description=(
            "JetStream publish bekleme suresi (sn). Tek mesaj icin. Tipik "
            "yerel cluster'da <10ms cevap doner. 2.0sn default: yerel/LAN "
            "cluster'da fazlasiyla yeterli; 4G veya WAN broker icin de uygun. "
            "Bu sureyi asan publish OutboxFullError'a sebep olmadan once "
            "exception ile geri doner, mesaj outbox'a yazilir ve retrier "
            "broker'a hizla bagi geldiginde bosaltir. Daha kucuk degerler "
            "(0.5-1.0) agresif outbox'a dusurme yapar — yerel laboratuvar "
            "icin tercih edilebilir; 100 cihaz x 30 sinyal yuk altinda "
            "2.0sn cycle_timeout_sec=120sn ile rahatlikla uyumludur."
        ),
    )
    # Geriye uyumluluk: nats_dual_publish_enabled artik anlamsiz cunku JetStream
    # tek yol. Eski .env'ler kirilmasin diye field tutulur ama goz ardi edilir.
    # Default False — yeni deploy'lar bu flag'i aktive etmemeli; bilincli set
    # eden operator startup'ta DEPRECATED uyarisi alir (boot warning).
    nats_dual_publish_enabled: bool = Field(
        default=False,
        description=(
            "DEPRECATED — JetStream artik tek primary yol; bu bayrak goz "
            "ardi edilir. Eski .env'lerdeki 'true' degerleri sessizce kabul "
            "edilir; main.py boot'ta WARN log atar."
        ),
    )

    # ----- RabbitMQ (LEGACY/DEPRECATED — gateway 0.4.x'te kaldirildi) ---------
    # Bu alanlar SADECE geriye-uyumlu .env parse'i icin tutuluyor; gateway
    # artik RabbitMQ'ya BAGLANMIYOR. Alarm mesajlari backend tarafinda
    # RabbitMQ'da kalmaya devam ediyor (gateway onunla ilgilenmez).
    #
    # Production validator (`_validate_production_safeguards`) bu field set
    # edilirse SystemExit eder — operator yanlislikla eski .env'i deploy
    # etmesin diye. Development/staging'te bos kabul edilir.
    rabbitmq_url: str = Field(
        default="",
        description=(
            "LEGACY/DEPRECATED — gateway 0.4.x'te RabbitMQ kullanmiyor. "
            "Production'da set edilemez (validator reddeder). Bos birakin."
        ),
    )
    # Eski deploy'larin parse hatasi vermemesi icin default'lar bos. Bu
    # field'lar runtime'da hicbir yerde okunmuyor — sadece pydantic schema
    # compat icin tutuluyor.
    rabbitmq_exchange: str = Field(default="")
    rabbitmq_routing_key: str = Field(default="")

    # ----- Health HTTP ---------------------------------------------------------
    worker_health_host: str = Field(default="127.0.0.1")
    worker_health_port: int = Field(
        default=8020,
        ge=0,
        le=65535,
        description=(
            "Health/metrics HTTP portu. 0 verilirse OS rastgele bos port atar; "
            "gercek port baslangictaki log'da ve /health icinde gosterilir. "
            "Ayni PC'de coklu gateway icin her birine ayri port verin."
        ),
    )
    health_trusted_proxies: str = Field(
        default="",
        description=(
            "`X-Forwarded-For` header'ina guvenilebilecek reverse proxy CIDR "
            "listesi (virgulle ayrik). Bos ise XFF YOK SAYILIR — direkt TCP "
            "client adresi kullanilir. Bu, en guvenli default'tur cunku XFF "
            "header'ini herhangi bir uzaktan attacker spoofing yapip "
            "`X-Forwarded-For: 127.0.0.1` ile rate-limit bypass edemez. "
            "Reverse proxy (nginx, Caddy) arkasinda calistiriyorsaniz proxy "
            "IP/subnet'ini buraya koyun; sadece proxy'den gelen istekler XFF "
            "ile cozumlenir, dogrudan gelen istekler TCP adresinden cozumlenir. "
            "Ornek: `10.0.0.0/8,192.168.1.5/32`."
        ),
    )

    # ----- Polling davranisi ---------------------------------------------------
    # Cycle interval: gateway'in due_devices kontrolu icin "uyanma" araligi.
    # Eski 5sn frontend gecikmesinin ana sebebiydi; 1sn ile gateway saniyede
    # bir cihaz queue'sunu kontrol eder. Ek I/O yuku yok cunku event-driven
    # mod cihazda yeni veri yoksa adapter'dan "no_change" dönuyor (publish
    # olmuyor).
    default_poll_interval_sec: int = Field(default=1, ge=1, le=3600)
    # Paralelizm: bir cycle'da kac cihaz aynı anda okunur. Hedef kapasite tek
    # gateway'de 500 cihaz (1.2.0+): 500 paralel worker ile her cihazin yanit
    # suresi <100ms oldugu icin cycle 1sn altinda biter ve kopuk cihaz
    # dalgasinda bile tek timeout dalgasi yasanir (worst case ~15sn, bkz.
    # device_poll_timeout_sec). Havuz tembel buyur (ThreadPoolExecutor
    # max_workers tavandir, thread ihtiyac olunca acilir) — kucuk sahada 500
    # varsayilanin maliyeti yok. RUNBOOK "Olcek" bolumundeki kademeli cikis
    # ve `poll_pool_starved` sinyali gecerliligini korur.
    max_parallel_devices: int = Field(default=500, ge=1, le=1000)
    # Tek bir cihaz okuma + publish icin maksimum sure (sn). Bu sureyi asarsa
    # cihaz "timeout" kabul edilir, mark_read cagirilir, diger cihazlar
    # etkilenmez. Kopuk cihaz worker'i bu sure boyunca isgal eder; 300+
    # cihazda 30sn havuzu ac birakiyordu (RUNBOOK ayar tablosundaki 15sn
    # onerisi 1.2.0'da varsayilan yapildi). WAN'da integrity poll'u 15sn'i
    # asan saha varsa env ile yukseltilebilir.
    device_poll_timeout_sec: float = Field(
        default=15.0,
        ge=1.0,
        le=600.0,
        description="Tek cihaz icin poll+publish maksimum sure (sn)",
    )
    # Tum cycle (paralel due_devices) icin global timeout. Uygulama default
    # device_timeout * sqrt(devices) ya da bu sabit; hangisi buyuk.
    cycle_timeout_sec: float = Field(
        default=120.0,
        ge=10.0,
        le=3600.0,
        description="Bir poll cycle'in (paralel) maksimum suresi (sn)",
    )
    # Container icinde calisirken cihaz IP'si "127.0.0.1" / "localhost" /
    # "0.0.0.0" olarak gelmisse host'a (host.docker.internal) cevir. Cati
    # yazilim + simulator + gateway ayni Windows host'unda calisirken bu
    # gerekli — aksi halde container kendisine baglanmaya calisir.
    rewrite_loopback_to_host: bool = Field(
        default=True,
        description="Device IP loopback ise host.docker.internal'a cevirilsin mi",
    )

    # ----- Outbox / messaging dayaniklilik -----------------------------------
    outbox_max_pending: int = Field(
        default=500_000,
        ge=1_000,
        le=10_000_000,
        description=(
            "Outbox doluluk limiti. Ulasilirsa publisher disk-full circuit "
            "breaker tetikler ve poll cycle'i durdurur (sessiz veri kaybi yerine "
            "kontrollu duraklatma). Saniyede ortalama 200 mesaj ile ~40 dakika "
            "broker outage karsilar."
        ),
    )
    outbox_max_retries: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description=(
            "Bir mesaj kac kere yeniden gonderilmeye calisilirsa dead-letter "
            "tablosuna tasinir (poison message korumasi)."
        ),
    )
    outbox_retrier_min_backoff_sec: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description="OutboxRetrier minimum backoff (broker dustugunde ilk bekleme)",
    )
    outbox_retrier_max_backoff_sec: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="OutboxRetrier maksimum backoff (broker uzun sure dustugunde cap)",
    )
    outbox_retrier_poll_interval_sec: float = Field(
        default=2.0,
        ge=0.5,
        le=60.0,
        description="OutboxRetrier saglikli durumda batch'ler arasi bekleme",
    )
    outbox_dead_letter_retain_days: float = Field(
        default=30.0,
        ge=1.0,
        le=3650.0,
        description=(
            "Dead-letter kayitlarinin saklanma suresi (gun). Tabloda RETENTION "
            "YOKTU: uzun bir backend semantik hatasindan sonra yuz binlerce "
            "satir birikip 100-200 MB kalici disk tuketiyor ve saha PC'sinde "
            "disk dolmasinin ana kaynagi oluyordu."
        ),
    )
    outbox_dead_letter_max_rows: int = Field(
        default=50_000,
        ge=1_000,
        le=5_000_000,
        description="Dead-letter tablosu ust satir siniri (yas siniri yetmezse en eskiler silinir)",
    )
    command_ledger_retain_days: float = Field(
        default=90.0,
        ge=1.0,
        le=3650.0,
        description=(
            "Teslim EDILMIS komut kayitlarinin saklanma suresi (gun). Teslim "
            "edilmemis sonuclar ve dead_letter kayitlari (denetim izi) budanmaz."
        ),
    )
    outbox_retrier_batch_size: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="Outbox'tan tek seferde alinan mesaj sayisi",
    )

    # ----- DNP3 master parametreleri ------------------------------------------
    dnp3_local_address: int = Field(default=1, ge=0, le=65519)
    dnp3_tcp_port: int = Field(
        default=20000,
        ge=1,
        le=65535,
        description="Varsayilan DNP3 TCP port; cihaz API'de dnp3_tcp_port verirse o baskin",
    )
    dnp3_device_allowed_subnets: str = Field(
        default="",
        description=(
            "Cihaz IP allowlist (CIDR listesi, virgulle ayrilmis). Bos ise tum "
            "IP'ler kabul edilir (geriye uyumlu). Backend kompromize olursa "
            "saldirgan `169.254.169.254` (cloud metadata), `8.8.8.8` veya ic ag "
            "scan'i icin gateway'i kullanamasin diye saha cihazlarinin "
            "bulundugu LAN subnet'lerini buraya koyun. "
            "Ornek: `192.168.10.0/24,10.0.5.0/24,172.16.20.0/24`. "
            "Hostname (FQDN) gelen cihazlar bu kontrolden gecemez — DNS "
            "cozumlemesini gateway yapmaz, IP olarak yapilandirin."
        ),
    )
    dnp3_response_timeout_sec: int = Field(
        default=15,
        ge=1,
        le=120,
        description="DNP3 yanit bekleme (s); tekil index okumalari coklu sinyalde toplam sureye eklenir",
    )
    dnp3_read_strategy: str = Field(
        default="event_driven",
        description=(
            "event_driven (varsayilan) = Class 1+2+3 event poll + periyodik Class 0 baseline "
            "(degisen noktalari yayinlar; 100+ cihaz icin onerilen) | "
            "direct = grup+index araligi (hafif, simulator uyumlu) | "
            "class0 = sadece statik (her cycle hepsini publish eder) | "
            "integrity = tum classlar (en kapsamli, en yorucu)."
        ),
    )
    dnp3_event_baseline_interval_sec: int = Field(
        default=60,
        ge=5,
        le=86400,
        description=(
            "event_driven mod: bu kadar saniyede bir Class 0 (tam baseline) tazelenir; "
            "arada Class 1/2/3 event poll yapilir. Drift toleransi olarak 30-300 sn idealdir."
        ),
    )
    # ----- Kopuk cihazi kendiliginden yoklama (4G/GSM hatlar icin) -----------
    # SAHADA: "bazen haberlesme gidiyor, manuel refresh atinca geliyor."
    # Cihaz 'lost' ve link ACIK gorunuyorken gateway ona hicbir sey sormuyordu;
    # 4G modem RRC idle'a dusup TCP soketi yari-acik kalinca iki taraf da
    # sessiz kaliyor ve kimse ilk hamleyi yapmiyordu.
    dnp3_lost_probe_interval_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=600.0,
        description=(
            "Kopuk ('lost') ama link'i acik gorunen cihaza kac saniyede bir "
            "integrity poll gonderilsin (manuel refresh'in yaptiginin aynisi). "
            "Sessiz outstation'i konusturur. 4G'de RRC idle -> aktif gecisi "
            "saniyeler surer; 30 sn cihaz basina saatte 120 istek demektir."
        ),
    )
    dnp3_lost_relink_after_probes: int = Field(
        default=3,
        ge=1,
        le=50,
        description=(
            "Kac sonucsuz yoklamadan sonra TCP oturumu ZORLA yeniden kurulsun. "
            "Yari-acik sokete integrity poll ise yaramaz; tek cikis kanali "
            "kapatip yeniden acmaktir. 3 x 30 sn = ~90 sn."
        ),
    )

    # ----- Horstmann Smart Mode oturum yasam dongusu ------------------------
    # Politika CIHAZ BASINA ve ACIKCA yapilandirilir:
    # `DeviceConfig.session_policy` = "continuous" (varsayilan) | "smart".
    # Bu env yalnizca cihaz bazli deger GELMEDIGINDE kullanilan yedegi verir.
    # Ayrintili anlatim: docs/HORSTMANN_SMART_MODE.md
    dnp3_smart_max_silence_sec: int = Field(
        default=0,
        ge=0,
        le=30 * 24 * 3600,
        description=(
            "session_policy=smart olan bir cihaz bu kadar saniye HIC gecerli DNP3 "
            "kaniti gondermezse smart_idle -> lost gecisi yapilir ve normal "
            "comm_lost mekanizmasi calisir. "
            "KABUL EDILEN DEGERLER: 0 (denetim KAPALI, varsayilan) VEYA 60..2592000. "
            "1..59 REDDEDILIR (boot'ta config hatasi): o araliktaki bir esik normal "
            "bir Smart uykusunu bile kopuk ilan eder. "
            "Bu ayar yalnizca cihaz bazli `smart_max_silence_sec` YOKKEN kullanilan "
            "yedegi verir. Dogru deger cihazin Dial-In rapor programina baglidir; "
            "gomulu bir varsayim YOKTUR."
        ),
    )

    # ----- Cihaz basina calisma-zamani sagligi (G-DEVICE-HEALTH-01) --------
    # AYRI, GIDEN, GOVDE tabanli kanal. `X-E1-Gateway-Health` toplu basligini
    # DEGISTIRMEZ ve komut duzlemine DOKUNMAZ.
    device_health_publish_enabled: bool = Field(
        default=False,
        description=(
            "Cihaz basina calisma-zamani sagligini backend'e POST eder "
            "(/gateways/{code}/device-health). VARSAYILAN KAPALI: backend bu ucu "
            "tanimadan acilirsa her turda 404 alinir ve log dolar. Grid tarafi "
            "hazir olunca acilir. Kapaliyken hicbir thread baslatilmaz."
        ),
    )
    device_health_batch_max: int = Field(
        default=50,
        ge=1,
        le=500,
        description=(
            "Tek HTTP govdesindeki AZAMI cihaz sayisi. 200+ cihazli filoda tek dev "
            "govde hem backend'i hem proxy'leri zorlar."
        ),
    )
    device_health_snapshot_interval_sec: int = Field(
        default=300,
        ge=30,
        le=86400,
        description=(
            "Periyodik TAM anlik goruntu (uzlastirma) araligi. Delta'lar kaybolsa "
            "bile backend en gec bu surede gercekle hizalanir."
        ),
    )
    device_health_change_debounce_sec: float = Field(
        default=2.0,
        ge=0.0,
        le=60.0,
        description=(
            "Durum degisikligi sonrasi toplama penceresi. Ayni saniyede 200 cihaz "
            "birden degisirse (config yenilemesi, toplu uyanma) tek partide "
            "toplanmalari daha iyidir."
        ),
    )

    dnp3_event_scan_interval_sec: int = Field(
        default=0,
        ge=0,
        le=3600,
        description=(
            "event_driven mod: Class 1/2/3 EVENT SCAN araligi (sn). 0 = "
            "DEFAULT_POLL_INTERVAL_SEC ile ayni (eski davranis).\n"
            "\n"
            "NEDEN AYRI AYAR: bu iki sure FARKLI isler ama tek ayara baglilardi.\n"
            "  * poll interval -> gateway'in cache'ten okuyup YAYINLAMA turu\n"
            "  * scan interval -> cihaza 'yeni olayin var mi' diye SORMA sikligi\n"
            "\n"
            "401 cihazli sahada olculdu (2026-08-04): scan=1s ile saniyede 401 DNP3\n"
            "istegi uretiliyordu ve gateway CPU'su cihaz sayisiyla dogru orantili\n"
            "artiyordu (mesaj sayisiyla DEGIL) — cunku her istek bir TCP round-trip +\n"
            "cerceve cozumleme + C++->Python callback zinciri demek.\n"
            "\n"
            "Cihazlar unsolicited modda calisir (disableUnsolOnStartup=False), yani\n"
            "degisiklikleri KENDILIGINDEN gonderir; scan yalnizca yedek mekanizmadir.\n"
            "Bu yuzden scan'i seyreltmek veri tazeligini orantili bozmaz.\n"
            "\n"
            "Yukseltmeden once/sonra `/health` 'oldest_frame_age_sec' izlenmelidir."
        ),
    )
    dnp3_direct_max_points_per_read: int = Field(
        default=24,
        ge=1,
        le=250,
        description="Bir DNP3 READ'de en fazla kac nokta (0-123 gibi aralik cokluklarda parcalar)",
    )
    dnp3_direct_sparse_ratio: int = Field(
        default=4,
        ge=2,
        le=20,
        description="benzersizIndexSayisi*oran < min-max+1 ise 'seyrek' kabul, sadece o indexlere tekil okur",
    )
    dnp3_confirm_required: bool = Field(
        default=False,
        description="Data link onayli cerceve; OpenDNP3 sim/outstation ile False genelde gerekir",
    )
    dnp3_link_reset_on_connect: bool = Field(
        default=True,
        description="TCP acildiktan sonra DNP3 Reset Link; bazi OpenDNP3 outstation'lar icin onerilir",
    )
    dnp3_disable_unsolicited_on_connect: bool = Field(
        default=False,
        description=(
            "Connect+Reset Link sonrasi DISABLE_UNSOLICITED gonderir. "
            "Bazi OpenDNP3 outstation'lar (saha cihazlari + simulator) bu mesaja "
            "TCP'yi kapatarak cevap verir; bu yuzden VARSAYILAN false. Empty-frame "
            "filter unsolicited frame'leri zaten yutuyor; gerekmiyorsa kapali kalsin."
        ),
    )
    dnp3_unsolicited_class_mask: int = Field(
        default=7,
        ge=0,
        le=7,
        description="Bitmask: 1=Class1, 2=Class2, 4=Class3 (varsayilan 7=hepsi)",
    )
    dnp3_log_raw_frames: bool = Field(
        default=False,
        description="nfm-dnp3 ham TX/RX cercevelerini loglar (sorun giderme; cok gurultulu)",
    )
    dnp3_manager_threads: int = Field(
        default=0,
        ge=0,
        le=64,
        description=(
            "yadnp3 (opendnp3.DNP3Manager) icin IO thread sayisi. 0 = otomatik "
            "(adapter heuristic, minimum 4). 100 cihazli instance icin 4-8 "
            "onerilir; daha azinda thread doyumu olur (eski sabit 2 yetersiz)."
        ),
    )
    dnp3_time_sync: str = Field(
        default="lan",
        description=(
            "DNP3 master->outstation zaman senkronizasyon PROSEDURU: "
            "lan | nonlan | none. "
            "DOGRU SECIM TASIYICIYA (TCP/serial) DEGIL, OUTSTATION'IN ILAN "
            "ETTIGI IMPLEMENTATION PROFILE'A BAGLIDIR. (Bu alanin eski "
            "aciklamasi 'TCP baglantilar icin lan dogru secimdir' diyordu; "
            "bu YANLISTI.) "
            "OLCULEN TEL DAVRANISI (yadnp3 3.2.1.1): "
            "lan -> FC=24 RECORD_CURRENT_TIME + WRITE G50V3; "
            "nonlan -> FC=23 DELAY_MEASUREMENT + WRITE G50V1; "
            "none -> saat HIC yazilmaz. "
            "HORSTMANN Smart Navigator 2.0 / Pole Master resmi Device Profile "
            "FC=23 ve G50V1 ILAN EDER; FC=24 ve G50V3 ILAN EDILMEMISTIR — bu "
            "cihazlarda dogru deger 'nonlan'dir. Varsayilan geriye uyumluluk "
            "icin 'lan' KALIR; Horstmann kurulumlarinda ACIKCA 'nonlan' verin. "
            "Outstation'lar tipik olarak ayda 10-60sn RTC drift yapar ve guc "
            "kesintisinden sonra saatlerini sifirlar; master zaman yazmazsa "
            "cihaz kendi olay damgalarini yanlis saatle uretir ve IIN1.4 "
            "(NEED_TIME) bayragini sonsuza kadar set tutar. "
            "SENKRONIZASYON TALEP GUDUMLUDUR: master yalnizca cihaz IIN1.4 "
            "asserted ettiginde saat yazar; saat yanlis olup cihaz NEED_TIME "
            "bildirmiyorsa gateway ZORLA yazmaz, durum yalnizca "
            "`device_clock_status` ile gorunur kilinir. "
            "Gateway saati guvenilmez goruldugunde (ClockGuard) yazim "
            "otomatik durur."
        ),
    )
    dnp3_publish_quality_flags: bool = Field(
        default=False,
        description=(
            "Cihazin DNP3 kalite bayraklarini (RESTART / LOCAL_FORCED / "
            "OVER_RANGE / REFERENCE_ERR ...) telemetri `quality` alanina yansit. "
            "VARSAYILAN KAPALI: yeni kalite degerleri (invalid/restart/forced) "
            "backend tag-engine tarafindan taninmadan acilirsa olcumler yanlis "
            "islenebilir. Backend hazir oldugunda saha genelinde acilacak. "
            "Bkz. docs/BACKEND_TODO.md#B1. Bayrak KAPALI olsa da bayraklar "
            "okunur ve teshis icin tasinir."
        ),
    )

    # ----- Logging -------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="text", description="text | json")
    # Default FALSE — token konsolda tam metin gozukurse docker logs'ta kalir;
    # log aggregator'a (ELK, Loki) gidip leak olabilir. Kasitli "false" guvenli.
    # Ihtiyac halinde .env: SHOW_GATEWAY_TOKEN_ON_START=true ile geri acilabilir.
    show_gateway_token_on_start: bool = Field(default=False)

    # Rotating dosya log'lama. NSSM/Windows servis stdout'u tek dosyaya yonlendirir
    # ama rotasyon YAPMAZ — 600 cihazli yuk altinda saatler icinde disk dolar.
    # LOG_FILE_PATH set edilirse her gateway instance kendi rotating dosyasina
    # yazar. {gateway_code} yer tutucu otomatik resolve edilir; boylece tek
    # template ile coklu instance'lar ayrik dosyalara yazar.
    log_file_path: str = Field(
        default="",
        description=(
            "Rotating log dosyasi yolu. Bos ise sadece stdout'a yazilir (mevcut "
            "davranis, Docker icin uygundur). Windows NSSM kurulumlarinda set "
            "edin: orn. 'C:/ProgramData/EnerjiOne/dnp3-gateway/{gateway_code}.log'. "
            "Yer tutucular: {gateway_code}, {instance_id}."
        ),
    )
    log_file_max_bytes: int = Field(
        default=20 * 1024 * 1024,  # 20 MB
        ge=1024 * 1024,  # 1 MB min
        le=2 * 1024 * 1024 * 1024,  # 2 GB max
        description="Tek log dosyasi maksimum boyutu (byte). Asilirsa rotate.",
    )
    log_file_backup_count: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Kac eski log dosyasi tutulsun (rotation sonrasi). 10 x 20MB = 200MB instance basina ust sinir."
        ),
    )

    # ----- Backend HTTP client guvenlik ---------------------------------------
    # Backend config response icin maksimum boyut (10 MB). Ustu raise eder
    # (memory DoS koruma). Tipik 100 cihaz config'i ~50KB; 10MB cok cok yeterli.
    backend_response_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=64 * 1024,  # min 64KB
        le=200 * 1024 * 1024,  # max 200MB
        description="Backend config response max size (DoS koruma)",
    )

    # ----- Validators ---------------------------------------------------------
    @field_validator("dnp3_smart_max_silence_sec")
    @classmethod
    def _validate_smart_max_silence(cls, v: int) -> int:
        """0 (kapali) VEYA 60..2592000. 1..59 FAIL-CLOSED.

        `ge=0` tek basina 1..59'u da kabul ederdi. Sozlesme cihaz alani icin
        alt siniri 60 saniye yapiyor; env yedegi ayni sozlesmeye uymazsa
        operator 30 sn yazar, kimse reddetmez ve NORMAL uyuyan her cihaz
        kopuk ilan edilir. Boot'ta acik hata vermek, sahada sessizce yanlis
        davranmaktan iyidir.
        """
        if v == 0:
            return v
        if _SMART_SILENCE_ENV_MIN <= v <= _SMART_SILENCE_ENV_MAX:
            return v
        raise ValueError(
            f"DNP3_SMART_MAX_SILENCE_SEC={v} gecersiz. Kabul edilen: 0 (denetim kapali) "
            f"veya {_SMART_SILENCE_ENV_MIN}..{_SMART_SILENCE_ENV_MAX} saniye. "
            f"1..{_SMART_SILENCE_ENV_MIN - 1} araligi REDDEDILIR: bu kadar kisa bir esik "
            "normal bir Smart uykusunu bile kopuk ilan eder."
        )

    @field_validator("telemetry_publisher")
    @classmethod
    def _validate_telemetry_publisher(cls, v: str) -> str:
        valid = {"http", "nats"}
        s = (v or "http").strip().lower()
        if s not in valid:
            raise ValueError(f"TELEMETRY_PUBLISHER gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("install_mode")
    @classmethod
    def _validate_install_mode(cls, v: str) -> str:
        valid = {"local", "remote"}
        s = (v or "remote").strip().lower()
        if s not in valid:
            raise ValueError(f"INSTALL_MODE gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("dnp3_read_strategy")
    @classmethod
    def _validate_read_strategy(cls, v: str) -> str:
        valid = {"event_driven", "direct", "class0", "integrity"}
        s = (v or "").strip().lower()
        if s not in valid:
            raise ValueError(f"DNP3_READ_STRATEGY gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("dnp3_time_sync")
    @classmethod
    def _validate_time_sync(cls, v: str) -> str:
        """Zaman senkronizasyon PROSEDURUNU dogrula.

        FAIL-CLOSED: gecersiz deger sessizce bir prosedure DUSMEZ. Eskiden
        `_apply_time_sync` taninmayan her degeri LAN sayiyordu; yani bir yazim
        hatasi (`DNP3_TIME_SYNC=nonlann`) operatore hicbir sey soylemeden
        cihazin ilan ETMEDIGI bir prosedurle saat yazdirabiliyordu. Artik
        gateway ACILMAZ.
        """
        # Geriye uyumluluk: 1.15.0'a kadar `_apply_time_sync` bu takma adlari
        # "senkronizasyon yok" olarak kabul ediyordu.
        takma = {"off": "none", "disabled": "none", "disable": "none"}
        s = (v or "").strip().lower()
        s = takma.get(s, s)
        valid = {"lan", "nonlan", "none"}
        if s not in valid:
            raise ValueError(
                f"DNP3_TIME_SYNC gecersiz: '{v}'. Gecerli: {sorted(valid)}. "
                "Prosedur outstation'in ilan ettigi profile gore secilir: "
                "FC=23/G50V1 ilan eden cihazlarda (or. Horstmann SN 2.0) "
                "'nonlan', FC=24/G50V3 ilan edenlerde 'lan'."
            )
        return s

    @field_validator("dnp3_library")
    @classmethod
    def _validate_library(cls, v: str) -> str:
        valid = {"yadnp3", "dnp3py"}
        s = (v or "").strip().lower()
        if s not in valid:
            raise ValueError(f"DNP3_LIBRARY gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("gateway_mode")
    @classmethod
    def _validate_gateway_mode(cls, v: str) -> str:
        valid = {"mock", "dnp3"}
        s = (v or "").strip().lower()
        if s not in valid:
            raise ValueError(f"GATEWAY_MODE gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        valid = {"text", "json"}
        s = (v or "").strip().lower()
        if s not in valid:
            raise ValueError(f"LOG_FORMAT gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        s = (v or "INFO").strip().upper()
        if s not in valid:
            raise ValueError(f"LOG_LEVEL gecersiz: '{v}'. Gecerli: {sorted(valid)}")
        return s

    @field_validator("backend_api_url")
    @classmethod
    def _validate_backend_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("BACKEND_API_URL bos olamaz")
        try:
            parsed = urlparse(v.strip())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"BACKEND_API_URL parse edilemedi: {exc}") from exc
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"BACKEND_API_URL scheme http/https olmali (gelen: '{parsed.scheme}')")
        if not parsed.netloc:
            raise ValueError(f"BACKEND_API_URL hostname icermiyor: '{v}'")
        return v.strip()

    @field_validator("rabbitmq_url")
    @classmethod
    def _validate_rabbitmq_url(cls, v: str) -> str:
        """LEGACY/DEPRECATED — gateway 0.4.x'te RabbitMQ'ya BAGLANMIYOR.

        Bu field tutuluyor cunku eski .env dosyalarinda satir olarak kalabilir
        ve pydantic-settings unknown env'i sessizce yutmuyor. Buradaki davranis:
          * Bos string  -> kabul (yeni dogru deploy).
          * Bos olmayan -> WARN log + KABUL (development/staging). Operator
            silmesi icin uyari verir ama boot'u bozmaz.
          * Production  -> `_validate_production_safeguards` icinde
            ValueError ile reddedilir (CR-3); operator silmek zorunda.

        Eski format dogrulamasi (amqp/amqps scheme) tutuluyor — bilgilendirme
        amacli. Gercekten baglanmiyoruz; sadece field tipini koruyoruz.
        """
        s = (v or "").strip()
        if not s:
            return ""
        try:
            parsed = urlparse(s)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"RABBITMQ_URL parse edilemedi: {exc}") from exc
        if parsed.scheme not in ("amqp", "amqps"):
            raise ValueError(
                f"RABBITMQ_URL scheme amqp/amqps olmali (gelen: '{parsed.scheme}'). "
                "NOT: Bu alan 0.4.x'te DEPRECATED — gateway artik kullanmiyor; "
                ".env'den silmeniz onerilir."
            )
        return s

    @model_validator(mode="after")
    def _validate_transport_contract(self) -> Settings:
        """Tasima yolu sozlesmesi — HER ortamda gecerli (dev dahil).

        YEREL KURULUMDA NATS_URL ACIKCA VERILMELIDIR.
        `nats_url` alaninin bir varsayilani var (`nats://localhost:4222`).
        Bu varsayilan uzak kurulumda zararsizdir (nasil olsa yedek yol var)
        ama YEREL kurulumda sessiz bir tuzaktir: ajan NATS_URL'i compose'a
        yazmayi atlarsa gateway varsayilana duser, `localhost:4222`'ye —
        yani KENDI CONTAINER'INA — baglanmayi dener, hicbir zaman
        baglanamaz ve bu yerel modda yedegi de olmadigi icin telemetri
        tamamen durur. Kurulumun ilk saniyesinde acik bir hata vermek,
        sahada saatler suren bir teshisten iyidir.

        Bu yuzden yerel modda alan yalnizca BOS olmamakla kalmaz, ACIKCA
        SET EDILMIS olmalidir (`model_fields_set`) — varsayilana dusmek
        kabul edilmez.
        """
        if self.install_mode != "local" or self.telemetry_publisher != "nats":
            return self
        acikca_verildi = "nats_url" in self.model_fields_set
        if not acikca_verildi or not (self.nats_url or "").strip():
            raise ValueError(
                "YAPILANDIRMA: INSTALL_MODE=local (bu cihaza kurulum) icin "
                "NATS_URL ACIKCA verilmelidir; varsayilana dusulemez. Yerel "
                "kurulumda NATS ZORUNLUDUR ve HTTP yedegi YOKTUR — backend ile "
                "ayni makinede NATS'a erisilememesi bir yapilandirma hatasidir "
                "ve gizlenmemelidir. Compose/.env icine NATS_URL=nats://<host>:4222 "
                "ekleyin. Uzak saha kurulumu yapiyorsaniz INSTALL_MODE=remote "
                "kullanin; o modda NATS erisilemezse HTTP yedegine dusulur."
            )
        return self

    @model_validator(mode="after")
    def _validate_production_safeguards(self) -> Settings:
        """Production / staging ortaminda guvenlik kontrolleri.

        Staging + Production icin:
        * TLS verify ZORUNLU — kapatilirsa SystemExit (MITM koruma).
        * BACKEND_API_URL: public hostname/IP icin https:// zorunlu; private
          ag (RFC1918, loopback, link-local, *.local/.lan/.internal,
          localhost) icin http:// kabul. Public clear-text HTTP -> token
          MITM riski.

        Sadece Production icin:
        * TELEMETRY_PUBLISHER=nats ise NATS_URL bos olamaz; public host'a
          tls:// zorunlu, private host'a nats:// kabul.
        * Token MIN length staging=16, production=32.
        * GATEWAY_TOKEN placeholder prefix ile baslayamaz (`change-me`,
          `gw-default`, `gw-001-token` vb. — `.env.example` kopyalanip token
          guncellenmeden boot edilirse erken yakalanir).
        * SHOW_GATEWAY_TOKEN_ON_START kapali olmali — token log'a sizmasin.
        * GATEWAY_REFRESH_TOKEN, GATEWAY_TOKEN'dan FARKLI olmali — ayni ise
          backend tokeni leak olunca uzaktan tum cihazlari yorma kapisi acilir.
        * GATEWAY_COMMAND_TOKEN, GATEWAY_TOKEN'dan FARKLI olmali — komut
          endpoint'i (/operate) ayri yetkiyle korunur.
        * RABBITMQ_URL set edilemez (cutover sonrasi gateway RabbitMQ'ya
          baglanmaz). Eski .env'den gelen amqp:// reddedilir; operator hatali
          deploy'u erken farkeder.
        * NATS_DUAL_PUBLISH_ENABLED true olamaz — 0.4.x'te JetStream tek yol;
          bu bayrak DEPRECATED ve no-op. Prod boot'unda hata vererek operator
          eski .env'den bu satiri silmesi icin yonlendirilir.
        """
        env = (self.app_environment or "development").strip().lower()
        is_prod = env in ("production", "prod")
        is_stg_or_prod = is_prod or env in ("staging", "stg")
        if is_stg_or_prod:
            if not self.backend_api_verify_ssl:
                raise ValueError(
                    f"GUVENLIK: APP_ENVIRONMENT={env} ortaminda "
                    "BACKEND_API_VERIFY_SSL=False olamaz (MITM riski). "
                    "Sertifika sorunu varsa BACKEND_API_CA_PATH ile kendi CA bundle'inizi verin."
                )
            backend_parsed = urlparse(self.backend_api_url)
            backend_is_https = backend_parsed.scheme.lower() == "https"
            backend_host_private = _host_is_private(backend_parsed.hostname)
            if (
                not backend_is_https
                and not backend_host_private
                and not self.gateway_insecure_allow_plaintext
            ):
                raise ValueError(
                    f"GUVENLIK: APP_ENVIRONMENT={env} ortaminda BACKEND_API_URL "
                    f"public host icin https:// olmali (gelen: {self.backend_api_url!r}). "
                    "Clear-text HTTP yalnizca private/loopback ag icin (RFC1918, "
                    "127.x, *.local, localhost, *.internal) izinlidir. "
                    "TLS henuz kurulamiyorsa GATEWAY_INSECURE_ALLOW_PLAINTEXT=true "
                    "ile bilincli opt-out yapabilirsiniz (boot'ta WARN log atilir)."
                )
            if is_prod:
                # ---- MOCK MODU URETIMDE YASAK --------------------------------
                # `gateway_mode` varsayilani "mock" ve `.env.example` de mock ile
                # geliyor. Validator TLS, token, deprecated alan gibi onlarca sey
                # kontrol ediyordu ama BUNU etmiyordu; tek koruma main.py'deki bir
                # WARNING satiriydi ve 7/24 servis loglarinda kayboluyordu.
                #
                # Senaryo: operator `.env.example`'i kopyalayip APP_ENVIRONMENT'i
                # production yapiyor, GATEWAY_TOKEN'i backend'den aldigi gercek
                # token ile degistiriyor (validator zorluyor, dikkati oraya
                # gidiyor) ama GATEWAY_MODE=mock satirini degistirmeyi unutuyor.
                # Gateway sorunsuz boot ediyor, /health "ok" donuyor, backend'e
                # saniye saniye UYDURMA voltaj/akim akiyor, frontend'de grafikler
                # dolu gorunuyor. DNP3 cihazina TCP baglantisi bile acilmiyor.
                # Ustelik mock adapter HER komuta {"ok": True} donduruyor —
                # operator kesici/reset komutunun uygulandigini saniyor.
                if self.gateway_mode.strip().lower() == "mock":
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_MODE=mock "
                        "olamaz. Mock adapter sahadan OKUMAZ; uydurma degerler "
                        "gercek telemetri gibi yayinlanir ve her komuta 'basarili' "
                        "doner. Saha kurulumu icin GATEWAY_MODE=dnp3 yapin."
                    )
                # NATS sadece legacy/rollback publisher secilirse zorunlu.
                if self.telemetry_publisher == "nats":
                    nats_url_raw = (self.nats_url or "").strip()
                    if not nats_url_raw:
                        raise ValueError(
                            "GUVENLIK: APP_ENVIRONMENT=production'da "
                            "TELEMETRY_PUBLISHER=nats icin NATS_URL bos olamaz."
                        )
                    nats_parsed = urlparse(nats_url_raw)
                    nats_scheme = nats_parsed.scheme.lower()
                    if nats_scheme not in ("tls", "nats"):
                        raise ValueError(
                            f"GUVENLIK: APP_ENVIRONMENT=production'da NATS_URL "
                            f"tls:// veya nats:// scheme olmali (gelen: {self.nats_url!r})."
                        )
                    nats_host_private = _host_is_private(nats_parsed.hostname)
                    if (
                        nats_scheme == "nats"
                        and not nats_host_private
                        and not self.gateway_insecure_allow_plaintext
                    ):
                        raise ValueError(
                            f"GUVENLIK: APP_ENVIRONMENT=production'da public NATS host "
                            f"icin tls:// olmali (gelen: {self.nats_url!r}). Clear-text "
                            "nats:// yalnizca private/loopback ag icin izinlidir. "
                            "TLS henuz kurulamiyorsa GATEWAY_INSECURE_ALLOW_PLAINTEXT=true "
                            "ile bilincli opt-out yapabilirsiniz."
                        )
                if self.show_gateway_token_on_start:
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da "
                        "SHOW_GATEWAY_TOKEN_ON_START=True olamaz (token log'da leak olur). "
                        "Token'i .env'den dogrulayin."
                    )
                if (
                    self.gateway_refresh_token
                    and self.gateway_token
                    and self.gateway_refresh_token.strip() == self.gateway_token.strip()
                ):
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_REFRESH_TOKEN, "
                        "GATEWAY_TOKEN ile AYNI olamaz. Rol ayrimi icin farkli, "
                        "yuksek-entropy bir token kullanin."
                    )
                if (
                    self.gateway_command_token
                    and self.gateway_token
                    and self.gateway_command_token.strip() == self.gateway_token.strip()
                ):
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_COMMAND_TOKEN, "
                        "GATEWAY_TOKEN ile AYNI olamaz. Komut endpoint'i (/operate) "
                        "icin farkli, yuksek-entropy bir token kullanin."
                    )
                # F5: kuyruklanmis komut duzlemi credential'i.
                #
                # BOS OLMASI HATA DEGIL - henuz provision edilmemis kurulumlar icin
                # geriye donuk uyumluluk yolu (bkz. alan aciklamasi). Ama DOLUYSA gercekten
                # AYRI bir sir olmali: digerleriyle ayni deger verilirse ayrimin
                # tamami kagit uzerinde kalir, komut duzlemi hala config
                # duzlemiyle ayni sirla korunuyor olurdu.
                cdt = (self.gateway_command_delivery_token or "").strip()
                if cdt and self.gateway_token and cdt == self.gateway_token.strip():
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da "
                        "GATEWAY_COMMAND_DELIVERY_TOKEN, GATEWAY_TOKEN ile AYNI "
                        "olamaz. Ayni deger verilirse komut duzlemi config "
                        "duzleminden AYRILMAMIS olur; ayri, yuksek-entropy bir "
                        "token uretin."
                    )
                if cdt and self.gateway_command_token and cdt == self.gateway_command_token.strip():
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da "
                        "GATEWAY_COMMAND_DELIVERY_TOKEN, GATEWAY_COMMAND_TOKEN "
                        "(/operate) ile AYNI olamaz. Iki uc farkli yetki "
                        "duzlemidir; ayri token kullanin."
                    )
                if cdt and _is_placeholder_token(cdt):
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da "
                        "GATEWAY_COMMAND_DELIVERY_TOKEN placeholder degerle "
                        "baslayamaz. Yuksek-entropy yeni bir token uretin "
                        '(`python -c "import secrets;print(secrets.token_urlsafe(32))"`).'
                    )
                # H-3: token placeholder prefix kontrolu. Auth katmanindaki
                # `ensure_credentials_allowed` zaten birkac literal placeholder'i
                # yakaliyor; bu kontrol prefix-bazli olup daha genis (.env.example
                # `gw-001-token` gibi degerleri de yakalar).
                if _is_placeholder_token(self.gateway_token):
                    raise ValueError(
                        f"GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_TOKEN "
                        f"placeholder degerle baslayamaz (gelen: {self.gateway_token[:16]!r}...). "
                        ".env.example'dan kopyaladiysaniz GATEWAY_TOKEN'i backend "
                        "`gateways.token` ile eslesen gercek (>=32 char) bir degere "
                        "guncelleyin. Placeholder prefix listesi: "
                        f"{', '.join(_PLACEHOLDER_TOKEN_PREFIXES[:6])}..."
                    )
                if self.gateway_refresh_token and _is_placeholder_token(self.gateway_refresh_token):
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_REFRESH_TOKEN "
                        "placeholder degerle baslayamaz. /refresh-all endpoint'i "
                        "kullanilacaksa yuksek-entropy yeni bir token uretin "
                        '(`python -c "import secrets;print(secrets.token_urlsafe(32))"`).'
                    )
                if self.gateway_command_token and _is_placeholder_token(self.gateway_command_token):
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da GATEWAY_COMMAND_TOKEN "
                        "placeholder degerle baslayamaz. /operate endpoint'i "
                        "kullanilacaksa yuksek-entropy yeni bir token uretin "
                        '(`python -c "import secrets;print(secrets.token_urlsafe(32))"`).'
                    )
                # CR-3: RabbitMQ telemetri akisindan kaldirildi (0.4.x cutover).
                # Eski .env'den `RABBITMQ_URL=amqp://...` gelirse production'da
                # reddet. Operator yanlislikla legacy deploy'a yonelmis demek;
                # sessiz "field tutuluyor ama yok sayiliyor" olarak gormek
                # incident response'i zorlastirir.
                if (self.rabbitmq_url or "").strip():
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da RABBITMQ_URL "
                        "set edilemez. Gateway telemetriyi HTTP ingest veya "
                        "legacy JetStream ile yollar; RabbitMQ'ya BAGLANMIYOR. "
                        ".env dosyasindan RABBITMQ_URL satirini silin (alarm akisi "
                        "backend tarafinda RabbitMQ'da kalir, gateway'i ilgilendirmez)."
                    )
                # CR-2: dual-publish bayragi 0.4.x'te DEPRECATED. Eski .env'lerden
                # `true` gelirse no-op olarak sessizce gecmek yerine prod'da hata
                # ver — operator beklediginden farkli bir davranisla karsilasmasin.
                if self.nats_dual_publish_enabled:
                    raise ValueError(
                        "GUVENLIK: APP_ENVIRONMENT=production'da "
                        "NATS_DUAL_PUBLISH_ENABLED=true olamaz. Bu bayrak 0.4.x "
                        "cutover'i sonrasi DEPRECATED (gateway artik tek yol "
                        "JetStream). .env'den NATS_DUAL_PUBLISH_ENABLED satirini "
                        "silin veya false yapin."
                    )
        return self

    @property
    def is_mock_mode(self) -> bool:
        return self.gateway_mode.strip().lower() == "mock"

    @property
    def is_dnp3_mode(self) -> bool:
        return self.gateway_mode.strip().lower() == "dnp3"


class ConfigError(RuntimeError):
    """Konfigurasyon gecersiz. Mesaj OPERATORE yoneliktir (traceback degil)."""


def format_config_error(exc: BaseException) -> str:
    """Pydantic dogrulama hatasini operatorun okuyabilecegi hale getirir.

    Ham `ValidationError` traceback'i sahada ise yaramiyor: 20 satirlik pydantic
    ic yigini iceriyor, hangi .env satirinin duzeltilecegi kaybolyor.
    """
    ham = str(exc)
    satirlar: list[str] = []
    for parca in ham.splitlines():
        p = parca.strip()
        if not p:
            continue
        # Belge linki operatorun isine yaramaz.
        if p.startswith("For further information visit"):
            continue
        # Pydantic "N validation error(s) for Settings" basligi gereksiz.
        if p.endswith("for Settings") and "validation error" in p:
            continue
        # Pydantic "Value error, <bizim mesajimiz>" formatini sadelestir.
        if p.startswith("Value error, "):
            p = p[len("Value error, ") :]
        # GUVENLIK: pydantic mesajin SONUNA `[type=..., input_value={...}]`
        # ekliyor ve `input_value` TUM AYAR SOZLUGUNU dokuyor — icinde
        # GATEWAY_TOKEN de olabilir. Bu kuyruk stdout'a, NSSM log dosyasina ve
        # `docker logs`a duserdi. Kesip atiyoruz.
        kesim = p.find(" [type=")
        if kesim >= 0:
            p = p[:kesim]
        satirlar.append(p.rstrip())
    return "\n".join(satirlar) if satirlar else ham


def load_settings(**kwargs: object) -> Settings:
    """Settings kurar; dogrulama hatasini ConfigError'a cevirir.

    Caller (bkz. __main__.main) bunu yakalayip operatore TEMIZ bir mesaj
    basar ve tanimli bir cikis koduyla doner.
    """
    try:
        return Settings(**kwargs)  # type: ignore[arg-type]
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError dahil
        raise ConfigError(format_config_error(exc)) from exc


# Modul-seviye singleton ARTIK LAZY.
#
# NEDEN: eskiden burada `settings = Settings()` vardi ve dogrulama IMPORT
# ANINDA calisiyordu. Sonuclari sahada agirdi:
#   * Hata argparse'tan ONCE olustugu icin `--env-file` / `--help` hic
#     islenemiyordu.
#   * Logging kurulmadan patliyordu -> log dosyasi olusmuyordu.
#   * Health server hic acilmiyordu -> operator uzaktan teshis edemiyordu.
#   * Cikti ham bir pydantic traceback'iydi; hangi .env satirinin yanlis
#     oldugu kayboluyordu.
#   * Docker'da `restart: unless-stopped` ile sessiz crash-loop.
# Artik ilk ERISIMDE kurulur; `__main__` zaten kendi Settings'ini kurup
# `run(cfg)`'e gecirdigi icin uretim yolunda buraya hic ugranmaz.
_settings_cache: Settings | None = None


def get_settings() -> Settings:
    """Surec omrunde tek kez kurulan varsayilan Settings ornegi."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache


def reset_settings_cache() -> None:
    """Testler icin: bir sonraki get_settings() yeniden kursun."""
    global _settings_cache
    _settings_cache = None


def __getattr__(name: str) -> object:
    """`from dnp3_gateway.config import settings` geriye uyumlulugu.

    PEP 562: modul sozlugunde bulunmayan ad icin cagrilir. Boylece eski
    import bicimi calismaya devam eder ama YALNIZCA gercekten kullanildiginda
    Settings kurulur — import etmek tek basina dogrulama tetiklemez.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
