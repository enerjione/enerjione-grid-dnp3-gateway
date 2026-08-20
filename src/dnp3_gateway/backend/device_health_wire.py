"""Cihaz basina calisma-zamani sagligi — TEL SOZLESMESI (`device_health_v1`).

NEDEN AYRI BIR KANAL — `X-E1-Gateway-Health` YETMEZ
---------------------------------------------------
Mevcut toplu saglik basligi `/pending` isteklerine binen bir BASLIKTIR ve
backend'in ayristirma tavani ~2 KB'dir (`health_header.MAX_HEADER_BYTES`
guvenli tarafta 1600). 200+ cihazin cihaz-bazli calisma-zamani durumu oraya
SIGMAZ; sigdirmaya calismak iki yoldan da zarar verir:

  * baslik buyurse KOMUT KANALI tehlikeye girer — `/pending` fiziksel kesici
    komutlarinin tasiyicisidir ve bir proxy/backend baslik limiti yuzunden
    400 donerse komutlar durur;
  * kirpilirsa operator "geri kalan her sey iyi" yanilgisina duser.

Bu yuzden cihaz sagligi AYRI, GOVDE tabanli ve KOMUTTAN BAGIMSIZ bir kanala
tasinir. Toplu baslik oldugu gibi KALIR (geriye uyumlu) ve buyutulmez.

SIRALAMA — NEDEN DUVAR SAATI DEGIL
----------------------------------
Yeniden denenen bayat bir gonderim, backend'deki DAHA YENI durumu ezmemeli.
Siralama `(boot_id, sequence)` ikilisiyle yapilir:

  * `sequence`: proses icinde MONOTONIK artan sayac.
  * `boot_id`: her proses BASLANGICINDA artan, DISKTE tutulan sayac.

`gateway_instance_id` bu is icin TEK BASINA YETMEZ: o kimlik disk uzerinde
KALICIDIR (`auth/identity.resolve_instance_id`) ve restart'ta AYNI kalir —
yani restart sonrasi sifirlanan bir `sequence` ile birlestiginde backend iki
farkli calismayi ayirt EDEMEZ.

Duvar saati siralama icin KULLANILMAZ: sahada RTC'si bos acilan gateway'ler
ve NTP siçramalari gercektir; saate bagli bir siralama tam da o anlarda
tersine doner. `boot_id` saatten BAGIMSIZDIR.

SOZLESME KAPSAMI
----------------
Yalnizca gateway'in calisma zamaninda GERCEKTEN sahip oldugu alanlar
tasinir. Bilinmeyen deger UYDURULMAZ; alan `null` gider ya da `unknown`
tokeni tasir.
"""

from __future__ import annotations

from typing import Any

#: Tel semasi kimligi. Alan EKLEMEK geriye uyumludur (backend tanimadigini
#: yok sayar); alan KALDIRMAK ya da anlam degistirmek YENI bir surum ister.
SCHEMA_VERSION = "device_health_v1"

#: Backend'in bekledigi baglanti durumlari. `device_health()` bunlarin
#: disinda bir token uretirse `unknown`a duseriz — UYDURMA YOK.
CONNECTION_STATES: frozenset[str] = frozenset(
    {"online", "smart_idle", "recovering", "lost", "listener_error", "unknown"}
)

#: `session_policy` degerleri (yapilandirilan).
CONFIGURED_POLICIES: frozenset[str] = frozenset({"continuous", "smart", "auto"})
#: ETKIN politika: `auto` cozulene kadar `unknown` olabilir.
EFFECTIVE_POLICIES: frozenset[str] = frozenset({"continuous", "smart", "unknown"})

#: Operation Mode tokenleri. 1 = Smart, 0 = Boost (bkz. operation_mode.py —
#: dokumandaki 0x01/0x81 DEGER DEGIL bayrak oktetidir).
OPERATION_MODES: frozenset[str] = frozenset({"smart", "boost", "unknown"})

#: ICMP sonda sonuclari.
IP_PROBE_STATES: frozenset[str] = frozenset({"reachable", "unreachable", "unsupported", "unknown"})
#: TCP durumu — yadnp3 KANAL durumundan turetilir, ham soket ACILMAZ.
TCP_PROBE_STATES: frozenset[str] = frozenset({"open", "connecting", "unknown"})


def _token(deger: Any, izinli: frozenset[str], varsayilan: str = "unknown") -> str:
    """Tokeni sozlukle SINIRLA. Tanimadigimizi UYDURMAYIZ."""
    s = str(deger or "").strip().lower()
    return s if s in izinli else varsayilan


def _epoch(deger: Any) -> float | None:
    """Duvar saati damgasi; yoksa/bozuksa `None`.

    `0` ve negatif degerler `None`a duser: "hic olmadi" ile "1970'te oldu"
    ayrimini backend'e tasimak, panelde 1970 tarihleri gostermek demektir.
    """
    if deger is None:
        return None
    try:
        f = float(deger)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _sayi(deger: Any) -> float | None:
    if deger is None:
        return None
    try:
        f = float(deger)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def build_device_record(device_code: str, saglik: dict[str, Any]) -> dict[str, Any]:
    """`device_health()` ciktisindan TEL KAYDI uretir.

    SEMANTIK KORUNUR (v1.14 ile birebir):
      * `smart_idle` != offline — SAGLIKLI uyku durumudur.
      * `report_late` != lost — DEGRADED uyaridir, `connection_state`
        `smart_idle` KALIR.
      * Sonda sonuclari `connection_state`i BELIRLEMEZ; salt teshistir.

    Sonda alanlarinin kayitta bulunmasi onlari saglik olcutu YAPMAZ; backend
    sozlesmesi (`probes_affect_connection_state: false`) bunu acikca soyler.
    """
    return {
        "device_code": device_code,
        # --- Baglanti durumu (TEK yetkili alan) ---
        "connection_state": _token(saglik.get("state"), CONNECTION_STATES),
        "connected": bool(saglik.get("connected")),
        "reachable": bool(saglik.get("reachable")),
        # --- Oturum politikasi / mod ---
        "configured_session_policy": _token(
            saglik.get("configured_session_policy"), CONFIGURED_POLICIES, "continuous"
        ),
        "effective_session_policy": _token(saglik.get("effective_session_policy"), EFFECTIVE_POLICIES),
        "operation_mode": _token(saglik.get("operation_mode"), OPERATION_MODES),
        # --- Dial-In beklentisi (late != lost) ---
        "dial_in_interval_min": saglik.get("dial_in_interval_min"),
        "next_expected_report_epoch": _epoch(saglik.get("next_expected_report_epoch")),
        "report_overdue_sec": _sayi(saglik.get("report_overdue_sec")),
        "report_late": bool(saglik.get("report_late")),
        # --- DNP3 kaniti (zaten yetkili) ---
        "last_valid_contact_epoch": _epoch(saglik.get("last_valid_contact_epoch")),
        "last_frame_epoch": _epoch(saglik.get("last_frame_epoch")),
        # --- SALT TESHIS: durum kararina GIRMEZ ---
        "ip_probe_status": _token(saglik.get("ip_probe_status"), IP_PROBE_STATES),
        "tcp_probe_status": _token(saglik.get("tcp_probe_status"), TCP_PROBE_STATES),
        "last_probe_epoch": _epoch(saglik.get("last_probe_epoch")),
        # --- Uc tipi (teshis; politikadan BAGIMSIZ) ---
        "ip_endpoint_type": _token(
            saglik.get("ip_endpoint_type"), frozenset({"listening", "initiating"}), "listening"
        ),
    }


def build_envelope(
    *,
    gateway_code: str,
    gateway_instance_id: str,
    boot_id: int,
    sequence: int,
    snapshot: bool,
    devices: list[dict[str, Any]],
    device_total: int,
    snapshot_id: str | None = None,
    snapshot_batch_index: int | None = None,
    snapshot_batch_count: int | None = None,
) -> dict[str, Any]:
    """Gonderilecek govde.

    `snapshot=True`: bu parti TAM durumun bir PARCASIDIR (uzlastirma).
    `snapshot=False`: yalnizca DEGISEN cihazlar (delta).

    COK PARCALI SNAPSHOT KORELASYONU — `device_total` TEK BASINA YETMEZ
    ------------------------------------------------------------------
    Bir snapshot birden fazla partiye bolunur. Kismi bir basarisizliktan
    sonra backend'in "bu parti HANGI snapshot'a ait" sorusunu
    cevaplayabilmesi gerekir; `device_total` bunu SOYLEMEZ:

        1. parti (200 cihaz, 1/4) -> BASARILI
        2. parti                   -> BASARISIZ
        (cihaz seti degisir; toplam YINE 200)
        yeniden deneme            -> yeni partiler, `device_total` HALA 200

    `device_total` degismedigi icin backend eski yarim snapshot ile yeniyi
    AYIRT EDEMEZ ve ikisini birlestirip TUTARSIZ bir tablo kurar — ya da
    daha kotusu, "eksik kalanlari sil" mantigi calisirsa var olan cihazlari
    siler.

    Bu yuzden her TAM snapshot kendi `snapshot_id`sini tasir:

      * ayni snapshot'in TUM partileri AYNI `snapshot_id`yi paylasir,
      * YENI bir tam snapshot YENI bir `snapshot_id` alir,
      * `snapshot_batch_index` (0 tabanli) ve `snapshot_batch_count` ile
        backend partinin tam gelip gelmedigini bilir.

    Backend boylece guvenle:
      * tek bir snapshot'i BIRLESTIREBILIR,
      * yeni bir snapshot basladiginda TAMAMLANMAMIS eskisini ATABILIR,
      * silinen cihazlari YALNIZCA snapshot TAMAMLANDIKTAN sonra uzlastirir.

    Delta partilerinde bu uc alan `null`dur.

    `(boot_id, sequence)` ikilisi ISTEK BASINA bayat siralama icin AYNEN
    kalir; snapshot korelasyonu ONUN YERINE GECMEZ, uzerine eklenir.
    """
    return {
        "schema": SCHEMA_VERSION,
        "gateway_code": gateway_code,
        "gateway_instance_id": gateway_instance_id,
        # SIRALAMA IKILISI — backend `(boot_id, sequence)` ile karsilastirir.
        # Duvar saati KULLANILMAZ (bkz. modul basligi).
        "boot_id": int(boot_id),
        "sequence": int(sequence),
        "snapshot": bool(snapshot),
        # COK PARCALI SNAPSHOT KORELASYONU (delta'da null).
        "snapshot_id": snapshot_id,
        "snapshot_batch_index": snapshot_batch_index,
        "snapshot_batch_count": snapshot_batch_count,
        "device_total": int(device_total),
        "devices": devices,
    }
