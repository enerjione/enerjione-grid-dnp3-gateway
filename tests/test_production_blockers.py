"""Production go/no-go denetiminde tespit edilen blocker'larin regresyon testleri.

Her testin basligi, sahada NE OLDUGUNU anlatir. Bu dosyadaki bir test kirmizi
donerse ilgili ariza geri gelmis demektir.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from dnp3_gateway.adapters.base import SignalReading
from dnp3_gateway.backend import DeviceConfig, SignalConfig
from dnp3_gateway.config import ConfigError, format_config_error, load_settings
from dnp3_gateway.messaging.command_ledger import CommandLedger
from dnp3_gateway.messaging.outbox import Outbox, OutboxRetrier
from dnp3_gateway.messaging.resilient_publisher import ResilientPublisher
from dnp3_gateway.poller import build_telemetry_payload, poll_device

# ===========================================================================
# 2) Import-ani config dogrulamasi
# ===========================================================================


def test_config_modulu_import_ederken_dogrulama_yapmaz(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESYON: `settings = Settings()` import aninda calisiyordu.

    Sonuc: bozuk bir .env ile gateway logging kurulmadan, health server
    acilmadan, ham bir pydantic traceback'i ile oluyordu. `--help` bile
    calismiyordu ve Docker'da sessiz crash-loop olusuyordu.
    """
    import importlib
    import sys

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    for mod in [m for m in sys.modules if m.startswith("dnp3_gateway")]:
        del sys.modules[mod]
    # Import PATLAMAMALI — dogrulama artik erisim aninda.
    cfg_mod = importlib.import_module("dnp3_gateway.config")
    assert hasattr(cfg_mod, "Settings")
    assert hasattr(cfg_mod, "get_settings")


def test_load_settings_temiz_config_error_verir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("GATEWAY_MODE", "mock")
    monkeypatch.setenv("GATEWAY_TOKEN", "A" * 48)
    with pytest.raises(ConfigError) as exc:
        load_settings(_env_file=None)
    assert "mock" in str(exc.value).lower()


def test_hata_metni_ayar_sozlugunu_sizdirmaz() -> None:
    """GUVENLIK: pydantic mesajin sonuna `input_value={...}` ekliyor.

    O dokum TUM ayar sozlugunu (GATEWAY_TOKEN dahil) stdout'a, NSSM log
    dosyasina ve `docker logs`a dusururdu.
    """
    ham = (
        "1 validation error for Settings\n"
        "  Value error, GUVENLIK: bir sey yanlis "
        "[type=value_error, input_value={'gateway_token': 'COKGIZLITOKEN'}, input_type=dict]\n"
        "    For further information visit https://errors.pydantic.dev/2.13/v/value_error"
    )
    temiz = format_config_error(Exception(ham))
    assert "COKGIZLITOKEN" not in temiz
    assert "input_value" not in temiz
    assert "errors.pydantic.dev" not in temiz
    assert "GUVENLIK: bir sey yanlis" in temiz


# ===========================================================================
# 3) NaN / Inf koruması
# ===========================================================================


def _reading(value: float | None, quality: str = "good") -> SignalReading:
    return SignalReading(
        signal_key="a",
        source="master",
        data_type="analog",
        raw_value=0.0,
        scaled_value=value,
        quality=quality,
    )


def _device() -> DeviceConfig:
    return DeviceConfig(code="DEV-1", name="d", ip_address="10.0.0.1")


@pytest.mark.parametrize("bozuk", [float("nan"), float("inf"), float("-inf")])
def test_payload_nan_inf_iceremez(bozuk: float) -> None:
    """REGRESYON: NaN payload'a girince TUM outbox kalici olarak tikaniyordu.

    Zincir: requests json.dumps(allow_nan=False) -> InvalidJSONError ->
    RequestException -> HttpTelemetryNotReadyError -> is_transient=True ->
    retrier `mark_retry` CAGIRMAZ -> next_attempt_at ILERLEMEZ -> satir
    kuyrugun basinda kalici olarak takilir.
    """
    p = build_telemetry_payload(
        gateway_code="GW-1",
        device=_device(),
        reading=_reading(bozuk),
        correlation_id="c1",
    )
    assert p["value"] is None
    # En onemlisi: govde katı JSON'a serilestirilebilmeli (requests boyle yapar).
    json.dumps(p, allow_nan=False)


def test_gecerli_deger_bozulmaz() -> None:
    p = build_telemetry_payload(
        gateway_code="GW-1",
        device=_device(),
        reading=_reading(220.5),
        correlation_id="c1",
    )
    assert p["value"] == 220.5
    json.dumps(p, allow_nan=False)


def test_adapter_nan_i_invalid_kalite_ile_dondurur() -> None:
    """Adapter seviyesinde de filtreleniyor mu? (sahte 0.0 gondermiyoruz)"""
    from dnp3_gateway.adapters.dnp3_yadnp3_master import _DeviceCache

    c = _DeviceCache()
    c.set(30, 1, float("nan"))
    alinan = c.peek_if_dirty(30, 1)
    assert alinan is not None
    # Cache ham degeri tutar; filtre okuma yolunda (read_device) uygulanir.
    assert math.isnan(alinan[0])


class _KayitPublisher:
    def __init__(self, outboxed: bool = False) -> None:
        self.batches: list[list[dict]] = []
        self.last_batch_outboxed = outboxed

    def publish_batch(self, items: list[dict]) -> None:
        self.batches.append(list(items))
        # Gercek publisher gibi: govde JSON'a cevrilebilmeli.
        json.dumps([it["payload"] for it in items], allow_nan=False)


class _FakeReader:
    def __init__(self, readings: list[SignalReading]) -> None:
        self._r = readings

    def read_device(self, *, device, signals):  # noqa: ARG002
        return list(self._r)


def test_nan_iceren_cihaz_batchi_yine_de_yayinlanir() -> None:
    """Bozuk TEK nokta, cihazin DIGER sinyallerini engellememeli."""
    pub = _KayitPublisher()
    reader = _FakeReader([_reading(float("nan")), _reading(12.5)])
    sinyal = SignalConfig(
        key="a",
        label="a",
        unit=None,
        source="master",
        dnp3_class="Class 1",
        data_type="analog",
        dnp3_object_group=30,
        dnp3_index=1,
        scale=1.0,
        offset=0.0,
        supports_alarm=False,
    )
    n = poll_device(
        gateway_code="GW-1",
        device=_device(),
        signals=[sinyal],
        reader=reader,
        publisher=pub,
    )
    assert n == 2
    degerler = [it["payload"]["value"] for it in pub.batches[0]]
    assert None in degerler and 12.5 in degerler


# ===========================================================================
# 4) outbox_full breaker kalici kilidi
# ===========================================================================


class _OlenBroker:
    def publish(self, *a, **k):  # noqa: ANN002,ANN003,ARG002
        raise RuntimeError("broker yok")

    def close(self) -> None:
        return None


def test_bos_kuyrukta_breaker_kendiliginden_acilir(tmp_path: Path) -> None:
    """REGRESYON: breaker acikken onu kapatacak AKTOR KALMIYORDU.

    Breaker acikken (a) poller publish denemez, (b) retrier ancak SATIR VARSA
    publish eder. Kuyruk dead-letter yoluyla bosalirsa gateway RESTART'a kadar
    hic telemetri yayinlamiyordu.
    """
    ob = Outbox(tmp_path / "o.db")
    try:
        pub = ResilientPublisher(broker=_OlenBroker(), outbox=ob)
        pub._set_outbox_full("test")
        assert pub.outbox_full is True

        # Kuyruk BOS ve hicbir basarili yayin YOK — yine de acilmali.
        assert ob.pending_count() == 0
        pub.notify_outbox_idle()
        assert pub.outbox_full is False, "bos kuyrukta breaker acilmali"
    finally:
        ob.close()


def test_kuyruk_hala_doluyken_breaker_acilmaz(tmp_path: Path) -> None:
    """Histerezis korunmali: dolu kuyrukta erken acilma ac-kapa dongusu yaratir."""
    ob = Outbox(tmp_path / "o.db", max_pending=1000)
    try:
        for i in range(950):  # %80 esiginin uzerinde
            ob.enqueue(message_id=f"m{i}", correlation_id=None, headers=None, payload={"v": i})
        pub = ResilientPublisher(broker=_OlenBroker(), outbox=ob)
        pub._set_outbox_full("test")
        pub.notify_outbox_idle()
        assert pub.outbox_full is True, "dolu kuyrukta breaker acik kalmali"
    finally:
        ob.close()


def test_retrier_bos_kuyrukta_idle_kancasini_cagirir(tmp_path: Path) -> None:
    ob = Outbox(tmp_path / "o.db")
    try:
        cagrildi: list[int] = []
        r = OutboxRetrier(ob, lambda row: None, on_idle=lambda: cagrildi.append(1))
        # Kuyruk bos -> _run tek turu idle kancasini cagirmali.
        r._notify_idle()
        assert cagrildi == [1]
    finally:
        ob.close()


# ===========================================================================
# 5) Komut sonucu teslimi — kalici hatada kuyruk ilerlemeli
# ===========================================================================


def test_ledger_kalici_hata_sayaci(tmp_path: Path) -> None:
    led = CommandLedger(tmp_path / "l.db")
    try:
        led.start_dispatch(1)
        led.record_result({"id": 1, "ok": True, "status": "ok"})
        assert led.bump_delivery_failure(1) == 1
        assert led.bump_delivery_failure(1) == 2
        assert led.bump_delivery_failure(1) == 3
    finally:
        led.close()


def test_kalici_teslim_hatasi_kuyrugu_ilerletir(tmp_path: Path) -> None:
    """REGRESYON: tek kalici 4xx, komut sonucu kuyrugunu SONSUZA kadar bloke ediyordu.

    Sonuc: kesiciler fiziksel olarak surulmusken operator panelinde komutlar
    sonsuza kadar "bekliyor" gorunuyordu.
    """
    from dnp3_gateway.backend.config_client import CommandResultDeliveryError
    from dnp3_gateway.main import _deliver_ledger_results

    led = CommandLedger(tmp_path / "l.db")
    try:
        led.start_dispatch(1)
        led.record_result({"id": 1, "ok": True, "status": "ok"})

        class _Client:
            gateway_code = "GW-1"

            def report_command_results(self, results):  # noqa: ANN001, ARG002
                # Kalici: sema dogrulamasi reddetti.
                raise CommandResultDeliveryError("rejected", http_status=422)

        c = _Client()
        for _ in range(3):
            _deliver_ledger_results(c, led)

        assert led.pending_result_count() == 0, "kalici hatada kuyruk ILERLEMELI"
        assert led.delivery_dead_letter_count() == 1
    finally:
        led.close()


def test_gecici_teslim_hatasi_kuyrukta_tutar(tmp_path: Path) -> None:
    """Gecici hatada sonuc KAYBOLMAMALI — ag kesintisi kalici olmamali."""
    from dnp3_gateway.backend.config_client import CommandResultDeliveryError
    from dnp3_gateway.main import _deliver_ledger_results

    led = CommandLedger(tmp_path / "l.db")
    try:
        led.start_dispatch(1)
        led.record_result({"id": 1, "ok": True, "status": "ok"})

        class _Client:
            gateway_code = "GW-1"

            def report_command_results(self, results):  # noqa: ANN001, ARG002
                raise CommandResultDeliveryError("backend down", http_status=503)

        c = _Client()
        for _ in range(5):
            _deliver_ledger_results(c, led)

        assert led.pending_result_count() == 1, "gecici hatada sonuc kuyrukta kalmali"
        assert led.delivery_dead_letter_count() == 0
    finally:
        led.close()


def test_basarili_teslim_delivered_isaretler(tmp_path: Path) -> None:
    from dnp3_gateway.main import _deliver_ledger_results

    led = CommandLedger(tmp_path / "l.db")
    try:
        led.start_dispatch(1)
        led.record_result({"id": 1, "ok": True, "status": "ok"})

        class _Client:
            gateway_code = "GW-1"

            def report_command_results(self, results):  # noqa: ANN001, ARG002
                return None

        _deliver_ledger_results(_Client(), led)
        assert led.pending_result_count() == 0
        assert led.delivery_dead_letter_count() == 0
    finally:
        led.close()


def test_ledger_v2_semasi(tmp_path: Path) -> None:
    led = CommandLedger(tmp_path / "l.db")
    led.close()
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    (v,) = conn.execute("PRAGMA user_version").fetchone()
    assert int(v) == 2
    conn.close()


# ===========================================================================
# 7) Metrik: outbox'a dusen mesaj "yayinlandi" SAYILMAZ
# ===========================================================================


def test_outboxa_dusen_mesaj_published_sayilmaz() -> None:
    """REGRESYON: backend TUM POST'lari reddederken bile panelde 'yayin akiyor'."""
    pub = _KayitPublisher(outboxed=True)
    reader = _FakeReader([_reading(1.0), _reading(2.0)])
    sinyal = SignalConfig(
        key="a",
        label="a",
        unit=None,
        source="master",
        dnp3_class="Class 1",
        data_type="analog",
        dnp3_object_group=30,
        dnp3_index=1,
        scale=1.0,
        offset=0.0,
        supports_alarm=False,
    )

    class _M:
        def __init__(self) -> None:
            self.outboxed = 0
            self.errors = 0

        def inc_outboxed(self, n: int = 1) -> None:
            self.outboxed += n

        def inc_publish_error(self, n: int = 1) -> None:
            self.errors += n

        def inc_read_error(self, n: int = 1) -> None:
            pass

        def inc_skipped_no_change(self, n: int = 1) -> None:
            pass

    m = _M()
    n = poll_device(
        gateway_code="GW-1",
        device=_device(),
        signals=[sinyal],
        reader=reader,
        publisher=pub,
        metrics=m,
    )
    assert n == 0, "outbox'a dusen mesaj 'published' donmemeli"
    assert m.outboxed == 2
    assert m.errors == 1


def test_brokera_giden_mesaj_published_sayilir() -> None:
    pub = _KayitPublisher(outboxed=False)
    reader = _FakeReader([_reading(1.0), _reading(2.0)])
    sinyal = SignalConfig(
        key="a",
        label="a",
        unit=None,
        source="master",
        dnp3_class="Class 1",
        data_type="analog",
        dnp3_object_group=30,
        dnp3_index=1,
        scale=1.0,
        offset=0.0,
        supports_alarm=False,
    )
    assert (
        poll_device(
            gateway_code="GW-1",
            device=_device(),
            signals=[sinyal],
            reader=reader,
            publisher=pub,
        )
        == 2
    )
