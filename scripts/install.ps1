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
