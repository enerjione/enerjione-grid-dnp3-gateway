"""Gateway mode'una gore uygun adapter'i ureten kucuk factory."""

from __future__ import annotations

import logging
from pathlib import Path

from dnp3_gateway.adapters.base import TelemetryReader
from dnp3_gateway.adapters.mock import MockTelemetryReader
from dnp3_gateway.config import Settings

logger = logging.getLogger(__name__)


# yadnp3 (PRIMARY) adapter'in OKUMADIGI, yalnizca legacy dnp3py dalinda
# kullanilan ayarlar. `.env.example`, docker sablonlari ve new_gateway.ps1
# bunlari set ediyor ve "sahada ayarlayin" diyordu; operator 4G link'te timeout
# alan cihazlar icin DNP3_RESPONSE_TIMEOUT_SEC'i buyutup servisi restart ediyor,
# HICBIR SEY DEGISMIYORDU — cunku primary adapter bu alani hic gormuyor.
# Ustelik log "adapter_selected ... scan=Xs baseline=Ys" basarak yanlis bir
# dogrulama hissi veriyordu. Artik varsayilandan farkli set edilmisse acikca
# uyariyoruz.
_YADNP3_IGNORED_SETTINGS = (
    "dnp3_response_timeout_sec",
    "dnp3_read_strategy",
    "dnp3_direct_max_points_per_read",
    "dnp3_direct_sparse_ratio",
    "dnp3_confirm_required",
    "dnp3_link_reset_on_connect",
    "dnp3_disable_unsolicited_on_connect",
    "dnp3_unsolicited_class_mask",
    "dnp3_log_raw_frames",
)


def _warn_ignored_dnp3_settings(settings: Settings) -> None:
    """Primary adapter'in yok saydigi ayarlar varsayilandan farkliysa uyar."""
    try:
        defaults = type(settings).model_fields
    except Exception:  # noqa: BLE001
        return
    changed = []
    for name in _YADNP3_IGNORED_SETTINGS:
        field = defaults.get(name)
        if field is None:
            continue
        current = getattr(settings, name, None)
        if current is not None and current != field.default:
            changed.append(f"{name.upper()}={current}")
    if changed:
        logger.warning(
            "dnp3_settings_ignored_by_yadnp3 %s — bu ayarlar YALNIZCA "
            "DNP3_LIBRARY=dnp3py (legacy) dalinda gecerlidir; aktif yadnp3 "
            "adapter'i bunlari YOK SAYAR. Degisikliginizin etkisi olmayacak.",
            ", ".join(changed),
        )


def build_adapter(settings: Settings) -> TelemetryReader:
    mode = settings.gateway_mode.strip().lower()
    if mode == "mock":
        logger.info("adapter_selected mode=mock")
        return MockTelemetryReader()

    if mode != "dnp3":
        raise ValueError(f"desteklenmeyen gateway_mode={settings.gateway_mode}")

    library = (settings.dnp3_library or "yadnp3").strip().lower()
    if library in ("yadnp3", "opendnp3"):
        from dnp3_gateway.adapters.dnp3_yadnp3_master import Yadnp3TelemetryReader

        _warn_ignored_dnp3_settings(settings)
        # Class 1/2/3 EVENT SCAN araligi. Eskiden dogrudan
        # `default_poll_interval_sec`e baglanmisti, ama bunlar farkli isler:
        # poll interval YAYIN turudur, scan interval cihaza SORMA sikligidir.
        # 401 cihazda scan=1s saniyede 401 DNP3 istegi uretiyordu.
        # 0 = eski davranis (poll interval ile ayni).
        event_scan_sec = settings.dnp3_event_scan_interval_sec or settings.default_poll_interval_sec
        okuyucu = Yadnp3TelemetryReader(
            local_address=settings.dnp3_local_address,
            default_dnp3_tcp_port=settings.dnp3_tcp_port,
            scan_interval_sec=event_scan_sec,
            baseline_interval_sec=settings.dnp3_event_baseline_interval_sec,
            manager_threads=settings.dnp3_manager_threads,
            time_sync=settings.dnp3_time_sync,
            publish_quality_flags=settings.dnp3_publish_quality_flags,
            # DNP3 IO thread sayisi icin olcek ipucu. Reader boot'ta, config
            # gelmeden once kuruldugu icin GERCEK cihaz sayisi burada bilinmez;
            # `MAX_PARALLEL_DEVICES` operatorun bekledigi olcegi tasiyan en
            # yakin degerdir. Sabit 4 thread 400 cihazda 100 cihaz/thread
            # demekti (bkz. dnp3_yadnp3_master.__init__).
            device_count_hint=settings.max_parallel_devices,
            # Kopuk cihazi kendiliginden yoklama: manuel refresh beklemeden
            # toparlanma (4G/GSM yari-acik soket senaryosu).
            lost_probe_interval_sec=settings.dnp3_lost_probe_interval_sec,
            lost_relink_after_probes=settings.dnp3_lost_relink_after_probes,
            # Smart oturum yasam dongusu: POLITIKA cihaz basina config'ten
            # gelir (`DeviceConfig.session_policy`); burada yalnizca kurulum
            # geneli yedek esik ve kalici kayit dosyasinin yolu tasinir.
            # Kayit dosyasi restart sonrasi uyuyan cihazlarin sahte comm_lost
            # firtinasi uretmesini onler (bkz. docs/HORSTMANN_SMART_MODE.md).
            smart_max_silence_sec=settings.dnp3_smart_max_silence_sec or None,
            session_store_path=str(
                Path(settings.gateway_state_dir) / f"session_state_{settings.gateway_code}.json"
            ),
        )
        # Log ADAPTER KURULDUKTAN SONRA: `manager_threads` eskiden "auto"
        # yaziyordu ve heuristigin GERCEKTE kac thread sectigi hicbir yerde
        # gorunmuyordu — operator 500 cihaza cikarken bunu dogrulayamiyordu.
        # `scan` de eskiden poll interval'i yaziyordu; artik ayri ayar var.
        logger.info(
            "adapter_selected mode=dnp3 library=yadnp3 (OpenDNP3) local_addr=%s default_tcp=%s "
            "scan=%ss%s baseline=%ss io_threads=%s time_sync=%s quality_flags_published=%s",
            settings.dnp3_local_address,
            settings.dnp3_tcp_port,
            event_scan_sec,
            "" if settings.dnp3_event_scan_interval_sec else " (poll ile ayni)",
            settings.dnp3_event_baseline_interval_sec,
            okuyucu.io_thread_count,
            settings.dnp3_time_sync,
            settings.dnp3_publish_quality_flags,
        )
        return okuyucu

    if library == "dnp3py":
        # Legacy: nfm-dnp3 (saf python). Group 110 yok, OpenDNP3 outstation
        # ile tutarsiz davranis. Sadece ozel durumlar icin.
        from dnp3_gateway.adapters.dnp3_master import Dnp3TelemetryReader

        logger.warning(
            "adapter_selected mode=dnp3 library=dnp3py (LEGACY, Group 110 yok). Onerilen: DNP3_LIBRARY=yadnp3"
        )
        return Dnp3TelemetryReader(
            local_address=settings.dnp3_local_address,
            default_dnp3_tcp_port=settings.dnp3_tcp_port,
            response_timeout_sec=settings.dnp3_response_timeout_sec,
            read_strategy=settings.dnp3_read_strategy,
            direct_max_points_per_read=settings.dnp3_direct_max_points_per_read,
            direct_sparse_ratio=settings.dnp3_direct_sparse_ratio,
            confirm_required=settings.dnp3_confirm_required,
            link_reset_on_connect=settings.dnp3_link_reset_on_connect,
            disable_unsolicited_on_connect=settings.dnp3_disable_unsolicited_on_connect,
            unsolicited_class_mask=settings.dnp3_unsolicited_class_mask,
            event_baseline_interval_sec=settings.dnp3_event_baseline_interval_sec,
            log_raw_frames=settings.dnp3_log_raw_frames,
        )

    raise ValueError(f"desteklenmeyen DNP3_LIBRARY={settings.dnp3_library!r} (yadnp3 | dnp3py)")
