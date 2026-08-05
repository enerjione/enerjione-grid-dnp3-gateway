"""Kurulum modu <-> tasima yolu sozlesmesi.

Bu dosya `INSTALL_MODE` ayarinin ne vaat ettigini kilitler:

* YEREL kurulum NATS_URL'i ACIKCA ister. Alanin bir varsayilani var
  (`nats://localhost:4222`) ve yerel modda bu varsayilana dusmek sessiz bir
  tuzak: gateway kendi container'ina baglanmaya calisir, hicbir zaman
  baglanamaz ve yerel modda yedek yol OLMADIGI icin telemetri tamamen
  durur. Kurulumun ilk saniyesinde acik hata vermek, sahada saatler suren
  bir teshisten iyidir.
* UZAK kurulum ayni durumda calismaya devam etmeli (yedegi var).
* Secilen brokerlar moda gore dogru kurulmali — yerel modda HTTP publisher
  HIC OLUSTURULMAMALI ki "kazara" kullanilamasin.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dnp3_gateway.config import Settings
from dnp3_gateway.messaging.transport_router import TelemetryTransportRouter


def _ayar(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ------------------------------------------------------- yapilandirma sozlesmesi
def test_varsayilan_mod_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mevcut kurulumlar (INSTALL_MODE gondermeyen) davranis degistirmemeli."""
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.install_mode == "remote"


def test_yerel_modda_nats_url_verilmezse_acik_hata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NATS_URL", raising=False)
    with pytest.raises(ValidationError) as exc:
        _ayar(monkeypatch, INSTALL_MODE="local")
    metin = str(exc.value)
    assert "NATS_URL" in metin
    assert "local" in metin
    # Hata mesaji operatore ne yapacagini soylemeli.
    assert "INSTALL_MODE=remote" in metin


def test_yerel_modda_bos_nats_url_de_reddedilir(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _ayar(monkeypatch, INSTALL_MODE="local", NATS_URL="   ")


def test_yerel_modda_acik_nats_url_kabul_edilir(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _ayar(monkeypatch, INSTALL_MODE="local", NATS_URL="nats://nats:4222")
    assert s.install_mode == "local"
    assert s.nats_url == "nats://nats:4222"


def test_uzak_modda_nats_url_verilmese_de_calisir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uzak kurulumda varsayilana dusmek sorun degil — yedek yol var."""
    monkeypatch.delenv("NATS_URL", raising=False)
    s = _ayar(monkeypatch, INSTALL_MODE="remote")
    assert s.install_mode == "remote"


def test_gecersiz_mod_reddedilir(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="INSTALL_MODE"):
        _ayar(monkeypatch, INSTALL_MODE="yerel")


def test_http_rollback_secildiginde_yerel_kural_uygulanmaz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TELEMETRY_PUBLISHER=http bilincli bir rollback; NATS aranmaz."""
    monkeypatch.delenv("NATS_URL", raising=False)
    s = _ayar(monkeypatch, INSTALL_MODE="local", TELEMETRY_PUBLISHER="http")
    assert s.telemetry_publisher == "http"


# ------------------------------------------------------------ broker secimi
class _SahteJetStream:
    subject = "e1.telemetry.raw.GW-TEST"
    is_ready = True

    def close(self) -> None:  # pragma: no cover
        pass


def _broker_kur(monkeypatch: pytest.MonkeyPatch, cfg: Settings):
    """`_build_telemetry_broker`i gercek NATS/HTTP kurmadan calistir."""
    from dnp3_gateway import main as main_mod
    from dnp3_gateway.messaging import jetstream_publisher as js_mod

    monkeypatch.setattr(js_mod.JetStreamPublisher, "create", classmethod(lambda cls, **kw: _SahteJetStream()))
    olusturulan_http: list[str] = []

    def _sahte_http(cfg_: Settings, identity_):
        olusturulan_http.append("olusturuldu")
        return _SahteJetStream()

    monkeypatch.setattr(main_mod, "_build_http_broker", _sahte_http)

    class _Kimlik:
        gateway_code = "GW-TEST"

    broker = main_mod._build_telemetry_broker(cfg, _Kimlik())
    return broker, olusturulan_http


def test_yerel_modda_http_publisher_hic_olusturulmaz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yedek yol yoksa nesnesi de olmamali — kazara kullanilamasin."""
    cfg = _ayar(monkeypatch, INSTALL_MODE="local", NATS_URL="nats://nats:4222")
    broker, http_olustu = _broker_kur(monkeypatch, cfg)
    assert isinstance(broker, TelemetryTransportRouter)
    assert broker.fallback_enabled is False
    assert http_olustu == []


def test_uzak_modda_her_iki_yol_da_kurulur(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ayar(monkeypatch, INSTALL_MODE="remote", NATS_URL="nats://nats:4222")
    broker, http_olustu = _broker_kur(monkeypatch, cfg)
    assert isinstance(broker, TelemetryTransportRouter)
    assert broker.fallback_enabled is True
    assert http_olustu == ["olusturuldu"]
    assert broker.active_transport == "nats"


def test_rollback_modunda_router_kullanilmaz(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ayar(monkeypatch, INSTALL_MODE="remote", TELEMETRY_PUBLISHER="http")
    broker, http_olustu = _broker_kur(monkeypatch, cfg)
    assert not isinstance(broker, TelemetryTransportRouter)
    assert http_olustu == ["olusturuldu"]


# --------------------------------------------- health endpoint gozlemlenebilirligi
def test_health_govdesi_aktif_yolu_raporlar(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Su an veri hangi yoldan gidiyor?" tek GET ile cevaplanabilmeli."""
    from dnp3_gateway.health_server import _outbox_snapshot

    cfg = _ayar(monkeypatch, INSTALL_MODE="remote", NATS_URL="nats://nats:4222")
    router, _ = _broker_kur(monkeypatch, cfg)

    class _SahteResilient:
        _broker = router
        outbox_full = False

        def pending_count(self) -> int:
            return 0

        def dead_letter_count(self) -> int:
            return 0

    snap = _outbox_snapshot(_SahteResilient())
    tasima = snap["telemetry_transport"]
    assert tasima["active_transport"] == "nats"
    assert tasima["install_mode"] == "remote"
    assert tasima["fallback_enabled"] is True
