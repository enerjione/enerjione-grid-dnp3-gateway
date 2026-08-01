"""`X-E1-Gateway-Health` basligi — uretim ve `/pending`e iliskirilmesi.

NE KORUNUYOR
------------
1. BASLIK BOYUTU. Backend 2048 bayt ustunu ayristirmiyor, ama asil tehlike
   nginx: tavana yaklasan bir baslik ISTEGIN TAMAMINI reddettirir, yani
   KOMUTLAR DA GITMEZ. 600 cihazin tamamini gondermek ~9 KB eder. Bu yuzden
   yalnizca `online` olmayanlar gidiyor ve liste kirpiliyor.

2. KOMUT KANALI KUTSAL. Saglik saglayicisinin patlamasi, sacma deger
   dondurmesi ya da baslik uretilememesi `/pending` cagrisini DUSURMEMELI.
   Ozellik bir teshis kolayligi; komut yolu SCADA'nin kendisi.

3. BACKEND SOZLESMESI. Yayindaki backend `payload["version"]` okuyor.
   `gateway_version` yazmak sessizce bos bir alan birakirdi — test bunu
   anahtar duzeyinde sabitliyor.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient, health_header


def _coz(baslik: str) -> dict[str, Any]:
    """Baslgi geri acar — testler URETIMI degil SONUCU dogrulasin."""
    return json.loads(base64.urlsafe_b64decode(baslik.encode("ascii")).decode("utf-8"))


# --------------------------------------------------------------------------
# Govde uretimi
# --------------------------------------------------------------------------


def test_yalnizca_online_olmayanlar_gonderilir() -> None:
    govde = health_header.build_payload(
        status="ok",
        device_summary={"total": 4, "online": 2, "lost": 1, "recovering": 1},
        per_device={
            "d1": {"state": "online"},
            "d2": {"state": "online"},
            "d3": {"state": "lost"},
            "d4": {"state": "recovering"},
        },
    )
    assert govde["devices"]["states"] == {"d3": "lost", "d4": "recovering"}
    # SAYIMLAR yine tam filo icin — "kac cihaz iyi" bilgisi kaybolmasin.
    assert govde["devices"]["total"] == 4
    assert govde["devices"]["online"] == 2


def test_hepsi_online_ise_states_hic_konmaz() -> None:
    """Normal isleyis: bos bir `states` sozlugu bile bayt harcamasin."""
    govde = health_header.build_payload(
        status="ok",
        device_summary={"total": 2, "online": 2},
        per_device={"d1": {"state": "online"}, "d2": {"state": "online"}},
    )
    assert "states" not in govde["devices"]
    assert "states_truncated" not in govde["devices"]


def test_bilinmeyen_durum_online_sayilmaz() -> None:
    """`state` eksik/bos gelirse cihaz SORUNLU tarafta olmali.

    Ters varsayim tehlikeli: eksik veriyi "iyi" saymak, gozden kacan cihaz
    demektir — bu ozelligin var olma sebebinin tam tersi.
    """
    govde = health_header.build_payload(
        status="ok",
        device_summary={"total": 2},
        per_device={"d1": {}, "d2": {"state": None}},
    )
    assert govde["devices"]["states"] == {"d1": "unknown", "d2": "unknown"}


def test_uzun_liste_kirpilir_ve_kirpildigi_bildirilir() -> None:
    per = {f"d{i:03d}": {"state": "lost"} for i in range(200)}
    govde = health_header.build_payload(
        status="degraded", device_summary={"total": 600, "lost": 200}, per_device=per
    )
    assert len(govde["devices"]["states"]) == health_header.MAX_STATE_ENTRIES
    # Sessiz kirpma backend'e "geri kalan her sey iyi" dedirtirdi.
    assert govde["devices"]["states_truncated"] is True


def test_surum_anahtari_backendin_okudugu_addir() -> None:
    """Yayindaki backend `version` okuyor; `gateway_version` degil."""
    govde = health_header.build_payload(
        status="ok", device_summary=None, per_device=None, gateway_version="1.2.3"
    )
    assert govde["version"] == "1.2.3"


# --------------------------------------------------------------------------
# Kodlama ve boyut tavani
# --------------------------------------------------------------------------


def test_en_kotu_durum_tavanin_altinda_kalir() -> None:
    """600 cihazin 600'u da kayip — baslik yine de tavani asmamali."""
    per = {f"device-{i:04d}": {"state": "lost"} for i in range(600)}
    govde = health_header.build_payload(
        status="degraded",
        device_summary={"total": 600, "online": 0, "lost": 600},
        per_device=per,
        outbox_pending=123456,
        outbox_dead_letter=99,
        uptime_sec=987654,
        gateway_version="0.0.0-test",
        issues=["x" * 300] * 20,
    )
    baslik = health_header.encode_header(govde)
    assert baslik is not None
    assert len(baslik) <= health_header.MAX_HEADER_BYTES
    # Kirpilsa bile SAYIMLAR duruyor: "600 cihazin 600'u kayip" sorusu
    # cihaz listesi olmadan da cevaplanabilir olmali.
    assert _coz(baslik)["devices"]["lost"] == 600


def test_devasa_states_dusurulur_sayimlar_kalir() -> None:
    """Tek basina `states` tavani asarsa liste dusulur, govde yine gider."""
    devasa = {f"{'u' * 300}-{i}": "lost" for i in range(20)}
    govde = {"status": "ok", "devices": {"total": 9, "lost": 9, "states": devasa}}
    baslik = health_header.encode_header(govde)
    assert baslik is not None
    cozulmus = _coz(baslik)
    assert "states" not in cozulmus["devices"]
    assert cozulmus["devices"]["states_truncated"] is True
    assert cozulmus["devices"]["lost"] == 9


def test_kodlanamayan_govde_istisna_firlatmaz() -> None:
    assert health_header.encode_header({"x": object()}) is None


# --------------------------------------------------------------------------
# `/pending` ile birlesim — komut kanali kutsal
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        body = json.dumps(payload)
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.content)),
        }

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int = 65536, decode_unicode: bool = False):  # noqa: ARG002
        if self.content:
            yield self.content

    def close(self) -> None:
        return None


class _KaydedenSession:
    """Istegin basliklarini yakalar — asil dogrulanacak sey o."""

    def __init__(self) -> None:
        self.gonderilen_headers: dict[str, str] = {}
        self.cagri_sayisi = 0

    def get(self, url: str, **kwargs: Any) -> _Resp:  # noqa: ARG002
        self.cagri_sayisi += 1
        self.gonderilen_headers = dict(kwargs.get("headers") or {})
        return _Resp({"commands": [], "config_nonce": 1, "refresh_nonce": 1})

    def post(self, url: str, **kwargs: Any) -> _Resp:  # noqa: ARG002
        return _Resp({})


def _client(session: _KaydedenSession, saglayici: Any) -> BackendConfigClient:
    return BackendConfigClient(
        base_url="http://backend/api/v1",
        identity=GatewayIdentity(
            gateway_code="GW-001",
            token="tok",
            instance_id="test-instance",
            app_version="0.0.0-test",
            app_environment="development",
        ),
        session=session,  # type: ignore[arg-type]
        health_provider=saglayici,
    )


def test_saglik_basligi_pending_istegine_biner() -> None:
    session = _KaydedenSession()
    govde = health_header.build_payload(
        status="ok",
        device_summary={"total": 3, "online": 2, "lost": 1},
        per_device={"d1": {"state": "online"}, "d9": {"state": "lost"}},
    )
    _client(session, lambda: govde).fetch_pending_commands()

    ham = session.gonderilen_headers[health_header.HEADER_NAME]
    assert _coz(ham)["devices"]["states"] == {"d9": "lost"}


def test_saglayici_yoksa_baslik_hic_eklenmez() -> None:
    """Geriye donuk uyum: saglayici verilmeyen client eskisi gibi davranir."""
    session = _KaydedenSession()
    _client(session, None).fetch_pending_commands()
    assert health_header.HEADER_NAME not in session.gonderilen_headers


def test_saglayici_patlarsa_komut_pollu_dusmez() -> None:
    session = _KaydedenSession()

    def _patlayan() -> dict[str, Any]:
        raise RuntimeError("adapter kilitlendi")

    poll = _client(session, _patlayan).fetch_pending_commands()

    # Istek GITTI ve yanit islendi — saglik hatasi komutu engellemedi.
    assert session.cagri_sayisi == 1
    assert poll is not None
    assert health_header.HEADER_NAME not in session.gonderilen_headers


def test_saglayici_sacma_dondururse_baslik_eklenmez() -> None:
    session = _KaydedenSession()
    _client(session, lambda: "cihazlar iyi").fetch_pending_commands()
    assert session.cagri_sayisi == 1
    assert health_header.HEADER_NAME not in session.gonderilen_headers


def test_kimlik_basliklari_saglik_yuzunden_bozulmaz() -> None:
    """Saglik basligi eklenirken auth basliklari EZILMEMELI."""
    session = _KaydedenSession()
    _client(session, lambda: {"status": "ok", "devices": {}}).fetch_pending_commands()
    assert session.gonderilen_headers.get("X-Gateway-Token") == "tok"
    assert session.gonderilen_headers.get("X-Gateway-Code") == "GW-001"
    assert health_header.HEADER_NAME in session.gonderilen_headers
