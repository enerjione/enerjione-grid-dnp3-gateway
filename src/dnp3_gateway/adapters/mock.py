"""Mock telemetri okuyucu.

Gercek DNP3 cihazi olmadan gateway'in baski testleri, tag-engine ile
integrasyon dogrulamasi ve frontend gelistirme icin kullanilabilir.

Ureten degerler tipik orta-gerilim sebeke sinyalleri icin makul araliklarda
(voltaj, akim, ariza akimi, sayac, binary status) — `signal.key` icindeki
anahtar kelimelere gore mantikli mock degerleri uretir. Saha tek tek sensor
karakteristigi degildir; uretici kim olursa olsun sinyal isimlendirme
konvansiyonuna gore eslesir.
"""

from __future__ import annotations

import random
from typing import Any

from dnp3_gateway.adapters.base import SignalReading, TelemetryReader
from dnp3_gateway.backend import DeviceConfig, SignalConfig


class MockTelemetryReader(TelemetryReader):
    def __init__(self, *, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def read_device(
        self,
        *,
        device: DeviceConfig,
        signals: list[SignalConfig],
    ) -> list[SignalReading]:
        _ = device
        return [self._generate(signal) for signal in signals]

    def operate_device(
        self,
        *,
        device: DeviceConfig,
        index: int,
        op_type: str = "latch_on",
        count: int = 1,
        on_time_ms: int = 0,
        off_time_ms: int = 0,
        timeout_sec: float = 10.0,
        mode: str = "direct",
    ) -> dict[str, Any]:
        # Gercek link yok; komutu her zaman basarili kabul et (test/dev).
        return {
            "ok": True,
            "status": "ok",
            "device_code": device.code,
            "index": int(index),
            "op_type": op_type,
            "count": int(count),
            "on_time_ms": int(on_time_ms),
            "off_time_ms": int(off_time_ms),
            "timeout_sec": float(timeout_sec),
            "mock": True,
        }

    # ------------------------------------------------------------------ impl
    def _generate(self, signal: SignalConfig) -> SignalReading:
        raw, quality = self._mock_value_for(signal)
        scaled = raw * signal.scale + signal.offset
        return SignalReading(
            signal_key=signal.key,
            source=signal.source,
            data_type=signal.data_type,
            raw_value=raw,
            scaled_value=round(scaled, 4),
            quality=quality,
        )

    def _mock_value_for(self, signal: SignalConfig) -> tuple[float, str]:
        key = signal.key.lower()
        data_type = signal.data_type

        if data_type in {"binary", "binary_output"}:
            return (1.0 if self._rng.random() < 0.04 else 0.0), "good"
        if data_type == "counter":
            return float(self._rng.randint(0, 50)), "good"
        if data_type == "string":
            return 0.0, "good"

        # analog / analog_output - tipik OG (orta gerilim) saha araliklari
        if "actual_current" in key or "average_current" in key:
            return self._rng.uniform(0, 1500), "good"
        if "fault_current" in key:
            return self._rng.uniform(0, 8000), "good"
        if "min_current" in key or "minimum_current" in key:
            return self._rng.uniform(0, 200), "good"
        if "max_current" in key or "maximum_current" in key:
            return self._rng.uniform(500, 2500), "good"
        if "trip_level" in key or "trip_current" in key:
            return self._rng.uniform(400, 800), "good"
        if "last_good_known_current" in key:
            return self._rng.uniform(50, 1200), "good"
        if "fault_duration" in key:
            return self._rng.uniform(30, 400), "good"
        if any(k in key for k in ("actual_voltage", "minimum_voltage", "maximum_voltage", "nominal_voltage")):
            return self._rng.uniform(10000, 11500), "good"
        if "battery_voltage" in key:
            return self._rng.uniform(320, 420), "good"
        if "conductor_temperature" in key:
            return self._rng.uniform(2000, 8500), "good"
        if "device_temperature" in key:
            return self._rng.uniform(-500, 4500), "good"
        if "phase_angle" in key:
            return self._rng.uniform(-1800, 1800), "good"
        if "pitch_angle" in key:
            return self._rng.uniform(-4500, 4500), "good"
        if "modem_rssi" in key or "rssi_satellite" in key:
            return self._rng.uniform(-120, -60), "good"
        if "latitude" in key or "longitude" in key:
            return self._rng.uniform(0, 60), "good"
        if "reporting_period" in key:
            return 15.0, "good"
        if "test_point_level" in key:
            return self._rng.uniform(0, 100), "good"
        if "instantaneous_factor" in key:
            return self._rng.uniform(1, 10), "good"

        return self._rng.uniform(0, 100), "good"
