"""`X-E1-Gateway-Health` basligi — gateway saglik ozetini backend'e tasir.

NEDEN BU KANAL
--------------
Gateway zaten saniyede bir `GET /gateways/{code}/pending` cagiriyor. Saglik
ozeti o istege bir baslikla biniyor; ek istek maliyeti YOK.

NEDEN CIHAZ BAZINDA DURUM
-------------------------
Backend `communication_status`i yalnizca TELEMETRI GELDIGINDE
guncelleyebiliyor. Ariza bekleyen bir gosterge saatlerce sessiz kalabilir
(deger degismezse gateway hicbir sey yayinlamaz) ve o sure boyunca cihazin
canli mi olu mu oldugu BILINMIYOR.

"Veri gelmiyor" ile "haberlesme yok" ayni sey degil. Bu ayrimi yapabilecek
tek yer burasi: DNP3 baglantisi gateway'de ve link durumu zaten takip
ediliyor (`online` / `recovering` / `lost`).

NEDEN YALNIZCA ONLINE OLMAYANLAR
--------------------------------
Baslik boyutu SERT bir kisit. Backend 2 KB uzerini HIC ayristirmiyor ve
daha kotusu: nginx varsayilan baslik tavanina yaklasan bir baslik ISTEGIN
TAMAMINI reddettirir — yani KOMUTLAR DA GITMEZ. 600 cihazin durumunu
gondermek ~9 KB eder; kabul edilemez.

Cozum: yalnizca `online` OLMAYAN cihazlar gonderiliyor. Normal isleyiste bu
birkac tane, cogu zaman hic. Tasidigi bilgi de tam ihtiyac duyulan bilgi —
"hangi cihaz sorunlu". Online olanlar icin backend'in mevcut davranisi zaten
dogru (telemetri geldikce ONLINE kalirlar).

Liste yine de tavani asarsa KIRPILIR ve `states_truncated` ile bildirilir.
Sessiz kirpma, backend'e "geri kalan her sey iyi" dedirtirdi.

KOMUT KANALI KUTSAL
-------------------
Bu modulun hicbir hatasi `/pending` cagrisini dusurmemeli: orasi SCADA komut
kanali. Uretim tamamen savunmaci — her hata sessizce yutulur ve baslik
eklenmez.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

HEADER_NAME = "X-E1-Gateway-Health"

#: Backend'in ayristirma tavani 2048 bayt. Guvenli tarafta kaliyoruz —
#: tavana dayanan bir baslik nginx'te tum istegi reddettirebilir.
MAX_HEADER_BYTES = 1600

#: `states` icine en fazla kac cihaz konur. Asilirsa kirpilir ve
#: `states_truncated` isaretlenir.
MAX_STATE_ENTRIES = 40

#: Bu durumdaki cihazlar `states`e GIRMEZ — backend'in varsayilan davranisi
#: onlar icin zaten dogru.
_NORMAL_STATE = "online"


def _problem_states(per_device: dict[str, dict[str, Any]]) -> tuple[dict[str, str], bool]:
    """Online OLMAYAN cihazlarin durumu + kirpildi mi bilgisi.

    Siralama deterministik: ayni durumda ayni baslik uretilsin ki backend
    tarafinda gereksiz yazma olmasin ve gunlukler karsilastirilabilsin.
    """
    sorunlu: dict[str, str] = {}
    for kod in sorted(per_device):
        bilgi = per_device.get(kod) or {}
        durum = str(bilgi.get("state") or "unknown").strip().lower()
        if durum == _NORMAL_STATE:
            continue
        sorunlu[str(kod)] = durum

    if len(sorunlu) <= MAX_STATE_ENTRIES:
        return sorunlu, False
    kirpilmis = dict(list(sorunlu.items())[:MAX_STATE_ENTRIES])
    return kirpilmis, True


def build_payload(
    *,
    status: str,
    device_summary: dict[str, Any] | None,
    per_device: dict[str, dict[str, Any]] | None,
    outbox_pending: int | None = None,
    outbox_dead_letter: int | None = None,
    uptime_sec: int | None = None,
    gateway_version: str | None = None,
    issues: list[str] | None = None,
) -> dict[str, Any]:
    """Backend'in bekledigi govdeyi kurar."""
    devices: dict[str, Any] = {}
    if device_summary:
        for anahtar in ("total", "online", "recovering", "lost", "unknown"):
            if anahtar in device_summary:
                devices[anahtar] = device_summary[anahtar]

    if per_device:
        durumlar, kirpildi = _problem_states(per_device)
        if durumlar:
            devices["states"] = durumlar
        if kirpildi:
            # Sessiz kirpma, backend'e "geri kalan her sey iyi" dedirtirdi.
            devices["states_truncated"] = True

    govde: dict[str, Any] = {"status": status, "devices": devices}
    if outbox_pending is not None:
        govde["outbox_pending"] = int(outbox_pending)
    if outbox_dead_letter is not None:
        govde["outbox_dead_letter"] = int(outbox_dead_letter)
    if uptime_sec is not None:
        govde["uptime_sec"] = int(uptime_sec)
    if gateway_version:
        # ANAHTAR ADI BILEREK `version`: yayindaki backend
        # (gateway_health_service.record_health) bu anahtari okuyor. Burada
        # `gateway_version` yazmak, alanin sahada hep bos kalmasi demekti.
        govde["version"] = str(gateway_version)[:40]
    if issues:
        govde["issues"] = [str(i)[:120] for i in issues[:10]]
    return govde


def _kodla(payload: dict[str, Any]) -> str:
    ham = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(ham.encode("utf-8")).decode("ascii")


def _states_dusur(payload: dict[str, Any]) -> dict[str, Any]:
    """Cihaz listesini dusur, SAYIMLAR kalsin."""
    kucuk = dict(payload)
    cihazlar = dict(kucuk.get("devices") or {})
    if "states" not in cihazlar:
        return kucuk
    cihazlar.pop("states", None)
    cihazlar["states_truncated"] = True
    kucuk["devices"] = cihazlar
    return kucuk


def _issues_dusur(payload: dict[str, Any]) -> dict[str, Any]:
    """Serbest metin sorun listesini dusur."""
    kucuk = dict(payload)
    kucuk.pop("issues", None)
    return kucuk


def _cekirdege_in(payload: dict[str, Any]) -> dict[str, Any]:
    """Son care: yalnizca durum + cihaz SAYIMLARI.

    Bu kadari bile "kac cihaz kayip" sorusunu cevaplar; hicbir sey
    gondermemekten kiyaslanamayacak kadar iyidir.
    """
    cihazlar = {
        k: v
        for k, v in (payload.get("devices") or {}).items()
        if k in ("total", "online", "recovering", "lost", "unknown")
    }
    return {"status": payload.get("status", "unknown"), "devices": cihazlar}


#: Tavan asilirsa sirayla uygulanan kucultme adimlari. Sira ONEM SIRASINA
#: gore TERS: once en az bilgi tasiyani (uzun serbest metinler) atiyoruz,
#: cihaz sayimlari en sona kaliyor.
_KUCULTME_ADIMLARI = (_states_dusur, _issues_dusur, _cekirdege_in)


def encode_header(payload: dict[str, Any]) -> str | None:
    """Govdeyi base64url'e cevirir; tavani asarsa kademeli kucultur.

    Kucultme sirasi: cihaz listesi -> sorun metinleri -> yalnizca sayimlar.
    Hicbiri yetmezse `None` doner ve baslik HIC eklenmez — bozuk/devasa bir
    baslik gondermek komut kanalini dusurebilir, sessiz kalmak dusurmez.
    """
    try:
        kodlu = _kodla(payload)
        if len(kodlu) <= MAX_HEADER_BYTES:
            return kodlu

        kucuk = payload
        for adim in _KUCULTME_ADIMLARI:
            kucuk = adim(kucuk)
            kodlu = _kodla(kucuk)
            if len(kodlu) <= MAX_HEADER_BYTES:
                logger.debug("saglik basligi kuculturuldu (%s)", adim.__name__)
                return kodlu

        logger.debug("saglik basligi cekirdek halde bile tavani asti, gonderilmiyor")
        return None
    except Exception:  # noqa: BLE001
        # Komut kanali kutsal: baslik uretilemezse sessizce vazgec.
        logger.debug("saglik basligi uretilemedi", exc_info=True)
        return None
