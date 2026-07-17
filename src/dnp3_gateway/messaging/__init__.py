"""Telemetri yayinlama modulu + persistent outbox.

Mimari (0.4.x cutover sonrasi):

    poller -> ResilientPublisher.publish()
                 |
                 v
            JetStreamPublisher (NATS JetStream)  --basari--> done
                 |                                              ^
                 |  fail / NATS_unavailable                     |
                 v                                              |
              Outbox (SQLite, persistent, at-least-once)        |
                 |                                              |
                 v                                              |
            OutboxRetrier (background thread, exponential       |
                          backoff, dead-letter on poison) ------+

* PRIMARY publisher: `JetStreamPublisher`. Gateway artik telemetriyi DIRECT
  NATS JetStream subject'ine (`e1.telemetry.raw.<gateway_code>`) basar.
* RabbitMQ desteği 0.4.x'te tamamen KALDIRILMISTIR (legacy
  `rabbit_publisher.py` modulu silindi). Alarm/notification akisi backend
  tarafinda RabbitMQ'da kalir; gateway onunla ilgilenmez.
* `ResilientPublisher` broker'i `Outbox` ile sarar — broker fail edince
  mesaj outbox'a yazilir, retrier baglanti gelince bosaltir. Mesaj kaybi
  YOK; tag-engine idempotent oldugu icin at-least-once garanti yeterlidir.
"""

from dnp3_gateway.messaging.jetstream_publisher import (
    JetStreamNotReadyError,
    JetStreamPublisher,
    JetStreamPublishError,
)
from dnp3_gateway.messaging.outbox import Outbox, OutboxFullError, OutboxRetrier
from dnp3_gateway.messaging.resilient_publisher import ResilientPublisher

__all__ = [
    "JetStreamPublisher",
    "JetStreamPublishError",
    "JetStreamNotReadyError",
    "Outbox",
    "OutboxFullError",
    "OutboxRetrier",
    "ResilientPublisher",
]
