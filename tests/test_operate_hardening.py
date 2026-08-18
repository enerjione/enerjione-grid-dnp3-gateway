"""F7-G — `POST /operate` sertlestirmesi.

DENETIMDE OLCULEN DURUM
-----------------------
Endpoint auth + F1/F2 + F6 tasiyordu, ama uc kapisi acikti. Asagidakiler
sahte reader ile ampirik olarak dogrulandi (gercek cihaza komut gitmedi):

  1. TTL YOKTU. `created_at` okunmuyordu bile: 2020 tarihli damgayla
     gonderilen istek KABUL EDILDI ve CROB uretti. Yakalanmis bir istek
     SURESIZ gecerliydi. Kuyruk yolu ayni anda F3C ile strict TTL
     uyguluyordu — ayni fiziksel manevra, iki farkli kural.

  2. TEKRAR KORUMASI OPSIYONELDI. `command_id` zorunlu degildi; ayni istek
     ard arda uc kez gonderildiginde UC FIZIKSEL CROB uretildi. Kod bunu
     biliyordu ve her seferinde "istek tekrarlanirsa CROB DA TEKRARLANIR"
     uyarisi basiyordu — ama engellemiyordu.

  3. GOVDE TIPI DOGRULANMIYORDU. `[1,2,3]` gibi gecerli-JSON ama
     nesne-olmayan bir govde yakalanmamis `AttributeError` uretiyor, sunucu
     istege HIC yanit vermeden baglantiyi kapatiyordu.

Ayrica defter (CommandLedger) OPSIYONELDI: yapilandirilmamissa komut yine
gonderiliyor, yalnizca `operate_ledger_missing` loglaniyordu — yani
tekrar-onleme garantisi olmadan fiziksel manevra.

SIRA SOZLESMESI
---------------
    auth -> JSON/tip -> zorunlu alanlar -> F1/F2 -> tazelik
         -> command_id + defter rezervasyonu -> F6 -> fiziksel calistirma

Rezervasyonun yetkilendirme ve tazelikten SONRA olmasi kritik: reddedilen
bir istek `command_id`yi TUKETMEMELI, yoksa operator duzeltilmis komutu ayni
id ile gonderdiginde defter onu "zaten islendi" sayip sessizce yutardi.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from threading import Event
from typing import Any

import pytest

from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader
from dnp3_gateway.backend import DeviceConfig
from dnp3_gateway.health_server import start_health_server
from dnp3_gateway.state import GatewayState

from .conftest import make_gateway_config, make_signal

#: `/operate` icin AYRILMIS credential (GATEWAY_COMMAND_TOKEN).
_COMMAND_TOKEN = "operate-icin-ayrilmis-token-1234567890"
#: Gateway'in backend'e kimligini kanitlayan ANA token (GATEWAY_TOKEN).
_GATEWAY_TOKEN = "gateway-ana-token-1234567890-abcdef"
#: F5 kuyruklanmis komut duzlemi credential'i (GATEWAY_COMMAND_DELIVERY_TOKEN).
_DELIVERY_TOKEN = "kuyruk-teslim-token-1234567890-abcdef"
#: `/refresh-all` credential'i (GATEWAY_REFRESH_TOKEN).
_REFRESH_TOKEN = "refresh-all-icin-token-1234567890-ab"

_KATALOG = [
    make_signal("master.reset_all_fcis", data_type="binary_output", object_group=10, index=7),
    # Katalogda GECERLI baska bir output — F2'nin "dogru komut, YANLIS nokta"
    # vakasi icin gerekli.
    make_signal("master.firmware_update", data_type="binary_output", object_group=10, index=2),
]


# ==========================================================================
# kosum takimi
# ==========================================================================


class _SayanMaster:
    def __init__(self, kayit: list[dict[str, Any]]) -> None:
        self._kayit = kayit

    def operate_crob(self, **kw: Any) -> dict[str, Any]:
        self._kayit.append(kw)
        return {"ok": True, "status": "success", "control": kw.get("op_type")}


class _GercekReader:
    """`operate_device` GERCEK implementasyondur — F6 validator'i dahil.

    Fiziksel cagri sayisi validator'dan SONRAYI olcer; sahte bir reader
    kullansaydik F6 reddi ile basarili gonderimi ayirt edemezdik.
    """

    def __init__(self) -> None:
        self.fiziksel: list[dict[str, Any]] = []
        self.oturum = 0

    def _ensure_master(self, device: DeviceConfig) -> Any:  # noqa: ARG002
        self.oturum += 1
        return _SayanMaster(self.fiziksel)

    def operate_device(self, **kw: Any) -> dict[str, Any]:
        return Yadnp3TelemetryReader.operate_device(self, **kw)


class _Defter:
    """CommandLedger'in `/operate`in kullandigi yuzeyi."""

    def __init__(self, *, patlat: bool = False) -> None:
        self.patlat = patlat
        self.dispatched: set[int] = set()
        self.results: dict[int, dict[str, Any]] = {}

    def start_dispatch(self, command_id: int) -> bool:
        if self.patlat:
            raise RuntimeError("veritabani kilitli")
        if command_id in self.dispatched:
            return False
        self.dispatched.add(command_id)
        return True

    def record_result(self, result: dict[str, Any]) -> None:
        self.results[int(result["id"])] = result

    def pending_results(self) -> list[dict[str, Any]]:
        return list(self.results.values())


def _state() -> GatewayState:
    st = GatewayState()
    st.update(
        make_gateway_config(
            devices=[DeviceConfig(code="SN2_0", name="SN2_0", ip_address="10.0.0.9", dnp3_address=4)],
            signals=list(_KATALOG),
        )
    )
    return st


@pytest.fixture
def sunucu():
    """Gercek HTTP sunucusu; reader/ledger testten enjekte edilir."""
    tutucu: dict[str, Any] = {"reader": _GercekReader(), "ledger": _Defter()}
    hazir = Event()
    hazir.set()
    server, _metrics, port = start_health_server(
        host="127.0.0.1",
        port=0,
        state=_state(),
        gateway_code="GW-002",
        gateway_mode="dnp3",
        config_ready=hazir,
        instance_id="test",
        app_environment="development",
        reader_provider=lambda: tutucu["reader"],
        ledger_provider=lambda: tutucu["ledger"],
        refresh_token=_REFRESH_TOKEN,
        command_token=_COMMAND_TOKEN,
        command_max_age_sec=120.0,
        command_clock_skew_tolerance_sec=5.0,
    )
    try:
        yield tutucu, port
    finally:
        server.shutdown()
        server.server_close()


def _ham_post(port: int, ham_govde: str, *, token: str = _COMMAND_TOKEN) -> tuple[Any, dict[str, Any]]:
    """Ham metin gonderir (JSON tip testleri icin)."""
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        veri = ham_govde.encode("utf-8")
        conn.request(
            "POST",
            "/operate",
            body=veri,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(veri)),
            },
        )
        resp = conn.getresponse()
        metin = resp.read().decode("utf-8")
        return resp.status, (json.loads(metin) if metin else {})
    except Exception as exc:  # noqa: BLE001
        # Sunucu yanit vermeden baglantiyi kaparsa bunu ACIKCA yakalamak
        # istiyoruz — F7 oncesi davranis buydu ve testin gormesi gerekiyor.
        return f"BAGLANTI_KOPTU:{type(exc).__name__}", {}
    finally:
        conn.close()


def _taze() -> str:
    return datetime.now(timezone.utc).isoformat()


def _govde(**alanlar: Any) -> dict[str, Any]:
    g: dict[str, Any] = {
        "device_code": "SN2_0",
        "command": "reset_all_fcis",
        "index": 7,
        "command_id": 5001,
        "created_at": _taze(),
    }
    g.update(alanlar)
    return g


def _post(port: int, govde: dict[str, Any], *, token: str = _COMMAND_TOKEN):
    return _ham_post(port, json.dumps(govde), token=token)


# ==========================================================================
# 1. GOVDE JSON NESNESI OLMALI
# ==========================================================================


@pytest.mark.parametrize(
    "ham",
    ["[1,2,3]", "[]", '"metin"', "42", "3.14", "true", "null"],
    ids=["dizi", "bos_dizi", "metin", "tamsayi", "ondalik", "bool", "null"],
)
def test_nesne_olmayan_govde_400(sunucu, ham: str) -> None:
    """REGRESYON: eskiden sunucu YANIT VERMEDEN baglantiyi kapatiyordu."""
    tutucu, port = sunucu
    kod, govde = _ham_post(port, ham)

    assert kod == 400, f"{ham!r} icin 400 bekleniyordu, {kod} geldi"
    assert govde.get("status") == "invalid_body"
    assert tutucu["reader"].fiziksel == []


def test_bozuk_json_400(sunucu) -> None:
    tutucu, port = sunucu
    kod, _g = _ham_post(port, "{bozuk")
    assert kod == 400
    assert tutucu["reader"].fiziksel == []


# ==========================================================================
# 2. command_id ZORUNLU
# ==========================================================================


def test_command_id_yoksa_reddedilir(sunucu) -> None:
    tutucu, port = sunucu
    g = _govde()
    del g["command_id"]
    kod, govde = _post(port, g)

    assert kod == 400
    assert govde["status"] == "command_id_missing"
    assert tutucu["reader"].fiziksel == []


@pytest.mark.parametrize(
    "cid",
    [None, True, False, "5001", "abc", 5001.0, 5001.5, [5001], {"id": 5001}],
    ids=[
        "null",
        "bool_true",
        "bool_false",
        "metin",
        "metin_sayisiz",
        "ondalik",
        "kesirli",
        "liste",
        "sozluk",
    ],
)
def test_command_id_gecersiz_tip_reddedilir(sunucu, cid: Any) -> None:
    """STRICT: `int(...)` ile donusturulmez.

    `True` bir `int` alt sinifidir ve `"5001"` sessizce 5001 olurdu; iki
    cagiran deftere ayri anahtar yazdigini sanip ayni kesiciyi iki kez
    surebilirdi.
    """
    tutucu, port = sunucu
    kod, govde = _post(port, _govde(command_id=cid))

    assert kod == 400
    assert govde["status"] in {"command_id_missing", "command_id_invalid"}
    assert tutucu["reader"].fiziksel == []


def test_negatif_ve_sifir_command_id_kabul_edilir(sunucu) -> None:
    """Defterin mevcut sozlesmesi TAMSAYI; aralik kisiti UYDURULMADI."""
    tutucu, port = sunucu
    for cid in (0, -1, 2**63):
        kod, govde = _post(port, _govde(command_id=cid))
        assert kod == 200, f"command_id={cid} reddedildi"
        assert govde["ok"] is True
    assert len(tutucu["reader"].fiziksel) == 3


# ==========================================================================
# 3. DEFTER ZORUNLU
# ==========================================================================


def test_defter_provider_yoksa_reddedilir() -> None:
    """`ledger_provider` HIC verilmemis — eskiden komut yine gonderiliyordu."""
    reader = _GercekReader()
    hazir = Event()
    hazir.set()
    server, _m, port = start_health_server(
        host="127.0.0.1",
        port=0,
        state=_state(),
        gateway_code="GW-002",
        gateway_mode="dnp3",
        config_ready=hazir,
        instance_id="test",
        app_environment="development",
        reader_provider=lambda: reader,
        command_token=_COMMAND_TOKEN,
    )
    try:
        kod, govde = _post(port, _govde())
        assert kod == 503
        assert govde["status"] == "rejected"
        assert reader.fiziksel == []
    finally:
        server.shutdown()
        server.server_close()


def test_defter_none_donerse_reddedilir(sunucu) -> None:
    tutucu, port = sunucu
    tutucu["ledger"] = None

    kod, govde = _post(port, _govde())

    assert kod == 503
    assert govde["status"] == "rejected"
    assert tutucu["reader"].fiziksel == []


def test_defter_patlarsa_503(sunucu) -> None:
    """Fiziksel manevrada 'belki iki kez surdum' kabul edilemez."""
    tutucu, port = sunucu
    tutucu["ledger"] = _Defter(patlat=True)

    kod, govde = _post(port, _govde())

    assert kod == 503
    assert govde["status"] == "rejected"
    assert tutucu["reader"].fiziksel == []


def test_rezervasyon_basarisizsa_fiziksel_cagri_yok(sunucu) -> None:
    """`start_dispatch` basarili olmadan `operate_device` cagrilmamali."""
    tutucu, port = sunucu
    defter = _Defter(patlat=True)
    tutucu["ledger"] = defter

    _post(port, _govde())

    assert defter.dispatched == set(), "rezervasyon yokken ilerlendi"
    assert tutucu["reader"].oturum == 0, "DNP3 oturumu acildi"


# ==========================================================================
# 4. TEKRAR (duplicate) — mevcut semantik korunuyor
# ==========================================================================


def test_ayni_istek_uc_kez_tek_fiziksel_calistirma(sunucu) -> None:
    """OLCULEN ESKI DAVRANIS: uc istek -> UC CROB."""
    tutucu, port = sunucu
    g = _govde(command_id=7001)

    kodlar = [_post(port, g)[0] for _ in range(3)]

    assert kodlar == [200, 200, 200]
    assert len(tutucu["reader"].fiziksel) == 1, "kesici birden fazla kez suruldu"


def test_duplicate_kayitli_sonucu_doner(sunucu) -> None:
    """Mevcut duplicate semantigi korundu."""
    tutucu, port = sunucu
    g = _govde(command_id=7002)

    _post(port, g)
    kod, govde = _post(port, g)

    assert kod == 200
    assert govde["duplicate"] is True
    assert govde["ok"] is True
    assert len(tutucu["reader"].fiziksel) == 1


def test_sonucu_bilinmeyen_duplicate_pending_doner(sunucu) -> None:
    """`ok:false` DEMEZ — operator yeni id ile tekrar deneyip kesiciyi iki
    kez surerdi. Mevcut `pending` semantigi korundu."""
    tutucu, port = sunucu
    defter = _Defter()
    defter.dispatched.add(7003)  # rezerve ama sonuc YOK
    tutucu["ledger"] = defter

    kod, govde = _post(port, _govde(command_id=7003))

    assert kod == 200
    assert govde["duplicate"] is True
    assert govde["ok"] is None
    assert govde["status"] == "pending"
    assert tutucu["reader"].fiziksel == []


# ==========================================================================
# 5. TAZELIK ZORUNLU (yeni kod yok — kuyrukla AYNI fonksiyon)
# ==========================================================================


def test_created_at_yoksa_reddedilir(sunucu) -> None:
    tutucu, port = sunucu
    g = _govde()
    del g["created_at"]

    kod, govde = _post(port, g)

    assert kod == 400
    assert govde["status"] == "command_timestamp_missing"
    assert tutucu["reader"].fiziksel == []


@pytest.mark.parametrize(
    ("etiket", "damga"),
    [
        ("bozuk", "yarin oglen"),
        ("bos", "   "),
        ("timezone_suz", "2026-08-18T10:00:00"),
        ("tarih_only", "2026-08-18"),
        ("null", None),
        ("sayi", 1755500000),
    ],
)
def test_gecersiz_damga_reddedilir(sunucu, etiket: str, damga: Any) -> None:
    """Timezone'suz damga da REDDEDILIR: hangi saat diliminde oldugu
    bilinmeyen bir damga TTL hesabini anlamsiz kilar."""
    tutucu, port = sunucu
    kod, govde = _post(port, _govde(created_at=damga))

    assert kod == 400, f"{etiket}: kabul edildi"
    assert govde["status"] in {"command_timestamp_missing", "command_timestamp_invalid"}
    assert tutucu["reader"].fiziksel == []


def test_suresi_dolmus_damga_reddedilir(sunucu) -> None:
    """Denetimde 2020 tarihli damga KABUL EDILIYORDU."""
    tutucu, port = sunucu
    eski = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    kod, govde = _post(port, _govde(created_at=eski))

    assert kod == 400
    assert govde["status"] == "expired"
    assert tutucu["reader"].fiziksel == []


def test_asiri_gelecek_damga_reddedilir(sunucu) -> None:
    tutucu, port = sunucu
    gelecek = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    kod, govde = _post(port, _govde(created_at=gelecek))

    assert kod == 400
    assert tutucu["reader"].fiziksel == []


def test_makul_saat_sapmasi_kabul_edilir(sunucu) -> None:
    """Mevcut clock-skew sozlesmesi yeniden kullanildi (uydurulmadi)."""
    tutucu, port = sunucu
    hafif = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()

    kod, govde = _post(port, _govde(created_at=hafif))

    assert kod == 200
    assert govde["ok"] is True
    assert len(tutucu["reader"].fiziksel) == 1


def test_ttl_siniri_icindeki_damga_kabul_edilir(sunucu) -> None:
    tutucu, port = sunucu
    icinde = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    kod, _g = _post(port, _govde(created_at=icinde))

    assert kod == 200
    assert len(tutucu["reader"].fiziksel) == 1


# ==========================================================================
# 6. RED, command_id'yi TUKETMEZ
# ==========================================================================


def test_f1f2_reddi_command_id_yi_tuketmez(sunucu) -> None:
    """Yetkisiz istek sonrasi AYNI id ile duzeltilmis komut gecmeli."""
    tutucu, port = sunucu
    defter = _Defter()
    tutucu["ledger"] = defter

    # `reset_all_fcis` + index 2 (firmware_update) -> niyet/nokta uyusmazligi
    kod, _g = _post(port, _govde(command_id=8001, index=2))
    assert kod == 403
    assert defter.dispatched == set(), "yetkisiz istek command_id'yi TUKETTI"

    kod2, govde2 = _post(port, _govde(command_id=8001))
    assert kod2 == 200
    assert govde2["ok"] is True
    assert len(tutucu["reader"].fiziksel) == 1


def test_tazelik_reddi_command_id_yi_tuketmez(sunucu) -> None:
    """Bayat istek sonrasi AYNI id ile taze komut gecmeli."""
    tutucu, port = sunucu
    defter = _Defter()
    tutucu["ledger"] = defter

    eski = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    kod, _g = _post(port, _govde(command_id=8002, created_at=eski))
    assert kod == 400
    assert defter.dispatched == set(), "bayat istek command_id'yi TUKETTI"

    kod2, govde2 = _post(port, _govde(command_id=8002))
    assert kod2 == 200
    assert govde2["ok"] is True
    assert len(tutucu["reader"].fiziksel) == 1


def test_govde_tipi_reddi_command_id_yi_tuketmez(sunucu) -> None:
    tutucu, port = sunucu
    defter = _Defter()
    tutucu["ledger"] = defter

    _ham_post(port, "[1,2,3]")

    assert defter.dispatched == set()


# ==========================================================================
# 7. F6 REDDI — fiziksel cagri 0 (rezervasyondan SONRA)
# ==========================================================================


@pytest.mark.parametrize(
    ("alan", "deger", "beklenen"),
    [
        ("count", 0, "invalid_count"),
        ("count", -1, "invalid_count"),
        ("count", 256, "invalid_count"),
        ("count", True, "invalid_count"),
        ("count", "1", "invalid_count"),
        ("on_time_ms", -1, "invalid_timing"),
        ("off_time_ms", "100", "invalid_timing"),
        ("op_type", "trip", "invalid_op_type"),
    ],
)
def test_f6_reddi_fiziksel_cagri_uretmez(sunucu, alan: str, deger: Any, beklenen: str) -> None:
    tutucu, port = sunucu
    kod, govde = _post(port, _govde(**{alan: deger}))

    assert kod == 200  # uc her zaman 200 doner; sonuc govdede
    assert govde["ok"] is False
    assert govde["status"] == beklenen
    assert tutucu["reader"].fiziksel == []
    assert tutucu["reader"].oturum == 0, "DNP3 oturumu acildi"


# ==========================================================================
# 8. TAM GECERLI AKIS
# ==========================================================================


def test_gecerli_istek_tam_bir_fiziksel_calistirma(sunucu) -> None:
    """taze + yetkili + gecerli id + calisan defter + gecerli parametreler."""
    tutucu, port = sunucu
    defter = _Defter()
    tutucu["ledger"] = defter

    kod, govde = _post(port, _govde(command_id=9001))

    assert kod == 200
    assert govde["ok"] is True
    assert govde["duplicate"] is False
    assert len(tutucu["reader"].fiziksel) == 1
    assert tutucu["reader"].fiziksel[0]["op_type"] == "latch_on"
    assert defter.dispatched == {9001}
    assert defter.results[9001]["ok"] is True


# ==========================================================================
# 9. CREDENTIAL AYRIMI — degismedi
# ==========================================================================


def test_ayrilmis_command_token_calisir(sunucu) -> None:
    tutucu, port = sunucu
    kod, govde = _post(port, _govde(), token=_COMMAND_TOKEN)
    assert kod == 200
    assert govde["ok"] is True
    assert len(tutucu["reader"].fiziksel) == 1


@pytest.mark.parametrize(
    ("etiket", "token"),
    [
        ("gateway_ana_token", _GATEWAY_TOKEN),
        ("f5_teslim_tokeni", _DELIVERY_TOKEN),
        ("refresh_all_tokeni", _REFRESH_TOKEN),
        ("bos", ""),
        ("yanlis", "tamamen-baska-bir-token-0000000000"),
    ],
)
def test_baska_credentiallar_operate_icin_gecersiz(sunucu, etiket: str, token: str) -> None:
    """Rol ayrimi: `/operate` YALNIZCA kendi tokenini kabul eder.

    GATEWAY_TOKEN sizarsa fiziksel komut duzlemi ele gecmemeli; F5 teslim
    credential'i da kuyruk duzlemine aittir, buraya gecmez.
    """
    tutucu, port = sunucu
    kod, _g = _post(port, _govde(), token=token)

    assert kod == 401, f"{etiket} ile /operate ACILDI"
    assert tutucu["reader"].fiziksel == []


def test_command_token_bossa_endpoint_devre_disi() -> None:
    """Varsayilan kurulumda GATEWAY_COMMAND_TOKEN tanimsiz -> 503."""
    reader = _GercekReader()
    hazir = Event()
    hazir.set()
    server, _m, port = start_health_server(
        host="127.0.0.1",
        port=0,
        state=_state(),
        gateway_code="GW-002",
        gateway_mode="dnp3",
        config_ready=hazir,
        instance_id="test",
        app_environment="development",
        reader_provider=lambda: reader,
        ledger_provider=lambda: _Defter(),
        command_token="",
    )
    try:
        kod, _g = _post(port, _govde(), token="herhangi-bir-token")
        assert kod == 503
        assert reader.fiziksel == []
    finally:
        server.shutdown()
        server.server_close()


# ==========================================================================
# 10. SIRA SOZLESMESI
# ==========================================================================


def test_kontrol_sirasi_kaynakta_kilitli() -> None:
    """auth -> JSON/tip -> zorunlu alan -> F1/F2 -> tazelik
    -> command_id + defter -> fiziksel.

    Sira bozulursa reddedilen bir istek command_id tuketebilir ya da
    yetkisiz bir istek defter kaydi birakabilir.
    """
    import inspect

    from dnp3_gateway import health_server

    kaynak = inspect.getsource(health_server)
    h = kaynak[kaynak.index("def _handle_operate") : kaynak.index("def _respond_json")]

    izler = [
        "_check_bearer_auth(command_token)",
        "isinstance(payload, dict)",
        "device_code ve index zorunlu",
        "authorize_output_command(",
        "validate_command_freshness(",
        "command_id_missing",
        "start_dispatch(ledger_key)",
        "reader.operate_device(",
    ]
    konumlar = [h.index(iz) for iz in izler]
    assert konumlar == sorted(konumlar), f"kontrol sirasi bozuldu: {list(zip(izler, konumlar, strict=True))}"


def test_operate_kuyrukla_ayni_tazelik_fonksiyonunu_kullanir() -> None:
    """Yeni freshness kodu YAZILMADI; iki kanal ayni karari verir."""
    import inspect

    from dnp3_gateway import health_server, main

    assert "validate_command_freshness(" in inspect.getsource(health_server)
    assert "validate_command_freshness(" in inspect.getsource(main)
