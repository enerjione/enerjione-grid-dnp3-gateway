from threading import Event

from dnp3_gateway.backend import PendingCommand
from dnp3_gateway.main import _run_command_poll
from dnp3_gateway.state import GatewayState


class _CommandClient:
    gateway_code = "GW-001"

    def __init__(self, stop_event: Event) -> None:
        self._stop_event = stop_event
        self.calls = 0

    def fetch_pending_commands(self) -> tuple[PendingCommand, ...]:
        self.calls += 1
        self._stop_event.set()
        return (PendingCommand(id=1, device_code="DEV-1", command="reset", dnp3_index=0),)


def test_command_poll_enqueues_commands_and_stops_promptly() -> None:
    stop_event = Event()
    client = _CommandClient(stop_event)
    state = GatewayState()

    _run_command_poll(client=client, state=state, stop_event=stop_event, poll_sec=1.0)

    assert client.calls == 1
    assert [command.id for command in state.take_pending_commands()] == [1]
