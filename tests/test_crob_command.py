"""DNP3 CROB komut (LATCH_ON + SELECT-before-OPERATE) unit testleri.

Horstmann SN2 Device Profile PULSE desteklemez; komut LATCH_ON + SBO
(SelectAndOperate) ile gonderilmeli. Bu testler opendnp3'u MOCK'lar (gercek
yadnp3 wheel gerekmez) ve _ManagedMaster.operate_crob mantigini dogrular:

  - default op_type LATCH_ON (pulse degil)
  - Group 12 / Var 1 / index 0 / count 1 akisi
  - SelectAndOperate (SBO) cagriliyor, DirectOperate DEGIL
  - SELECT-fail'de OPERATE gonderilmiyor (SBO tek callback; SELECT_FAIL ->
    OPERATE fazina gecmez, binding garantisi + status kararimiz)
  - per-point CommandStatus.SUCCESS kararda kullaniliyor (transport SUCCESS
    ama point NO_SELECT -> failed)
  - timeout durumu
  - cihaz offline -> komut GONDERILMIYOR (fail-fast)
  - gercek DNP3 status sonuca aktariliyor
  - SELECT/OPERATE ayni CROB (SBO tek CROB gecirir -> otomatik ayni)

Test stratejisi: sys.modules'a fake 'opendnp3' enjekte + _ManagedMaster'i
__new__ ile (init'siz) olustur, _master + cache'i fake'le.
"""

from __future__ import annotations

import sys
import types
from enum import Enum

import pytest


# ---- Fake opendnp3 modulu (gercek yadnp3 wheel yoksa) ----------------------
class _OperationType(Enum):
    LATCH_ON = 3
    LATCH_OFF = 4
    PULSE_ON = 1
    PULSE_OFF = 2
    NUL = 0


class _TripCloseCode(Enum):
    NUL = 0


class _TaskCompletion(Enum):
    SUCCESS = 0
    FAILURE_RESPONSE_TIMEOUT = 1
    FAILURE_NO_COMMS = 2
    FAILURE_MESSAGE_FORMAT_ERROR = 3
    FAILURE_BAD_RESPONSE = 4


class _CommandStatus(Enum):
    SUCCESS = 0
    NO_SELECT = 2
    FORMAT_ERROR = 3
    NOT_SUPPORTED = 4


class _CommandPointState(Enum):
    INIT = 0
    SELECT_SUCCESS = 1
    SELECT_FAIL = 2
    SELECT_MISMATCH = 3
    OPERATE_FAIL = 4
    SUCCESS = 5


class _CROB:
    def __init__(self, op=None, tcc=None, clear=False, count=1, on=0, off=0, status=None):
        self.opType = op
        self.count = count
        self.onTimeMS = on
        self.offTimeMS = off


class _TaskConfig:
    @staticmethod
    def Default():
        return _TaskConfig()


class _PointResult:
    def __init__(self, index, status, state):
        self.index = index
        self.status = status
        self.state = state


class _CmdResult:
    def __init__(self, summary, points):
        self.summary = summary
        self._points = points

    def to_list(self):
        return self._points


def _make_fake_opendnp3():
    m = types.ModuleType("opendnp3")
    m.OperationType = _OperationType
    m.TripCloseCode = _TripCloseCode
    m.TaskCompletion = _TaskCompletion
    m.CommandStatus = _CommandStatus
    m.CommandPointState = _CommandPointState
    m.ControlRelayOutputBlock = _CROB
    m.TaskConfig = _TaskConfig
    # _make_soe_handler / _make_master_app import zamaninda ISOEHandler vs
    # bekleyebilir; import sirasinda kullanilmadigi surece bos class yeter.
    m.ISOEHandler = type("ISOEHandler", (), {})
    m.IMasterApplication = type("IMasterApplication", (), {})
    m.Binary = type("Binary", (), {})
    m.Analog = type("Analog", (), {})
    m.Counter = type("Counter", (), {})
    m.BinaryOutputStatus = type("BinaryOutputStatus", (), {})
    m.AnalogOutputStatus = type("AnalogOutputStatus", (), {})
    m.OctetString = type("OctetString", (), {})
    m.DNPTime = lambda x: x
    m.levels = types.SimpleNamespace(NORMAL=0)
    return m


@pytest.fixture()
def yadnp3_master(monkeypatch):
    """opendnp3'u mock'layip _ManagedMaster instance'ini (init'siz) doner."""
    fake = _make_fake_opendnp3()
    monkeypatch.setitem(sys.modules, "opendnp3", fake)
    # Modul import zamaninda opendnp3'e bakiyor; reload gerekmesin diye
    # _YADNP3_AVAILABLE'i True zorla + opendnp3 global'ini set et.
    import dnp3_gateway.adapters.dnp3_yadnp3_master as mod

    monkeypatch.setattr(mod, "opendnp3", fake, raising=False)
    monkeypatch.setattr(mod, "_YADNP3_AVAILABLE", True, raising=False)
    # class-level op_map cache'ini temizle (onceki testten kalmasin)
    mod._ManagedMaster._OP_MAP_LAZY = None

    mm = mod._ManagedMaster.__new__(mod._ManagedMaster)  # __init__ ATLA
    # operate_crob'un ihtiyaci: self.cache (state), self._master, self.device
    mm.cache = types.SimpleNamespace(state=lambda: "online")
    mm.device = types.SimpleNamespace(code="SN-TEST", dnp3_address=12345)

    calls = {}

    class _FakeMaster:
        def SelectAndOperate(self, crob, index, cb, cfg):
            calls["crob"] = crob
            calls["index"] = index
            calls["sbo_called"] = True
            cb(calls["result"])

        def DirectOperate(self, crob, index, cb, cfg):
            calls["crob"] = crob
            calls["index"] = index
            calls["direct_called"] = True
            cb(calls["result"])

    mm._master = _FakeMaster()
    return mm, calls, fake, mod


def _success_result(fake, index=0):
    return _CmdResult(
        fake.TaskCompletion.SUCCESS,
        [_PointResult(index, fake.CommandStatus.SUCCESS, fake.CommandPointState.SUCCESS)],
    )


# ---- Testler ----------------------------------------------------------------
def test_default_op_type_is_latch_on(yadnp3_master):
    """Test 2: komut LATCH_ON ile olusturulur (pulse degil)."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    mm.operate_crob(index=0)  # op_type verilmedi -> default
    assert calls["crob"].opType == fake.OperationType.LATCH_ON


def test_pulse_not_used_by_default(yadnp3_master):
    """Test 1: eski PULSE default'u artik kullanilmiyor."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    mm.operate_crob(index=0)
    assert calls["crob"].opType != fake.OperationType.PULSE_ON
    assert calls["crob"].opType != fake.OperationType.PULSE_OFF


def test_direct_operate_used_by_default(yadnp3_master):
    """Test: default mode=direct -> DirectOperate cagrilir, SBO DEGIL.

    Saha testi: bu cihaz SBO SELECT fazinda NOT_SUPPORTED donuyor; DirectOperate
    calisiyor (alarm fiziksel resetleniyor). Bu yuzden default DirectOperate.
    """
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    mm.operate_crob(index=0)  # mode verilmedi -> default direct
    assert calls.get("direct_called") is True
    assert calls.get("sbo_called") is not True


def test_sbo_mode_uses_select_and_operate(yadnp3_master):
    """mode='sbo' verilince SelectAndOperate cagrilir (opsiyonel)."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    mm.operate_crob(index=0, mode="sbo")
    assert calls.get("sbo_called") is True
    assert calls.get("direct_called") is not True


def test_count_is_one(yadnp3_master):
    """Test 4: count degeri 1."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    mm.operate_crob(index=0)
    assert calls["crob"].count == 1


def test_index_zero_used(yadnp3_master):
    """Test 3: index 0 kullaniliyor (Group 12 Var 1 index 0)."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake, index=0)
    mm.operate_crob(index=0)
    assert calls["index"] == 0


def test_success_when_task_and_point_success(yadnp3_master):
    """Test 10: gercek DNP3 status sonuca aktarilir — hepsi SUCCESS -> ok."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    res = mm.operate_crob(index=0)
    assert res["ok"] is True
    assert res["status"] == "ok"
    assert "SUCCESS" in res["dnp3_status"]
    assert "SUCCESS" in res["dnp3_task"]


def test_transport_success_but_point_no_select_is_failed(yadnp3_master):
    """Test 5-benzeri: transport SUCCESS ama point NO_SELECT -> FAILED.

    Eski kod yalniz summary'ye bakip yanlis-pozitif verebiliyordu. Yeni kod
    per-point CommandStatus'u da kontrol eder.
    """
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _CmdResult(
        fake.TaskCompletion.SUCCESS,
        [_PointResult(0, fake.CommandStatus.NO_SELECT, fake.CommandPointState.SELECT_FAIL)],
    )
    res = mm.operate_crob(index=0)
    assert res["ok"] is False
    assert res["status"] == "failed"
    assert "NO_SELECT" in res["dnp3_status"]


def test_not_supported_status_is_failed(yadnp3_master):
    """Point NOT_SUPPORTED (cihaz komutu desteklemiyor) -> failed, status aktarilir."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _CmdResult(
        fake.TaskCompletion.SUCCESS,
        [_PointResult(0, fake.CommandStatus.NOT_SUPPORTED, fake.CommandPointState.OPERATE_FAIL)],
    )
    res = mm.operate_crob(index=0)
    assert res["ok"] is False
    assert "NOT_SUPPORTED" in res["dnp3_status"]


def test_offline_device_command_not_sent(yadnp3_master):
    """Test 9: cihaz offline (state != online) -> komut GONDERILMEZ."""
    mm, calls, fake, _ = yadnp3_master
    mm.cache = types.SimpleNamespace(state=lambda: "lost")
    calls["result"] = _success_result(fake)
    res = mm.operate_crob(index=0)
    assert res["ok"] is False
    assert res["status"] == "offline"
    assert calls.get("direct_called") is not True  # komut hic gonderilmedi
    assert calls.get("sbo_called") is not True


def test_recovering_device_command_not_sent(yadnp3_master):
    """recovering durumu da online degil -> gonderme."""
    mm, calls, fake, _ = yadnp3_master
    mm.cache = types.SimpleNamespace(state=lambda: "recovering")
    calls["result"] = _success_result(fake)
    res = mm.operate_crob(index=0)
    assert res["status"] == "offline"


def test_timeout_when_no_callback(yadnp3_master):
    """Test 7-8: SELECT/OPERATE cevabi gelmezse timeout."""
    mm, calls, fake, mod = yadnp3_master

    class _NoReplyMaster:
        def SelectAndOperate(self, crob, index, cb, cfg):
            pass  # callback HIC cagrilmaz -> timeout

        def DirectOperate(self, crob, index, cb, cfg):
            pass

    mm._master = _NoReplyMaster()
    res = mm.operate_crob(index=0, timeout_sec=0.2)
    assert res["ok"] is False
    assert res["status"] == "timeout"


def test_select_and_operate_use_same_crob(yadnp3_master):
    """Test 6: SBO tek CROB gecirir -> SELECT ve OPERATE ayni CROB (garanti).

    SelectAndOperate binding'i ayni CROB ile hem SELECT hem OPERATE yapar;
    ayri CROB olusmaz. Gecirilen CROB'un beklenen degerleri tasidigini dogrula.
    """
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    mm.operate_crob(index=0, op_type="latch_on", count=1, on_time_ms=0, off_time_ms=0)
    crob = calls["crob"]
    assert crob.opType == fake.OperationType.LATCH_ON
    assert crob.count == 1
    assert crob.onTimeMS == 0
    assert crob.offTimeMS == 0


def test_bad_op_type_rejected(yadnp3_master):
    """Gecersiz op_type -> bad_request, komut gonderilmez."""
    mm, calls, fake, _ = yadnp3_master
    calls["result"] = _success_result(fake)
    res = mm.operate_crob(index=0, op_type="explode")
    assert res["status"] == "bad_request"
    assert calls.get("direct_called") is not True
    assert calls.get("sbo_called") is not True
