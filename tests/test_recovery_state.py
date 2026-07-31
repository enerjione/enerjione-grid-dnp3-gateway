"""_DeviceCache recovery state machine unit testleri.

Lost → recovering → online gecisini ve grace period timeout'unu test eder.
yadnp3 paketinin yuklu olmasini gerektirmez (sadece _DeviceCache import edilir).
"""

from __future__ import annotations

import time

from dnp3_gateway.adapters.dnp3_yadnp3_master import _DeviceCache


def test_initial_state_is_lost() -> None:
    cache = _DeviceCache()
    assert cache.state() == "lost"
    assert cache.recovery_age() == 0.0


def test_begin_recovery_transitions_to_recovering() -> None:
    cache = _DeviceCache()
    cache.begin_recovery()
    assert cache.state() == "recovering"
    # Grace period saymaya basladi
    assert cache.recovery_age() >= 0.0


def test_fresh_frame_during_recovering_confirms_online() -> None:
    """OnOpen sonrasi gelen ilk cache.set() recovery'yi onaylar."""
    cache = _DeviceCache()
    cache.begin_recovery()
    assert cache.state() == "recovering"
    assert not cache.consume_recovery_publish()
    # Cihazdan ilk frame gelir
    cache.set(30, 0, 230.5)
    assert cache.state() == "online"
    # Tek seferlik flag tuketildi
    assert cache.consume_recovery_publish() is True
    # Ikinci consume False doner
    assert cache.consume_recovery_publish() is False


def test_set_in_lost_state_does_not_promote_to_online() -> None:
    """begin_recovery cagirilmadan gelen frame'ler state'i degistirmez.

    Bu durumda zaten link de connected=False olabilir; SCADA komut farkli
    bir konumda. set() recovery confirm etmemeli."""
    cache = _DeviceCache()
    assert cache.state() == "lost"
    cache.set(30, 0, 230.5)
    assert cache.state() == "lost"
    assert not cache.consume_recovery_publish()


def test_fail_recovery_returns_to_lost() -> None:
    cache = _DeviceCache()
    cache.begin_recovery()
    cache.fail_recovery()
    assert cache.state() == "lost"
    assert not cache.consume_recovery_publish()


def test_set_connected_false_resets_state_and_flags() -> None:
    """Link tamamen koparsa state lost'a duser, recovery flag'leri silinir."""
    cache = _DeviceCache()
    cache.begin_recovery()
    cache.set(30, 0, 230.5)
    assert cache.state() == "online"
    cache.set_connected(False)
    assert cache.state() == "lost"
    # Pending publish flag de temizlenmis olmali
    assert not cache.consume_recovery_publish()


def test_begin_recovery_idempotent() -> None:
    """Iki kez begin_recovery cagrilirsa grace sayaci sifirlanmaz."""
    cache = _DeviceCache()
    cache.begin_recovery()
    age1 = cache.recovery_age()
    time.sleep(0.05)
    cache.begin_recovery()  # tekrar cagir
    age2 = cache.recovery_age()
    # Ikinci cagri grace'i sifirlamaz; yas artmaya devam eder
    assert age2 >= age1


def test_mark_all_dirty_force_publishes_existing_values() -> None:
    cache = _DeviceCache()
    cache.set(30, 0, 230.5)
    cache.set(30, 1, 100.0)
    cache.clear_dirty(30, 0)
    cache.clear_dirty(30, 1)
    assert not cache.is_dirty(30, 0)
    n = cache.mark_all_dirty()
    assert n == 2
    assert cache.is_dirty(30, 0)
    assert cache.is_dirty(30, 1)


def test_recovery_publish_flag_only_set_once_per_recovery() -> None:
    """Recovery confirmed sonrasi gelen ek frame'ler pending flag'i tekrar set etmez.

    Pending flag tek seferlik 'recovery cycle'i baladi' isareti; sonraki normal
    set'ler bunu yeniden tetiklememeli, aksi halde her frame'de mark_all_dirty
    tetiklenir."""
    cache = _DeviceCache()
    cache.begin_recovery()
    cache.set(30, 0, 1.0)  # recovery confirmed
    assert cache.consume_recovery_publish()
    # Ikinci frame: state online, pending publish False
    cache.set(30, 1, 2.0)
    assert not cache.consume_recovery_publish()
