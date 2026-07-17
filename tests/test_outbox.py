"""Outbox SQLite persistent kuyruk: enqueue/dequeue/dead-letter/disk-full.

Kritik davranis testleri:
  * enqueue + delete + COUNT estimate dogru
  * max_pending limit asilinca OutboxFullError
  * mark_retry + move_to_dead_letter mantigi
  * Restart simulasyonu (yeni Outbox instance ayni db_path'i okur)
"""

from __future__ import annotations

import pytest

from dnp3_gateway.messaging.outbox import Outbox, OutboxFullError


@pytest.fixture
def outbox(tmp_path):
    """Her test ayri tmp DB ile baslar."""
    db = tmp_path / "test_outbox.db"
    ob = Outbox(db, max_pending=100)
    yield ob
    ob.close()


def _enqueue_n(outbox: Outbox, n: int) -> list[int]:
    """N mesaj enqueue eder; row id listesini doner."""
    ids: list[int] = []
    for i in range(n):
        row_id = outbox.enqueue(
            message_id=f"msg-{i}",
            correlation_id=f"corr-{i}",
            headers={"x": str(i)},
            payload={"value": i},
        )
        ids.append(row_id)
    return ids


def test_init_empty_db_has_zero_pending(tmp_path) -> None:
    ob = Outbox(tmp_path / "empty.db")
    assert ob.pending_count() == 0
    assert ob.pending_count_exact() == 0
    assert ob.dead_letter_count() == 0
    ob.close()


def test_enqueue_increments_pending(outbox: Outbox) -> None:
    _enqueue_n(outbox, 5)
    assert outbox.pending_count() == 5
    assert outbox.pending_count_exact() == 5  # estimate ile gercek esit


def test_fetch_batch_returns_oldest_first(outbox: Outbox) -> None:
    _enqueue_n(outbox, 3)
    rows = outbox.fetch_batch(limit=10)
    assert len(rows) == 3
    # ID asc — en eski ilk
    assert rows[0]["message_id"] == "msg-0"
    assert rows[2]["message_id"] == "msg-2"
    assert rows[0]["payload"] == {"value": 0}
    assert rows[0]["headers"] == {"x": "0"}


def test_delete_decrements_pending(outbox: Outbox) -> None:
    ids = _enqueue_n(outbox, 3)
    outbox.delete(ids[0])
    assert outbox.pending_count() == 2
    rows = outbox.fetch_batch()
    assert len(rows) == 2
    assert rows[0]["message_id"] == "msg-1"


def test_max_pending_raises_outbox_full_error(tmp_path) -> None:
    """max_pending'i asilinca OutboxFullError; counter degismez.

    NOT: Outbox max_pending'i max(1000, ...) ile clamp eder (DDoS koruma —
    saldirgan max=1 yapip enqueue'yu kilitleyemesin). Bu yuzden 1001 mesaj
    enqueue ediyoruz.
    """
    ob = Outbox(tmp_path / "full.db", max_pending=1000)
    _enqueue_n(ob, 1000)
    assert ob.pending_count() == 1000
    with pytest.raises(OutboxFullError):
        ob.enqueue(
            message_id="overflow",
            correlation_id=None,
            headers=None,
            payload={"v": 1},
        )
    # Counter artmadi
    assert ob.pending_count() == 1000
    ob.close()


def test_mark_retry_increments_counter(outbox: Outbox) -> None:
    ids = _enqueue_n(outbox, 1)
    outbox.mark_retry(ids[0], "boom")
    rows = outbox.fetch_batch()
    assert rows[0]["retry_count"] == 1
    assert rows[0]["last_error"] == "boom"
    outbox.mark_retry(ids[0], "still failing")
    rows = outbox.fetch_batch()
    assert rows[0]["retry_count"] == 2
    assert rows[0]["last_error"] == "still failing"


def test_move_to_dead_letter_removes_from_main_queue(outbox: Outbox) -> None:
    ids = _enqueue_n(outbox, 2)
    moved = outbox.move_to_dead_letter(ids[0], "poison")
    assert moved is True
    assert outbox.pending_count() == 1
    assert outbox.dead_letter_count() == 1
    # Geriye sadece msg-1 kalmali
    rows = outbox.fetch_batch()
    assert len(rows) == 1
    assert rows[0]["message_id"] == "msg-1"


def test_move_to_dead_letter_returns_false_for_missing(outbox: Outbox) -> None:
    assert outbox.move_to_dead_letter(99999, "not found") is False


def test_restart_preserves_messages(tmp_path) -> None:
    """Yeni Outbox instance ayni db'yi okumali."""
    db_path = tmp_path / "restart.db"
    ob1 = Outbox(db_path)
    _enqueue_n(ob1, 7)
    ob1.close()

    # Yeniden ac
    ob2 = Outbox(db_path)
    assert ob2.pending_count() == 7
    rows = ob2.fetch_batch()
    assert rows[0]["message_id"] == "msg-0"
    ob2.close()


def test_estimate_resync_after_restart(tmp_path) -> None:
    """Restart sonrasi _pending_estimate gercek COUNT ile sync olur."""
    db_path = tmp_path / "estimate.db"
    ob1 = Outbox(db_path)
    _enqueue_n(ob1, 10)
    ob1.close()

    ob2 = Outbox(db_path)
    assert ob2.pending_count() == ob2.pending_count_exact() == 10
    ob2.close()


def test_payload_with_unicode(outbox: Outbox) -> None:
    """JSON serileme Turkce karakterler dahil dogru calismali."""
    outbox.enqueue(
        message_id="unicode-test",
        correlation_id=None,
        headers={"label": "Üç noktalı"},
        payload={"name": "şahin", "value": "çok güzel"},
    )
    rows = outbox.fetch_batch()
    assert rows[0]["payload"]["name"] == "şahin"
    assert rows[0]["headers"]["label"] == "Üç noktalı"
