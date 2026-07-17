"""health_server.py: rate-limiter + trusted-proxy CIDR parser + IP helpers.

Server end-to-end testleri agir (port bind, thread, HTTP client). Burada
critical bilesenleri unit-level test ediyoruz:
  * `_SlidingWindowRateLimiter.allow` — sayim + localhost muafiyet + cleanup
  * `_parse_trusted_proxies` — CIDR parse + bos/gecersiz davranis
  * `_ip_in_networks` — XFF trust boundary mantigi
"""

from __future__ import annotations

import time

import pytest

from dnp3_gateway.health_server import (
    _ip_in_networks,
    _parse_trusted_proxies,
    _SlidingWindowRateLimiter,
)


class TestSlidingWindowRateLimiter:
    def test_allows_up_to_limit(self) -> None:
        rl = _SlidingWindowRateLimiter(max_per_window=3, window_sec=60.0)
        for _ in range(3):
            assert rl.allow("10.0.0.1") is True
        assert rl.allow("10.0.0.1") is False  # 4. istek reddedilir

    def test_localhost_always_allowed(self) -> None:
        rl = _SlidingWindowRateLimiter(max_per_window=1, window_sec=60.0)
        # 127.0.0.1 ve ::1 muaf — limit 1 olsa bile 100 istek geçer
        for _ in range(100):
            assert rl.allow("127.0.0.1") is True
            assert rl.allow("::1") is True
            assert rl.allow("localhost") is True

    def test_empty_key_allowed(self) -> None:
        """IP bulunamadiysa (degerlendirme yapamayiz) izin verilir; aksi halde
        broken request rate-limit'i tetiklerdi."""
        rl = _SlidingWindowRateLimiter(max_per_window=1, window_sec=60.0)
        assert rl.allow("") is True
        assert rl.allow("") is True

    def test_different_ips_independent_buckets(self) -> None:
        rl = _SlidingWindowRateLimiter(max_per_window=2, window_sec=60.0)
        assert rl.allow("10.0.0.1") is True
        assert rl.allow("10.0.0.1") is True
        assert rl.allow("10.0.0.1") is False  # ilkin bucket'i dolu
        # Farkli IP'nin bucket'i bagimsiz
        assert rl.allow("10.0.0.2") is True
        assert rl.allow("10.0.0.2") is True

    def test_window_slides_after_time(self) -> None:
        """Window suresi gectikten sonra eski istekler sayilmaz.

        NOT: _SlidingWindowRateLimiter window_sec'i max(1.0, ...) ile
        clamp eder (DDoS koruma — saldirgan window=0.001 yapip bypass
        edemesin). Bu yuzden test 1.2 saniye gercek bekler.
        """
        rl = _SlidingWindowRateLimiter(max_per_window=2, window_sec=1.0)
        assert rl.allow("10.0.0.1") is True
        assert rl.allow("10.0.0.1") is True
        assert rl.allow("10.0.0.1") is False
        # 1.1sn bekle — window asilir
        time.sleep(1.1)
        assert rl.allow("10.0.0.1") is True

    def test_cleanup_stale_removes_old_buckets(self) -> None:
        """cleanup_stale eskimis IP entry'lerini siler (memory leak onlemi).

        window_sec=1.0 (en alt sinir); 1.2sn bekleme ile bucket eskir.
        """
        rl = _SlidingWindowRateLimiter(max_per_window=10, window_sec=1.0)
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            rl.allow(ip)
        # 1.2sn bekle — tum istekler window disinda
        time.sleep(1.2)
        removed = rl.cleanup_stale()
        assert removed == 3
        # Sonraki cleanup hicbir sey silmemeli
        assert rl.cleanup_stale() == 0

    def test_cleanup_keeps_active_buckets(self) -> None:
        """Window icindeki bucket'lar silinmez."""
        rl = _SlidingWindowRateLimiter(max_per_window=10, window_sec=60.0)
        rl.allow("10.0.0.1")
        removed = rl.cleanup_stale()
        # 10.0.0.1 hala window icinde
        assert removed == 0


class TestParseTrustedProxies:
    def test_empty_returns_empty_list(self) -> None:
        assert _parse_trusted_proxies("") == []
        assert _parse_trusted_proxies("   ") == []

    def test_single_cidr(self) -> None:
        nets = _parse_trusted_proxies("10.0.0.0/8")
        assert len(nets) == 1
        assert str(nets[0]) == "10.0.0.0/8"

    def test_multiple_cidrs_comma_separated(self) -> None:
        nets = _parse_trusted_proxies("10.0.0.0/8, 192.168.1.5/32, 172.16.0.0/12")
        assert len(nets) == 3

    def test_invalid_entry_skipped_with_warning(self, caplog) -> None:
        nets = _parse_trusted_proxies("10.0.0.0/8,not-a-cidr,192.168.1.0/24")
        # Sadece gecerli iki CIDR kalir
        assert len(nets) == 2
        # Gecersiz giris log'a yazildi
        assert "trusted_proxies_parse_failed" in caplog.text or "atlandi" in caplog.text

    def test_ipv6_cidr(self) -> None:
        nets = _parse_trusted_proxies("2001:db8::/32")
        assert len(nets) == 1


class TestIpInNetworks:
    def test_ip_in_single_cidr(self) -> None:
        nets = _parse_trusted_proxies("10.0.0.0/8")
        assert _ip_in_networks("10.5.3.2", nets) is True
        assert _ip_in_networks("192.168.1.1", nets) is False

    def test_ip_in_multiple_cidrs(self) -> None:
        nets = _parse_trusted_proxies("10.0.0.0/8,192.168.1.0/24")
        assert _ip_in_networks("10.0.0.1", nets) is True
        assert _ip_in_networks("192.168.1.5", nets) is True
        assert _ip_in_networks("172.16.0.1", nets) is False

    def test_empty_networks_returns_false(self) -> None:
        """Trusted list bos -> hicbir IP guvenilir degil (en guvenli default)."""
        assert _ip_in_networks("10.0.0.1", []) is False

    def test_invalid_ip_returns_false(self) -> None:
        nets = _parse_trusted_proxies("10.0.0.0/8")
        assert _ip_in_networks("not-an-ip", nets) is False
        assert _ip_in_networks("", nets) is False
