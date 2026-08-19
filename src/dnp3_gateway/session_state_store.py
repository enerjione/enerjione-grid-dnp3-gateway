"""Cihaz basina DNP3 oturum durumu — YENIDEN BASLATMAYA DAYANIKLI kayit.

NEDEN GEREKLI
-------------
Sahada Smart Mode cihazlari normal sekilde UYKUDA olabilir: modemleri
kapali, bir sonraki zamanlanmis raporlarina kadar hicbir TCP oturumu yok.
Gateway/container yeniden baslarsa bellekteki "bu cihaz saglikli uyuyor"
bilgisi kaybolur ve 600 cihazin TAMAMI, bir sonraki gunluk check-in'lerine
kadar `comm_lost` gorunur. On-prem bir kurulumda bu, tek bir restart'in
sahayi saatlerce yanlis alarmla doldurmasi demektir.

Bu modul, o karari verebilmek icin gereken EN AZ bilgiyi diske yazar:
cihaz basina son oturum durumu ve son GERCEK temas ani.

NEDEN AYRI DOSYA (ikinci bir veritabani DEGIL)
----------------------------------------------
Ayni state dizininde duran `config_<GW>.json` BACKEND KONFIGURASYONUNUN
anlik goruntusudur ve her config surumunde bastan yazilir; calisma zamani
durumu oraya karistirmak, konfigurasyon onbelleginin anlamini bozardi
(ornegin backend erisilemezken yazilan bir mod guncellemesi config'i
"tazelenmis" gosterirdi). Bu yuzden kardes bir JSON dosyasi kullaniliyor —
yeni bir veritabani ya da yeni bir bagimlilik YOK, `GatewayState` ile ayni
atomik yazim deseni (tmp + os.replace).

BOZUK / ESKI DOSYA
------------------
Surum uyusmuyorsa, JSON bozuksa ya da dosya yoksa: SESSIZCE BOS kabul
edilir. Sonuc, ozelligin hic olmadigi durumla ayni davranistir (tum cihazlar
UNKNOWN ile baslar, mevcut comm_lost anlamlari gecerlidir) — yani hata
guvenli tarafa duser, gateway ACILMAMAZLIK etmez.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Dosya bicimi surumu. Uyusmayan surum YOK SAYILIR (yukseltme/geri alma
#: sirasinda yarim yamalak bir kayitla karar vermektense hic karar vermemek
#: dogrudur).
STORE_VERSION = 1

#: Diske yazim hiz siniri. `read_device` her cihaz icin her cycle cagriliyor;
#: 600 cihazli bir sahada her degisiklikte fsync'siz de olsa dosya yazmak
#: gereksiz I/O olurdu. Degisiklik BIRIKTIRILIR, bu aralikta bir kez yazilir.
MIN_WRITE_INTERVAL_SEC = 5.0

#: Kayit tutulan cihaz sayisi ust siniri (dosya sisirmeye karsi). Saha hedefi
#: 600; 5000 fazlasiyla comert ve bozuk/kompromize bir config'in diski
#: doldurmasini engeller.
MAX_RECORDS = 5000


#: Diskte kabul edilen durum tokenleri. Tanimsiz bir token SESSIZCE
#: kabul edilirse asagidaki tum karsilastirmalar yanlis dala girerdi.
_STATES: frozenset[str] = frozenset({"lost", "recovering", "online", "smart_idle"})


@dataclass(frozen=True)
class SessionStateRecord:
    """Tek bir outstation'in kalici oturum ozeti."""

    #: `lost` | `recovering` | `online` | `smart_idle`
    state: str = "lost"
    #: Cihazdan SON GECERLI DNP3 kaniti alinan an (duvar saati, epoch saniye).
    #: Sessizlik denetimi restart'tan sonra BU degerden devam eder.
    last_valid_contact_unix: float | None = None
    #: `smart_idle`e girildigi an (duvar saati). None = idle degil.
    smart_idle_since_unix: float | None = None


def _temiz_kayit(ham: Any) -> SessionStateRecord | None:
    """Diskten gelen bir satiri DOGRULAYARAK kayda cevirir; bozuksa None.

    Dogrulama sart: bu dosya diskte duran, disaridan degistirilebilen bir
    metin. Tanimsiz bir mod tokeni ("smrt") kabul edilirse asagidaki tum
    karsilastirmalar sessizce yanlis dala girerdi.
    """
    if not isinstance(ham, dict):
        return None
    durum = str(ham.get("state") or "lost").strip().lower()
    if durum not in _STATES:
        durum = "lost"

    def _zaman(alan: str) -> float | None:
        deger = ham.get(alan)
        if deger is None:
            return None
        try:
            f = float(deger)
        except (TypeError, ValueError):
            return None
        # Gelecege ya da 2000 oncesine damgali kayitlar bozuktur; kullanilirsa
        # uyku son kullanma hesabi anlamsiz olurdu.
        if f <= 946_684_800.0 or f > time.time() + 86_400.0:
            return None
        return f

    return SessionStateRecord(
        state=durum,
        last_valid_contact_unix=_zaman("last_valid_contact_unix"),
        smart_idle_since_unix=_zaman("smart_idle_since_unix"),
    )


class SessionStateStore:
    """Cihaz basina mod/durum kayitlarinin thread-safe, atomik kalici deposu.

    `path=None` verilirse tamamen bellek-ici calisir (testler ve kalicilik
    istemeyen kurulumlar); API ayni kalir, disk'e dokunulmaz.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path: Path | None = Path(path) if path else None
        self._lock = threading.Lock()
        self._kayitlar: dict[str, SessionStateRecord] = {}
        self._kirli = False
        self._son_yazim = 0.0
        self._yazim_hatasi_uyarildi = False

    # ---- okuma ----------------------------------------------------------

    def load(self) -> int:
        """Diskteki kayitlari yukler. Donen: yuklenen cihaz sayisi.

        Hicbir kosulda ISTISNA ATMAZ — bu dosya yuzunden gateway acilmamazlik
        edemez.
        """
        if self._path is None or not self._path.exists():
            return 0
        try:
            ham = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "session_state_store_load_failed path=%s error=%s — kayitlar YOK "
                "sayildi; cihazlar `lost` ile baslar (mevcut davranis)",
                self._path,
                exc,
            )
            return 0
        if not isinstance(ham, dict):
            return 0
        try:
            surum = int(ham.get("version") or 0)
        except (TypeError, ValueError):
            surum = 0
        if surum != STORE_VERSION:
            logger.warning(
                "session_state_store_version_mismatch path=%s dosya=%s beklenen=%s — "
                "kayitlar YOK sayildi (guvenli taraf)",
                self._path,
                surum,
                STORE_VERSION,
            )
            return 0
        cihazlar = ham.get("devices")
        if not isinstance(cihazlar, dict):
            return 0

        yuklenen: dict[str, SessionStateRecord] = {}
        for kod, satir in list(cihazlar.items())[:MAX_RECORDS]:
            kayit = _temiz_kayit(satir)
            if kayit is not None:
                yuklenen[str(kod)] = kayit
        with self._lock:
            self._kayitlar = yuklenen
        if yuklenen:
            logger.info(
                "session_state_store_loaded path=%s devices=%d smart_idle=%d",
                self._path,
                len(yuklenen),
                sum(1 for k in yuklenen.values() if k.state == "smart_idle"),
            )
        return len(yuklenen)

    def get(self, device_code: str) -> SessionStateRecord | None:
        with self._lock:
            return self._kayitlar.get(device_code)

    def snapshot(self) -> dict[str, SessionStateRecord]:
        with self._lock:
            return dict(self._kayitlar)

    # ---- yazma ----------------------------------------------------------

    def record(self, device_code: str, kayit: SessionStateRecord) -> None:
        """Kaydi guncelle. DEGISMEDIYSE hicbir sey yapilmaz (I/O tetiklenmez)."""
        with self._lock:
            if self._kayitlar.get(device_code) == kayit:
                return
            if device_code not in self._kayitlar and len(self._kayitlar) >= MAX_RECORDS:
                return
            self._kayitlar[device_code] = kayit
            self._kirli = True

    def forget(self, active_device_codes: set[str]) -> int:
        """Config'ten cikarilmis cihazlarin kayitlarini duser."""
        with self._lock:
            silinecek = [k for k in self._kayitlar if k not in active_device_codes]
            for k in silinecek:
                self._kayitlar.pop(k, None)
            if silinecek:
                self._kirli = True
            return len(silinecek)

    def flush(self, *, force: bool = False) -> bool:
        """Degisiklik varsa diske yazar. Donen: yazildi mi.

        Hiz sinirlidir (`MIN_WRITE_INTERVAL_SEC`); `force=True` kapanista
        siniri atlar — surec olurken son durum kaybolmamali.

        DAYANIKLILIK: yazim BASARISIZ olursa `_kirli` GERI ALINIR.
        Aksi halde tek bir gecici disk hatasi (dolu disk, kilitli dosya,
        anlik izin sorunu) kaydi KALICI olarak dusururdu: bayrak temizlenmis
        oldugu icin bir daha hic yazilmaz ve restart'ta uyuyan filo sahte
        comm_lost uretirdi — yani tam da bu dosyanin onlemek icin var oldugu
        sey.

        `_son_yazim` BILEREK geri alinmaz: kalici bir disk hatasinda her
        `read_device` cagrisi yeni bir yazim denemesi tetiklerdi. Hiz siniri
        korunarak yeniden deneme ~5 saniyede bire indirilir; kapanistaki
        `force=True` yolu bu sinirdan etkilenmez.
        """
        if self._path is None:
            return False
        now = time.monotonic()
        with self._lock:
            if not self._kirli:
                return False
            if not force and (now - self._son_yazim) < MIN_WRITE_INTERVAL_SEC:
                return False
            govde = {
                "version": STORE_VERSION,
                "written_at_unix": time.time(),
                "devices": {kod: asdict(k) for kod, k in self._kayitlar.items()},
            }
            self._kirli = False
            self._son_yazim = now

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".session-state-", suffix=".json.tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(govde, f, ensure_ascii=False)
                os.replace(tmp, self._path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except Exception as exc:  # noqa: BLE001
            # KAYIT KAYBOLMASIN: bayragi geri ac ki bir sonraki flush yeniden
            # denesin. `record()` bu arada yeni bir degisiklik yazdiysa zaten
            # True'dur; True'yu tekrar True yapmak zararsizdir (yaris yok).
            with self._lock:
                self._kirli = True
            # Diske yazamamak telemetriyi DURDURMAZ — istisna DISARI SIZMAZ;
            # yalnizca restart sonrasi uyku bilgisi kaybolabilir. Tek satir
            # uyari yeter (her 5 sn'de bir loglamak asil sorunu gizlerdi).
            if not self._yazim_hatasi_uyarildi:
                self._yazim_hatasi_uyarildi = True
                logger.warning(
                    "session_state_store_persist_failed path=%s error=%s — restart "
                    "sonrasi smart_idle durumu geri yuklenemeyebilir; yazim yeniden "
                    "denenecek",
                    self._path,
                    exc,
                )
            return False
