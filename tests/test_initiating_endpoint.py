"""G-INIT-02 — initiating uc: fail-closed config + host port yayini.

KAPATILAN URETIM RISKLERI
-------------------------
1. `ip_endpoint_type` YAZIM HATASI SESSIZCE `listening`e DUSUYORDU.
   Bu iki degerin anlami BIRBIRININ TERSI: kimin baglanti actigini belirler.
   `"initating"` -> `listening` demek, gateway'in uyuyan bir Horstmann'a TCP
   client olarak baglanmaya calismasi demektir; cihaz hicbir zaman gelmez ve
   saha bunu yalnizca "cihaz yok" olarak gorur.

2. `master_ip_port` EKSIK/BOZUKKEN SESSIZCE `None` OLUYORDU ve adapter
   `DNP3_TCP_PORT`e (20000) dusuyordu — yani TUM initiating cihazlar ayni
   portu bind etmeye calisiyor, ilki disindakiler anlasilmaz bir soket
   hatasiyla dusuyordu.

3. COMPOSE SABLONU YALNIZCA HEALTH PORTUNU YAYINLIYORDU. Bridge ag modunda
   cihazin `master_ip_port`a yaptigi baglanti container'a HIC ULASMIYORDU.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from dnp3_gateway.backend import GatewayConfigError
from dnp3_gateway.backend.config_client import (
    IP_ENDPOINT_TYPES,
    MASTER_IP_PORT_MAX,
    MASTER_IP_PORT_MIN,
    _parse_gateway_config,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from render_compose import (  # noqa: E402
    INITIATING_PORT_MAX_TOTAL,
    RenderError,
    parse_initiating_ports,
    render_compose,
)

_KOK = Path(__file__).resolve().parents[1]


def _cihaz(**kw: Any) -> dict[str, Any]:
    ham: dict[str, Any] = {"code": "D1", "ip_address": "10.0.0.10", "dnp3_address": 1}
    ham.update(kw)
    return ham


def _parse(*cihazlar: dict[str, Any]):
    return _parse_gateway_config(
        {"config_version": "v1", "devices": list(cihazlar), "signals": []},
        default_gateway_code="GW-001",
    )


# ==========================================================================
# 01-09 — CONFIG DOGRULAMASI (fail-closed)
# ==========================================================================


def test_01_listening_yeni_alanlar_olmadan_degismedi() -> None:
    """Eski config (yeni alan YOK) -> bugunku davranis birebir."""
    cfg = _parse(_cihaz())
    d = cfg.devices[0]
    assert d.ip_endpoint_type == "listening"
    assert d.master_ip_port is None
    assert d.session_policy == "continuous"


def test_02_initiating_kabul_ve_port_tasinir() -> None:
    cfg = _parse(_cihaz(ip_endpoint_type="initiating", master_ip_port=20100))
    assert cfg.devices[0].ip_endpoint_type == "initiating"
    assert cfg.devices[0].master_ip_port == 20100


def test_02b_initiating_adapter_tcp_server_kullanir() -> None:
    """Uc tipi -> `AddTCPServer` (cihaz baglanir), `AddTCPClient` DEGIL."""
    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    from dataclasses import replace

    from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

    from .conftest import make_device

    cagrilar: dict[str, Any] = {}

    class _Master:
        def AddClassScan(self, *a, **k):  # noqa: N802
            return object()

        def Enable(self):  # noqa: N802
            pass

    class _Kanal:
        def AddMaster(self, *a, **k):  # noqa: N802
            return _Master()

    class _Manager:
        def AddTCPServer(self, ad, lvl, kabul, endpoint, dinleyici):  # noqa: N802, ARG002
            cagrilar["server_port"] = endpoint.port
            return _Kanal()

        def AddTCPClient(self, *a, **k):  # noqa: N802
            cagrilar["client"] = True
            return _Kanal()

    device = replace(make_device("SN2-1"), ip_endpoint_type="initiating", master_ip_port=20100)
    mm = mod._ManagedMaster(
        _Manager(),
        device=device,
        local_address=1,
        tcp_port=20000,
        scan_interval_sec=5,
        baseline_interval_sec=30,
    )
    assert cagrilar.get("server_port") == 20100
    assert "client" not in cagrilar
    assert mm.listen_port == 20100


@pytest.mark.parametrize("deger", ["initating", "listenin", "server", "client", "INITIATING_X", "1"])
def test_03_04_gecersiz_uc_tipi_configu_dusurur(deger: str) -> None:
    """SESSIZCE `listening`e DUSMEK YASAK — kimin baglandigini tersine cevirir."""
    with pytest.raises(GatewayConfigError) as hata:
        _parse(_cihaz(ip_endpoint_type=deger))
    assert "ip_endpoint_type" in str(hata.value)


def test_03b_izin_verilen_uc_tipleri() -> None:
    assert IP_ENDPOINT_TYPES == frozenset({"listening", "initiating"})


@pytest.mark.parametrize("eksik", [None, "", 0])
def test_05_initiating_portsuz_configu_dusurur(eksik: Any) -> None:
    """Eskiden sessizce None olup DNP3_TCP_PORT'a dusuyordu."""
    cihaz = _cihaz(ip_endpoint_type="initiating")
    if eksik is not None:
        cihaz["master_ip_port"] = eksik
    with pytest.raises(GatewayConfigError) as hata:
        _parse(cihaz)
    assert "master_ip_port" in str(hata.value)


@pytest.mark.parametrize("port", [-1, 0, 1, 80, 443, 1023])
def test_06_dusuk_port_reddedilir(port: int) -> None:
    """Ayricalikli portlar: container root olmayan kullaniciyla kosar."""
    with pytest.raises(GatewayConfigError):
        _parse(_cihaz(ip_endpoint_type="initiating", master_ip_port=port))


@pytest.mark.parametrize("port", [65536, 70000, 999999])
def test_07_yuksek_port_reddedilir(port: int) -> None:
    with pytest.raises(GatewayConfigError):
        _parse(_cihaz(ip_endpoint_type="initiating", master_ip_port=port))


@pytest.mark.parametrize("port", [1024, 20100, 65535])
def test_07b_sinir_portlari_kabul(port: int) -> None:
    cfg = _parse(_cihaz(ip_endpoint_type="initiating", master_ip_port=port))
    assert cfg.devices[0].master_ip_port == port
    assert MASTER_IP_PORT_MIN == 1024
    assert MASTER_IP_PORT_MAX == 65535


def test_07c_bozuk_tip_reddedilir() -> None:
    with pytest.raises(GatewayConfigError):
        _parse(_cihaz(ip_endpoint_type="initiating", master_ip_port="abc"))


def test_08_ayni_gatewayde_cakisan_port_reddedilir() -> None:
    """Ikinci master calisma zamaninda anlasilmaz bir soket hatasiyla dusmemeli."""
    with pytest.raises(GatewayConfigError) as hata:
        _parse(
            _cihaz(code="A", ip_endpoint_type="initiating", master_ip_port=20100),
            _cihaz(code="B", ip_address="10.0.0.11", ip_endpoint_type="initiating", master_ip_port=20100),
        )
    mesaj = str(hata.value)
    assert "20100" in mesaj and "cakis" in mesaj.lower()


def test_09_farkli_portlu_iki_initiating_kabul() -> None:
    cfg = _parse(
        _cihaz(code="A", ip_endpoint_type="initiating", master_ip_port=20100),
        _cihaz(code="B", ip_address="10.0.0.11", ip_endpoint_type="initiating", master_ip_port=20101),
    )
    assert [d.master_ip_port for d in cfg.devices] == [20100, 20101]


def test_09b_listening_cihazlarda_port_cakismasi_aranmaz() -> None:
    """`listening` cihazda `master_ip_port` ANLAMSIZDIR; dinleyici acilmaz."""
    cfg = _parse(
        _cihaz(code="A", master_ip_port=20100),
        _cihaz(code="B", ip_address="10.0.0.11", master_ip_port=20100),
    )
    assert all(d.master_ip_port is None for d in cfg.devices)


# ==========================================================================
# 10-12 — RENDER: host port yayini
# ==========================================================================


def _render(**kw: Any) -> str:
    ham: dict[str, Any] = {
        "code": "GW-001",
        "token": "x" * 40,
        "name": "GW",
        "backend_url": "https://api.local/api/v1",
        "nats_url": "nats://n:4222",
        "host_port": 8020,
        "install_mode": "remote",
    }
    ham.update(kw)
    return render_compose(**ham)


def _ports(yaml_text: str) -> list[str]:
    return yaml.safe_load(yaml_text)["services"]["gateway"]["ports"]


def test_10_render_initiating_tcp_yayinini_iceriyor() -> None:
    """ASIL DUZELTME: bridge ag modunda cihaz container'a ancak boyle ulasir."""
    ports = _ports(_render(initiating_ports="20100-20199"))
    assert "127.0.0.1:8020:8020" in ports
    assert "0.0.0.0:20100-20199:20100-20199" in ports


def test_10b_esleme_kimliktir() -> None:
    """host portu == container portu == master_ip_port.

    Farkli olsaydi cihaz `master_ip_port`a baglanir ama gateway baska bir
    portu dinlerdi; zincir sessizce kopardi.
    """
    for giris in ("20100", "20100-20199", "20100-20149,20300"):
        for esleme in _ports(_render(initiating_ports=giris)):
            if esleme.startswith("127.0.0.1"):
                continue
            _bind, host_taraf, container_taraf = esleme.split(":")
            assert host_taraf == container_taraf, esleme


def test_10c_blok_verilmezse_yayin_yok_geriye_uyum() -> None:
    """Yalnizca `listening` cihazi olan kurulumlar ETKILENMEZ."""
    assert _ports(_render()) == ["127.0.0.1:8020:8020"]
    assert _ports(_render(initiating_ports="")) == ["127.0.0.1:8020:8020"]


def test_10d_bind_arayuzu_daraltilabilir() -> None:
    ports = _ports(_render(initiating_ports="20100-20109", initiating_bind_host="10.0.0.5"))
    assert "10.0.0.5:20100-20109:20100-20109" in ports


def test_10e_uretilen_yaml_gecerli_ve_deterministik() -> None:
    a = _render(initiating_ports="20100-20199")
    b = _render(initiating_ports="20100-20199")
    assert a == b, "render deterministik degil"
    yaml.safe_load(a)  # parse edilebilir olmali


@pytest.mark.parametrize(
    "ham",
    ["20100-20099", "abc", "20100-", "-20100", "1023", "65536", "20100-20199-20200", " , ", "20a-20b"],
)
def test_11_gecersiz_port_ifadesi_render_error(ham: str) -> None:
    """Bicimsiz metin oldugu gibi YAML'e KOPYALANMAZ."""
    with pytest.raises(RenderError):
        parse_initiating_ports(ham)


def test_12_cakisan_araliklar_reddedilir() -> None:
    with pytest.raises(RenderError) as hata:
        parse_initiating_ports("20100-20199,20150-20250")
    assert "cakis" in str(hata.value).lower()


def test_12b_saglik_portuyla_cakisma_reddedilir() -> None:
    with pytest.raises(RenderError) as hata:
        _render(host_port=20150, initiating_ports="20100-20199")
    assert "20150" in str(hata.value)


def test_12c_asiri_genis_blok_reddedilir() -> None:
    """Docker her port icin bir docker-proxy sureci baslatir."""
    with pytest.raises(RenderError) as hata:
        parse_initiating_ports(f"1024-{1024 + INITIATING_PORT_MAX_TOTAL}")
    assert "docker-proxy" in str(hata.value)


def test_12d_cli_render_calisiyor() -> None:
    """CLI yolu da (backend bunu cagiriyor) calismali."""
    sonuc = subprocess.run(
        [
            sys.executable,
            str(_KOK / "scripts/render_compose.py"),
            "--code",
            "GW-002",
            "--token",
            "y" * 40,
            "--backend-url",
            "https://a/api/v1",
            "--nats-url",
            "nats://n:4222",
            "--host-port",
            "8021",
            "--install-mode",
            "remote",
            "--initiating-ports",
            "20200-20299",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "0.0.0.0:20200-20299:20200-20299" in sonuc.stdout
    yaml.safe_load(sonuc.stdout)


def test_12e_cli_gecersiz_blok_hata_kodu_doner() -> None:
    sonuc = subprocess.run(
        [
            sys.executable,
            str(_KOK / "scripts/render_compose.py"),
            "--code",
            "GW-002",
            "--token",
            "y" * 40,
            "--backend-url",
            "https://a/api/v1",
            "--nats-url",
            "nats://n:4222",
            "--host-port",
            "8021",
            "--install-mode",
            "remote",
            "--initiating-ports",
            "20200-20100",
        ],
        capture_output=True,
        text=True,
    )
    assert sonuc.returncode != 0


def test_12f_iki_gateway_ayrik_bloklarla_render_edilebilir() -> None:
    """Ayni HOST'ta N gateway: bloklar AYRIK olmali (deployment sorumlulugu).

    Gateway prosesi kardes instance'lari GOREMEZ; burada dogrulanan sey
    renderlayicinin ayrik bloklari dogru uretebildigidir.
    """
    gw1 = _ports(_render(code="GW-001", host_port=8020, initiating_ports="20100-20199"))
    gw2 = _ports(_render(code="GW-002", host_port=8021, initiating_ports="20200-20299"))
    assert "0.0.0.0:20100-20199:20100-20199" in gw1
    assert "0.0.0.0:20200-20299:20200-20299" in gw2
    assert not (set(gw1) & set(gw2) - {"127.0.0.1:8020:8020"})


# ==========================================================================
# 13 — DINLEYICI TANILAMA
# ==========================================================================


def test_13_bind_hatasi_cihaz_bazinda_izole_ve_gorunur() -> None:
    """Bind hatasi TUM gateway'i dusurmemeli ama cihazi SAGLIKLI gostermemeli."""
    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    from dataclasses import replace

    from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

    from .conftest import make_device

    r = mod.Yadnp3TelemetryReader.__new__(mod.Yadnp3TelemetryReader)
    r._init_runtime_state()
    device = replace(make_device("SN2-1"), ip_endpoint_type="initiating", master_ip_port=20100)

    r._dinleyici_hatasi_kaydet(device, OSError("address already in use"))
    hatalar = r.listener_errors()
    assert "SN2-1" in hatalar
    assert hatalar["SN2-1"]["port"] == 20100

    saglik = r.device_health()
    assert saglik["SN2-1"]["state"] == "listener_error"
    assert saglik["SN2-1"]["connected"] is False
    assert saglik["SN2-1"]["reachable"] is False
    assert saglik["SN2-1"]["listener_port"] == 20100
    assert "already in use" in saglik["SN2-1"]["listener_error"]
    # Token/kimlik SIZMAZ.
    assert "token" not in str(saglik).lower()

    # Dinleyici acilinca kayit DUSER.
    r._dinleyici_hatasi_temizle("SN2-1")
    assert r.listener_errors() == {}
    assert "SN2-1" not in r.device_health()


def test_13b_bind_hatasi_health_ozetinde_ayri_sayilir() -> None:
    """Bu bir HABERLESME arizasi degil KURULUM arizasi; ayri sayilmali."""
    from dnp3_gateway.health_server import _device_health_snapshot

    class _R:
        def device_health(self):
            return {"A": {"state": "listener_error", "session_policy": "unknown"}}

    ozet = _device_health_snapshot(_R(), 1)
    assert ozet["listener_error"] == 1
    assert ozet["lost"] == 0, "kurulum hatasi haberlesme kopmasi olarak sayilmis"
    assert ozet["online"] == 0


def test_13c_master_ip_port_health_te_gorunur() -> None:
    """Teshis icin gerekli; gizli bilgi degil."""
    pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")
    from dnp3_gateway.adapters import dnp3_yadnp3_master as mod

    alanlar = ("ip_endpoint_type", "master_ip_port", "listener_expected", "listener_port")
    kaynak = Path(mod.__file__).read_text(encoding="utf-8")
    for alan in alanlar:
        assert f'"{alan}"' in kaynak, f"/health `{alan}` tasimiyor"


# ==========================================================================
# SABLON / SOZLESME
# ==========================================================================


def test_sablon_gecerli_yaml_kalmali() -> None:
    """Sablonun KENDISI parse edilebilir olmali (testler/editorler okuyor)."""
    ham = (_KOK / "docker/compose.template.yml").read_text(encoding="utf-8")
    yaml.safe_load(re.sub(r"\{\{[A-Z_]+\}\}", "yer-tutucu", ham))


def test_sozlesme_initiating_bolumu_kodla_uyumlu() -> None:
    import json

    d = json.loads((_KOK / "docker/gateway-deployment-contract.json").read_text(encoding="utf-8"))
    ie = d["initiating_endpoint"]
    assert ie["port_min"] == MASTER_IP_PORT_MIN
    assert ie["port_max"] == MASTER_IP_PORT_MAX
    assert ie["max_published_ports_per_gateway"] == INITIATING_PORT_MAX_TOTAL
    assert ie["privileged_ports"] == "rejected"
    assert "identity" in ie["host_port_mapping"]
    # Host-port tekilligi sorumlulugu ACIKCA yazilmali.
    assert "ORKESTRASYON" in ie["host_port_uniqueness_scope"].upper()
