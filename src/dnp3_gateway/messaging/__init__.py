"""Telemetri yayinlama modulu + persistent outbox.

Varsayilan akis:

    poller -> ResilientPublisher.publish()
                 |
                 v
            HttpTelemetryPublisher  --basari--> backend /telemetry/gateway/{code}
                 |
                 |  fail / backend_unavailable
                 v
              Outbox (SQLite, persistent, at-least-once)
                 |
                 v
            OutboxRetrier (background thread, exponential backoff)

JetStreamPublisher legacy/rollback icin durur; TELEMETRY_PUBLISHER=nats ile acilir.
"""

from dnp3_gateway.messaging.command_ledger import CommandLedger
from dnp3_gateway.messaging.http_publisher import (
    HttpTelemetryNotReadyError,
    HttpTelemetryPublisher,
    HttpTelemetryPublishError,
)
from dnp3_gateway.messaging.jetstream_publisher import (
    JetStreamNotReadyError,
    JetStreamPublisher,
    JetStreamPublishError,
)
from dnp3_gateway.messaging.outbox import Outbox, OutboxFullError, OutboxRetrier
from dnp3_gateway.messaging.resilient_publisher import ResilientPublisher

__all__ = [
    "CommandLedger",
    "HttpTelemetryPublisher",
    "HttpTelemetryPublishError",
    "HttpTelemetryNotReadyError",
    "JetStreamPublisher",
    "JetStreamPublishError",
    "JetStreamNotReadyError",
    "Outbox",
    "OutboxFullError",
    "OutboxRetrier",
    "ResilientPublisher",
]
