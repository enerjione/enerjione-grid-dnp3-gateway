from __future__ import annotations

from dataclasses import dataclass

from dnp3_gateway.adapters import SignalReading, TelemetryReader
from dnp3_gateway.poller import (
    build_telemetry_payload,
    filter_readable_signals,
    poll_device,
    run_poll_cycle,
)
from dnp3_gateway.state import GatewayState

from .conftest import make_device, make_gateway_config, make_signal


@dataclass
class _RecordedPublish:
    payload: dict
    message_id: str
    correlation_id: str | None
    headers: dict | None


class _StubPublisher:
    def __init__(self) -> None:
        self.calls: list[_RecordedPublish] = []

    def publish(self, payload, *, message_id, correlation_id=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append(_RecordedPublish(payload, message_id, correlation_id, headers))

    def close(self) -> None:
        pass


class _StubReader(TelemetryReader):
    def __init__(self, readings: list[SignalReading]) -> None:
        self._readings = readings
        self.read_calls = 0

    def read_device(self, *, device, signals):  # type: ignore[no-untyped-def]
        _ = device, signals
        self.read_calls += 1
        return list(self._readings)

    def operate_device(self, **kwargs):  # type: ignore[no-untyped-def]
        return {"ok": True, "status": "ok", **kwargs}


def test_filter_readable_signals_drops_commands_only() -> None:
    """Gateway 0.4.x: string sinyal (Group 110) artik yayinlanir; sadece
    binary_output (master->outstation komut kanali) yayindan haric tutulur."""
    signals = [
        make_signal("a", data_type="analog"),
        make_signal("b", data_type="binary"),
        make_signal("c", data_type="counter"),
        make_signal("d", data_type="binary_output"),  # KOMUT — dahil edilmez
        make_signal("e", data_type="string"),         # Group 110 — dahil edilir
        make_signal("f", data_type="analog_output"),  # analog setpoint okuma — dahil
    ]
    keys = [s.key for s in filter_readable_signals(signals)]
    assert keys == ["a", "b", "c", "e", "f"]


def test_build_telemetry_payload_shape() -> None:
    device = make_device("DEV-1")
    reading = SignalReading(
        signal_key="master.actual_current",
        source="master",
        data_type="analog",
        raw_value=1234.5,
        scaled_value=1234.5,
        quality="good",
    )
    payload = build_telemetry_payload(
        gateway_code="GW-001",
        device=device,
        reading=reading,
        correlation_id="corr-1",
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert payload["source_gateway"] == "GW-001"
    assert payload["device_code"] == "DEV-1"
    assert payload["signal_key"] == "master.actual_current"
    assert payload["signal_source"] == "master"
    assert payload["value"] == 1234.5
    assert payload["quality"] == "good"
    assert payload["correlation_id"] == "corr-1"
    assert payload["source_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert "message_id" in payload


def test_poll_device_publishes_each_reading() -> None:
    device = make_device("DEV-1")
    signal = make_signal("master.actual_current")
    readings = [
        SignalReading(signal.key, signal.source, signal.data_type, 1.0, 1.0, "good"),
        SignalReading("sat01.voltage", "sat01", "analog", 2.0, 2.0, "good"),
    ]
    reader = _StubReader(readings)
    publisher = _StubPublisher()

    published = poll_device(
        gateway_code="GW-001",
        device=device,
        signals=[signal],
        reader=reader,
        publisher=publisher,
    )
    assert published == 2
    assert len(publisher.calls) == 2
    # Her mesaj ayni correlation_id paylasmali
    corr_ids = {call.correlation_id for call in publisher.calls}
    assert len(corr_ids) == 1


def test_poll_device_swallows_reader_errors() -> None:
    class _BoomReader(TelemetryReader):
        def read_device(self, *, device, signals):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        def operate_device(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"ok": False, "status": "unsupported"}

    published = poll_device(
        gateway_code="GW-001",
        device=make_device(),
        signals=[make_signal()],
        reader=_BoomReader(),
        publisher=_StubPublisher(),
    )
    assert published == 0


def test_run_poll_cycle_skips_when_inactive() -> None:
    state = GatewayState()
    state.update(make_gateway_config(is_active=False))
    reader = _StubReader([])
    publisher = _StubPublisher()
    assert (
        run_poll_cycle(
            gateway_code="GW-001",
            state=state,
            reader=reader,
            publisher=publisher,
            now_monotonic=1.0,
        )
        == 0
    )
    assert reader.read_calls == 0


def test_run_poll_cycle_parallel_reads_all_due_devices() -> None:
    devices = [make_device(f"DEV-{i:03d}", poll_interval_sec=5) for i in range(6)]
    signal = make_signal("master.actual_current")
    state = GatewayState()
    state.update(make_gateway_config(devices=devices, signals=[signal]))

    class _CountingReader(TelemetryReader):
        def __init__(self) -> None:
            self.read_devices: list[str] = []
            self._lock_free_ok = True

        def read_device(self, *, device, signals):  # type: ignore[no-untyped-def]
            self.read_devices.append(device.code)
            return [SignalReading(signal.key, signal.source, signal.data_type, 1.0, 1.0, "good")]

        def operate_device(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"ok": True, "status": "ok"}

    reader = _CountingReader()
    publisher = _StubPublisher()

    published = run_poll_cycle(
        gateway_code="GW-001",
        state=state,
        reader=reader,
        publisher=publisher,
        now_monotonic=100.0,
        max_parallel=4,
    )
    assert published == 6
    assert sorted(reader.read_devices) == [d.code for d in devices]
    # Paralel sonrasi tum cihazlar `mark_read` edilmis olmali -> ayni tick'te
    # yeniden cagri 0 donmeli
    assert (
        run_poll_cycle(
            gateway_code="GW-001",
            state=state,
            reader=reader,
            publisher=publisher,
            now_monotonic=100.5,
            max_parallel=4,
        )
        == 0
    )


def test_run_poll_cycle_publishes_for_due_devices() -> None:
    device = make_device("DEV-A", poll_interval_sec=5)
    signal = make_signal("master.actual_current")
    state = GatewayState()
    state.update(make_gateway_config(devices=[device], signals=[signal]))

    reader = _StubReader([
        SignalReading(signal.key, signal.source, signal.data_type, 1.0, 1.0, "good"),
    ])
    publisher = _StubPublisher()

    published = run_poll_cycle(
        gateway_code="GW-001",
        state=state,
        reader=reader,
        publisher=publisher,
        now_monotonic=100.0,
    )
    assert published == 1
    assert len(publisher.calls) == 1

    # Ayni tick'te tekrar cagirilirsa due degil
    assert (
        run_poll_cycle(
            gateway_code="GW-001",
            state=state,
            reader=reader,
            publisher=publisher,
            now_monotonic=100.5,
        )
        == 0
    )
