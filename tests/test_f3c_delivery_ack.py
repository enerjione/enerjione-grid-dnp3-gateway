"""F3C — komut teslim kirasi ve DAYANIKLI kabul (gateway yarisi).

KAPATILAN ARIZA
---------------
Backend `/pending` yanitini uretirken komutu `sent` isaretliyordu. Iki pencere
acikti:

  A) backend `sent` COMMIT etti, HTTP yaniti gateway'e ULASMADI
  B) gateway yaniti okudu, DAYANIKLI deftere yazmadan oldu

Her ikisinde de backend `sent`, gateway defteri bos, cihaz komutu hic almadi ve
komut sonsuza kadar `sent` kaliyordu. Kayip SESSIZDI.

Artik `sent` = "gateway komutu DAYANIKLI olarak kabul etti (ACK)".

BU DOSYANIN KILITLEDIGI EN ONEMLI SEY
-------------------------------------
Teslimin yeniden denenebilir olmasi, FIZIKSEL KOMUTUN yeniden denenmesi
DEGILDIR:

    Backend -> Gateway TESLIM     : yeniden denenebilir
    Gateway -> Cihaz   CALISTIRMA : ASLA otomatik yeniden denenmez
    Gateway -> Backend SONUC      : yeniden denenebilir

Ayni `command_id` icin `operate_device` cagrisi EN FAZLA 1 olmalidir; bu
dosyadaki her senaryo o sayaci olcer.

Gercek proses/SIGKILL senaryolari `test_f3c_crash_recovery.py` icinde.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dnp3_gateway.backend import health_header
from dnp3_gateway.backend.config_client import PendingCommand, PendingPoll
from dnp3_gateway.command_freshness import (
    FreshnessReason,
    validate_command_freshness,
)
from dnp3_gateway.main import _deliver_delivery_acks, _execute_pending_commands
from dnp3_gateway.messaging.command_ledger import CommandLedger
from dnp3_gateway.state import GatewayState

from .conftest import make_device, make_gateway_config, make_signal

TTL = 120.0
JETON = "ornek-teslim-jetonu-opak"


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------


def _iso(saniye_once: float, *, taban: datetime | None = None) -> str:
    an = (taban or datetime.now(timezone.utc)) - timedelta(seconds=saniye_once)
    return an.isoformat()


class _SayanReader:
    """Fiziksel komut sayaci — gercek DNP3 yerine.

    `operate_device` cagrilarini SAYAR. F3C'nin tum iddiasi bu sayaca
    dayanir: ayni `command_id` icin EN FAZLA 1.
    """

    def __init__(self) -> None:
        self.cagrilar: list[dict[str, Any]] = []

    def operate_device(self, **kw: Any) -> dict[str, Any]:
        self.cagrilar.append(dict(kw))
        return {"ok": True, "status": "ok"}

    @property
    def sayi(self) -> int:
        return len(self.cagrilar)


def _state() -> GatewayState:
    st = GatewayState()
    st.update(
        make_gateway_config(
            devices=[make_device(code="DEV-1", ip_address="10.0.0.5", dnp3_address=1)],
            signals=[
                make_signal(
                    key="master.fault_reset",
                    data_type="binary_output",
                    object_group=10,
                    index=3,
                )
            ],
        )
    )
    return st


#: `created_at=None` GERCEKTEN "damga yok" demek olsun diye ayri nobetci.
#: Varsayilani `None` yapip "verilmediyse taze damga uret" demek, damgasiz
#: komutu test etmeyi IMKANSIZ kilardi (bu tuzaga bir kez dusuldu).
_VARSAYILAN = object()


def _komut(
    komut_id: int = 1,
    *,
    created_at: Any = _VARSAYILAN,
    delivery_token: str | None = JETON,
    delivery_not_after: str | None = None,
) -> PendingCommand:
    return PendingCommand(
        id=komut_id,
        device_code="DEV-1",
        command="fault_reset",
        dnp3_index=3,
        created_at=_iso(1) if created_at is _VARSAYILAN else created_at,
        delivery_token=delivery_token,
        delivery_not_after=delivery_not_after,
    )


@pytest.fixture()
def ledger(tmp_path: Path):
    led = CommandLedger(tmp_path / "command_ledger_GW-001.db")
    try:
        yield led
    finally:
        led.close()


class _SahteIstemci:
    """ACK/sonuc uclarini taklit eder; gonderilenleri kaydeder."""

    gateway_code = "GW-001"

    def __init__(self, *, ack_hatasi: Exception | None = None) -> None:
        self.ack_partileri: list[list[dict]] = []
        self.ack_hatasi = ack_hatasi

    def report_delivery_acks(self, acks: list[dict]) -> set[int]:
        if self.ack_hatasi is not None:
            raise self.ack_hatasi
        self.ack_partileri.append([dict(a) for a in acks])
        return {int(a["command_id"]) for a in acks}

    def report_command_results(self, results: list[dict]) -> None:
        return None


# ===========================================================================
# G01 / G02 — yetenek ve defter kimligi
# ===========================================================================


def test_g01_yetenek_acikca_bildirilir() -> None:
    """Yetenek ACIK olmali; backend surum ayristirmasi YAPMAZ.

    "1.8.0 ise destekliyordur" gibi ortuk bir cikarim, yetenegi olmayan bir
    yapiyi destekliyor sanmaya yol acardi.
    """
    assert health_header.CAPABILITY_COMMAND_DELIVERY_ACK_V1 == "command_delivery_ack_v1"
    assert health_header.DELIVERY_HEADER_NAME == "X-E1-Delivery"

    govde = health_header.build_payload(
        status="ok", device_summary=None, per_device=None, ledger_epoch="epoch-1"
    )
    assert govde["capabilities"] == ["command_delivery_ack_v1"]
    assert govde["ledger_epoch"] == "epoch-1"


def test_g01b_epoch_yoksa_yetenek_bildirilmez() -> None:
    """Defter kimligi uretilemediyse yetenek IDDIA EDILMEZ (fail-closed)."""
    govde = health_header.build_payload(status="ok", device_summary=None, per_device=None)
    assert "capabilities" not in govde
    assert "ledger_epoch" not in govde


def test_g01c_teslim_basligi_backend_sozlesmesine_uyar() -> None:
    """Backend `{"v":int,"epoch":str}` bekliyor (30ed9a5)."""
    kodlu = health_header.encode_delivery_header("epoch-abc")
    assert kodlu is not None
    pad = "=" * (-len(kodlu) % 4)
    govde = json.loads(base64.urlsafe_b64decode(kodlu + pad).decode())
    assert govde == {"v": 1, "epoch": "epoch-abc"}
    # Baslik saniyede bir gidiyor; kucuk kalmali.
    assert len(kodlu) < 120


def test_g01d_bos_epoch_baslik_uretmez() -> None:
    assert health_header.encode_delivery_header("") is None
    assert health_header.encode_delivery_header("   ") is None


def test_g02_defter_kimligi_uretilir(ledger) -> None:
    assert ledger.epoch
    assert len(ledger.epoch) >= 32, "UUID bekleniyor"
    assert ledger.status_snapshot()["ledger_epoch"] == ledger.epoch


def test_g03_ayni_dosya_ayni_kimlik(tmp_path: Path) -> None:
    """Proses restart -> AYNI defter dosyasi -> AYNI kimlik.

    (Gercek proses restart'i `test_f3c_crash_recovery.py` icinde.)
    """
    yol = tmp_path / "l.db"
    a = CommandLedger(yol)
    ilk = a.epoch
    a.close()

    b = CommandLedger(yol)
    try:
        assert b.epoch == ilk, "ayni dosya farkli kimlik uretti"
    finally:
        b.close()


def test_g05_defter_silinirse_kimlik_degisir(tmp_path: Path) -> None:
    """Defter kaybi = tekrar-onleme kanitinin kaybi. Kimlik DEGISMELI.

    Backend bu farki gorup komutu otomatik teslim ETMEYECEK; aksi halde bos
    defterli gateway komutu YENI sanip CROB'u TEKRARLARDI.
    """
    yol = tmp_path / "l.db"
    a = CommandLedger(yol)
    ilk = a.epoch
    a.close()

    for ek in ("", "-wal", "-shm"):
        p = Path(str(yol) + ek)
        if p.exists():
            p.unlink()

    b = CommandLedger(yol)
    try:
        assert b.epoch != ilk, "defter silindi ama kimlik AYNI kaldi"
    finally:
        b.close()


@pytest.fixture()
def karantina_temiz():
    """Karantina KAYDI global; test sonrasi GERI ALINIR.

    `sqlite_support._QUARANTINED` proses omru boyunca birikiyor ve `/health`
    onu okuyup durumu `degraded` yapiyor. Temizlenmezse bu dosyadan SONRA
    kosan saglik testleri, kendi kurgularinda hicbir sey bozuk olmamasina
    ragmen `degraded` gorurdu — testler arasi sizinti. (Mevcut karantina
    testleri `test_s...` ile alfabetik olarak saglik testlerinden SONRA
    kostugu icin bu sizinti bugune kadar gorunmemisti.)
    """
    from dnp3_gateway.messaging import sqlite_support

    onceki = list(sqlite_support._QUARANTINED)
    try:
        yield
    finally:
        sqlite_support._QUARANTINED[:] = onceki


def test_g29_bozuk_defter_karantinasi_kimligi_degistirir(tmp_path: Path, karantina_temiz) -> None:
    """Bozulma -> karantina -> yeni defter -> YENI kimlik."""
    yol = tmp_path / "l.db"
    a = CommandLedger(yol)
    a.start_dispatch(101, JETON)
    ilk = a.epoch
    a.close()

    ham = bytearray(yol.read_bytes())
    for i in range(100, min(len(ham), 4096)):
        ham[i] = 0xFF
    yol.write_bytes(bytes(ham))

    b = CommandLedger(yol)
    try:
        assert b.journal_reset_at is not None
        assert b.epoch != ilk, "defter sifirlandi ama kimlik AYNI kaldi"
    finally:
        b.close()


# ===========================================================================
# G06 / G07 — backend uyumlulugu
# ===========================================================================


def test_g06_backend_v296_payloadu_eski_yolda_calisir(ledger) -> None:
    """Backend 2.96.0 teslim alanlarini GONDERMEZ; gateway crash ETMEZ.

    Jeton yoksa ACK kuyrugu HIC olusmaz ve davranis 1.8.0 ile ayni kalir.
    """
    cmd = _komut(1, delivery_token=None, delivery_not_after=None)
    assert ledger.start_dispatch(cmd.id, cmd.delivery_token) is True
    assert ledger.pending_acks() == [], "eski backend icin ACK kuyruklandi"
    assert ledger.pending_ack_count() == 0

    reader = _SayanReader()
    sonuc = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 1
    assert sonuc[0]["ok"] is True


def test_g06b_eski_payload_ayristirilir() -> None:
    """Teslim alanlari olmayan sozluk hala gecerli bir komut uretir."""
    cmd = PendingCommand(id=7, device_code="DEV-1", command="fault_reset", dnp3_index=3)
    assert cmd.delivery_token is None
    assert cmd.delivery_not_after is None


def test_g07_f3c_payloadu_ayristirilir_ve_ack_kuyruklanir(ledger) -> None:
    son = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    cmd = _komut(2, delivery_token=JETON, delivery_not_after=son)

    assert ledger.start_dispatch(cmd.id, cmd.delivery_token) is True
    bekleyen = ledger.pending_acks()
    assert bekleyen == [{"command_id": 2, "delivery_token": JETON}]


def test_g08_jeton_dayanikli(tmp_path: Path) -> None:
    """Jeton defterde; proses yeniden acildiginda KAYBOLMAZ."""
    yol = tmp_path / "l.db"
    a = CommandLedger(yol)
    a.start_dispatch(5, JETON)
    a.close()

    b = CommandLedger(yol)
    try:
        assert b.pending_acks() == [{"command_id": 5, "delivery_token": JETON}]
    finally:
        b.close()


# ===========================================================================
# G09 / G10 / G19 — MUTLAK son kullanma ani
# ===========================================================================


def test_g09_son_kullanma_tam_sinirda_kabul() -> None:
    """SINIR: `now <= delivery_not_after` KABUL."""
    now = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    sonuc = validate_command_freshness(
        _iso(1, taban=now),
        now=now,
        max_age_sec=TTL,
        require_timestamp=True,
        delivery_not_after=now.isoformat(),
    )
    assert sonuc.fresh, "tam sinirda reddedildi"


def test_g10_son_kullanma_gecmisse_reddedilir() -> None:
    now = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    sonuc = validate_command_freshness(
        _iso(1, taban=now),
        now=now,
        max_age_sec=TTL,
        require_timestamp=True,
        delivery_not_after=(now - timedelta(microseconds=1)).isoformat(),
    )
    assert sonuc.reason is FreshnessReason.DELIVERY_DEADLINE_PASSED


def test_g19_kira_ttl_yi_uzatamaz() -> None:
    """SPEC §10 — bu testin gecmemesi F3C'nin basarisizligidir.

        COMMAND_MAX_AGE_SEC = 120
        10:00:00 komut olusturuldu
        10:00:05 ilk kira
                 HTTP yaniti KAYBOLDU
        10:03:00 gateway poll — komut ILK KEZ deftere girecek

    Beklenen: FIZIKSEL EXECUTE = 0.

    Iki bagimsiz savunma da bunu yakalamali: yerel TTL (yas 180s > 120s) ve
    backend'in bildirdigi mutlak son kullanma ani.
    """
    olusum = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    poll_ani = olusum + timedelta(seconds=180)
    son_kullanma = olusum + timedelta(seconds=TTL)

    sonuc = validate_command_freshness(
        olusum.isoformat(),
        now=poll_ani,
        max_age_sec=TTL,
        require_timestamp=True,
        delivery_not_after=son_kullanma.isoformat(),
    )
    assert not sonuc.fresh, "3 dakikalik komut TAZE sayildi"

    # Fiziksel katmanda da kanitla: CROB SAYISI 0.
    reader = _SayanReader()
    cmd = _komut(
        9,
        created_at=olusum.isoformat(),
        delivery_not_after=son_kullanma.isoformat(),
    )
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 0, "BAYAT KOMUT FIZIKSEL OLARAK CALISTIRILDI"
    assert sonuclar[0]["ok"] is False


def test_g19b_yerel_ttl_genis_olsa_bile_backend_siniri_uygulanir() -> None:
    """SAVUNMA DERINLIGI: `max_age_sec` gateway'de genis ayarlanmis olsa bile
    backend'in DEGISMEZ son kullanma ani komutu keser."""
    olusum = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    poll_ani = olusum + timedelta(seconds=300)

    reader = _SayanReader()
    cmd = _komut(
        10,
        created_at=olusum.isoformat(),
        delivery_not_after=(olusum + timedelta(seconds=120)).isoformat(),
    )
    # Gateway TTL'si 1 saat: yerel kontrol komutu TAZE sayardi.
    import dnp3_gateway.main as m

    gercek = m.datetime

    class _Donmus(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return poll_ani if tz is None else poll_ani.astimezone(tz)

    m.datetime = _Donmus
    try:
        _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=3600.0)
    finally:
        m.datetime = gercek
    assert reader.sayi == 0, "backend son kullanma ani UYGULANMADI"


def test_g07b_bozuk_teslim_ustverisi_fail_closed() -> None:
    """Ustverisini ANLAMADIGIMIZ komut cihaza GONDERILMEZ; sessizce de dusmez."""
    reader = _SayanReader()
    cmd = _komut(11, delivery_not_after="bu-bir-tarih-degil")
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 0
    assert len(sonuclar) == 1, "red TERMINAL bir sonuc uretmeli (sessiz drop YOK)"
    assert sonuclar[0]["status"] == FreshnessReason.DELIVERY_METADATA_INVALID.value


def test_g07c_timezone_suz_son_kullanma_reddedilir() -> None:
    now = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    sonuc = validate_command_freshness(
        _iso(1, taban=now),
        now=now,
        max_age_sec=TTL,
        require_timestamp=True,
        delivery_not_after="2026-08-15T10:05:00",
    )
    assert sonuc.reason is FreshnessReason.DELIVERY_METADATA_INVALID


# ===========================================================================
# G11 / G12 — saat sapmasi
# ===========================================================================


def test_g11_bes_saniye_icindeki_sapma_kabul() -> None:
    now = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    for ileri in (0.067, 1.0, 5.0):
        sonuc = validate_command_freshness(
            (now + timedelta(seconds=ileri)).isoformat(),
            now=now,
            max_age_sec=TTL,
            require_timestamp=True,
            clock_skew_tolerance_sec=5.0,
        )
        assert sonuc.fresh, f"{ileri}s sapma reddedildi"
        assert sonuc.age_sec == 0.0


def test_g12_bes_saniyeyi_asan_sapma_reddedilir() -> None:
    """SAAT GERI ADIMI da buraya duser — fail-closed, bilincli takas."""
    now = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    sonuc = validate_command_freshness(
        (now + timedelta(seconds=5.001)).isoformat(),
        now=now,
        max_age_sec=TTL,
        require_timestamp=True,
        clock_skew_tolerance_sec=5.0,
    )
    assert sonuc.reason is FreshnessReason.TIMESTAMP_FUTURE


def test_g12b_saat_geri_adiminda_fiziksel_komut_yok() -> None:
    """NTP saati geri alirsa komut gelecekte gorunur -> CROB gonderilmez."""
    reader = _SayanReader()
    gelecek = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    cmd = _komut(12, created_at=gelecek)
    sonuclar = _execute_pending_commands(
        reader,
        _state(),
        [cmd],
        gateway_code="GW-001",
        max_age_sec=TTL,
        clock_skew_tolerance_sec=5.0,
    )
    assert reader.sayi == 0
    assert sonuclar[0]["status"] == FreshnessReason.TIMESTAMP_FUTURE.value


def test_g12c_tolerans_ayari_pozitif_olmali() -> None:
    """FAIL-OPEN YOK: 0/negatif deger acilista reddedilir."""
    from pydantic import ValidationError

    from dnp3_gateway.config import Settings

    for deger in (0, -1):
        with pytest.raises(ValidationError):
            Settings(command_clock_skew_tolerance_sec=deger)


def test_g12d_tolerans_varsayilani_5() -> None:
    from dnp3_gateway.config import Settings

    assert Settings().command_clock_skew_tolerance_sec == 5.0


# ===========================================================================
# G13 / G14 / G18 — mukerrer teslim
# ===========================================================================


def test_g13_ayni_komut_ikinci_kez_deftere_girmez(ledger) -> None:
    assert ledger.start_dispatch(20, JETON) is True
    assert ledger.start_dispatch(20, JETON) is False, "tekrar-onleme kirildi"


def test_g14_ayni_komut_farkli_jetonla_gelirse_execute_yok(ledger) -> None:
    """Protokol catismasi: mevcut DAYANIKLI kayda guvenilir.

    Farkli jeton, komutun backend'de yeniden kiralandigi (jeton dondurulmus ya
    da kayit karismis) anlamina gelir. Fiziksel komut TEKRARLANMAZ.
    """
    assert ledger.start_dispatch(21, JETON) is True
    assert ledger.start_dispatch(21, "BASKA-JETON") is False

    kayitli, jeton = ledger.kayitli_jeton(21)
    assert kayitli is True
    assert jeton == JETON, "ilk dayanikli jeton EZILDI"


def test_g18_mukerrer_teslim_yalnizca_ack_uretir(ledger) -> None:
    """Kira yeniden sunumu -> FIZIKSEL EXECUTE 0, ACK yeniden kuyruklanir.

    Bu, F3C'nin en onemli testlerinden biri (spec §19).
    """
    assert ledger.start_dispatch(22, JETON) is True
    ledger.mark_ack_delivered(22)
    assert ledger.pending_acks() == []

    # Backend komutu YENIDEN sundu -> ACK'in ulasmadigi anlasilir.
    assert ledger.ack_yeniden_kuyrukla(22) is True
    assert ledger.pending_acks() == [{"command_id": 22, "delivery_token": JETON}]

    # Ve komut yine deftere GIRMEZ (fiziksel tekrar yok).
    assert ledger.start_dispatch(22, JETON) is False


def test_g18b_jetonsuz_kayit_icin_ack_kuyruklanmaz(ledger) -> None:
    """Eski backend komutu icin ACK uretmek anlamsiz olurdu."""
    ledger.start_dispatch(23, None)
    assert ledger.ack_yeniden_kuyrukla(23) is False
    assert ledger.pending_acks() == []


# ===========================================================================
# G15 / G16 / G17 — dayanikli ACK ve yeniden deneme
# ===========================================================================


def test_g15_ack_ledger_commit_inden_sonra_dogar(ledger) -> None:
    """ACK ancak dayanikli yazim tamamlaninca uretilebilir."""
    assert ledger.pending_acks() == []
    ledger.start_dispatch(30, JETON)
    assert ledger.pending_acks() == [{"command_id": 30, "delivery_token": JETON}]


def test_g16_ack_teslim_edilemezse_kuyrukta_kalir(ledger) -> None:
    from dnp3_gateway.backend.config_client import CommandResultDeliveryError

    ledger.start_dispatch(31, JETON)
    istemci = _SahteIstemci(ack_hatasi=CommandResultDeliveryError("backend down", http_status=503))

    for _ in range(3):
        _deliver_delivery_acks(istemci, ledger)

    assert ledger.pending_ack_count() == 1, "ACK kaybedildi"


def test_g16b_kalici_hatada_da_ack_kaybolmaz(ledger) -> None:
    """DEAD-LETTER YOK.

    ACK'ten vazgecmek, backend'in komutu hicbir zaman `sent` gormemesi
    demektir. Bedeli sessiz degil GORUNUR bir teslim basarisizligidir
    (backend kira/deneme tukenince `delivery_failed` yazar), bu yuzden
    sonsuz yeniden deneme dogru davranistir.
    """
    from dnp3_gateway.backend.config_client import CommandResultDeliveryError

    ledger.start_dispatch(32, JETON)
    istemci = _SahteIstemci(ack_hatasi=CommandResultDeliveryError("rejected", http_status=422))

    for _ in range(10):
        _deliver_delivery_acks(istemci, ledger)

    assert ledger.pending_ack_count() == 1, "kalici hatada ACK DUSURULDU"


def test_g16c_basarili_teslim_ack_i_kapatir(ledger) -> None:
    ledger.start_dispatch(33, JETON)
    istemci = _SahteIstemci()
    _deliver_delivery_acks(istemci, ledger)

    assert ledger.pending_ack_count() == 0
    assert istemci.ack_partileri == [[{"command_id": 33, "delivery_token": JETON}]]


def test_g17_ack_proses_yeniden_acilinca_devam_eder(tmp_path: Path) -> None:
    yol = tmp_path / "l.db"
    a = CommandLedger(yol)
    a.start_dispatch(34, JETON)
    a.close()  # "crash"

    b = CommandLedger(yol)
    try:
        assert b.pending_acks() == [{"command_id": 34, "delivery_token": JETON}]
        istemci = _SahteIstemci()
        _deliver_delivery_acks(istemci, b)
        assert b.pending_ack_count() == 0
    finally:
        b.close()


def test_ack_kuyrugu_sonuc_kuyrugundan_ayri(ledger) -> None:
    """Iki ayri dayanikli yasam dongusu — karistirilmamali.

    ACK "kabul ettim" der ve komut cihazda calismadan ONCE dogar; sonuc "cihaz
    sunu yapti" der. Biri teslim edilmisken digeri kuyrukta olabilir.
    """
    ledger.start_dispatch(35, JETON)
    assert ledger.pending_ack_count() == 1
    assert ledger.pending_result_count() == 0

    ledger.record_result({"id": 35, "ok": True, "status": "ok"})
    assert ledger.pending_ack_count() == 1, "sonuc kaydi ACK'i etkiledi"
    assert ledger.pending_result_count() == 1

    ledger.mark_ack_delivered(35)
    assert ledger.pending_ack_count() == 0
    assert ledger.pending_result_count() == 1, "ACK teslimi SONUCU dusurdu"


def test_jeton_loglanmaz(ledger, caplog) -> None:
    """Jeton opak bir yetki kanitidir; log'a DUSMEMELI."""
    import logging

    gizli = "COK-GIZLI-JETON-9f3a"
    with caplog.at_level(logging.DEBUG):
        ledger.start_dispatch(40, gizli)
        _deliver_delivery_acks(_SahteIstemci(), ledger)
    assert gizli not in caplog.text


# ===========================================================================
# G20-G24 — mevcut guvenlikler BOZULMADI
# ===========================================================================


def test_g22_damgasiz_komut_hala_fail_closed() -> None:
    reader = _SayanReader()
    cmd = _komut(50, created_at=None)
    sonuclar = _execute_pending_commands(
        reader,
        _state(),
        [cmd],
        gateway_code="GW-001",
        max_age_sec=TTL,
        require_timestamp=True,
    )
    assert reader.sayi == 0
    assert sonuclar[0]["status"] == FreshnessReason.TIMESTAMP_MISSING.value


def test_g23_bozuk_damga_hala_fail_closed() -> None:
    reader = _SayanReader()
    cmd = _komut(51, created_at="tarih-degil")
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 0
    assert sonuclar[0]["status"] == FreshnessReason.TIMESTAMP_INVALID.value


def test_g24_timezone_suz_damga_hala_fail_closed() -> None:
    reader = _SayanReader()
    cmd = _komut(52, created_at="2026-08-15T10:00:00")
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 0
    assert sonuclar[0]["status"] == FreshnessReason.TIMESTAMP_INVALID.value


def test_g20_f1_yetkilendirme_hala_uygulanir() -> None:
    """Katalogda OLMAYAN indeks -> CROB YOK (F1 regresyonu)."""
    reader = _SayanReader()
    cmd = PendingCommand(
        id=53,
        device_code="DEV-1",
        command="fault_reset",
        dnp3_index=99,  # katalogda yok
        created_at=_iso(1),
        delivery_token=JETON,
    )
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 0, "yetkisiz indekse CROB gonderildi"
    assert sonuclar[0]["ok"] is False


def test_g21_f2_intent_dogrulamasi_hala_uygulanir() -> None:
    """Komut adi ile cozulen nokta uyusmazsa CROB YOK (F2 regresyonu)."""
    reader = _SayanReader()
    cmd = PendingCommand(
        id=54,
        device_code="DEV-1",
        command="bilinmeyen_komut",
        dnp3_index=3,
        created_at=_iso(1),
        delivery_token=JETON,
    )
    sonuclar = _execute_pending_commands(reader, _state(), [cmd], gateway_code="GW-001", max_age_sec=TTL)
    assert reader.sayi == 0
    assert sonuclar[0]["ok"] is False


def test_taze_yetkili_komut_bir_kez_calisir() -> None:
    """Pozitif kontrol: yukaridaki redlerin sebebi 'her sey reddediliyor' degil."""
    reader = _SayanReader()
    sonuclar = _execute_pending_commands(
        reader, _state(), [_komut(55)], gateway_code="GW-001", max_age_sec=TTL
    )
    assert reader.sayi == 1
    assert sonuclar[0]["ok"] is True


def test_g28_sonuc_teslimi_bozulmadi(ledger) -> None:
    """Mevcut dayanikli sonuc tekrari AYNEN calisir."""
    ledger.start_dispatch(60, JETON)
    ledger.record_result({"id": 60, "ok": True, "status": "ok"})
    assert ledger.pending_result_count() == 1
    ledger.mark_delivered(60)
    assert ledger.pending_result_count() == 0


def test_g27_yarim_kalan_dispatch_unknown_olur(tmp_path: Path) -> None:
    """Restart kurtarmasi bozulmadi: `dispatching` -> `unknown`."""
    yol = tmp_path / "l.db"
    a = CommandLedger(yol)
    a.start_dispatch(61, JETON)
    a.close()

    b = CommandLedger(yol)
    try:
        kurtarilan = b.recover_unknown_results()
        assert [r["id"] for r in kurtarilan] == [61]
        assert kurtarilan[0]["status"] == "unknown"
        # ACK hala bekliyor: kurtarma ACK kuyrugunu ETKILEMEZ.
        assert b.pending_ack_count() == 1
    finally:
        b.close()


def test_poll_yaniti_teslim_alanlarini_tasir() -> None:
    """`PendingPoll` -> `PendingCommand` zinciri alanlari kaybetmemeli."""
    cmd = _komut(70, delivery_not_after="2026-08-15T10:02:00+00:00")
    poll = PendingPoll(commands=(cmd,))
    assert poll.commands[0].delivery_token == JETON
    assert poll.commands[0].delivery_not_after == "2026-08-15T10:02:00+00:00"
