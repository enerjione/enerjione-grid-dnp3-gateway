<#
.SYNOPSIS
    Gateway'i .venv icindeki Python ile calistirir.

.DESCRIPTION
    `.env` dosyasi otomatik yuklenir (pydantic-settings). Ayni makinede birden
    fazla gateway instance'i calistirilacaksa -GatewayCode / -GatewayToken /
    -HealthPort parametreleri process-level override olarak verilebilir.

.EXAMPLE
    ./scripts/run_gateway.ps1

.EXAMPLE
    ./scripts/run_gateway.ps1 -GatewayCode GW-002 -GatewayToken <token> -HealthPort 8021

.EXAMPLE
    # Mevcut .env'i ezmeden saha test deploy'u
    ./scripts/run_gateway.ps1 -BackendUrl "https://api.enerjione.local/api/v1" -NatsUrl "nats://nats.local:4222"
#>
[CmdletBinding()]
param(
    [string] $GatewayCode,
    [string] $GatewayToken,
    [string] $Mode,
    [int] $HealthPort,
    [string] $BackendUrl,
    [string] $NatsUrl
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path $python)) {
    throw "venv kurulmamis. Once: ./scripts/install.ps1"
}

if ($GatewayCode)   { $env:GATEWAY_CODE   = $GatewayCode }
if ($GatewayToken)  { $env:GATEWAY_TOKEN  = $GatewayToken }
if ($Mode)          { $env:GATEWAY_MODE   = $Mode }
if ($HealthPort)    { $env:WORKER_HEALTH_PORT = "$HealthPort" }
if ($BackendUrl)    { $env:BACKEND_API_URL = $BackendUrl }
if ($NatsUrl)       { $env:NATS_URL       = $NatsUrl }

$env:PYTHONPATH = (Join-Path $projectRoot 'src') + [IO.Path]::PathSeparator + $env:PYTHONPATH
# .env sadece Python/pydantic ile yuklenir; bos env ile yaniltici satir vermeyelim
$summary = Join-Path $projectRoot 'scripts\show_env_summary.py'
if (Test-Path $summary) {
    Write-Host '--- .env ozeti (calisma oncesi) ---' -ForegroundColor Cyan
    & $python $summary
    Write-Host '------------------------------------' -ForegroundColor Cyan
}

& $python -m dnp3_gateway
exit $LASTEXITCODE
