"""Config SARTLI istekle cekilir — degismediyse agdan veri INMEZ.

YASANAN DURUM (bu testler yazilmadan once)
------------------------------------------
Backend config yaniti icin ETag uretiyordu (`config_version` = payload'in
hash'i) ve `If-None-Match` eslesirse 304 donuyordu. Gateway bu header'i
GONDERMIYORDU — yani backend'deki 304 yolu HIC calismadi ve her config
yoklamasinda 193 sinyal tel uzerinden indi, her seferinde yeniden
ayristirildi.

Dahasi 304 gelse `status_code != 200` kontrolune takilip HATA sayilacakti:
sartli istek "calissa" bile config yenileme dongusu kirilirdi.

SIMDI
-----
Ilk cagri tam indirir ve ETag'i saklar. Sonraki cagrilar yalnizca "degisti mi"
diye sorar; degismediyse body yok, imza dogrulamasi yok, JSON ayristirmasi yok.
Degistiginde tam payload iner ve onbellek tazelenir.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dnp3_gateway.auth import GatewayIdentity
from dnp3_gateway.backend import BackendConfigClient, GatewayConfigError


def _kimlik() -> GatewayIdentity:
    return GatewayIdentity(
        gateway_code="GW-001",
        token="tok",
        instance_id="test-instance",
        app_version="0.0.0-test",
        app_environment="development",
    )


_PAYLOAD_V1: dict[str, Any] = {
    "gateway_code": "GW-001",
    "config_version": "surum-1",
    "is_active": True,
    "devices": [],
    "signals": [{"key": "master.current", "data_type": "analog"}],
}
_PAYLOAD_V2: dict[str, Any] = {
    **_PAYLOAD_V1,
    "config_version": "surum-2",
    "signals": [
        {"key": "master.current", "data_type": "analog"},
        {"key": "master.voltage", "data_type": "analog"},
    ],
}


class _Yanit:
    def __init__(self, status_code: int, payload: Any = None, etag: str | None = None) -> None:
        self.status_code = status_code
        govde = json.dumps(payload) if payload is not None else ""
        self.content = govde.encode("utf-8")
        self.text = govde
        self.headers: dict[str, str] = {"Content-Length": str(len(self.content))}
        if etag:
            self.headers["ETag"] = etag
        self.kapatildi = False

    def iter_content(self, chunk_size: int = 65536, decode_unicode: bool = False):  # noqa: ARG002
        if self.content:
            yield self.content

    def close(self) -> None:
        self.kapatildi = True


class _Oturum:
    """Sirayla verilen yanitlari dondurur; gonderilen header'lari kaydeder."""

    def __init__(self, yanitlar: list[_Yanit]) -> None:
        self._yanitlar = list(yanitlar)
        self.istek_headerlari: list[dict[str, str]] = []
        self.indirilen_bayt = 0

    def get(self, url: str, headers=None, timeout: int = 5, stream: bool = False, **_k: Any):  # noqa: ARG002
        self.istek_headerlari.append(dict(headers or {}))
        yanit = self._yanitlar.pop(0)
        self.indirilen_bayt += len(yanit.content)
        return yanit


def _istemci(oturum: _Oturum) -> BackendConfigClient:
    return BackendConfigClient(
        base_url="http://api/api/v1",
        identity=_kimlik(),
        session=oturum,  # type: ignore[arg-type]
    )


def test_ilk_cagri_sartsiz_gider():
    """Elde config yokken If-None-Match gonderilmemeli.

    Gonderilseydi ve 304 gelseydi donecek hicbir sey olmazdi.
    """
    oturum = _Oturum([_Yanit(200, _PAYLOAD_V1, etag='"surum-1"')])
    cfg = _istemci(oturum).fetch_config()

    assert cfg.config_version == "surum-1"
    assert "If-None-Match" not in oturum.istek_headerlari[0]


def test_ikinci_cagri_sartli_gider_ve_304te_veri_inmez():
    """Testin ozu: degismemis config icin agdan bayt inmemeli."""
    oturum = _Oturum(
        [
            _Yanit(200, _PAYLOAD_V1, etag='"surum-1"'),
            _Yanit(304),  # govde YOK
        ]
    )
    istemci = _istemci(oturum)

    istemci.fetch_config()
    ilk_bayt = oturum.indirilen_bayt
    assert ilk_bayt > 0

    ikinci = istemci.fetch_config()

    assert oturum.istek_headerlari[1].get("If-None-Match") == '"surum-1"'
    assert oturum.indirilen_bayt == ilk_bayt, "304'te veri inmis"
    assert ikinci.config_version == "surum-1"
    assert [s.key for s in ikinci.signals] == ["master.current"]


def test_304_hata_sayilmaz():
    """Once `status_code != 200` kontrolune takilip exception atiyordu."""
    oturum = _Oturum([_Yanit(200, _PAYLOAD_V1, etag='"surum-1"'), _Yanit(304)])
    istemci = _istemci(oturum)
    istemci.fetch_config()
    istemci.fetch_config()  # patlarsa test kirmizi


def test_degisince_tam_payload_iner_ve_onbellek_tazelenir():
    """ "Degisirse yine oku" tarafi."""
    oturum = _Oturum(
        [
            _Yanit(200, _PAYLOAD_V1, etag='"surum-1"'),
            _Yanit(200, _PAYLOAD_V2, etag='"surum-2"'),
            _Yanit(304),
        ]
    )
    istemci = _istemci(oturum)

    istemci.fetch_config()
    ikinci = istemci.fetch_config()
    assert [s.key for s in ikinci.signals] == ["master.current", "master.voltage"]

    # Ucuncu cagri ARTIK YENI etag ile sartli gitmeli; eskisi gonderilseydi
    # backend surekli tam payload dondururdu (kazanc sifir).
    ucuncu = istemci.fetch_config()
    assert oturum.istek_headerlari[2].get("If-None-Match") == '"surum-2"'
    assert [s.key for s in ucuncu.signals] == ["master.current", "master.voltage"]


def test_etagsiz_backend_ile_davranis_degismez():
    """Eski backend ETag gondermiyorsa her cagri tam iner — bozulma yok."""
    oturum = _Oturum([_Yanit(200, _PAYLOAD_V1), _Yanit(200, _PAYLOAD_V2)])
    istemci = _istemci(oturum)

    istemci.fetch_config()
    ikinci = istemci.fetch_config()

    assert "If-None-Match" not in oturum.istek_headerlari[1]
    assert [s.key for s in ikinci.signals] == ["master.current", "master.voltage"]


def test_bozuk_payload_etagi_saklanmaz():
    """Kritik sira: onbellek yalnizca BASARILI ayristirmadan sonra tazelenir.

    Once yazsaydik, bozuk bir payload'in ETag'i saklanir ve sonraki cagrilar
    304 alip bozuk config'i "gecerli" saymaya devam ederdi.
    """
    bozuk = {"config_version": "kotu", "signals": "liste degil"}
    oturum = _Oturum(
        [
            _Yanit(200, _PAYLOAD_V1, etag='"surum-1"'),
            _Yanit(200, bozuk, etag='"kotu"'),
            _Yanit(304),
        ]
    )
    istemci = _istemci(oturum)
    istemci.fetch_config()

    with pytest.raises(GatewayConfigError):
        istemci.fetch_config()

    # Onbellek hala SAGLAM surumu tutuyor olmali
    ucuncu = istemci.fetch_config()
    assert oturum.istek_headerlari[2].get("If-None-Match") == '"surum-1"', "bozuk payload'in etag'i saklanmis"
    assert ucuncu.config_version == "surum-1"


def test_304_govdesi_kapatilir():
    """Baglanti havuzuna geri verilsin; sizinti olmasin."""
    uc_yuz_dort = _Yanit(304)
    oturum = _Oturum([_Yanit(200, _PAYLOAD_V1, etag='"surum-1"'), uc_yuz_dort])
    istemci = _istemci(oturum)
    istemci.fetch_config()
    istemci.fetch_config()
    assert uc_yuz_dort.kapatildi is True
