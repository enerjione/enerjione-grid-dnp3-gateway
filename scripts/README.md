# Scripts

PowerShell yardimcilari (Windows ortami icin):

- **`install.ps1`** — Python 3.10+ venv olusturur, requirements.txt'i yukler,
  `.env` dosyasi yoksa `.env.example`'dan UTF-8 BOM-siz kopyalar ve NTFS ACL
  ile sadece mevcut kullaniciya erisim verir.
  ```powershell
  ./scripts/install.ps1
  ./scripts/install.ps1 -Recreate           # .venv'i sifirlayip yeniden kur
  ./scripts/install.ps1 -Python "py -3.12"  # farkli Python surumu
  ```

- **`run_gateway.ps1`** — `.env`'yi yukleyip `py -m dnp3_gateway` calistirir.
  Parametre ile process-level override:
  ```powershell
  ./scripts/run_gateway.ps1
  ./scripts/run_gateway.ps1 -GatewayCode GW-002 -GatewayToken <token> -HealthPort 8021
  ```

- **`new_gateway.ps1`** — Yeni bir gateway instance icin `.env.<CODE>` dosyasi
  uretir. Rastgele 32-byte token uretir + NTFS ACL ile dosya izinlerini
  kisitlar. Token konsola yazilmaz (PSReadLine history sizmasini onler).
  ```powershell
  ./scripts/new_gateway.ps1 -Code GW-002
  ./scripts/new_gateway.ps1 -Code GW-002 -HealthPort 8021 -Environment production
  py -m dnp3_gateway --env-file .env.GW-002
  ```

## "ps1 calismiyor" (sik nedenler)

1. **Execution Policy** — Proje kokunden:
   ```powershell
   Set-Location "...\EnerjiOne Grid DNP3 Gateway"
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_gateway.ps1
   ```
2. **Profil yok / Path** — Yukaridaki gibi `powershell -File` tam yol ile deneyin.
3. **Sag tikla calistir** bazen Working Directory yanlis alir; yukaridaki
   `Set-Location` yontemi en guvenli.

Kurulumdan sonra:
- `run_gateway.cmd` (varsa) cift tik ile baslatma, veya
- proje kokunden: `.\.venv\Scripts\python.exe -m dnp3_gateway`

## NSSM ile Windows servisi olarak kurulum

Production saha kurulumu icin tam adimlar: [`docs/RUNBOOK.md`](../docs/RUNBOOK.md#1-baslatma).

Ozet:
```powershell
nssm install EnerjiOneDnp3Gateway `
    "C:\Projeler\EnerjiOne Grid DNP3 Gateway\.venv\Scripts\python.exe" `
    "-m dnp3_gateway"
nssm set EnerjiOneDnp3Gateway AppDirectory "C:\Projeler\EnerjiOne Grid DNP3 Gateway"
nssm start EnerjiOneDnp3Gateway
```

Onemli: NSSM stdout rotasyonu yapmaz; `.env`'de `LOG_FILE_PATH` set ederek
gateway'in kendi RotatingFileHandler'ini kullanmasini saglayin (yoksa servis
log dosyasi sonsuza kadar buyur).
