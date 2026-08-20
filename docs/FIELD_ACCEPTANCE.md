# Saha kabulu — Horstmann Smart Navigator 2.0 (FAT/SAT)

Bu dokuman **gercek cihazla** yapilmasi gereken kabul adimlarini tarif eder.
Otomatik testler bu adimlarin YERINE GECMEZ: gateway'in sessiz kalmasi
ancak gercek bir modemin kapandigini gormekle dogrulanir.

> **DURUM: FIELD_PENDING.** Bu depoya fiziksel bir Smart Navigator bagli
> DEGIL. Asagidaki adimlarin hicbiri PASS olarak isaretlenmemistir.

---

## 0. Kurallar (ihlal edilemez)

| | |
|---|---|
| CROB / SELECT / OPERATE | **YASAK** — ayrica yetkilendirilmedikce |
| `reset_all_fcis`, cikis komutlari, firmware islemleri | **YASAK** |
| Konfigurasyon yazma (cihaza) | **YASAK** |
| Izin verilen | TCP gozlemi, telemetri, olay alimi, pasif paket yakalama, `/health` okuma |

Bir olayi tetiklemek icin saha ekipmanini fiziksel olarak degistirmek
gerekiyorsa **otomatik yapilmaz**; adim `REQUIRES_OPERATOR_FIELD_ACTION`
olarak isaretlenir.

---

## 1. On kosullar

```bash
# 1) Gateway surumu ve etkin ayarlar
curl -s http://127.0.0.1:8020/health | jq '{version, status}'

# 2) Cihaz sozlesmesi (backend'de)
#    ip_endpoint_type = initiating
#    master_ip_port   = <blok icinde bir port>
#    session_policy   = smart | auto
#
# 3) Host port blogu compose'da YAYINLANMIS olmali
docker inspect eg-gw-gw-001 --format '{{json .HostConfig.PortBindings}}' | jq
#    -> "20100/tcp" ... gorunmeli. Gorunmuyorsa cihaz HICBIR ZAMAN baglanamaz.

# 4) Dinleyici gercekten acik mi
ss -lntp | grep -E ':(20100|20101)'
```

`/health` -> cihaz ozetinde `listener_error` sayaci **0** olmali. Degilse
port baska bir surec/gateway tarafindan kullaniliyordur.

---

## 2. Paket yakalama (Wireshark GUI GEREKMEZ)

On-prem Ubuntu icin `tcpdump` yeterlidir. Gateway -> cihaz yonundeki DNP3
trafigini olcuyoruz; ilgisiz host trafigi SAYILMAZ.

```bash
# CIHAZ IP'si ve master portu ile daralt (DNP3 = 0x0564 ile baslar)
sudo tcpdump -i any -n "host <CIHAZ_IP> and tcp port <MASTER_IP_PORT>" \
     -w /tmp/sn2-kabul.pcap

# Yalnizca GATEWAY -> CIHAZ yonu, bayt sayimi:
sudo tcpdump -r /tmp/sn2-kabul.pcap -n \
     "src host <GATEWAY_IP> and tcp port <MASTER_IP_PORT> and greater 1" | wc -l
```

`scripts/field_capture.sh` bu komutlari sarmalar ve sessizlik penceresini
otomatik olcer (bkz. bolum 3A).

---

## 3A. SMART + INITIATING yasam dongusu

**Amac:** cihaz uyanir, veriyi verir, gateway SUSAR, modem kapanir ve bu
`comm_lost` URETMEZ.

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihazda ariza/olay tetikle (ya da zamanlanmis raporu bekle) | modem acilir | `REQUIRES_OPERATOR_FIELD_ACTION` |
| 2 | Cihaz gateway host/master portuna TCP acar | `ss` ile ESTABLISHED gorulur | FIELD_PENDING |
| 3 | Docker/host dogru container'a yonlendirir | `yadnp3_master_link_open device=...` logu | FIELD_PENDING |
| 4 | DNP3 oturumu acilir, gecerli kanit gelir | `/health` -> `state=online` | FIELD_PENDING |
| 5 | Unsolicited/olay verisi yayinlanir | telemetri backend'e ulasir | FIELD_PENDING |
| 6 | **Gateway susar** | `>= 15 sn` boyunca gateway->cihaz **0 DNP3 bayti** | FIELD_PENDING |
| 7 | Cihaz TCP'yi kapatir / modem uyur | `yadnp3_master_link_close` | FIELD_PENDING |
| 8 | Gateway `smart_idle`e gecer | `/health` -> `state=smart_idle` | FIELD_PENDING |
| 9 | `connected=false`, `reachable=false` | `/health` | FIELD_PENDING |
| 10 | **comm_lost YOK** | SCADA'da cihaz kopuk GORUNMEZ | FIELD_PENDING |

Sessizlik kaniti (adim 6) icin:

```bash
./scripts/field_capture.sh --device-ip <CIHAZ_IP> --port <MASTER_IP_PORT> \
    --gateway-ip <GATEWAY_IP> --window 20
# Beklenen cikti: "gateway -> cihaz DNP3 bayti (20s): 0"
```

---

## 3B. `smart_idle` iken RESTART

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihazi dogrulanmis `smart_idle`e getir (3A) | — | FIELD_PENDING |
| 2 | `docker compose restart` (volume KORUNUR) | — | FIELD_PENDING |
| 3 | `smart_idle` geri yuklenir | log: `smart_idle_restored device=...` | FIELD_PENDING |
| 4 | Filo capinda comm_lost firtinasi YOK | `/health` -> `devices.lost` artmaz | FIELD_PENDING |
| 5 | Son gecerli temas korunur | `/health` -> `last_valid_contact_epoch` | FIELD_PENDING |
| 6 | Cihaza istek URETILMEZ | capture: 0 bayt | FIELD_PENDING |

> Volume silinirse (`docker compose down -v`) bu test ANLAMSIZDIR — kalici
> kayit da silinmis olur.

---

## 3C. BOOST kontrol testi (ayni gateway)

`MASTER-A = Smart`, `MASTER-B = Boost` ayni gateway'de:

| # | Beklenen | Sonuc |
|---|---|---|
| 1 | A: Smart yasam dongusu (sessiz + uyku) | FIELD_PENDING |
| 2 | B: surekli baglanti, normal taramalar | FIELD_PENDING |
| 3 | A uyurken B'nin taramasi DEVAM eder | FIELD_PENDING |
| 4 | B'nin trafigi A'yi uyandirmaz | FIELD_PENDING |
| 5 | `/health` -> `session_policies` ikisini de dogru sayar | FIELD_PENDING |

---

## 3D. AUTO testi

`session_policy=auto` ile:

| # | Adim | Beklenen | Sonuc |
|---|---|---|---|
| 1 | Cihaz Smart bildirir | `operation_mode=smart`, `effective_session_policy=smart` | FIELD_PENDING |
| 2 | Cihaz Boost bildirir | `operation_mode=boost`, `effective=continuous` | FIELD_PENDING |
| 3 | Smart -> Boost gecisi | log `device_operation_mode_changed`, taramalar baslar, **oturum yikilmaz** | FIELD_PENDING |
| 4 | Boost -> Smart gecisi | YALNIZCA o cihazin master'i yeniden kurulur | FIELD_PENDING |
| 5 | Komsu cihazlarin master'i yeniden kurulmaz | log'da baska `device=` yok | FIELD_PENDING |

**Mod polaritesi dogrulamasi (KRITIK):** ilk gecerli gozlemde log satirini
okuyun:

```
device_operation_mode_changed device=SN2-1 old=unknown new=smart raw=1.0 ...
```

Cihazin PANELDE gosterdigi mod ile `new=` esesmiyorsa polarite terstir.
Kod degisikligi GEREKMEZ — `operation_mode.normalize_operation_mode`
`smart_raw_value` parametresiyle ters cevrilebilir (bkz. Acik Riskler).

---

## 3E. Hata senaryolari

| # | Senaryo | Beklenen | Sonuc |
|---|---|---|---|
| 1 | TCP acilir ama DNP3 kaniti YOK -> kapanir | `smart_idle` DEGIL, `lost` | FIELD_PENDING |
| 2 | Yanlis `dnp3_address` | `recovering` -> `lost`, log'da gorunur | FIELD_PENDING |
| 3 | Host portu kapali/yanlis | cihaz baglanamaz; `ss` bos, `/health` `listener_expected=true` | FIELD_PENDING |
| 4 | Ayni gateway'de cakisan `master_ip_port` | **config REDDEDILIR** (calisma zamanina hic gelmez) | ✅ otomatik test |
| 5 | Sessizlik esigi asilir | `smart_idle` -> `lost`, comm_lost **TAM BIR KEZ** | FIELD_PENDING |
| 6 | Taze kanitla yeniden baglanma | `recovering` -> `online` | FIELD_PENDING |
| 7 | `smart_idle` iken komut | `status=offline`, fiziksel islem 0 | ✅ otomatik test |

---

## 4. Kalite bayragi pilotu (G-QUALITY-PILOT)

**Ayar (dogrulanmis kanonik ad):** `DNP3_PUBLISH_QUALITY_FLAGS`
(`config.py` -> `dnp3_publish_quality_flags`). Varsayilan `false`.

Bu bayrak **TEK BIR gateway'de** acilir; filo geneline saha dogrulamasi
yapilmadan ACILMAZ.

```bash
# Pilot gateway'de
DNP3_PUBLISH_QUALITY_FLAGS=true
```

| Durum | Nasil uretilir | Guvenli mi | Sonuc |
|---|---|---|---|
| normal analog -> `good` | dogal isletme | ✅ | FIELD_PENDING |
| normal binary -> `good` | dogal isletme | ✅ | FIELD_PENDING |
| normal double-bit -> `good` | kesici pozisyonu | ✅ | FIELD_PENDING |
| `restart` | cihaz yeniden baslatilir | ⚠ operator karari | `REQUIRES_OPERATOR_FIELD_ACTION` |
| `forced` (local/remote) | noktayi elle zorlama | ⚠ operator karari | `REQUIRES_OPERATOR_FIELD_ACTION` |
| analog `invalid` / referans hatasi | CT/referans kaybi | ❌ guvenli uretilemez | `NOT_TESTED_PHYSICAL` |
| `comm_lost` | link kopmasi (kablo/APN) | ✅ | FIELD_PENDING |

> **Cift-bit (G3) durum bitleri analog referans hatasiyla KARISTIRILAMAZ.**
> Ayni bayrak byte'i (`0x41`) G3'te "kesici ACIK", G30'da "REFERENCE_ERR"
> demektir. Bu ayrim `map_dnp3_quality(flags, object_group)` icinde tipe
> gore yapilir ve `tests/test_dnp3_quality_and_resilience.py` ile gercek
> DNP3 trafigi uzerinde pinlenir — **otomatik dogrulanmistir**, sahada
> yeniden uretilmesi GEREKMEZ.

Guvenli sekilde uretilemeyen durumlar icin **fabrikasyon PASS yazilmaz**;
`NOT_TESTED_PHYSICAL` isaretlenir.

---

## 5. Kabul ozeti sablonu

Raporlarken her satiri su dortluden biriyle isaretleyin:

```
PASS                            (kanit ekli)
FAIL                            (kanit ekli)
FIELD_PENDING                   (fiziksel cihaz yok)
REQUIRES_OPERATOR_FIELD_ACTION  (saha mudahalesi gerekiyor)
NOT_TESTED_PHYSICAL             (guvenli uretilemez)
```

Kanit = `pcap` dosyasi, `/health` ciktisi ya da log satiri. Kanitsiz PASS
KABUL EDILMEZ.
