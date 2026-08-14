"""F1+F2 — fiziksel komutlarin YEREL cikis yetkilendirmesi.

Kapatilan uretim riski
----------------------
Gateway backend'den gelen `dnp3_index`'i hicbir yerde dogrulamadan CROB'a
ceviriyordu; `PendingCommand.command` ise parse edilip atiliyordu. Gercek
saha katalogunda su noktalar bulunuyor:

    index  7 -> master.reset_all_fcis     (rutin, sahada kullanilan komut)
    index  2 -> master.firmware_update
    index 22 -> master.modem_firmware_ota
    index 23 -> master.software_reset

Yani "FCI sifirla" niyetiyle gonderilmis ama index'i 2 olan tek bir komut,
saha cihazinda FIRMWARE GUNCELLEMESI baslatiyordu. Cihaz hata dondurmez,
istenmeyen seyi yapar.

Test verisi UYDURULMADI: asagidaki key/index ciftleri GW-002'nin calisan
runtime katalogundan alinmistir (SN 2.0 ve Pole Master Kit profilleri).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from dnp3_gateway.backend import DeviceConfig, PendingCommand
from dnp3_gateway.command_authorization import (
    AuthorizationReason,
    authorize_output_command,
)
from dnp3_gateway.main import _execute_pending_commands
from dnp3_gateway.state import GatewayState

from .conftest import make_gateway_config, make_signal

# --------------------------------------------------------------------------
# gercek katalog alt kumeleri (saha runtime'indan)
# --------------------------------------------------------------------------

#: Horstmann SN 2.0 — 18 binary output'un ilgili kesiti.
_SN2 = [
    make_signal("master.config_update", data_type="binary_output", object_group=10, index=0),
    make_signal("master.firmware_update", data_type="binary_output", object_group=10, index=2),
    make_signal("master.start_csv_upload", data_type="binary_output", object_group=10, index=3),
    make_signal("master.upload_debug_file", data_type="binary_output", object_group=10, index=4),
    make_signal("master.reset_all_fcis", data_type="binary_output", object_group=10, index=7),
    make_signal("master.software_reset", data_type="binary_output", object_group=10, index=23),
    # SN 2.0'da boost_mode index 26 (Pole Master'da 30 — model izolasyonu testi)
    make_signal("master.boost_mode", data_type="binary_output", object_group=10, index=26),
    make_signal("master.clear_dnp3_buffer", data_type="binary_output", object_group=10, index=28),
    # Telemetri noktalari — komut hedefi OLAMAZ
    make_signal("master.overcurrent_tripped", data_type="binary", object_group=1, index=7),
    make_signal("master.actual_current", data_type="analog", object_group=30, index=2),
]

#: Horstmann Pole Master Kit — boost_mode index 30.
_PM = [
    make_signal("master.firmware_update", data_type="binary_output", object_group=10, index=2),
    make_signal("master.start_csv_upload", data_type="binary_output", object_group=10, index=3),
    make_signal("master.upload_debug_file", data_type="binary_output", object_group=10, index=4),
    make_signal("master.reset_all_fcis", data_type="binary_output", object_group=10, index=7),
    make_signal("master.boost_mode", data_type="binary_output", object_group=10, index=30),
]


def _sebep(signals, index: int, command: str | None) -> AuthorizationReason:
    return authorize_output_command(signals, dnp3_index=index, command=command).reason


# --------------------------------------------------------------------------
# A. mesru gecmis sozlesme — uretimde CALISMIS komutlar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("katalog", "command", "index"),
    [
        (_SN2, "reset_all_fcis", 7),  # device_commands id 16,17,19,22
        (_PM, "start_csv_upload", 3),  # device_commands id 20
        (_PM, "upload_debug_file", 4),  # device_commands id 21
        (_SN2, "clear_dnp3_buffer", 28),  # device_commands id 6
    ],
)
def test_gecmiste_calismis_komutlar_yetkili_kalir(katalog, command: str, index: int) -> None:
    """REGRESYON KILIDI: uretimde basariyla calismis komutlar reddedilemez."""
    sonuc = authorize_output_command(katalog, dnp3_index=index, command=command)
    assert sonuc.authorized, f"{command}/{index} reddedildi: {sonuc.detail}"
    assert sonuc.signal is not None
    assert sonuc.signal.key == f"master.{command}"


# --------------------------------------------------------------------------
# B. niyet uyusmazligi — F2'nin ASIL kanit testi
# --------------------------------------------------------------------------


def test_dogru_komut_yanlis_index_reddedilir() -> None:
    """`reset_all_fcis` + index 2 (firmware_update) REDDEDILMELI.

    Index 2 katalogda GECERLI bir binary output'tur; yani yalnizca index
    allowlist'i (F1) bu komutu GECIRIR. Onu durduran sey F2'dir.
    """
    sonuc = authorize_output_command(_SN2, dnp3_index=2, command="reset_all_fcis")
    assert sonuc.reason is AuthorizationReason.COMMAND_INDEX_MISMATCH
    assert "master.firmware_update" in sonuc.detail


def test_gecerli_index_baska_gecerli_komut_reddedilir() -> None:
    """Iki tarafi da katalogda olan ama eslesmeyen cift."""
    assert _sebep(_SN2, 23, "start_csv_upload") is AuthorizationReason.COMMAND_INDEX_MISMATCH
    assert _sebep(_SN2, 0, "software_reset") is AuthorizationReason.COMMAND_INDEX_MISMATCH


# --------------------------------------------------------------------------
# C. katalogda olmayan index
# --------------------------------------------------------------------------


@pytest.mark.parametrize("index", [999, 5, 6, 13, 24, -1])
def test_katalogda_olmayan_index_reddedilir(index: int) -> None:
    """5, 6, 13, 24 gercek katalog BOSLUKLARIDIR (index uzayi seyrek)."""
    assert _sebep(_SN2, index, "reset_all_fcis") is AuthorizationReason.INDEX_NOT_AUTHORIZED


# --------------------------------------------------------------------------
# D. binary INPUT / analog noktaya yazma denemesi
# --------------------------------------------------------------------------


def test_binary_input_index_i_komut_hedefi_olamaz() -> None:
    """G1 index 7 katalogda var ama data_type='binary'.

    Ayni sayisal index G10'da baska bir noktaya aittir; tip filtresi
    olmasaydi input adresine yazma denemesi output gibi gorunurdu.
    """
    assert _sebep(_SN2, 7, "overcurrent_tripped") is AuthorizationReason.COMMAND_INDEX_MISMATCH
    # G30 analog index 2 -> yalnizca binary_output degerlendirilir
    assert _sebep(_SN2, 2, "actual_current") is AuthorizationReason.COMMAND_INDEX_MISMATCH


def test_yalnizca_binary_output_katalog_sayilir() -> None:
    """Sette hic binary_output yoksa katalog YOK sayilir (fail-closed)."""
    yalniz_telemetri = [s for s in _SN2 if s.data_type != "binary_output"]
    assert _sebep(yalniz_telemetri, 7, "reset_all_fcis") is AuthorizationReason.CATALOG_UNAVAILABLE


# --------------------------------------------------------------------------
# E. model izolasyonu — global allowlist YETMEZ
# --------------------------------------------------------------------------


def test_boost_mode_model_izolasyonu() -> None:
    """`master.boost_mode` SN2'de 26, Pole Master'da 30 (backend'de olculdu).

    Global bir index listesi ikisini de kabul eder ve komutu YANLIS noktaya
    gonderirdi; yetkilendirme cihazin KENDI setine karsi yapilmali.
    """
    assert authorize_output_command(_SN2, dnp3_index=26, command="boost_mode").authorized
    assert authorize_output_command(_PM, dnp3_index=30, command="boost_mode").authorized
    # PM'de 26 bos -> index yetkisiz
    assert _sebep(_PM, 26, "boost_mode") is AuthorizationReason.INDEX_NOT_AUTHORIZED
    # SN2'de 30 bos
    assert _sebep(_SN2, 30, "boost_mode") is AuthorizationReason.INDEX_NOT_AUTHORIZED


# --------------------------------------------------------------------------
# F. komut slug'i eksik -> F2 bypass edilemez
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["", "   ", "\t", None])
def test_bos_komut_reddedilir(command) -> None:
    """Bos slug ile index-only kontrole DUSULMEZ; bu bir F2 bypass'i olurdu."""
    assert _sebep(_SN2, 7, command) is AuthorizationReason.COMMAND_MISSING


def test_normalizasyon_yapilmaz() -> None:
    """Case-fold / trim / alias YOK — gevsek eslestirme fail-open olurdu."""
    assert _sebep(_SN2, 7, "RESET_ALL_FCIS") is AuthorizationReason.COMMAND_INDEX_MISMATCH
    assert _sebep(_SN2, 7, " reset_all_fcis ") is AuthorizationReason.COMMAND_INDEX_MISMATCH
    assert _sebep(_SN2, 7, "reset_all_fci") is AuthorizationReason.COMMAND_INDEX_MISMATCH
    # `master.` oneki cagiran tarafindan GONDERILMEZ; gonderilirse cift onek olur
    assert _sebep(_SN2, 7, "master.reset_all_fcis") is AuthorizationReason.COMMAND_INDEX_MISMATCH


# --------------------------------------------------------------------------
# G. katalog yok
# --------------------------------------------------------------------------


@pytest.mark.parametrize("katalog", [None, [], ()])
def test_katalog_yoksa_fail_closed(katalog) -> None:
    assert _sebep(katalog, 7, "reset_all_fcis") is AuthorizationReason.CATALOG_UNAVAILABLE


# --------------------------------------------------------------------------
# pull yolu entegrasyonu — operate_device CAGRILMAMALI
# --------------------------------------------------------------------------


class _SayanReader:
    """operate_device cagrilirsa yakalar."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def operate_device(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "status": "ok", "control": kwargs.get("op_type")}


def _state_ile(katalog) -> tuple[GatewayState, DeviceConfig]:
    device = DeviceConfig(code="SN2_0", name="SN2_0", ip_address="10.0.0.9", dnp3_address=4)
    state = GatewayState()
    state.update(make_gateway_config(devices=[device], signals=list(katalog)))
    return state, device


def _komut(**kw: Any) -> PendingCommand:
    """F1/F2 testleri icin komut.

    `created_at` VARSAYILAN OLARAK DOLU ve taze: uretim sozlesmesinde backend
    (F3B ve sonrasi) her komutta timezone-aware `created_at` gonderiyor ve
    gateway damgasiz komutu varsayilan olarak reddediyor. Damgasiz birakmak,
    bu testleri F1/F2 yerine tazelik kontrolunu olcer hale getirirdi.
    """
    varsayilan = {
        "id": 1,
        "device_code": "SN2_0",
        "command": "reset_all_fcis",
        "dnp3_index": 7,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    varsayilan.update(kw)
    return PendingCommand(**varsayilan)  # type: ignore[arg-type]


def test_pull_yolu_yetkili_komutu_calistirir() -> None:
    """I. Mevcut basari akisi bozulmadi."""
    state, _d = _state_ile(_SN2)
    reader = _SayanReader()

    sonuclar = _execute_pending_commands(reader, state, [_komut()], gateway_code="GW-002")

    assert len(reader.calls) == 1
    assert reader.calls[0]["index"] == 7
    assert sonuclar[0]["ok"] is True


def test_pull_yolu_niyet_uyusmazliginda_crob_gondermez() -> None:
    """B + J: reddedilen komut CROB uretmez ama SESSIZ de dusmez."""
    state, _d = _state_ile(_SN2)
    reader = _SayanReader()

    # "FCI sifirla" niyeti, firmware_update index'i
    sonuclar = _execute_pending_commands(reader, state, [_komut(id=55, dnp3_index=2)], gateway_code="GW-002")

    assert reader.calls == [], "yetkisiz komut icin CROB GONDERILDI"
    assert len(sonuclar) == 1, "reddedilen komut sessizce dusurulmus"
    assert sonuclar[0]["id"] == 55
    assert sonuclar[0]["ok"] is False
    assert sonuclar[0]["status"] == "command_index_mismatch"
    assert sonuclar[0]["error"]


@pytest.mark.parametrize(
    ("komut", "beklenen"),
    [
        (dict(dnp3_index=999), "index_not_authorized"),
        (dict(command=""), "command_missing"),
        (dict(device_code="YOK"), "device_not_found"),
    ],
)
def test_pull_yolu_red_sebepleri(komut: dict, beklenen: str) -> None:
    state, _d = _state_ile(_SN2)
    reader = _SayanReader()

    sonuclar = _execute_pending_commands(reader, state, [_komut(**komut)], gateway_code="GW-002")

    assert reader.calls == []
    assert sonuclar[0]["status"] == beklenen
    assert sonuclar[0]["ok"] is False


def test_pull_yolu_katalog_yoksa_fail_closed() -> None:
    """G: cihaz cozumleniyor ama sinyal seti bos -> komut gonderilmez."""
    state, _d = _state_ile([s for s in _SN2 if s.data_type != "binary_output"])
    reader = _SayanReader()

    sonuclar = _execute_pending_commands(reader, state, [_komut()], gateway_code="GW-002")

    assert reader.calls == []
    assert sonuclar[0]["status"] == "catalog_unavailable"


def test_reddedilen_komut_sonucu_backend_bicimindedir() -> None:
    """J: sonuc, ledger/backend teslim yolunun bekledigi sekli tasimali."""
    state, _d = _state_ile(_SN2)
    sonuclar = _execute_pending_commands(
        _SayanReader(), state, [_komut(id=9, dnp3_index=2)], gateway_code="GW-002"
    )
    kayit = sonuclar[0]
    assert set(kayit) >= {"id", "ok", "status", "error"}
    assert isinstance(kayit["id"], int)
    assert kayit["ok"] is False
