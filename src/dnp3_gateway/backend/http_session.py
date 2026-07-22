"""Ortak HTTP session factory — connection pool + retry.

Default `requests.Session()` connection pool'u kucuktur (pool_maxsize=10) ve
retry yoktur. Gateway'de poller (paralel worker'lar) + outbox retrier +
config-refresh + command-poll thread'leri ayni backend host'una es zamanli
istek atar; default pool doyunca keep-alive baglantilar bloke olur, her
istekte yeni TLS handshake yapilir -> yavaslik ve "Read timed out".

Bu factory:
  * pool_connections/pool_maxsize'i buyutur (tek host oldugu icin ikisi de esit)
  * SADECE idempotent istekleri (GET) ve 429/5xx'i retry eder (backoff ile);
    POST telemetri retry EDILMEZ (at-least-once outbox zaten hallediyor,
    cift yayin olmasin).

requests + urllib3 zaten kurulu; yeni bagimlilik yok.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_http_session(
    *,
    pool_maxsize: int = 32,
    retries: int = 2,
    verify: bool | str = True,
) -> requests.Session:
    """Connection-pooled + (idempotent) retry'li Session dondurur."""
    session = requests.Session()
    session.verify = verify  # type: ignore[assignment]
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.3,
        # Sadece idempotent GET retry edilir; POST (telemetri/komut sonucu)
        # retry EDILMEZ -> outbox/ledger cift yayin uretmesin.
        allowed_methods=frozenset({"GET"}),
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=pool_maxsize,
        pool_maxsize=pool_maxsize,
        max_retries=retry,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
