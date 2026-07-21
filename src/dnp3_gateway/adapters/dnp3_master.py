"""FALLBACK DNP3 master adapter'i (nfm-dnp3 / dnp3py tabanli, saf Python).

Saha DNP3 outstation'larina TCP uzerinden DNP3 master olarak baglanir;
backend sinyal katalogundaki `dnp3_object_group` + `dnp3_index` ile esleşen
degerler okunur ve `SignalReading` listesine donusturulur.

NOT: Production'da ONERILEN adapter `dnp3_yadnp3_master.py` (yadnp3 / OpenDNP3
native binding). Bu modul:
  * Group 110 (Octet String) sinyallerini DESTEKLEMEZ
  * Bazi OpenDNP3 outstation'larla "Transport segment data too short" /
    TCP RST sorunlari yasayabilir (kutuphane uyumsuzlugu)

Tercih edilen sebep: yadnp3 wheel saglanamayan platformlar veya saf-Python
deploy ihtiyaci.

Her cihaz icin `Dnp3DeviceSession` tutulur; varsayilan strateji `direct`
(sadece katalogdaki DNP3 grup/index aralik okumalari). `integrity` / `class0`
cihaz/SCADA uyarlamasina gore .env: `DNP3_READ_STRATEGY` ile secilir.

> `nfm-dnp3` (import adi: `dnp3py`) saf Python'dur ve Windows dahil
> tekerlek ile kurulur. Mock modda bu modul yuklenmez.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Any, TypeVar

from dnp3_gateway.adapters.base import SignalReading, TelemetryReader
from dnp3_gateway.backend import DeviceConfig, SignalConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Dnp3AdapterError(RuntimeError):
    """DNP3 adapter'inda olusan hata."""


# Mock modda `dnp3py` yuklu olmayabilir; gercek modda ImportError’da anlamli mesaj.
try:  # pragma: no cover - opsiyonel bagimlilik
    from dnp3py import DNP3Config, DNP3Master
    from dnp3py.core.exceptions import DNP3Error
    from dnp3py.core.master import PollResult

    _DNP3_AVAILABLE = True
    _DNP3_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001
    DNP3Config = None  # type: ignore[misc, assignment]
    DNP3Master = None  # type: ignore[misc, assignment]
    PollResult = Any  # type: ignore[misc, assignment]
    DNP3Error = RuntimeError  # type: ignore[misc, assignment]
    _DNP3_AVAILABLE = False
    _DNP3_IMPORT_ERROR = _exc

def _install_empty_frame_filter(master: Any) -> None:
    """Link-layer kontrol cercevelerini (ACK/NACK/RESET_LINK_ACK) transport'a beslemeden filtreler.

    nfm-dnp3'un bilinen bir sorunu: outstation user_data'si bos olan link-layer
    kontrol cercevesi gonderdiginde, _receive_response bu cerceveyi transport'a
    feed eder ve `TransportSegment.from_bytes(b"")` 'Transport segment data too
    short' hatasi atar. Bizim wrap, _receive_frame'i sarmalayip bos user_data
    ceren cerceveleri skip eder; boylece reassemble sadece anlamli APDU
    fragment'lariyla calisir.
    """
    original_receive_frame = master._receive_frame

    def filtered_receive_frame(timeout: float | None = None) -> Any:
        deadline = time.monotonic() + float(timeout or master.config.response_timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # orjinal timeout exception'ini ureten yola dus
                return original_receive_frame(0.001)
            frame = original_receive_frame(remaining)
            if frame.user_data:
                return frame
            logger.debug(
                "dnp3_link_control_frame_skipped src=%s dst=%s ctrl=0x%02X",
                getattr(frame, "source", "?"),
                getattr(frame, "destination", "?"),
                getattr(frame, "control", 0),
            )

    master._receive_frame = filtered_receive_frame


_OBJECT_GROUP_BINARY_INPUT = 1
_OBJECT_GROUP_BINARY_OUTPUT = 10
_OBJECT_GROUP_COUNTER = 20
_OBJECT_GROUP_ANALOG_INPUT = 30
_OBJECT_GROUP_ANALOG_OUTPUT = 40
_OBJECT_GROUP_STRING = 110

_READ_INTEGRITY = "integrity"
_READ_CLASS0 = "class0"
_READ_DIRECT = "direct"
_READ_EVENT_DRIVEN = "event_driven"
# Grup 30: analog nokta basina daha cok bayt; buyuk aralik READ sikca timeout (OpenDNP3 + dnp3py)
_ANALOG_GROUPS_MAX_CHUNK = 12


def _normalize_read_strategy(value: str) -> str:
    s = (value or _READ_EVENT_DRIVEN).strip().lower()
    if s in ("integrity", "full", "all"):
        return _READ_INTEGRITY
    if s in ("class0", "class_0", "c0", "static"):
        return _READ_CLASS0
    if s in ("direct", "range", "by_index", "points"):
        return _READ_DIRECT
    if s in ("event_driven", "event-driven", "events", "unsolicited", "unsol"):
        return _READ_EVENT_DRIVEN
    logger.warning("dnp3_read_strategy_bilinmeyen value=%r default=event_driven", value)
    return _READ_EVENT_DRIVEN


def _point_raw_from_object(
    group: int,
    obj: Any,
) -> float:
    if group in (_OBJECT_GROUP_BINARY_INPUT, _OBJECT_GROUP_BINARY_OUTPUT):
        return 1.0 if obj.value else 0.0
    return float(obj.value)


def _direct_range_ok_for_group(group: int) -> bool:
    """Aralik (chunk) READ destegi: tum okunabilir DNP3 grup tipleri (1/10/20/30/40).

    Hata gelirse `_direct_fill_range_chunked` zaten sequential fallback'a duser;
    range READ tek istekte cok nokta getirdigi icin 100+ noktada cycle suresini
    onemli olcude dusurur (200 sinyal -> dakikalar yerine saniyeler).
    """
    return group in (
        _OBJECT_GROUP_BINARY_INPUT,
        _OBJECT_GROUP_BINARY_OUTPUT,
        _OBJECT_GROUP_COUNTER,
        _OBJECT_GROUP_ANALOG_INPUT,
        _OBJECT_GROUP_ANALOG_OUTPUT,
    )


def _effective_read_chunk_size(base_max: int, group: int) -> int:
    """Parcali aralik sadece grup 30 icin kullanilir."""
    if group == _OBJECT_GROUP_ANALOG_INPUT:
        return min(base_max, _ANALOG_GROUPS_MAX_CHUNK)
    return base_max


def _find_index(items: list[T], index: int) -> T | None:
    for it in items:
        if it.index == index:  # type: ignore[attr-defined]
            return it
    return None


class Dnp3DeviceSession:
    """Tek outstation baglantisi: `dnp3py.DNP3Master` yasam dongusu."""

    def __init__(
        self,
        *,
        device: DeviceConfig,
        local_address: int,
        tcp_port: int,
        response_timeout_sec: int,
        read_strategy: str,
        direct_max_points_per_read: int = 24,
        direct_sparse_ratio: int = 4,
        confirm_required: bool = False,
        link_reset_on_connect: bool = True,
        disable_unsolicited_on_connect: bool = True,
        unsolicited_class_mask: int = 7,
        event_baseline_interval_sec: int = 60,
        log_raw_frames: bool = False,
    ) -> None:
        if not _DNP3_AVAILABLE or DNP3Config is None or DNP3Master is None:
            raise Dnp3AdapterError(
                "dnp3 (nfm-dnp3) yuklu degil. `pip install nfm-dnp3` veya "
                f"GATEWAY_MODE=mock kullanin. Import: {_DNP3_IMPORT_ERROR}"
            )
        self.device = device
        self.local_address = local_address
        self.tcp_port = tcp_port
        self.response_timeout_sec = response_timeout_sec
        self._read_strategy = _normalize_read_strategy(read_strategy)
        self._direct_max_points = max(1, int(direct_max_points_per_read))
        self._direct_sparse_ratio = max(2, int(direct_sparse_ratio))
        self._confirm_required = bool(confirm_required)
        self._link_reset_on_connect = bool(link_reset_on_connect)
        self._disable_unsolicited_on_connect = bool(disable_unsolicited_on_connect)
        self._unsolicited_class_mask = max(0, min(7, int(unsolicited_class_mask)))
        self._event_baseline_interval_sec = max(5, int(event_baseline_interval_sec))
        self._log_raw_frames = bool(log_raw_frames)
        self._master: Any | None = None
        self._lock = threading.Lock()
        # Event-driven cache REFERANSI: device-level olarak Reader tarafindan
        # tutulan dict gecirilir. Session yeniden olusturulsa bile (TCP kopmasi
        # halinde) cache korunur; her cycle'da eksik gruplari tamamlar.
        # Default bos dict (standalone test/eski kullanim).
        self._event_value_cache: dict[tuple[int, int], float] = {}
        self._event_baseline_at: float = 0.0
        self.connection_fingerprint: tuple[str, str, int, int, int] = (
            self.device.code,
            (self.device.ip_address or "").strip(),
            int(self.tcp_port),
            int(self.device.dnp3_address),
            int(self.local_address),
        )

    def connect(self) -> None:
        if self._master is not None:
            return
        logger.info(
            "dnp3_session_connecting device=%s ip=%s:%s remote_addr=%s local_addr=%s",
            self.device.code,
            self.device.ip_address,
            self.tcp_port,
            self.device.dnp3_address,
            self.local_address,
        )
        try:
            cfg = DNP3Config(
                host=self.device.ip_address,
                port=self.tcp_port,
                master_address=self.local_address,
                outstation_address=self.device.dnp3_address,
                response_timeout=float(self.response_timeout_sec),
                log_level="DEBUG" if self._log_raw_frames else "WARNING",
                log_raw_frames=self._log_raw_frames,
                # OpenDNP3 (yadnp3) outstation: onayli PRM ile bazi ortamlar timeout verir
                confirm_required=self._confirm_required,
            )
            self._master = DNP3Master(cfg)
            # dnp3py: `connect()` sadece context manager; TCP acilisi `open()`.
            self._master.open()
            # Bos user_data'li link-layer cerceveleri (ACK/NACK/RESET_LINK_ACK)
            # transport reassemble'a girmeden once filtrele — yoksa
            # 'Transport segment data too short' bilinen kutuphane kusuru tetiklenir.
            _install_empty_frame_filter(self._master)
            if self._link_reset_on_connect:
                reset_fn = getattr(self._master, "_reset_link", None)
                if callable(reset_fn):
                    try:
                        reset_fn()
                        logger.info("dnp3_link_reset_ok device=%s", self.device.code)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "dnp3_link_reset_failed device=%s error=%s",
                            self.device.code,
                            exc,
                        )
                # OpenDNP3 outstation reset_link sonrasi internal state'i ayarlamak
                # icin ~200-300ms ihtiyac duyuyor. Hemen READ atilirsa frame
                # confused -> timeout/RST. Standalone test bu bekleme ile %100 OK.
                time.sleep(0.3)
            # disable_unsolicited bazi outstation'larda (saha cihazlari / OpenDNP3 sim)
            # connection'i tamamen kopariyor; event_driven mod zaten unsolicited
            # FRAME'lerini empty-frame filtresi + read_class polling ile saglikli
            # ele aliyor — bu yuzden event_driven'da disable cagrisini ATLA.
            should_disable = (
                self._disable_unsolicited_on_connect
                and self._unsolicited_class_mask > 0
                and self._read_strategy != _READ_EVENT_DRIVEN
            )
            if should_disable:
                try:
                    ok = self._master.disable_unsolicited(self._unsolicited_class_mask)
                    logger.info(
                        "dnp3_disable_unsolicited device=%s class_mask=%s ok=%s",
                        self.device.code,
                        self._unsolicited_class_mask,
                        ok,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dnp3_disable_unsolicited_failed device=%s error=%s",
                        self.device.code,
                        exc,
                    )
                time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            self._master = None
            raise Dnp3AdapterError(
                f"DNP3 baglanti kurulamadi device={self.device.code} "
                f"ip={self.device.ip_address}:{self.tcp_port} error={exc}"
            ) from exc

    def attach_cache(
        self,
        cache: dict[tuple[int, int], float],
        baseline_at_ref: list[float],
    ) -> None:
        """Reader katmanindan device-level cache + baseline timestamp ref'i alir.

        Boylece session yeniden olusturulsa bile (TCP koparsa) cache korunur;
        eksik gruplar zaman icinde tamamlanir. baseline_at_ref tek elemanli list
        (mutable container) olarak gecer ki session timestamp'i Reader'da
        kalici olarak guncelleyebilsin.
        """
        self._event_value_cache = cache
        # _event_baseline_at session-local kalir ama bilgi olarak ref'ten okunur
        if baseline_at_ref:
            self._event_baseline_at = float(baseline_at_ref[0])
        self._baseline_at_ref = baseline_at_ref  # type: ignore[attr-defined]

    def close(self) -> None:
        with self._lock:
            if self._master is None:
                return
            try:
                self._master.close()
            except Exception:  # noqa: BLE001
                logger.debug("dnp3_session_close_failed", exc_info=True)
            finally:
                self._master = None

    def _master_connected(self) -> bool:
        """Master TCP soketi hala canli mi? dnp3py.DNP3Master.is_connected bir property."""
        m = self._master
        if m is None:
            return False
        try:
            return bool(m.is_connected)
        except Exception:  # noqa: BLE001
            return False

    def _mark_disconnected(self) -> None:
        """Lock altinda zaten cagrilir; master'i None'a cek, ust katmanin reset etmesi icin."""
        try:
            if self._master is not None:
                self._master.close()
        except Exception:  # noqa: BLE001
            logger.debug("dnp3_disconnected_close_failed", exc_info=True)
        self._master = None

    def read_batch(self, signals: list[SignalConfig]) -> list[tuple[float, str]]:
        """Stratejiye gore (event_driven | direct | class0 | integrity) cihazdan okur.

        Event-driven mod: Class 1+2+3 event poll + periyodik Class 0 baseline.
        Sadece DEGISEN noktalar 'good' kalitesi ile doner; geri kalanlar
        'no_change' (poller bunlari yayinlamaz). 600+ cihazli kurulumda
        bant genisligi cok dusuk: cogu cycle'da event class poll bos doner.
        """
        with self._lock:
            if self._master is None:
                raise Dnp3AdapterError(f"device session closed device={self.device.code}")
            master = self._master

            if not signals:
                return []
            if self._read_strategy == _READ_EVENT_DRIVEN:
                return self._read_batch_event_driven(master, signals)
            if self._read_strategy == _READ_DIRECT:
                return self._read_batch_direct(master, signals)

            poll: Any = None
            last_err: str | None = None
            # Outstation bazen ilk poll'a cevap vermez (warm-up sonrasi bile);
            # 2 deneme ile geri donen ilk basarili poll'u kullan. Cogu OpenDNP3
            # outstation 1 basarili response sonrasi TCP'yi kapatir; o yuzden
            # uzun retry yerine az deneme + session evict (ust katmanda) ile
            # her cycle baslangicta yeni session ile temiz bir baslangic yap.
            for attempt in range(2):
                try:
                    if self._read_strategy == _READ_CLASS0:
                        poll = master.read_class(0)
                    else:
                        poll = master.integrity_poll()
                except DNP3Error as exc:
                    last_err = str(exc)
                    logger.debug(
                        "dnp3_poll_dnp3_error device=%s attempt=%s error=%s",
                        self.device.code,
                        attempt + 1,
                        exc,
                    )
                    poll = None
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    logger.debug(
                        "dnp3_poll_exception device=%s attempt=%s error=%s",
                        self.device.code,
                        attempt + 1,
                        exc,
                    )
                    poll = None
                    continue
                if getattr(poll, "success", False):
                    break
                last_err = getattr(poll, "error", "poll failed")
                logger.debug(
                    "dnp3_poll_retry device=%s attempt=%s error=%s",
                    self.device.code,
                    attempt + 1,
                    last_err,
                )
                poll = None

            if poll is None:
                logger.warning(
                    "dnp3_poll_unsuccessful device=%s strategy=%s error=%s",
                    self.device.code,
                    self._read_strategy,
                    last_err or "poll failed",
                )
                # Cogu OpenDNP3 outstation TIMEOUT veya hatali response sonrasi
                # baglantiyi karisik durumda birakir; her durumda session'i
                # at, sonraki cycle taze bir TCP + reset_link ile yeniden basla.
                self._mark_disconnected()
                # Fail durumunda 'invalid' yerine 'no_change' dondur ki frontend
                # son iyi degeri korusun (poller no_change'i yayinlamaz).
                return [(_invalid_raw(), "no_change") for _ in signals]

            result = self._read_batch_from_poll(poll, master, signals)
            # OpenDNP3 outstation 1 basarili response sonrasi TCP'yi tek tarafli
            # kapatma egiliminde. Her durumda (kapali olsun olmasin) session'i
            # bos at — sonraki cycle taze socket + reset_link ile baslayacak.
            self._mark_disconnected()
            return result

    def _read_batch_from_poll(
        self,
        poll: Any,
        master: Any,
        signals: list[SignalConfig],
    ) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        for signal in signals:
            try:
                raw = self._raw_for_signal(poll, master, signal)
                out.append((raw, "good"))
            except Dnp3AdapterError as exc:
                logger.debug(
                    "dnp3_point_unresolved device=%s signal=%s error=%s",
                    self.device.code,
                    signal.key,
                    exc,
                )
                # Outstation cevabinda nokta yok / direct read fail: son iyi
                # degeri korumak icin no_change. Poller bunu yayinlamaz.
                out.append((_invalid_raw(), "no_change"))
        return out

    def _direct_fill_sequential_indices(
        self,
        master: Any,
        group: int,
        indices: list[int],
        cache: dict[tuple[int, int], float],
    ) -> None:
        """Aralik basarisiz veya cikis grubu: her (genelde benzersiz) index icin ayri DNP3 okuma."""
        for idx in indices:
            try:
                if group in (_OBJECT_GROUP_ANALOG_INPUT, _OBJECT_GROUP_ANALOG_OUTPUT):
                    is_in = group == _OBJECT_GROUP_ANALOG_INPUT
                    fn = master.read_analog_inputs if is_in else master.read_analog_outputs
                    items = fn(idx, idx)
                elif group in (_OBJECT_GROUP_BINARY_INPUT, _OBJECT_GROUP_BINARY_OUTPUT):
                    is_in = group == _OBJECT_GROUP_BINARY_INPUT
                    fn = master.read_binary_inputs if is_in else master.read_binary_outputs
                    items = fn(idx, idx)
                elif group == _OBJECT_GROUP_COUNTER:
                    items = master.read_counters(idx, idx)
                else:
                    return
            except DNP3Error as exc:
                logger.debug(
                    "dnp3_direct_sequential_index_failed device=%s group=%s index=%s error=%s",
                    self.device.code,
                    group,
                    idx,
                    exc,
                )
                continue
            for obj in items:
                cache[(group, obj.index)] = _point_raw_from_object(group, obj)

    def _direct_read_range_one(
        self,
        master: Any,
        group: int,
        start: int,
        end: int,
    ) -> list[Any]:
        """[start, end] kapali aralik icin tek DNP3 okuma; grup tipine gore listeler."""
        if group in (_OBJECT_GROUP_ANALOG_INPUT, _OBJECT_GROUP_ANALOG_OUTPUT):
            is_in = group == _OBJECT_GROUP_ANALOG_INPUT
            fn = master.read_analog_inputs if is_in else master.read_analog_outputs
            return fn(start, end)
        if group in (_OBJECT_GROUP_BINARY_INPUT, _OBJECT_GROUP_BINARY_OUTPUT):
            is_in = group == _OBJECT_GROUP_BINARY_INPUT
            fn = master.read_binary_inputs if is_in else master.read_binary_outputs
            return fn(start, end)
        if group == _OBJECT_GROUP_COUNTER:
            return master.read_counters(start, end)
        return []

    def _direct_fill_range_chunked(
        self,
        master: Any,
        group: int,
        lo: int,
        hi: int,
        idxs: list[int],
        cache: dict[tuple[int, int], float],
    ) -> None:
        """Genis araligi (orn. 0-122) maksN noktali parcalarla oku; parca basarisizsa
        bir kez daha dener, yine fail ise sub_need indexlerini tekil okumayi dener.

        Chunklar arasina kisa gecikme: OpenDNP3 outstation ardisik hizli istekleri
        zaman zaman timeout ile dusurur — kisa nefes alinca cache hit orani ciddi
        artar (sim + gercek saha cihazlari ile gozlendi).
        """
        nmax = _effective_read_chunk_size(self._direct_max_points, group)
        need = set(idxs)
        a = lo
        first_chunk = True
        while a <= hi:
            b = min(a + nmax - 1, hi)
            sub_need = [i for i in idxs if a <= i <= b]
            if not first_chunk:
                time.sleep(0.05)  # outstation chunk-arasi nefes
            first_chunk = False

            success = False
            for attempt in range(2):
                try:
                    items = self._direct_read_range_one(master, group, a, b)
                    for obj in items:
                        if obj.index in need:
                            cache[(group, obj.index)] = _point_raw_from_object(group, obj)
                    success = True
                    break
                except DNP3Error as exc:
                    if attempt == 0:
                        logger.debug(
                            "dnp3_direct_chunk_retry device=%s group=%s range=%s-%s error=%s",
                            self.device.code,
                            group,
                            a,
                            b,
                            exc,
                        )
                        time.sleep(0.1)
                        continue
                    logger.warning(
                        "dnp3_direct_chunk_read_failed device=%s group=%s range=%s-%s error=%s — sub-index fallback",
                        self.device.code,
                        group,
                        a,
                        b,
                        exc,
                    )
            if not success:
                self._direct_fill_sequential_indices(master, group, sub_need, cache)
            a = b + 1

    def _read_batch_event_driven(
        self,
        master: Any,
        signals: list[SignalConfig],
    ) -> list[tuple[float, str]]:
        """Class 1+2+3 event poll + periyodik Class 0 baseline.

        Akis:
          1. Cache henuz dolmamissa veya baseline_interval doldu ise: read_class(0)
             yap, _event_value_cache'i tum noktalarla doldur, baseline_at guncelle.
             Bu cycle'da TUM sinyaller 'good' doner.
          2. Aksi halde: read_class(1), read_class(2), read_class(3) yapip event'leri
             topla; sadece *degisen* veya *yeni* noktalari cache'e yaz, bu cycle'da
             sadece degisenleri 'good' diger sinyalleri 'no_change' olarak dondur.
        """
        now = time.monotonic()
        is_baseline_due = (
            not self._event_value_cache
            or (now - self._event_baseline_at) >= self._event_baseline_interval_sec
        )

        if is_baseline_due:
            # Baseline: catalog'daki sinyalleri DIRECT range READ ile cek.
            # Bazi OpenDNP3 outstation'larda (saha cihazlari + simulator)
            # read_class(0) timeout verip TCP'yi kopariyor; direct range read
            # stabil calisiyor.
            self._absorb_signals_via_direct(master, signals)
            absorbed = len(self._event_value_cache)

            if absorbed == 0:
                # Direct de fail: bagi koparmis olabilir; eski cache yoksa
                # 'no_change' (frontend son iyi degeri korur, invalid uretmez).
                if not self._event_value_cache:
                    self._mark_disconnected()
                    return [(_invalid_raw(), "no_change") for _ in signals]
                logger.debug(
                    "dnp3_event_baseline_skipped device=%s — eski cache ile devam",
                    self.device.code,
                )
            else:
                self._event_baseline_at = now
                # Reader-level baseline_at ref'i de senkron tut
                ref = getattr(self, "_baseline_at_ref", None)
                if ref is not None:
                    ref[0] = now
                logger.info(
                    "dnp3_event_baseline_refreshed device=%s cached_points=%s "
                    "(direct range read)",
                    self.device.code,
                    absorbed,
                )
                return self._materialize_from_cache(signals, force_good=True)

        # Cache mevcut, sadece event class poll yap
        changed: set[tuple[int, int]] = set()
        any_success = False
        for cls in (1, 2, 3):
            poll = self._poll_with_retry(master, lambda c=cls: master.read_class(c), retries=2)
            if poll is None:
                continue
            any_success = True
            changed.update(self._absorb_poll_into_cache(poll))

        if not any_success and not self._event_value_cache:
            return [(_invalid_raw(), "invalid") for _ in signals]

        return self._materialize_from_cache(signals, changed_keys=changed)

    def _poll_with_retry(
        self,
        master: Any,
        poll_fn: Any,
        retries: int = 3,
    ) -> Any | None:
        """Tek bir poll cagrisini, basarisizlikta tekrarlayarak yapar; basarili poll dondurur."""
        for attempt in range(retries):
            try:
                poll = poll_fn()
            except DNP3Error as exc:
                logger.debug(
                    "dnp3_poll_dnp3_error device=%s attempt=%s error=%s",
                    self.device.code,
                    attempt + 1,
                    exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "dnp3_poll_exception device=%s attempt=%s error=%s",
                    self.device.code,
                    attempt + 1,
                    exc,
                )
                continue
            if getattr(poll, "success", False):
                return poll
            logger.debug(
                "dnp3_poll_unsuccessful_retry device=%s attempt=%s err=%s",
                self.device.code,
                attempt + 1,
                getattr(poll, "error", "?"),
            )
        return None

    def _absorb_signals_via_direct(
        self,
        master: Any,
        signals: list[SignalConfig],
    ) -> None:
        """Catalog'daki sinyalleri group/index araliginda DIRECT READ ile cache'e doldurur.

        read_class(0) cevapsiz outstation'larda baseline icin kullanilir.
        Direct strategy mevcut chunked range/sequential mantigini kullanir.
        Sonuclar self._event_value_cache'e yazilir.
        """
        # _read_batch_direct (group, index) -> raw cache uretir; biz onu yeniden
        # kullanmak yerine ic mantigi tekrar etmeden cagiralim ve sonuclari
        # event cache'ine kopyalayalim.
        by_group: dict[int, list[SignalConfig]] = defaultdict(list)
        for s in signals:
            by_group[s.dnp3_object_group].append(s)
        # Onceligi olan gruplar (input) once okunsun: bagi koparan bir grup
        # gelirse en azindan input degerleri cache'lensin. Output gruplarini
        # outstation'lar genelde READ'e desteklemez.
        priority = {
            _OBJECT_GROUP_BINARY_INPUT: 0,
            _OBJECT_GROUP_ANALOG_INPUT: 1,
            _OBJECT_GROUP_COUNTER: 2,
            _OBJECT_GROUP_BINARY_OUTPUT: 3,
            _OBJECT_GROUP_ANALOG_OUTPUT: 4,
        }
        ordered_groups = sorted(
            by_group.keys(), key=lambda g: priority.get(g, 99)
        )
        for group in ordered_groups:
            if group == _OBJECT_GROUP_STRING:
                continue
            if not self._master_connected():
                logger.warning(
                    "dnp3_direct_baseline_aborted device=%s group=%s — TCP koptu",
                    self.device.code,
                    group,
                )
                break
            sigs = by_group[group]
            idxs = sorted({s.dnp3_index for s in sigs})
            if not _direct_range_ok_for_group(group):
                self._direct_fill_sequential_indices(master, group, idxs, self._event_value_cache)
                continue
            lo, hi = idxs[0], idxs[-1]
            span = hi - lo + 1
            n = len(idxs)
            if n * self._direct_sparse_ratio < span:
                self._direct_fill_sequential_indices(master, group, idxs, self._event_value_cache)
                continue
            eff = _effective_read_chunk_size(self._direct_max_points, group)
            if span <= eff:
                try:
                    items = self._direct_read_range_one(master, group, lo, hi)
                    for obj in items:
                        self._event_value_cache[(group, int(obj.index))] = _point_raw_from_object(group, obj)
                except DNP3Error as exc:
                    logger.warning(
                        "dnp3_direct_baseline_range_failed device=%s group=%s lo=%s hi=%s error=%s",
                        self.device.code,
                        group,
                        lo,
                        hi,
                        exc,
                    )
                    if self._master_connected():
                        self._direct_fill_sequential_indices(master, group, idxs, self._event_value_cache)
                except Exception as exc:  # noqa: BLE001
                    # 10053 (Send/Receive failed) gibi TCP-level hatalar DNP3Error
                    # disinda olabilir; bagi kopardiysa break.
                    logger.warning(
                        "dnp3_direct_baseline_io_error device=%s group=%s error=%s — yarida birakildi",
                        self.device.code,
                        group,
                        exc,
                    )
                    break
            else:
                self._direct_fill_range_chunked(master, group, lo, hi, idxs, self._event_value_cache)

    def _absorb_poll_into_cache(self, poll: Any) -> set[tuple[int, int]]:
        """Poll sonucundaki tum noktalari cache'e yazar; cache'de degisen anahtarlari doner."""
        changed: set[tuple[int, int]] = set()

        def _put(group: int, items: list[Any]) -> None:
            for obj in items:
                key = (group, int(obj.index))
                value = _point_raw_from_object(group, obj)
                prev = self._event_value_cache.get(key)
                if prev is None or prev != value:
                    self._event_value_cache[key] = value
                    changed.add(key)

        _put(_OBJECT_GROUP_BINARY_INPUT, getattr(poll, "binary_inputs", []))
        _put(_OBJECT_GROUP_BINARY_OUTPUT, getattr(poll, "binary_outputs", []))
        _put(_OBJECT_GROUP_ANALOG_INPUT, getattr(poll, "analog_inputs", []))
        _put(_OBJECT_GROUP_ANALOG_OUTPUT, getattr(poll, "analog_outputs", []))
        _put(_OBJECT_GROUP_COUNTER, getattr(poll, "counters", []))
        return changed

    def _materialize_from_cache(
        self,
        signals: list[SignalConfig],
        *,
        force_good: bool = False,
        changed_keys: set[tuple[int, int]] | None = None,
    ) -> list[tuple[float, str]]:
        """Cache'den sinyal listesine event-driven cevap uretir.

        force_good=True: baseline cycle, hepsi yayinlanir
        changed_keys verildi: sadece eslesenler 'good', kalan 'no_change'
        """
        out: list[tuple[float, str]] = []
        for s in signals:
            if s.dnp3_object_group == _OBJECT_GROUP_STRING:
                out.append((0.0, "good" if force_good else "no_change"))
                continue
            key = (s.dnp3_object_group, s.dnp3_index)
            value = self._event_value_cache.get(key)
            if value is None:
                # Bu cycle'da gelmemis ve baseline'da da yokmus → muhtemelen
                # outstation'da nokta yok. Catalog mismatch'i ortaya cikarmak
                # icin invalid dondur.
                out.append((_invalid_raw(), "invalid"))
                continue
            if force_good:
                out.append((value, "good"))
            elif changed_keys is not None and key in changed_keys:
                out.append((value, "good"))
            else:
                out.append((value, "no_change"))
        return out

    def _read_batch_direct(self, master: Any, signals: list[SignalConfig]) -> list[tuple[float, str]]:
        """Katalog: gruba gore seyrek/yogun strateji + cok noktada parcali okuma."""
        inv = _invalid_raw()
        if not signals:
            return []
        by_group: dict[int, list[SignalConfig]] = defaultdict(list)
        for s in signals:
            by_group[s.dnp3_object_group].append(s)
        # (object_group, index) -> float
        cache: dict[tuple[int, int], float] = {}
        for group, sigs in by_group.items():
            if group == _OBJECT_GROUP_STRING:
                for s in sigs:
                    cache[(group, s.dnp3_index)] = 0.0
                continue
            idxs = sorted({s.dnp3_index for s in sigs})
            if not _direct_range_ok_for_group(group):
                self._direct_fill_sequential_indices(master, group, idxs, cache)
                continue
            lo, hi = idxs[0], idxs[-1]
            span = hi - lo + 1
            n = len(idxs)
            # Katalogdaki noktalar aslinda genis aralikta seyrekse (orn. index 0,5,100): tek sefer 0-100 isteme
            if n * self._direct_sparse_ratio < span:
                self._direct_fill_sequential_indices(master, group, idxs, cache)
                continue
            eff = _effective_read_chunk_size(self._direct_max_points, group)
            if span <= eff:
                try:
                    items = self._direct_read_range_one(master, group, lo, hi)
                    for obj in items:
                        cache[(group, obj.index)] = _point_raw_from_object(group, obj)
                except DNP3Error as exc:
                    logger.warning(
                        "dnp3_direct_group_read_failed device=%s group=%s range=%s-%s error=%s — tekil indexe dusuluyor",
                        self.device.code,
                        group,
                        lo,
                        hi,
                        exc,
                    )
                    self._direct_fill_sequential_indices(master, group, idxs, cache)
            else:
                self._direct_fill_range_chunked(master, group, lo, hi, idxs, cache)

        out: list[tuple[float, str]] = []
        for signal in signals:
            g = signal.dnp3_object_group
            if g == _OBJECT_GROUP_STRING:
                out.append((0.0, "good"))
                continue
            key = (g, signal.dnp3_index)
            if key not in cache:
                # Bu cycle'da okunamadi: 'invalid' yerine 'no_change' ile
                # frontend'in son iyi degeri korumasina izin ver.
                out.append((inv, "no_change"))
            else:
                out.append((cache[key], "good"))
        return out

    def _raw_for_signal(self, poll: Any, master: Any, signal: SignalConfig) -> float:
        try:
            return self._raw_from_poll(poll, signal)
        except Dnp3AdapterError:
            return self._read_direct(master, signal)

    @staticmethod
    def _raw_from_poll(poll: Any, signal: SignalConfig) -> float:
        group = signal.dnp3_object_group
        index = signal.dnp3_index

        if group == _OBJECT_GROUP_STRING:
            return 0.0

        if group in (_OBJECT_GROUP_ANALOG_INPUT, _OBJECT_GROUP_ANALOG_OUTPUT):
            src = poll.analog_inputs if group == _OBJECT_GROUP_ANALOG_INPUT else poll.analog_outputs
            p = _find_index(src, index)
            if p is None:
                raise Dnp3AdapterError(f"analog index={index} yok (group={group})")
            return float(p.value)

        if group in (_OBJECT_GROUP_BINARY_INPUT, _OBJECT_GROUP_BINARY_OUTPUT):
            src = poll.binary_inputs if group == _OBJECT_GROUP_BINARY_INPUT else poll.binary_outputs
            p = _find_index(src, index)
            if p is None:
                raise Dnp3AdapterError(f"binary index={index} yok (group={group})")
            return 1.0 if p.value else 0.0

        if group == _OBJECT_GROUP_COUNTER:
            p = _find_index(poll.counters, index)
            if p is None:
                raise Dnp3AdapterError(f"counter index={index} yok")
            return float(p.value)

        raise Dnp3AdapterError(
            f"desteklenmeyen object_group={group} signal={signal.key}"
        )

    def _read_direct(self, master: Any, signal: SignalConfig) -> float:
        group = signal.dnp3_object_group
        index = signal.dnp3_index

        if group == _OBJECT_GROUP_STRING:
            return 0.0

        try:
            if group in (_OBJECT_GROUP_ANALOG_INPUT, _OBJECT_GROUP_ANALOG_OUTPUT):
                is_in = group == _OBJECT_GROUP_ANALOG_INPUT
                fn = master.read_analog_inputs if is_in else master.read_analog_outputs
                items = fn(index, index)
            elif group in (_OBJECT_GROUP_BINARY_INPUT, _OBJECT_GROUP_BINARY_OUTPUT):
                is_in = group == _OBJECT_GROUP_BINARY_INPUT
                fn = master.read_binary_inputs if is_in else master.read_binary_outputs
                items = fn(index, index)
            elif group == _OBJECT_GROUP_COUNTER:
                items = master.read_counters(index, index)
            else:
                raise Dnp3AdapterError(
                    f"desteklenmeyen object_group={group} signal={signal.key}"
                )
        except DNP3Error as exc:
            raise Dnp3AdapterError(str(exc)) from exc

        p = _find_index(items, index) or (items[0] if items else None)
        if p is None:
            raise Dnp3AdapterError("direkt okuma bosa")
        return _point_raw_from_object(group, p)


def _invalid_raw() -> float:
    return 0.0


class Dnp3TelemetryReader(TelemetryReader):
    """Cihaz basina session; her `read_device`’da toplu poll + sinyal listesi."""

    def __init__(
        self,
        *,
        local_address: int,
        default_dnp3_tcp_port: int,
        response_timeout_sec: int,
        read_strategy: str = _READ_EVENT_DRIVEN,
        direct_max_points_per_read: int = 24,
        direct_sparse_ratio: int = 4,
        confirm_required: bool = False,
        link_reset_on_connect: bool = True,
        disable_unsolicited_on_connect: bool = True,
        unsolicited_class_mask: int = 7,
        event_baseline_interval_sec: int = 60,
        log_raw_frames: bool = False,
    ) -> None:
        if not _DNP3_AVAILABLE:
            raise Dnp3AdapterError(
                "dnp3 (nfm-dnp3) yuklu degil. `pip install nfm-dnp3` veya "
                f"GATEWAY_MODE=mock kullanin. Import: {_DNP3_IMPORT_ERROR}"
            )
        self.local_address = local_address
        self._default_dnp3_tcp_port = int(default_dnp3_tcp_port)
        self.response_timeout_sec = response_timeout_sec
        self._read_strategy = _normalize_read_strategy(read_strategy)
        self._direct_max_points = max(1, int(direct_max_points_per_read))
        self._direct_sparse_ratio = max(2, int(direct_sparse_ratio))
        self._confirm_required = bool(confirm_required)
        self._link_reset_on_connect = bool(link_reset_on_connect)
        self._disable_unsolicited_on_connect = bool(disable_unsolicited_on_connect)
        self._unsolicited_class_mask = max(0, min(7, int(unsolicited_class_mask)))
        self._event_baseline_interval_sec = max(5, int(event_baseline_interval_sec))
        self._log_raw_frames = bool(log_raw_frames)
        self._sessions: dict[str, Dnp3DeviceSession] = {}
        self._sessions_lock = threading.Lock()
        # Device-level event_driven cache: session yeniden olussa bile
        # (TCP kopmasi sonrasi) device basina cache + baseline timestamp korunur.
        # Boylece eksik gruplari sonraki cycle'lar zaman icinde tamamlar.
        self._device_event_cache: dict[str, dict[tuple[int, int], float]] = {}
        self._device_baseline_at: dict[str, list[float]] = {}

    @staticmethod
    def _resolve_tcp_port(device: DeviceConfig, default_port: int) -> int:
        p = device.dnp3_tcp_port
        if p is not None and 1 <= p <= 65535:
            return int(p)
        return int(default_port)

    @staticmethod
    def _resolve_local_address(device: DeviceConfig, default_addr: int) -> int:
        a = device.master_address
        if a is not None and 0 <= a <= 65519:
            return int(a)
        return int(default_addr)

    def _fingerprint(self, device: DeviceConfig) -> tuple[str, str, int, int, int]:
        port = self._resolve_tcp_port(device, self._default_dnp3_tcp_port)
        local_addr = self._resolve_local_address(device, self.local_address)
        return (
            device.code,
            (device.ip_address or "").strip(),
            port,
            int(device.dnp3_address),
            int(local_addr),
        )

    def read_device(
        self,
        *,
        device: DeviceConfig,
        signals: list[SignalConfig],
    ) -> list[SignalReading]:
        session = self._get_session(device)
        try:
            batch = session.read_batch(signals)
        except Dnp3AdapterError as exc:
            logger.warning("dnp3_read_batch_failed device=%s error=%s", device.code, exc)
            # Bozuk session'i cache'den dusur ki sonraki cycle yenisini acsin.
            self._evict_session(device.code)
            # Fail durumunda 'invalid' yerine 'no_change' — frontend son iyi
            # degeri korur, poller bu mesajlari yayinlamaz.
            return [
                SignalReading(
                    signal_key=s.key,
                    source=s.source,
                    data_type=s.data_type,
                    raw_value=0.0,
                    scaled_value=0.0,
                    quality="no_change",
                )
                for s in signals
            ]
        # Read sirasinda outstation'in baglantiyi kapatmasi (bazi saha cihazlarinda)
        # session._master'i None yapar; cache'den dus ki sonraki cycle yenilensin.
        if not session._master_connected():
            self._evict_session(device.code)

        readings: list[SignalReading] = []
        for signal, (raw, quality) in zip(signals, batch, strict=True):
            if quality == "no_change":
                # Cache'deki gerçek değer korunur (poller bunu publish etmez ama
                # health/diagnostic icin scaled_value anlamli olsun).
                scaled = raw * signal.scale + signal.offset
                readings.append(
                    SignalReading(
                        signal_key=signal.key,
                        source=signal.source,
                        data_type=signal.data_type,
                        raw_value=raw,
                        scaled_value=round(scaled, 4),
                        quality="no_change",
                    )
                )
                continue
            if quality != "good":
                readings.append(
                    SignalReading(
                        signal_key=signal.key,
                        source=signal.source,
                        data_type=signal.data_type,
                        raw_value=0.0,
                        scaled_value=0.0,
                        quality=quality,
                    )
                )
                continue
            scaled = raw * signal.scale + signal.offset
            readings.append(
                SignalReading(
                    signal_key=signal.key,
                    source=signal.source,
                    data_type=signal.data_type,
                    raw_value=raw,
                    scaled_value=round(scaled, 4),
                    quality=quality,
                )
            )
        return readings

    def operate_device(
        self,
        *,
        device: DeviceConfig,
        index: int,
        op_type: str = "latch_on",
        count: int = 1,
        on_time_ms: int = 0,
        off_time_ms: int = 0,
        timeout_sec: float = 10.0,
    ) -> dict[str, Any]:
        # dnp3py adapter'i CROB gonderimi desteklemiyor; komut icin yadnp3 gerek.
        _ = index, op_type, count, on_time_ms, off_time_ms, timeout_sec
        return {
            "ok": False,
            "status": "unsupported",
            "device_code": device.code,
            "error": "dnp3py adapter komut desteklemiyor; DNP3_LIBRARY=yadnp3 kullanin",
        }

    def close(self) -> None:
        with self._sessions_lock:
            for session in list(self._sessions.values()):
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    logger.debug("dnp3_session_close_error", exc_info=True)
            self._sessions.clear()

    def forget_devices(self, active_device_codes: set[str]) -> int:
        """Backend config'inden silinen cihazlarin DNP3 session'larini kapat.

        Base class default no-op'tur; bu adapter `_sessions` dict + per-device
        cache (`_device_event_cache`, `_device_baseline_at`) tutar. Cihaz
        config'ten silinince bunlar bellekte zombie kalir; her cycle'da
        gereksiz TCP socket + memory tutulur ve `_get_session` polled cihaza
        yanlislikla cevap verebilir. Bu metod silinen tum cihazlarin session'lari
        + cache'lerini temizler ve kac cihazin silindigini dondurur.
        """
        cleaned = 0
        with self._sessions_lock:
            stale = [code for code in self._sessions if code not in active_device_codes]
        for code in stale:
            with self._sessions_lock:
                session = self._sessions.pop(code, None)
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    logger.debug("dnp3_session_forget_close_failed", exc_info=True)
            # Per-device cache yapilarini temizle
            self._device_event_cache.pop(code, None)
            self._device_baseline_at.pop(code, None)
            cleaned += 1
        return cleaned

    def _evict_session(self, device_code: str) -> None:
        """Hatali/dusen session'i cache'den dus; bir sonraki read_device yenisini acar."""
        with self._sessions_lock:
            session = self._sessions.pop(device_code, None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                logger.debug("dnp3_session_evict_close_failed", exc_info=True)

    def _get_session(self, device: DeviceConfig) -> Dnp3DeviceSession:
        with self._sessions_lock:
            want = self._fingerprint(device)
            existing = self._sessions.get(device.code)
            if existing is not None and existing.connection_fingerprint != want:
                try:
                    existing.close()
                except Exception:  # noqa: BLE001
                    logger.debug("dnp3_session_reconnect_close_failed", exc_info=True)
                self._sessions.pop(device.code, None)
            if device.code not in self._sessions:
                port = self._resolve_tcp_port(device, self._default_dnp3_tcp_port)
                local_addr = self._resolve_local_address(device, self.local_address)
                session = Dnp3DeviceSession(
                    device=device,
                    local_address=local_addr,
                    tcp_port=port,
                    response_timeout_sec=self.response_timeout_sec,
                    read_strategy=self._read_strategy,
                    direct_max_points_per_read=self._direct_max_points,
                    direct_sparse_ratio=self._direct_sparse_ratio,
                    confirm_required=self._confirm_required,
                    link_reset_on_connect=self._link_reset_on_connect,
                    disable_unsolicited_on_connect=self._disable_unsolicited_on_connect,
                    unsolicited_class_mask=self._unsolicited_class_mask,
                    event_baseline_interval_sec=self._event_baseline_interval_sec,
                    log_raw_frames=self._log_raw_frames,
                )
                # Device-level cache'i session'a bagla (TCP koparsa veriyi kaybetme)
                cache = self._device_event_cache.setdefault(device.code, {})
                baseline_ref = self._device_baseline_at.setdefault(device.code, [0.0])
                session.attach_cache(cache, baseline_ref)
                session.connect()
                self._sessions[device.code] = session
            return self._sessions[device.code]
