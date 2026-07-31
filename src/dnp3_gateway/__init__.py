"""EnerjiOne DNP3 Gateway paketi.

DNP3 protokolu uzerinden saha outstation cihazlarina TCP master olarak
baglanan, okudugu sinyalleri normalize ederek NATS JetStream uzerinden
EnerjiOne Grid backend'inin tag-engine servisine ileten standalone gateway
servisi.

Telemetri yayin yolu (0.4.x ve sonrasi):
    DNP3 cihaz -> adapter -> poller -> ResilientPublisher
        -> JetStreamPublisher -> NATS subject `e1.telemetry.raw.<GATEWAY_CODE>`
        -> backend stream TELEMETRY_RAW -> tag-engine

NATS bagi yoksa mesajlar SQLite outbox'a yazilir, baglanti gelince retrier
bosaltir (at-least-once).
"""

from pathlib import Path

__all__ = ["__version__"]


def _load_version() -> str:
    """Paket surumu.

    Once kurulu paket metadata'sindan okunur (`pip install .` ile kurulmus
    normal dagitimlarda repo koku YOKTUR; eskiden bu durumda surum sessizce
    "0.0.0" oluyordu ve /health, User-Agent, log satirlari yanlis surum
    raporluyordu). Metadata yoksa (repo'dan dogrudan calistirma, editable
    kurulum) repo kokundeki VERSION dosyasina duseriz.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("enerjione-dnp3-gateway")
    except PackageNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — metadata okunamadi, dosyaya dus
        pass

    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = _load_version()
