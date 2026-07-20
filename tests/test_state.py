from dnp3_gateway.backend import PendingCommand
from dnp3_gateway.state import GatewayState

from .conftest import make_device, make_gateway_config, make_signal


def test_update_sets_active_and_returns_true_on_version_change() -> None:
    state = GatewayState()
    config = make_gateway_config(version="v1")
    assert state.update(config) is True
    assert state.is_active() is True
    assert state.config_version() == "v1"

    # Ayni versiyon tekrar gelirse False doner
    assert state.update(config) is False

    # Degisen versiyon yine True
    config2 = make_gateway_config(version="v2")
    assert state.update(config2) is True


def test_due_devices_honors_poll_interval() -> None:
    state = GatewayState()
    device = make_device("DEV-A", poll_interval_sec=5)
    state.update(make_gateway_config(devices=[device]))

    now = 100.0
    # Ilk cagride hic kayitsiz oldugu icin due
    assert [d.code for d in state.due_devices(now)] == ["DEV-A"]

    state.mark_read("DEV-A", now)
    # Interval dolmadan tekrar due degil
    assert state.due_devices(now + 2) == []
    # Interval dolunca tekrar due
    assert [d.code for d in state.due_devices(now + 5)] == ["DEV-A"]


def test_removed_device_read_tracking_cleared() -> None:
    state = GatewayState()
    dev_a = make_device("A")
    dev_b = make_device("B")
    state.update(make_gateway_config(devices=[dev_a, dev_b]))
    state.mark_read("A", 10.0)
    state.mark_read("B", 10.0)

    # B cikartilirsa B'nin son okuma kaydi da silinmeli
    state.update(make_gateway_config(devices=[dev_a], version="v2"))
    assert state.snapshot()["device_count"] == 1
    # A icin son okuma devam ediyor, due degil
    assert state.due_devices(12.0) == []


def test_enqueue_pending_commands_deduplicates_ids() -> None:
    state = GatewayState()
    first = PendingCommand(id=1, device_code="DEV-1", command="reset", dnp3_index=0)
    duplicate = PendingCommand(id=1, device_code="DEV-1", command="reset", dnp3_index=0)
    second = PendingCommand(id=2, device_code="DEV-2", command="pulse", dnp3_index=1)

    assert state.enqueue_pending_commands([first, duplicate, second]) == 2
    assert [command.id for command in state.take_pending_commands()] == [1, 2]
    assert state.enqueue_pending_commands([first, second]) == 0
    assert state.take_pending_commands() == []


def test_signals_returns_copy() -> None:
    state = GatewayState()
    sig = make_signal("master.test")
    state.update(make_gateway_config(signals=[sig]))
    out = state.signals()
    out.clear()
    # state icindeki liste degismemis olmali
    assert len(state.signals()) == 1
