"""KOMUT KIMLIGI — restore'dan bagimsiz int8 kimliklerle UYUMLULUK.

BAGLAM (saha olayi, 2026-08-21, GW-002)
---------------------------------------
Backend `device_commands` tablosu daha eski bir ana alindi. `id` bir
PostgreSQL SERIAL oldugu icin sequence de geriye dondu ve **39-42 kimlikleri
FARKLI komutlar icin yeniden kullanildi**.

Gateway DOGRU davrandi: defterinde o kimligi `completed/acked` gorup
fiziksel islemi TEKRARLAMADI ve eski dayanikli ACK'i yeniden gonderdi.
Backend yeni komut icin baska bir teslim jetonu bekledigi icin
`token_mismatch` uretti; `sent_at` dolmadi, 120 sn tazelik penceresi doldu,
komut `failed / result_unknown` oldu.

Iki taraf da sozlesmeye uydu. **Hatali olan kimligin kendisiydi.**

COZUM NEREDE
------------
Duzeltme **Grid (backend)** tarafinda: `enerjione/enerjione-grid@84746a3`.
Kimlik kaynagi sequence olmaktan cikarildi; artik duvar saati bilesenli,
monoton, JS-guvenli (< 2^53) bir **int8** uretiliyor ve migration 0078
`device_commands.id`i int4 -> int8 genisletiyor.

Gateway tarafinda **hicbir kod degismedi** — jeton dogrulamasi, 120 sn
tazelik, `delivery_not_after`, kira/ACK mantigi ve defter aynen duruyor.

BU DOSYA NEDEN VAR
------------------
Gateway kodu degismedi, ama **tasidigi veri degisti**. Depodaki hicbir test
9001'den buyuk bir komut kimligi kullanmiyordu; yani uretimde artik gelen
~1.79e15 buyuklugundeki kimlikler gateway yolunda **HIC denenmemisti**.

Bu dosya o bosluğu kapatir: yeni kimlik bicimi defter, dedup, jeton, ACK ve
sonuc yollarindan gecirilerek dogrulanir. Bir gun `INTEGER` yerine dar bir
tip ya da bir `int32` varsayimi girerse bu testler kirilir.
"""

from __future__ import annotations

import json

import pytest

from dnp3_gateway.backend.config_client import PendingCommand
from dnp3_gateway.messaging.command_ledger import CommandLedger

# ---------------------------------------------------------------------------
# Grid'in urettigi kimlik bicimi (enerjione-grid@84746a3)
# ---------------------------------------------------------------------------

#: Uretimde gorulen buyukluk sinifi: duvar saati bilesenli, ~1.79e15.
YENI_KIMLIK = 1_792_345_678_901_234

#: JavaScript `Number` tam sayi hassasiyet tavani. Arayuz kimligi `number`
#: tasidigi icin kimlik bunun ALTINDA kalmak ZORUNDA; ustunde sessizce
#: yuvarlanir ve operator gonderdigi komutu listede goremez.
JS_GUVENLI_TAVAN = 2**53 - 1

#: SQLite `INTEGER PRIMARY KEY` (rowid takma adi) 8 bayt isaretli tamsayidir.
SQLITE_INT64_TAVAN = 2**63 - 1

#: Saha olayinda yeniden kullanilan kimlikler.
CAKISAN_ESKI_KIMLIKLER = (39, 40, 41, 42)


@pytest.fixture
def defter(tmp_path) -> CommandLedger:
    d = CommandLedger(tmp_path / "command_ledger_TEST.db")
    yield d
    d.close()


# ==========================================================================
# 1 — KIMLIK BICIMI SINIRLARI
# ==========================================================================


def test_yeni_kimlik_js_guvenli_araligin_icinde() -> None:
    """Arayuz kimligi `number` tasir; 2^53 ustu SESSIZCE bozulur.

    Bu, Grid tarafinda UUID yerine int8 secilmesinin gerekcelerinden biri.
    """
    assert 0 < YENI_KIMLIK <= JS_GUVENLI_TAVAN
    assert YENI_KIMLIK > 2**31 - 1, "test degeri int32'ye siğiyor — regresyonu yakalamaz"


def test_yeni_kimlik_sqlite_int64_e_sigar() -> None:
    assert YENI_KIMLIK < SQLITE_INT64_TAVAN


# ==========================================================================
# 2 — PARSE YOLU: /pending govdesinden PendingCommand'a
# ==========================================================================


def test_pending_parse_buyuk_kimligi_bozmadan_tasir() -> None:
    """`int(item["id"])` Python'da keyfi hassasiyet — daralma OLMAMALI."""
    ham = {
        "id": YENI_KIMLIK,
        "device_code": "SN2_0",
        "command": "reset_all_fcis",
        "dnp3_index": 7,
        "op_type": "latch_on",
        "count": 1,
    }
    cmd = PendingCommand(
        id=int(ham["id"]),
        device_code=str(ham["device_code"]),
        command=str(ham["command"]),
        dnp3_index=int(ham["dnp3_index"]),
        op_type=str(ham["op_type"]),
        count=ham["count"],
    )
    assert cmd.id == YENI_KIMLIK


def test_json_round_trip_kimligi_bozmaz() -> None:
    """Tel uzerinde JSON: buyuk tamsayi float'a DONMEMELI."""
    govde = json.dumps({"id": YENI_KIMLIK})
    geri = json.loads(govde)["id"]
    assert isinstance(geri, int), f"kimlik {type(geri).__name__} olarak geri geldi"
    assert geri == YENI_KIMLIK


# ==========================================================================
# 3 — DEFTER YOLU: dispatch / jeton / ACK / sonuc
# ==========================================================================


def test_defter_buyuk_kimlikle_tam_yasam_dongusu(defter: CommandLedger) -> None:
    """dispatch -> jeton -> sonuc -> teslim: hepsi buyuk kimlikle."""
    jeton = "SzemMm2T6RSVe406NlblWmqBYo6Q4tfaT3A8l7OzsFg"

    ilk = defter.start_dispatch(YENI_KIMLIK, jeton)
    assert ilk is True, "yeni kimlik ilk kez dispatch edilemedi"

    biliniyor, kayitli = defter.kayitli_jeton(YENI_KIMLIK)
    assert biliniyor is True
    assert kayitli == jeton, "jeton buyuk kimlik altinda saklanmadi"

    defter.record_result({"id": YENI_KIMLIK, "ok": True, "status": "ok", "error": None})
    bekleyen = defter.pending_results()
    assert [r["id"] for r in bekleyen] == [YENI_KIMLIK]
    assert isinstance(bekleyen[0]["id"], int)

    defter.mark_delivered(YENI_KIMLIK)
    assert defter.pending_results() == []
    assert YENI_KIMLIK in defter.known_command_ids()


def test_defter_mukerrer_dispatch_i_buyuk_kimlikte_de_engeller(
    defter: CommandLedger,
) -> None:
    """CIFT-CALISTIRMA KORUMASI kimlik buyuklugunden BAGIMSIZ calismali.

    Bu koruma fiziksel bir kesici komutunun iki kez uygulanmasini engelleyen
    mekanizmadir; kimlik bicimi degisti diye zayiflamamali.
    """
    assert defter.start_dispatch(YENI_KIMLIK, "jeton-1") is True
    assert defter.start_dispatch(YENI_KIMLIK, "jeton-2") is False, (
        "ayni kimlik IKINCI kez dispatch edildi — cift-calistirma korumasi delinmis"
    )


def test_bitisik_buyuk_kimlikler_ayri_kayitlar(defter: CommandLedger) -> None:
    """Ardisik iki kimlik SESSIZCE ayni satira dusmemeli.

    Bir yerde `int32`/float daralmasi olsaydi komsu degerler cakisirdi.
    """
    a, b = YENI_KIMLIK, YENI_KIMLIK + 1
    assert defter.start_dispatch(a, "jeton-a") is True
    assert defter.start_dispatch(b, "jeton-b") is True, "komsu kimlik ayni satira dustu"
    assert defter.kayitli_jeton(a)[1] == "jeton-a"
    assert defter.kayitli_jeton(b)[1] == "jeton-b"
    assert defter.known_command_ids() >= {a, b}


def test_ack_yeniden_kuyruklama_buyuk_kimlikte_calisir(defter: CommandLedger) -> None:
    """Saha olayindaki `ack_yeniden_kuyrukla` yolu buyuk kimlikle de isler."""
    defter.start_dispatch(YENI_KIMLIK, "jeton")
    defter.mark_ack_delivered(YENI_KIMLIK)
    assert defter.pending_ack_count() == 0

    assert defter.ack_yeniden_kuyrukla(YENI_KIMLIK) is True
    assert defter.pending_ack_count() == 1
    assert [a["command_id"] for a in defter.pending_acks()] == [YENI_KIMLIK]


# ==========================================================================
# 4 — SAHA OLAYININ KENDISI
# ==========================================================================


@pytest.mark.parametrize("eski_kimlik", CAKISAN_ESKI_KIMLIKLER)
def test_yeniden_kullanilan_kimlik_hala_tekrar_calistirilmaz(defter: CommandLedger, eski_kimlik: int) -> None:
    """OLAYIN AYNISI: geri yuklenen backend ayni kimligi tekrar gonderirse.

    Gateway'in davranisi DEGISMEMELI — fiziksel islem TEKRARLANMAZ. Bu,
    duzeltilmesi gereken bir kusur DEGIL, korunmasi gereken bir guvencedir.
    Backend duzeltmesi kimliklerin tekrar kullanilmasini ONLER; gateway'in
    savunmasi ise SON CARE olarak yerinde kalir.
    """
    defter.start_dispatch(eski_kimlik, "eski-jeton")
    defter.record_result({"id": eski_kimlik, "ok": True, "status": "ok", "error": None})
    defter.mark_delivered(eski_kimlik)

    # Geri yuklenmis backend AYNI kimligi YENI bir komut icin gonderiyor.
    assert defter.start_dispatch(eski_kimlik, "yeni-jeton") is False, (
        "yeniden kullanilan kimlik FIZIKSEL olarak tekrar calistirildi — cift-calistirma korumasi kaybedilmis"
    )
    # Eski jeton korunur; gateway uydurma bir jeton URETMEZ.
    assert defter.kayitli_jeton(eski_kimlik)[1] == "eski-jeton"


def test_yeni_kimlik_bicimi_eski_defter_kayitlariyla_cakismaz(
    defter: CommandLedger,
) -> None:
    """DUZELTMENIN OZU: yeni kimlikler eski kucuk kimliklerle CAKISMAZ.

    Defterde 1..46 arasi eski kayitlar dururken backend artik ~1.79e15
    uretiyor; yeni komutlar temiz kimlik alir ve NORMAL calisir.
    """
    for eski in range(1, 47):
        defter.start_dispatch(eski, f"eski-{eski}")
        defter.record_result({"id": eski, "ok": True, "status": "ok", "error": None})
        defter.mark_delivered(eski)

    bilinen = defter.known_command_ids()
    assert len(bilinen) == 46

    # Grid duzeltmesinden SONRA gelen komut:
    assert defter.start_dispatch(YENI_KIMLIK, "taze-jeton") is True, (
        "yeni bicimli kimlik eski defter kayitlariyla cakisti"
    )
    assert defter.kayitli_jeton(YENI_KIMLIK)[1] == "taze-jeton"


def test_eski_kucuk_kimlikler_okunmaya_devam_eder(defter: CommandLedger) -> None:
    """GERIYE UYUMLULUK: migration mevcut kucuk kimlikleri DEGISTIRMEZ."""
    defter.start_dispatch(38, "jeton-38")
    defter.record_result({"id": 38, "ok": True, "status": "ok", "error": None})
    assert [r["id"] for r in defter.pending_results()] == [38]
    assert defter.kayitli_jeton(38) == (True, "jeton-38")


# ==========================================================================
# 5 — CALISMA SIRASINDA COKME (buyuk kimlikle)
# ==========================================================================


def test_cokme_kurtarmasi_buyuk_kimligi_bozmaz(tmp_path) -> None:
    """Dispatch sirasinda process olurse sonuc `unknown` olur, KIMLIK korunur.

    Kritik: komut TEKRAR OYNATILMAZ — sonucu bilinmiyor olsa bile.
    """
    yol = tmp_path / "ledger.db"
    d1 = CommandLedger(yol)
    d1.start_dispatch(YENI_KIMLIK, "jeton")
    d1.close()  # sonuc yazilmadan "cokme"

    d2 = CommandLedger(yol)
    try:
        kurtarilan = d2.recover_unknown_results()
        assert [r["id"] for r in kurtarilan] == [YENI_KIMLIK]
        assert kurtarilan[0]["ok"] is False
        assert kurtarilan[0]["status"] == "unknown"
        # Kurtarildiktan sonra da tekrar calistirilamaz.
        assert d2.start_dispatch(YENI_KIMLIK, "jeton") is False
    finally:
        d2.close()
