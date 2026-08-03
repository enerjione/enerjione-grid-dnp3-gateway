# Yeni gateway instance icin .env dosyasi olusturur.
#
# Kullanim:
#   .\scripts\new_gateway.ps1 -Code GW-007
#   .\scripts\new_gateway.ps1 -Code GW-007 -HealthPort 8027 -BackendUrl http://192.168.1.10:8000/api/v1
#
# Ayni PC'de coklu gateway icin her birine ayri kod + ayri health portu verin;
# tum gateway'ler ayni NATS JetStream broker'ina yayin yapar, backend de
# aynidir. Cihazlar backend tarafinda gateway-koduna atanir (Smart Logger
# frontend). RabbitMQ artik telemetri akisi icin kullanilmiyor (0.4.x
# cutover)  -  alarm/notification icin backend tarafinda kalir, gateway
# onunla ilgilenmez.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, HelpMessage = "Gateway kodu (orn: GW-007). Backend kayit edilen kodla AYNI olmali.")]
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")]
    [string]$Code,

    [Parameter(HelpMessage = "Health/metrics HTTP portu. 0 = OS rastgele atasin. Bos birakirsaniz Code uzerinden hesaplanir.")]
    [int]$HealthPort = -1,

    [Parameter(HelpMessage = "Backend API base URL (Smart Logger Process Backend).")]
    [string]$BackendUrl = "http://127.0.0.1:8000/api/v1",

    [Parameter(HelpMessage = "NATS JetStream URL (gateway'in tek telemetri yayin yolu).")]
    [string]$NatsUrl = "nats://localhost:4222",

    [Parameter(HelpMessage = "Ortam: development | staging | production.")]
    [ValidateSet("development", "staging", "production")]
    [string]$Environment = "development",

    [Parameter(HelpMessage = "MAX_PARALLEL_DEVICES varsayilani. Buyuk gateway'lerde 100+ verin.")]
    [int]$MaxParallelDevices = 25,

    [Parameter(HelpMessage = "Cikti dosyasi. Belirtmezseniz .env.<Code>")]
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not $OutFile) { $OutFile = ".env.$Code" }

if (Test-Path $OutFile) {
    Write-Error "Cikti dosyasi zaten var: $OutFile  (uzerine yazmaz). Once silin veya -OutFile farkli verin."
    exit 1
}

# Health port: kullanici vermediyse Code'un sonundaki sayidan +8000 hesapla, yoksa 0 (auto)
if ($HealthPort -lt 0) {
    if ($Code -match '(\d+)$') {
        $n = [int]$matches[1]
        $HealthPort = 8020 + $n
    } else {
        $HealthPort = 0
    }
}

# 32 byte rastgele token (production icin yeterli)
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$token = [Convert]::ToBase64String($bytes).Replace("+","-").Replace("/","_").TrimEnd("=")

$envContent = @"
# Bu dosya scripts/new_gateway.ps1 tarafindan uretilmistir. Code: $Code
# ----------------------------------------------------------------------------
# EnerjiOne DNP3 Gateway - $Code
# ----------------------------------------------------------------------------

# Kimlik
GATEWAY_CODE=$Code
GATEWAY_TOKEN=$token
GATEWAY_NAME=EnerjiOne DNP3 Gateway $Code

# Ortam
APP_ENVIRONMENT=$Environment

# Coklu gateway: her instance icin ayri state-dir alt dizini kullanmak gerekirse
# GATEWAY_STATE_DIR=.gateway_state\\$Code  seklinde acin. Mevcut implementasyon
# instance dosyasini code-bazli ayirir (.gateway_state\\instance_$Code.id),
# yani ayni dizinde kalmasi sorun olusturmaz.
GATEWAY_STATE_DIR=.gateway_state

# Calisma modu: mock | dnp3
GATEWAY_MODE=dnp3

# Backend API (Smart Logger Process Backend)
BACKEND_API_URL=$BackendUrl
BACKEND_API_VERIFY_SSL=true
# BACKEND_API_CA_PATH=
CONFIG_REFRESH_SEC=30
CONFIG_TIMEOUT_SEC=5

# NATS JetStream (PRIMARY  -  gateway'in tek telemetri yayin yolu)
# Subject: e1.telemetry.raw.<GATEWAY_CODE>. Backend stream TELEMETRY_RAW
# bu prefix'i `e1.telemetry.raw.>` wildcard ile yakalar. NATS erisilemezse
# gateway yine ayaga kalkar; mesajlar outbox'a yazilir, baglanti gelince
# retrier bosaltir (mesaj kaybi yok).
NATS_URL=$NatsUrl
NATS_SUBJECT_PREFIX=e1.telemetry.raw

# Health HTTP (port=0 -> OS rastgele bos port atar; gercek port log'da gorunur)
WORKER_HEALTH_HOST=127.0.0.1
WORKER_HEALTH_PORT=$HealthPort

# Polling / paralellik
# NOT: 5sn cok yavasti  -  frontend'de olcum yasi 5-30sn araliginda salinuyordu.
# config.py varsayilani 1sn; sablon artik onu eziyor DEGIL.
DEFAULT_POLL_INTERVAL_SEC=1
MAX_PARALLEL_DEVICES=$MaxParallelDevices

# Her gateway KENDI state dizinini kullanmali: outbox, komut ledger'i,
# config cache ve instance lock burada tutulur. Ayni dizini paylasan iki
# instance ayni SQLite dosyalarina yazar.
GATEWAY_STATE_DIR=.gateway_state/$Code

# DNP3 master parametreleri
DNP3_LOCAL_ADDRESS=1
DNP3_TCP_PORT=20000
DNP3_EVENT_BASELINE_INTERVAL_SEC=60
# opendnp3 IO thread sayisi: 0 = otomatik (min 4). 100+ cihazda 8 onerilir.
DNP3_MANAGER_THREADS=0
# Outstation saatlerini senkronize et (drift + guc kesintisi sonrasi RTC reset).
DNP3_TIME_SYNC=lan

# Loglama
LOG_LEVEL=INFO
LOG_FORMAT=text
# NSSM ile servis olarak kurulacaksa rotation icin ZORUNLU. Bu satir
# uretilmedigi icin sahada "zorunlu" denilen rotation aslinda yoktu:
# RotatingFileHandler hic devreye girmiyor, tum cikti NSSM'in AppStdout
# dosyasina siniersiz akiyordu.
LOG_FILE_PATH=logs/$Code.log
"@

Set-Content -Path $OutFile -Value $envContent -Encoding utf8

# Token'i dosyaya yazdiktan SONRA dosya izinlerini kisitla (sadece sahibi okusun).
# Windows'ta NTFS ACL ile Inheritance kapatip sadece mevcut kullaniciya Read/Write veriyoruz.
try {
    $acl = Get-Acl $OutFile
    $acl.SetAccessRuleProtection($true, $false)  # protection ON, inherited rules OFF
    $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentUser,
        "FullControl",
        "Allow"
    )
    $acl.SetAccessRule($rule)
    Set-Acl -Path $OutFile -AclObject $acl
} catch {
    Write-Warning "Dosya izinleri kisitlanamadi: $_  (devam ediliyor)"
}

Write-Host ""
Write-Host "Olusturuldu: $OutFile" -ForegroundColor Green
Write-Host "  GATEWAY_CODE   : $Code"
Write-Host "  HEALTH_PORT    : $HealthPort"
Write-Host "  BACKEND_API_URL: $BackendUrl"
Write-Host "  NATS_URL       : $NatsUrl"
Write-Host ""
Write-Host "GATEWAY_TOKEN olusturuldu ve dosyaya yazildi." -ForegroundColor Green
Write-Host "  -> $OutFile  (icindeki GATEWAY_TOKEN= satirini Smart Logger backend kaydina yapistirin)"
Write-Host "  -> Konsolda gosterilmiyor: PSReadLine history / screen-share / log aggregator sizintisini onlemek icin."
Write-Host ""
Write-Host "Sonraki adimlar:" -ForegroundColor Yellow
Write-Host "  1. Backend gateway kaydini olustur: arayuz / API uzerinden ayni Code + ayni TOKEN."
Write-Host "     Token'i okumak icin (terminal'i kapatip acmadan, sadece ihtiyacin oldugunda):"
Write-Host "       Select-String -Path $OutFile -Pattern '^GATEWAY_TOKEN='"
Write-Host "  2. Cihazlari frontend uzerinden bu gateway'e atayin."
Write-Host "  3. Gateway'i baslatin:"
Write-Host "       py -m dnp3_gateway --env-file $OutFile"
Write-Host ""

# Token degiskenini bellekten temizle (best-effort).
$token = $null
Remove-Variable token -ErrorAction SilentlyContinue
