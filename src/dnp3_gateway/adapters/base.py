"""Telemetri adapter'lari icin sozlesme."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from dnp3_gateway.backend import DeviceConfig, SignalConfig


@dataclass(frozen=True)
class SignalReading:
    """Tek bir sinyalin okunmus halini temsil eder.

    `raw_value`     : cihazdan gelen olceklendirilmemis ham deger (numeric tipler)
    `scaled_value`  : scale/offset uygulandiktan sonraki gercek deger (yayinlanir)
    `value_string`  : data_type='string' icin metin icerik (None = bilinmiyor)
                      Frontend numeric value yerine bunu gosterir.
    `quality`       : good | offline | invalid | comm_lost | restart | no_change
                      no_change: Event-driven okumada bu sinyal bu cycle'da
                      degisen event vermedi; cached deger gecerli, poller bunu
                      yayinlamaz (delta-only publish).
    `read_token`    : Adapter'a OZEL, poller icin ANLAMSIZ bir isaretcidir.
                      Poller yayin BASARIYLA kalicilastiktan sonra okumalari
                      `commit_published` ile adapter'a geri verir; adapter bu
                      token sayesinde "yayinlanan surum hala guncel mi"
                      kontrolunu yapip degisiklik bayragini temizleyebilir.
                      Bkz. `TelemetryReader.commit_published`.
    """

    signal_key: str
    source: str
    data_type: str
    raw_value: float
    scaled_value: float
    quality: str = "good"
    value_string: str | None = None
    read_token: Any = None


class TelemetryReader(ABC):
    """Cihaz -> (sinyal listesi) okumasini sarmallayan adapter arayuzu."""

    @abstractmethod
    def read_device(
        self,
        *,
        device: DeviceConfig,
        signals: list[SignalConfig],
    ) -> list[SignalReading]:
        """Verilen cihazdan tum `signals` listesini okur.

        Implementasyon sirasinda hata olursa ya okuma basarisiz sinyaller
        quality='offline' / 'invalid' olarak donebilir ya da exception raise
        edilebilir. Exception uygulama katmaninda tek cihaz bazinda try/except
        ile loglanir, dongu durmaz.
        """

    @abstractmethod
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
        mode: str = "direct",
    ) -> dict[str, Any]:
        """Cihaza DNP3 binary output (CROB) komutu gonderir.

        Horstmann SN2 komutlari binary output index'lerine PULSE_ON ile
        tetiklenir. Doner: {ok: bool, status: str, ...}. Adapter destegi
        yoksa {ok: False, status: 'unsupported'} donmeli, exception atmamali.
        """

    def commit_published(
        self,
        *,
        device: DeviceConfig,
        readings: list[SignalReading],
    ) -> int:
        """Yayin KALICILASTIKTAN sonra cagirilir; degisiklik bayragini temizler.

        NEDEN AYRI BIR ADIM
        -------------------
        Eskiden adapter, degeri `read_device` icinde dondururken dirty bayragini
        HEMEN temizliyordu — yani yayindan ONCE. Iki ayri kalici veri kaybi
        uretiyordu:

        1. **TOCTOU yarisi.** Deger okunduktan sonra, dirty temizlenmeden once
           DNP3 IO thread'i yeni bir olcum yazabiliyordu. Poller ESKI degeri
           yayinlayip yeni degerin bayragini siliyordu. Deger bir daha DEGISENE
           kadar (saatler/gunler) yayinlanmiyordu; periyodik integrity poll de
           ayni degeri yazdigi icin "degismedi" sayilip durumu duzeltmiyordu.
           Ornek: acilan bir kesici SCADA'da kapali gorunmeye devam ediyordu.

        2. **Yayin basarisiz olsa bile bayrak temizdi.** Outbox dolarsa veya
           disk hatasi olursa mesaj ne broker'a ne diske gidiyordu; deger
           cache'te "yayinlanmis" sayildigi icin telafi edilemiyordu.

        Yeni akis: `read_device` yalnizca OKUR (bayrak korunur). Poller yayini
        kalicilastirdiktan sonra (broker ack VEYA outbox'a yazildiktan sonra)
        bu metodu cagirir. Adapter, `read_token` ile "yayinlanan surum hala
        guncel mi" diye bakar; arada yeni deger geldiyse bayragi TEMIZLEMEZ ve
        yeni deger bir sonraki cycle'da yayinlanir.

        Returns: bayragi temizlenen sinyal sayisi.
        Default: no-op (state tutmayan adapter'lar icin).
        """
        _ = device, readings
        return 0

    def forget_devices(self, active_device_codes: set[str]) -> int:
        """Backend config'inden artik gorulmeyen cihazlarin acik master/channel
        kaynaklarini kapat.

        `active_device_codes` set'i mevcut config'te olan cihaz kodlarini icerir.
        Adapter ic state'inde bu set'te olmayan tum cihaz baglantilarini
        disable etmeli (TCP RST, link kapatma). Aksi halde silinen cihazlar
        zombie olarak yasamaya devam eder ve baska cihazlarla port/IP
        catismalarina yol acar (mevcut bug).

        Returns: kaç cihazın temizlendigi.

        Default: no-op (mock adapter gibi state tutmayanlar icin).
        """
        _ = active_device_codes
        return 0

    def close(self) -> None:
        """Kaynak temizleme; varsayilanda no-op."""
