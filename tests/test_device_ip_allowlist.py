"""Cihaz IP allowlist (SSRF / ic-ag tarama korumasi) regresyon testleri.

Bu dosya, uretimde gateway'i kalici olarak config-cekemez hale getiren bir
hatanin (config_client icinde `_logging` NameError'u) geri gelmemesini garanti
eder. Uc kod yolu vardi ve HICBIRI test edilmemisti:

  1. `_parse_allowed_subnets` gecersiz CIDR yolu (warning log satiri)
  2. `_allowed_networks_cached` allowlist-dolu yolu (info log satiri)
  3. `fetch_pending_commands` bozuk-komut yolu (bkz. test_pending_commands.py)

Ayrica yeni FAIL-CLOSED davranisini kilitler: allowlist tanimlandi ama hicbir
giris gecerli degilse tum cihazlar reddedilir (eskiden sessizce herkes gecerdi).
"""

from __future__ import annotations

import pytest

from dnp3_gateway.backend import parse_device_ip_allowlist
from dnp3_gateway.backend.config_client import _parse_gateway_config


def _config_payload(*ips: str) -> dict:
    return {
        "gateway_code": "GW-001",
        "gateway_name": "Test",
        "batch_interval_sec": 5,
        "max_devices": 100,
        "is_active": True,
        "config_version": "v1",
        "devices": [
            {
                "code": f"DEV-{i}",
                "name": f"Device {i}",
                "ip_address": ip,
                "dnp3_address": 1,
            }
            for i, ip in enumerate(ips, start=1)
        ],
        "signals": [],
    }


# --------------------------------------------------------------------------
# parse_device_ip_allowlist
# --------------------------------------------------------------------------


def test_bos_allowlist_configured_degil() -> None:
    for raw in ("", "   ", None):
        al = parse_device_ip_allowlist(raw)
        assert al.configured is False
        assert al.networks == ()
        assert al.fail_closed is False
        # Allowlist yok -> geriye uyumlu: her IP gecer
        assert al.allows("8.8.8.8") is True


def test_gecerli_cidr_listesi_parse_edilir() -> None:
    al = parse_device_ip_allowlist("192.168.10.0/24, 10.0.5.0/24")
    assert al.configured is True
    assert len(al.networks) == 2
    assert al.invalid_entries == ()
    assert al.fail_closed is False
    assert al.allows("192.168.10.5") is True
    assert al.allows("10.0.5.200") is True
    assert al.allows("169.254.169.254") is False  # cloud metadata
    assert al.allows("8.8.8.8") is False


def test_gecersiz_giris_raporlanir_gecerliler_calisir() -> None:
    """Tek yazim hatasi NameError ATMAMALI; diger girisler calismaya devam etmeli."""
    al = parse_device_ip_allowlist("192.168.10.0/24,10.0.6.0/33,not-a-cidr")
    assert al.configured is True
    assert len(al.networks) == 1
    assert set(al.invalid_entries) == {"10.0.6.0/33", "not-a-cidr"}
    assert al.fail_closed is False
    assert al.allows("192.168.10.5") is True
    assert al.allows("10.0.6.5") is False


def test_hepsi_gecersizse_fail_closed() -> None:
    """GUVENLIK: allowlist istendi ama hicbiri gecerli degil -> HERKESI reddet.

    Eski davranis: sessizce bos liste -> allowlist devre disi -> tum IP'ler
    kabul. Bir yazim hatasi guvenlik kontrolunu tamamen kapatiyordu.
    """
    al = parse_device_ip_allowlist("10.0.6.0/33,bozuk,192.168.1.0/99")
    assert al.configured is True
    assert al.networks == ()
    assert al.fail_closed is True
    assert al.allows("192.168.10.5") is False
    assert al.allows("10.0.6.5") is False


def test_hostname_allowlist_aktifken_reddedilir() -> None:
    al = parse_device_ip_allowlist("192.168.10.0/24")
    assert al.allows("rtu-01.saha.local") is False


def test_bos_ip_initiating_modda_kabul_edilir() -> None:
    """ip_endpoint_type=initiating cihazlarin IP'si yoktur; allowlist onlari elemez."""
    al = parse_device_ip_allowlist("192.168.10.0/24")
    assert al.allows("") is True
    assert al.allows("   ") is True


# --------------------------------------------------------------------------
# _parse_gateway_config ile uctan uca
# --------------------------------------------------------------------------


def test_config_parse_allowlist_ile_patlamaz() -> None:
    """REGRESYON: allowlist dolu iken config parse NameError atiyordu."""
    al = parse_device_ip_allowlist("192.168.10.0/24")
    cfg = _parse_gateway_config(
        _config_payload("192.168.10.5", "8.8.8.8"),
        default_gateway_code="GW-001",
        allowlist=al,
    )
    codes = [d.code for d in cfg.devices]
    assert codes == ["DEV-1"]  # 8.8.8.8 allowlist disinda, elendi


def test_config_parse_gecersiz_cidr_ile_patlamaz() -> None:
    """REGRESYON: gecersiz CIDR yolunda NameError -> her config fetch basarisizdi."""
    al = parse_device_ip_allowlist("192.168.10.0/24,10.0.6.0/33")
    cfg = _parse_gateway_config(
        _config_payload("192.168.10.5"),
        default_gateway_code="GW-001",
        allowlist=al,
    )
    assert [d.code for d in cfg.devices] == ["DEV-1"]


def test_config_parse_fail_closed_tum_cihazlari_eler() -> None:
    al = parse_device_ip_allowlist("10.0.6.0/33")
    cfg = _parse_gateway_config(
        _config_payload("192.168.10.5", "10.0.5.1"),
        default_gateway_code="GW-001",
        allowlist=al,
    )
    assert cfg.devices == []


def test_config_parse_allowlist_yoksa_hepsi_gecer() -> None:
    cfg = _parse_gateway_config(
        _config_payload("192.168.10.5", "8.8.8.8"),
        default_gateway_code="GW-001",
        allowlist=parse_device_ip_allowlist(""),
    )
    assert len(cfg.devices) == 2


@pytest.mark.parametrize(
    "raw",
    [
        "192.168.10.0/24",
        "192.168.10.0/24,10.0.5.0/24",
        "10.0.6.0/33",
        "bozuk",
        "",
    ],
)
def test_hicbir_allowlist_girdisi_exception_atmaz(raw: str) -> None:
    """Hangi giris gelirse gelsin parse + kullanim exception atmamali."""
    al = parse_device_ip_allowlist(raw)
    al.allows("192.168.10.5")
    al.allows("")
    al.allows("hostname")
