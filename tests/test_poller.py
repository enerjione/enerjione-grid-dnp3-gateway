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


class _BatchStubPublisher:
    """publish_batch destekleyen stub — HTTP publisher gibi tek cagride toplar."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self.single_calls: list[_RecordedPublish] = []

    def publish_batch(self, items):  # type: ignore[no-untyped-def]
        self.batches.append(list(items))

    def publish(self, payload, *, message_id, correlation_id=None, headers=None):  # type: ignore[no-untyped-def]
        self.single_calls.append(_RecordedPublish(payload, message_id, correlation_id, headers))

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
        make_signal("e", data_type="string"),  # Group 110 — dahil edilir
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


def test_poll_device_uses_batch_when_supported() -> None:
    """publish_batch destekleyen publisher'da tum sinyaller TEK batch cagrisiyla
    gonderilir (N POST yerine 1). Fallback publish cagrilmaz."""
    device = make_device("DEV-1")
    signal = make_signal("master.actual_current")
    readings = [
        SignalReading(signal.key, signal.source, signal.data_type, 1.0, 1.0, "good"),
        SignalReading("sat01.voltage", "sat01", "analog", 2.0, 2.0, "good"),
        SignalReading("sat02.temp", "sat02", "analog", 3.0, 3.0, "good"),
    ]
    publisher = _BatchStubPublisher()
    published = poll_device(
        gateway_code="GW-001",
        device=device,
        signals=[signal],
        reader=_StubReader(readings),
        publisher=publisher,
    )
    assert published == 3
    assert len(publisher.batches) == 1  # tek batch cagrisi
    assert len(publisher.batches[0]) == 3  # 3 item icinde
    assert not publisher.single_calls  # tekli publish'e dusmedi
    # Her item beklenen alanlari icermeli
    for item in publisher.batches[0]:
        assert set(item) >= {"payload", "message_id", "correlation_id", "headers"}


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

    reader = _StubReader(
        [
            SignalReading(signal.key, signal.source, signal.data_type, 1.0, 1.0, "good"),
        ]
    )
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


def test_dnp3_flags_govdeye_giriyor() -> None:
    """`dnp3_flags` telemetri govdesinde OLMAK ZORUNDA.

    Backend kalitenin NOKTA mi CIHAZ seviyesinde mi oldugunu bu alanin
    VARLIGINDAN anliyor (schemas/telemetry.py + tag_engine_service):

        dnp3_flags is not None -> nokta seviyesi; `invalid`/`restart`/`forced`
                                  gelse bile cihaz ONLINE kalir
        dnp3_flags is None     -> cihaz seviyesi (legacy)

    Adapter bayragi okuyup `SignalReading`de tasiyordu ama govde onu
    dusuruyordu. Bu haliyle `DNP3_PUBLISH_QUALITY_FLAGS` acilsaydi TEK bir
    noktanin REFERENCE_ERR'i TUM CIHAZI OFFLINE yapardi.
    """
    device = make_device("DEV-1")
    reading = SignalReading(
        signal_key="master.actual_current",
        source="master",
        data_type="analog",
        raw_value=0.0,
        scaled_value=0.0,
        quality="invalid",
        dnp3_flags=0x21,  # ONLINE | REFERENCE_ERR
    )
    payload = build_telemetry_payload(
        gateway_code="GW-001",
        device=device,
        reading=reading,
        correlation_id="corr-1",
    )
    assert payload["dnp3_flags"] == 0x21
    assert payload["quality"] == "invalid"


def test_dnp3_flags_yoksa_none_gider_anahtar_yine_var() -> None:
    """Bayrak okunamamis olabilir; anahtar yine bulunmali, degeri None olmali.

    Anahtarin HIC olmamasi ile `None` olmasi backend'de AYNI sonucu verir
    (legacy/cihaz seviyesi), ama alani her zaman gondermek sozlesmeyi
    tek bicimli tutar ve "gonderilmeyi unuttuk mu" sorusunu ortadan kaldirir.
    """
    device = make_device("DEV-1")
    reading = SignalReading(
        signal_key="master.actual_current",
        source="master",
        data_type="analog",
        raw_value=1.0,
        scaled_value=1.0,
        quality="good",
    )
    payload = build_telemetry_payload(
        gateway_code="GW-001", device=device, reading=reading, correlation_id="c"
    )
    assert "dnp3_flags" in payload
    assert payload["dnp3_flags"] is None


# --------------------------------------------------------------------------
# havuz acligi: kuyrukta bekleyen cihaz "okundu" sayilmamali
# --------------------------------------------------------------------------


def test_baslamayan_cihaz_okundu_sayilmaz() -> None:
    """REGRESYON: kuyrukta bekleyen cihazlar iptal edilip `mark_read` ediliyordu.

    Eski kod per-device timeout'u SUBMIT zamanindan olcuyordu ve tum
    future'lar icin bu deger AYNIYDI. 300 cihaz / 25 worker'da kuyrugun
    sonundaki cihaz daha ISE BASLAMADAN "timeout" sayilip iptal ediliyor,
    sonra "okundu" isaretleniyordu. Iki sonucu vardi:

      * cihaza o cycle'da HIC istek gitmiyordu ama sistem okumus sayiyordu,
      * `due_devices` config sirasini korudugu icin ayni cihazlar ertesi
        cycle'da YINE en sona diziliyordu — panelde birkac gosterge sonsuza
        kadar bayat veri gosteriyordu.

    Log da yaniltiyordu: "cihaz yanit vermedi" diyordu, oysa istek hic
    gonderilmemisti; operator sahada cihaz/hat ariyordu.
    """
    import threading

    from dnp3_gateway import poller as _poller

    # Havuz modul seviyesinde singleton'dir ve KUCULMEZ; onceki testler 4
    # worker'lik bir havuz birakmis olabilir. Bu test kuyrukta bekleme
    # davranisini olctugu icin temiz, tek worker'lik bir havuz sart.
    with _poller._pool_lock:
        _onceki, _onceki_max = _poller._pool, _poller._pool_max_workers
        _poller._pool, _poller._pool_max_workers = None, 0

    devices = [make_device(f"DEV-{i:03d}", poll_interval_sec=5) for i in range(4)]
    signal = make_signal("master.actual_current")
    state = GatewayState()
    state.update(make_gateway_config(devices=devices, signals=[signal]))

    kapi = threading.Event()  # ilk worker'i cycle timeout'u boyunca tutar

    class _TakilanReader(TelemetryReader):
        def __init__(self) -> None:
            self.okunanlar: list[str] = []
            self._lock = threading.Lock()

        def read_device(self, *, device, signals):  # type: ignore[no-untyped-def]
            with self._lock:
                self.okunanlar.append(device.code)
            kapi.wait(timeout=10)
            return [SignalReading(signal.key, signal.source, signal.data_type, 1.0, 1.0, "good")]

        def operate_device(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"ok": True, "status": "ok"}

    reader = _TakilanReader()
    try:
        run_poll_cycle(
            gateway_code="GW-001",
            state=state,
            reader=reader,
            publisher=_StubPublisher(),
            now_monotonic=100.0,
            # 2 worker / 4 cihaz -> 2'si kuyrukta kalir.
            # (max_parallel<=1 SERI yolu secer; paralel yolu test ediyoruz.)
            max_parallel=2,
            cycle_timeout_sec=0.4,
            device_timeout_sec=0.2,
        )
    finally:
        kapi.set()
        with _poller._pool_lock:
            _yeni = _poller._pool
            _poller._pool, _poller._pool_max_workers = _onceki, _onceki_max
        if _yeni is not None:
            _yeni.shutdown(wait=False)

    baslayanlar = set(reader.okunanlar)
    assert len(baslayanlar) == 2, f"2 worker'da baslayan cihaz sayisi beklenmedik: {baslayanlar}"

    # Kuyrukta kalan cihazlar okundu SAYILMAMALI: ayni tick'te hala due
    # olmalilar (yoksa bir sonraki cycle'a kadar sessizce atlanirlardi).
    hala_due = {d.code for d in state.due_devices(100.0)}
    baslamayanlar = {d.code for d in devices} - baslayanlar
    assert baslamayanlar <= hala_due, (
        f"kuyrukta bekleyen cihazlar 'okundu' isaretlenmis: {baslamayanlar - hala_due}"
    )


def test_due_devices_en_bayat_cihazi_one_alir() -> None:
    """Aclik dongusunu kiran siralama.

    Config sirasi korunsaydi, kapasitesi yetmeyen bir kurulumda kuyrugun
    sonundaki cihazlar her cycle'da yine sona dizilir ve hic okunmazdi.
    """
    devices = [make_device(f"DEV-{i}", poll_interval_sec=1) for i in range(3)]
    state = GatewayState()
    state.update(make_gateway_config(devices=devices, signals=[]))

    # DEV-0 ve DEV-1 okundu; DEV-2 hic okunmadi.
    state.mark_read("DEV-0", 50.0)
    state.mark_read("DEV-1", 60.0)

    sira = [d.code for d in state.due_devices(100.0)]
    assert sira == ["DEV-2", "DEV-0", "DEV-1"], f"bayatlik sirasi yanlis: {sira}"
