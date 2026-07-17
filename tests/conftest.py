"""Ortak pytest fixture ve helper'lari."""

from __future__ import annotations

from dnp3_gateway.backend import DeviceConfig, GatewayConfig, SignalConfig


def make_signal(
    key: str = "master.actual_current",
    *,
    source: str = "master",
    data_type: str = "analog",
    object_group: int = 30,
    index: int = 0,
    scale: float = 1.0,
    offset: float = 0.0,
    supports_alarm: bool = False,
) -> SignalConfig:
    return SignalConfig(
        key=key,
        label=key,
        unit=None,
        source=source,
        dnp3_class="Class 1",
        data_type=data_type,
        dnp3_object_group=object_group,
        dnp3_index=index,
        scale=scale,
        offset=offset,
        supports_alarm=supports_alarm,
    )


def make_device(
    code: str = "DEV-001",
    *,
    ip_address: str = "10.0.0.10",
    dnp3_address: int = 1,
    dnp3_tcp_port: int | None = None,
    poll_interval_sec: int = 5,
) -> DeviceConfig:
    return DeviceConfig(
        code=code,
        name=code,
        ip_address=ip_address,
        dnp3_address=dnp3_address,
        dnp3_tcp_port=dnp3_tcp_port,
        poll_interval_sec=poll_interval_sec,
        timeout_ms=3000,
        retry_count=2,
        signal_profile="default",
    )


def make_gateway_config(
    *,
    devices: list[DeviceConfig] | None = None,
    signals: list[SignalConfig] | None = None,
    is_active: bool = True,
    version: str = "v1",
) -> GatewayConfig:
    return GatewayConfig(
        gateway_code="GW-001",
        gateway_name="Test GW",
        batch_interval_sec=5,
        max_devices=200,
        is_active=is_active,
        config_version=version,
        devices=devices or [make_device()],
        signals=signals or [make_signal()],
    )
