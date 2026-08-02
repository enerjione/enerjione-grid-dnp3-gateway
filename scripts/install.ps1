<#
.SYNOPSIS
    EnerjiOne DNP3 Gateway kurulumu: venv + requirements + .env hazirligi.

.DESCRIPTION
    Ilk kullanimda calistirin. Varolan kurulumu temiz sifirlamak icin
    -Recreate parametresi kullanilabilir.

    NSSM ile servis olarak kurulum icin ek adimlar docs/RUNBOOK.md
    icindedir; bu betik sadece venv + bagimliliklar + .env iskeletini
    hazirlar.

.PARAMETER Recreate
    Mevcut .venv klasoru silinir ve bastan kurulur.

.PARAMETER Python
    Kullanilacak python launcher (varsayilan: py -3.10).
#>
[CmdletBinding()]
param(
    [switch] $Recreate,
    [string] $Python = 'py -3.10'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

Write-Host "[install] project root: $projectRoot" -ForegroundColor Cyan

if ($Recreate -and (Test-Path '.venv')) {
    Write-Host '[install] removing existing .venv' -ForegroundColor Yellow
    Remove-Item '.venv' -Recurse -Force
}

if (-not (Test-Path '.venv')) {
    Write-Host "[install] creating venv via: $Python -m venv .venv" -ForegroundColor Cyan
    # cmd ile degil dogrudan: 'py -3.10' veya tek yol (python.exe) calisir
    $argv = -split $Python
    $pyArgs = [System.Collections.ArrayList]@()
    if ($argv.Count -gt 1) { [void]$pyArgs.AddRange($argv[1..($argv.Count - 1)]) }
    $pyArgs.AddRange(@('-m', 'venv', '.venv'))
    & $argv[0] @($pyArgs.ToArray())
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}

$pip = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path $pip)) {
    throw "python executable bulunamadi: $pip"
}

Write-Host '[install] upgrading pip' -ForegroundColor Cyan
& $pip -m pip install --upgrade pip | Out-Null

Write-Host '[install] installing requirements.txt' -ForegroundColor Cyan
& $pip -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install basarisiz (cikis kodu: ' + $LASTEXITCODE + ')' }

# DNP3 master kutuphanesi (yadnp3 / OpenDNP3 native).
#
# ONEMLI: Bu adim EKSIKTI. Saha muhendisi dokumante edilen install.ps1'i
# calistirip .env'de GATEWAY_MODE=dnp3 yaptiginda gateway boot ediyor, health
# server aciliyor, sonra `Yadnp3AdapterError: yadnp3 (opendnp3) yuklu degil`
# ile duruyordu — yani "hizli kurulum scripti" varsayilan konfigurasyonla
# calisan bir kurulum uretmiyordu.
Write-Host '[install] installing yadnp3 (OpenDNP3 native DNP3 master)' -ForegroundColor Cyan
& $pip -m pip install "yadnp3==3.2.1.1"
if ($LASTEXITCODE -ne 0) {
    Write-Warning @'
yadnp3 kurulamadi. Gateway GATEWAY_MODE=dnp3 ile CALISMAYACAKTIR.
Olasi sebepler:
  * Python surumu icin wheel yok (desteklenen: 3.10 - 3.12)
  * Internet/proxy erisimi yok  -> wheel'i elle indirip:
      .venv\Scripts\python.exe -m pip install <indirilen>.whl
Alternatif (SINIRLI): DNP3_LIBRARY=dnp3py — saf Python, Group 110 string
sinyalleri DESTEKLEMEZ ve OpenDNP3 outstation'larla tutarsiz davranabilir.
'@
} else {
    Write-Host '[install] yadnp3 kurulumu tamam' -ForegroundColor Green
}

# Paketin KENDISI. Bu adim EKSIKTI ve dokumante edilen TUM Windows baslatma
# yollarini kiriyordu:
#   README.md, docs/RUNBOOK.md, scripts/README.md ve new_gateway.ps1 hepsi
#   `py -m dnp3_gateway` diyor. Paket kurulmadigi icin bu komut
#   `ModuleNotFoundError: No module named 'dnp3_gateway'` ile aninda oluyordu.
#   NSSM servisi SERVICE_AUTO_START oldugu icin sonsuz restart ediyor; health
#   server hic acilmadigi ve logging kurulmadan patladigi icin operator
#   /health'e baglanamiyor ve log dosyasi da olusmuyordu.
# `-e` (editable): saha kurulumunda kaynak dizini yerinde kalir, `git pull` ile
# guncelleme yeniden kurulum gerektirmez.
Write-Host '[install] installing package (editable)' -ForegroundColor Cyan
& $pip -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "paket kurulumu basarisiz (cikis kodu: $LASTEXITCODE)" }

# Kurulum DOGRULAMASI — "kuruldu ama calismiyor" durumunu burada yakala.
Write-Host '[install] verifying installation' -ForegroundColor Cyan
$verify = & $pip -c "import dnp3_gateway; print(dnp3_gateway.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "DOGRULAMA BASARISIZ: paket import edilemiyor.`n$verify"
}
Write-Host "[install] dnp3_gateway $verify import edilebiliyor" -ForegroundColor Green

$modeCheck = & $pip -m dnp3_gateway --help 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "python -m dnp3_gateway calistirilamadi:`n$modeCheck"
} else {
    Write-Host '[install] `python -m dnp3_gateway` calisiyor' -ForegroundColor Green
}

if (-not (Test-Path '.env')) {
    if (Test-Path '.env.example') {
        Write-Host '[install] creating .env from .env.example (UTF-8 no-BOM)' -ForegroundColor Green
        # UTF-8 BOM olmadan yaz — bazi YAML/.env parser'lar (ozellikle Docker
        # Compose env_file) BOM karakterini ilk anahtarin parcasi sanir
        # (﻿SECRET_KEY) ve hatali parse eder.
        $content = Get-Content '.env.example' -Raw -Encoding UTF8
        [System.IO.File]::WriteAllText(
            (Resolve-Path '.env.example').Path.Replace('.env.example', '.env'),
            $content,
            (New-Object System.Text.UTF8Encoding($false))
        )
    } else {
        Write-Warning '.env.example bulunamadi; .env olusturulmadi'
    }
} else {
    Write-Host '[install] .env zaten mevcut; degistirilmedi' -ForegroundColor DarkGray
}

# Production hardening: .env ACL kisitla (sadece sahibi okusun).
# Token bu dosyada plain-text durur; PowerShell history / log aggregator / yedek
# yedeklerine sizmasini sinirla.
if (Test-Path '.env') {
    try {
        $acl = Get-Acl '.env'
        $acl.SetAccessRuleProtection($true, $false)  # protection ON, inherited rules OFF
        $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $currentUser, "FullControl", "Allow"
        )
        $acl.SetAccessRule($rule)
        Set-Acl -Path '.env' -AclObject $acl
        Write-Host '[install] .env ACL kisitlandi (sadece sahibi)' -ForegroundColor Green
    } catch {
        Write-Warning ".env ACL kisitlanamadi: $_  (devam ediliyor — production icin manuel duzeltin)"
    }
}

Write-Host '[install] tamamlandi.' -ForegroundColor Green
Write-Host '  Calistirmak icin: ./scripts/run_gateway.ps1' -ForegroundColor Gray
Write-Host '  Production checklist: docs/RUNBOOK.md + docs/SECURITY.md' -ForegroundColor Gray
