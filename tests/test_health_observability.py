"""Gozlemlenebilirlik testleri — "sistem kor" sorunlarinin regresyonu.

Kapatilan uretim korlukleri:

1. **Tum cihazlar comm_lost iken /health "ok" donuyordu.** Health body cihaz
   sagligini HIC icermiyordu. Saha switch'i coktugunde 300 outstation'in
   tamami erisilemez oluyor, gateway ilk cycle'da bir kez comm_lost yayinlayip
   sonsuza kadar sessiz kaliyor, Docker healthcheck yesil kaliyordu. Ariza
   ancak saatler sonra, backend'de tag'lerin donmus oldugu fark edilince
   anlasiliyordu.

2. **Ana dongu donsa bile "ok".** `last_cycle_at_epoch` yaziliyor ama hicbir
   esige sokulmuyordu; watchdog yoktu.

3. **Thread'ler sessizce oluyordu.** retrier olurse telemetri teslimi,
   command-poll olurse TUM SCADA komut kanali kalici duruyordu — hicbir
   gosterge yoktu.

4. **Metrik sayaclari olu koddu.** publish/read hata sayaclari repoda HIC
   cagrilmiyordu; panelde her zaman 0 goruntuleniyordu.
"""

from __future__ import annotations

import time
from threading import Event
from typing import Any

from dnp3_gateway.health_server import (
    GatewayMetrics,
    ThreadLiveness,
    _build_health_body,
    _device_health_snapshot,
)
from dnp3_gateway.state import GatewayState


class _FakeReader:
    def __init__(self, health: dict[str, dict[str, Any]]) -> None:
        self._health = health

    def device_health(self) -> dict[str, dict[str, Any]]:
        return self._health


class _DeadThread:
    def is_alive(self) -> bool:
        return False


class _LiveThread:
    def is_alive(self) -> bool:
        return True


def _state(*, active: bool = True, device_count: int = 3) -> GatewayState:
    from dnp3_gateway.backend import DeviceConfig, GatewayConfig

    st = GatewayState()
    st.update(
        GatewayConfig(
            gateway_code="GW-001",
            gateway_name="Test",
            batch_interval_sec=5,
            max_devices=100,
            is_active=active,
            config_version="v1",
            devices=[
                DeviceConfig(code=f"DEV-{i}", name=f"d{i}", ip_address=f"10.0.0.{i}")
                for i in range(1, device_count + 1)
            ],
            signals=[],
        )
    )
    return st


def _body(**kwargs: Any) -> tuple[dict[str, Any], int]:
    ready = Event()
    ready.set()
    defaults: dict[str, Any] = {
        "state": _state(),
        "gateway_code": "GW-001",
        "gateway_mode": "dnp3",
        "config_ready": ready,
        "instance_id": "i1",
        "app_environment": "production",
        "health_port": 8020,
        "publisher": None,
        "metrics": GatewayMetrics(),
        "reader": None,
        "liveness": None,
        "poll_interval_sec": 1.0,
    }
    defaults.update(kwargs)
    return _build_health_body(**defaults)


# --------------------------------------------------------------------------
# cihaz sagligi
# --------------------------------------------------------------------------


def test_tum_cihazlar_comm_lost_ise_unhealthy() -> None:
    """REGRESYON: 300 cihazin tamami kopukken /health "ok" + HTTP 200 donuyordu."""
    reader = _FakeReader({f"DEV-{i}": {"state": "lost", "last_frame_epoch": None} for i in (1, 2, 3)})
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=0)
    body, code = _body(reader=reader, metrics=m)
    assert "all_devices_comm_lost" in body["issues"]
    assert body["status"] == "unhealthy"
    assert code == 503
    assert body["devices"]["lost"] == 3
    assert body["devices"]["online"] == 0


def test_cogunluk_kopuksa_degraded() -> None:
    reader = _FakeReader(
        {
            "DEV-1": {"state": "lost", "last_frame_epoch": None},
            "DEV-2": {"state": "lost", "last_frame_epoch": None},
            "DEV-3": {"state": "online", "last_frame_epoch": time.time()},
        }
    )
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=1)
    body, code = _body(reader=reader, metrics=m)
    assert "majority_devices_comm_lost" in body["issues"]
    assert body["status"] == "degraded"
    assert code == 200


def test_hepsi_online_ise_ok() -> None:
    now = time.time()
    reader = _FakeReader({f"DEV-{i}": {"state": "online", "last_frame_epoch": now} for i in (1, 2, 3)})
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=5)
    body, code = _body(reader=reader, metrics=m)
    assert body["status"] == "ok"
    assert code == 200
    assert body["devices"]["online"] == 3


def test_cihaz_ozeti_kod_ve_ip_sizdirmaz() -> None:
    """/health auth'suz — recon malzemesi (cihaz kodu/IP) icermemeli."""
    now = time.time()
    reader = _FakeReader({"DEV-1": {"state": "online", "last_frame_epoch": now}})
    body, _c = _body(reader=reader, state=_state(device_count=1))
    serialized = repr(body)
    assert "DEV-1" not in serialized
    assert "10.0.0.1" not in serialized
    assert set(body["devices"]) >= {"total", "online", "lost", "recovering", "unknown"}


def test_adapter_master_acmadiysa_unknown_sayilir() -> None:
    snap = _device_health_snapshot(_FakeReader({}), 5)
    assert snap["total"] == 5
    assert snap["unknown"] == 5


# --------------------------------------------------------------------------
# watchdog
# --------------------------------------------------------------------------


def test_donmus_poll_dongusu_unhealthy() -> None:
    """REGRESYON: ana dongu donsa bile /health saatlerce 200/ok donuyordu."""
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=1)
    m.last_cycle_at_epoch = time.time() - 3600  # 1 saattir cycle yok
    body, code = _body(metrics=m, poll_interval_sec=1.0)
    assert "poll_loop_stalled" in body["issues"]
    assert body["status"] == "unhealthy"
    assert code == 503


def test_taze_cycle_stall_saymaz() -> None:
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=1)
    body, _c = _body(metrics=m)
    assert "poll_loop_stalled" not in body["issues"]
    assert body["metrics"]["seconds_since_last_cycle"] is not None


def test_pasif_gateway_stall_saymaz() -> None:
    """is_active=False iken cycle calismamasi normaldir."""
    m = GatewayMetrics()
    m.record_cycle(devices=0, published=0)
    m.last_cycle_at_epoch = time.time() - 3600
    body, _c = _body(state=_state(active=False), metrics=m)
    assert "poll_loop_stalled" not in body["issues"]


# --------------------------------------------------------------------------
# thread canliligi
# --------------------------------------------------------------------------


def test_olu_thread_unhealthy_yapar() -> None:
    """REGRESYON: retrier olurse telemetri teslimi durur ama /health 'ok' derdi."""
    lv = ThreadLiveness()
    lv.register("outbox-retrier", _DeadThread())
    lv.register("config-refresh", _LiveThread())
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=1)
    body, code = _body(liveness=lv, metrics=m)
    assert "thread_dead:outbox-retrier" in body["issues"]
    assert body["status"] == "unhealthy"
    assert code == 503
    assert body["threads"]["alive"]["config-refresh"] is True


def test_canli_threadler_sorun_yaratmaz() -> None:
    lv = ThreadLiveness()
    lv.register("outbox-retrier", _LiveThread())
    lv.register("command-poll", _LiveThread())
    m = GatewayMetrics()
    m.record_cycle(devices=3, published=1)
    body, code = _body(liveness=lv, metrics=m)
    assert body["status"] == "ok"
    assert code == 200


# --------------------------------------------------------------------------
# metrikler
# --------------------------------------------------------------------------


def test_yeni_metrik_sayaclari_body_de() -> None:
    m = GatewayMetrics()
    m.record_cycle(devices=2, published=10)
    m.inc_read_error(3)
    m.inc_publish_error(2)
    m.inc_outboxed(7)
    body, _c = _body(metrics=m)
    assert body["metrics"]["read_errors_total"] == 3
    assert body["metrics"]["publish_errors_total"] == 2
    assert body["metrics"]["signals_outboxed_total"] == 7


def test_poller_metrikleri_gercekten_besler() -> None:
    """REGRESYON: inc_read_error/inc_publish_error repoda HIC cagrilmiyordu."""
    from dnp3_gateway.backend import DeviceConfig, SignalConfig
    from dnp3_gateway.poller import poll_device

    class _BrokenReader:
        def read_device(self, *, device, signals):  # noqa: ARG002
            raise RuntimeError("DNP3 okuma hatasi")

    m = GatewayMetrics()
    poll_device(
        gateway_code="GW-001",
        device=DeviceConfig(code="D1", name="d", ip_address="10.0.0.1"),
        signals=[
            SignalConfig(
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
        ],
        reader=_BrokenReader(),
        publisher=object(),
        metrics=m,
    )
    assert m.snapshot()["read_errors_total"] == 1, "okuma hatasi sayaca yansimali"


# --------------------------------------------------------------------------
# komut defteri sifirlanmasi
# --------------------------------------------------------------------------


def test_komut_defteri_sifirlanmasi_ayri_kod_ile_raporlanir() -> None:
    """`state_db_quarantined` ile AYNI SEY DEGIL.

    Orada kaybedilen birikmis telemetridir; burada FIZIKSEL KOMUT gecmisi ve
    tekrar-onleme garantisi. Ayni kodu paylasmalari mudahaleyi yanlis yone
    cevirirdi: operator "telemetri kaybi" diye gecistirirken bekleyen
    komutlarin durumu dogrulanmadan kalirdi.
    """

    class _Ledger:
        def status_snapshot(self):
            return {
                "journal_reset": True,
                "journal_reset_at": 1.0,
                "journal_reset_path": "/state/command_ledger.db.corrupt.123",
                "result_pending": 0,
                "result_dead_letter": 0,
            }

    body, code = _body(ledger_provider=lambda: _Ledger())
    assert "command_journal_reset" in body["issues"]
    assert body["status"] == "degraded"
    assert code == 200
    assert body["command_ledger"]["journal_reset"] is True
    # /health auth'suz — karantina DOSYA YOLU burada gorunmemeli.
    assert "journal_reset_path" not in body["command_ledger"]


def test_saglikli_defter_sorun_uretmez() -> None:
    class _Ledger:
        def status_snapshot(self):
            return {"journal_reset": False, "result_pending": 2, "result_dead_letter": 0}

    body, _c = _body(ledger_provider=lambda: _Ledger())
    assert "command_journal_reset" not in body["issues"]
    assert body["command_ledger"]["result_pending"] == 2


def test_defter_erisilemezse_health_patlamaz() -> None:
    """Health probe'u bir yan bilesenin hatasi dusuremez."""

    def _patlayan():
        raise RuntimeError("ledger kapali")

    body, code = _body(ledger_provider=_patlayan)
    assert code in (200, 503)
    assert "command_journal_reset" not in body["issues"]


# --------------------------------------------------------------------------
# SCADA komut kanali sagligi
# --------------------------------------------------------------------------


def _state_komut_hatali(ardisik: int):
    st = _state()
    for _ in range(ardisik):
        st.record_command_poll(ok=False, error="pending request returned 500")
    return st


def test_komut_kanali_olurken_health_ok_dememeli() -> None:
    """REGRESYON: sahada komut kanali 660 ardisik hatayla OLUYDU, /health "ok" diyordu.

    Thread YASIYORDU (dolayisiyla `thread_dead:` tetiklenmedi) ama her turda
    500 aliyordu. Sonuc: SCADA komutlari gateway'e HIC ulasmiyordu, ustelik
    `config_nonce` de okunamadigi icin yeni cihazlar 5 dakikaya kadar
    gorulmuyordu — ve panelde hicbir uyari yoktu. Operatorun bunu fark
    etmesinin bir yolu YOKTU.
    """
    body, code = _body(state=_state_komut_hatali(660))
    assert "command_channel_down" in body["issues"]
    assert body["status"] == "unhealthy"
    assert code == 503
    assert body["command_channel"]["consecutive_errors"] == 660


def test_kisa_komut_kanali_kesintisi_degraded() -> None:
    """Backend restart penceresi normaldir; ~15sn kesinti degraded olmali."""
    body, code = _body(state=_state_komut_hatali(20))
    assert "command_channel_failing" in body["issues"]
    assert body["status"] == "degraded"
    assert code == 200


def test_anlik_komut_hatasi_alarm_uretmez() -> None:
    """Tek tuk hata gurultu yapmamali (1sn'lik poll'de birkac hata normal)."""
    body, _c = _body(state=_state_komut_hatali(3))
    assert "command_channel_failing" not in body["issues"]
    assert "command_channel_down" not in body["issues"]


def test_komut_kanali_toparlaninca_sayac_sifirlanir() -> None:
    st = _state_komut_hatali(100)
    st.record_command_poll(ok=True)
    body, code = _body(state=st)
    assert "command_channel_down" not in body["issues"]
    assert "command_channel_failing" not in body["issues"]
    assert code == 200


# --------------------------------------------------------------------------
# gateway <-> backend haberlesmesi gorunur olmali
# --------------------------------------------------------------------------


class _Broker:
    def __init__(self, ready: bool) -> None:
        self.is_ready = ready


class _Publisher:
    """ResilientPublisher'in /health tarafindan okunan yuzeyi."""

    def __init__(self, *, ready: bool, pending: int = 0) -> None:
        # `_broker` private ama health_server oradan okuyor (bkz. _outbox_snapshot).
        self._broker = _Broker(ready)
        self._pending = pending
        self.outbox_full = False

    def pending_count(self) -> int:
        return self._pending

    def dead_letter_count(self) -> int:
        return 0


def test_backend_erisilemezken_health_uyarir() -> None:
    """REGRESYON: `broker_ready` govdede vardi ama HICBIR sorun kodu uretmiyordu.

    Backend'e telemetri hic gitmezken `/health` "ok" diyebiliyordu; ariza
    ancak outbox dolmaya basladiginda — dakikalar sonra — gorunurdu.
    Gateway <-> backend haberlesmesi kopar kopmaz gorunmeli.
    """
    body, _c = _body(publisher=_Publisher(ready=False))
    assert "telemetry_backend_unreachable" in body["issues"]
    assert body["status"] == "degraded"


def test_backend_erisilebilirken_uyari_yok() -> None:
    body, code = _body(publisher=_Publisher(ready=True))
    assert "telemetry_backend_unreachable" not in body["issues"]
    assert code == 200


# --------------------------------------------------------------------------
# "cevap veriyor ama olcum gelmiyor" gorunurlugu (1.7.0)
# --------------------------------------------------------------------------
#
# Bu durum comm_lost DEGILDIR — cihaz konusuyor — ve artik sahte kopma
# uretmiyor. Ama GORUNMEZ de olmamali: kalici olmasi Class 0 integrity
# poll'unun dustugune isaret eder. Eskiden ayni belirti `some_devices_comm_lost`
# olarak raporlaniyordu, yani operator "kopuk" arayip bosuna modem/anten
# kontrol ediyordu.


class _SessizReader(_FakeReader):
    """`recovery_stats` da doner — gercek adapter gibi."""

    def __init__(self, health, alive_no_data: int) -> None:
        super().__init__(health)
        self._alive_no_data = alive_no_data

    def recovery_stats(self) -> dict[str, int]:
        return {
            "lost_probe_total": 0,
            "forced_relink_total": 0,
            "devices_probing": 0,
            "data_silence_poll_total": 3,
            "devices_alive_no_data": self._alive_no_data,
        }


def test_cevap_veren_ama_sessiz_cihaz_issue_uretir() -> None:
    now = time.time()
    reader = _SessizReader(
        {f"DEV-{i}": {"state": "online", "last_frame_epoch": now} for i in (1, 2)},
        alive_no_data=1,
    )
    m = GatewayMetrics()
    m.record_cycle(devices=2, published=0)
    body, code = _body(reader=reader, metrics=m, state=_state(device_count=2))

    assert "devices_alive_no_data" in body["issues"]
    assert body["devices"]["alive_no_data"] == 1
    # KOPUK DEGIL: comm_lost kodlari CIKMAMALI.
    assert not [i for i in body["issues"] if "comm_lost" in i]
    # Uyari seviyesi: degraded, unhealthy DEGIL (cihaz calisiyor).
    assert body["status"] == "degraded"
    assert code == 200


def test_sessizlik_yoksa_issue_uretilmez() -> None:
    now = time.time()
    reader = _SessizReader(
        {f"DEV-{i}": {"state": "online", "last_frame_epoch": now} for i in (1, 2)},
        alive_no_data=0,
    )
    m = GatewayMetrics()
    m.record_cycle(devices=2, published=4)
    body, _c = _body(reader=reader, metrics=m, state=_state(device_count=2))

    assert "devices_alive_no_data" not in body["issues"]
    assert body["status"] == "ok"


def test_sessizlik_sayimi_health_de_kod_sizdirmaz() -> None:
    """Filo sayimi auth'suz uctan gorulur ama cihaz kodu YINE sizmamali."""
    now = time.time()
    reader = _SessizReader({"DEV-GIZLI": {"state": "online", "last_frame_epoch": now}}, alive_no_data=1)
    body, _c = _body(reader=reader, state=_state(device_count=1))
    assert "DEV-GIZLI" not in repr(body)
    assert body["devices"]["alive_no_data"] == 1
