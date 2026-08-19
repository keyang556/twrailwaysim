"""班次資料模型（規格 §6.5）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Schedule", "Service"]


@dataclass(frozen=True)
class Schedule:
    """一個班次的營運時刻摘要（規格 §6.5、§22）。

    來源是營運單位公布的時刻表。捷運的班次是「營運模式」而不是某一列特定
    的車，因此除了逐站發車時刻，玩家真正想知道的是**這個模式一天怎麼跑**：
    首班、末班、一天幾班。這些寫在班次上而不是散在匯入程式裡，兩個介面才
    顯示得出同一份內容（§25.5）。

    Attributes:
        service_days: 這份時刻表適用哪些日子（平日、假日、週六…）。
        first_departure: 起站的首班發車時刻 ``HH:MM``。
        last_departure: 起站的末班發車時刻。跨午夜的末班會寫成 ``00:15``。
        departures_per_day: 起站一天發幾班。
        run_time_min: 起站發車到**最後一個有公布發車時刻的車站**的分鐘數。
            不是完整旅行時間：來源只公布發車時刻，終點站只有到達，因此最後
            一段行車時間不在裡面。只有兩站的支線沒有這個值。
        verification_status: ``official`` 表示來源的營運路線與本班次的停靠
            站完全相同；``derived_from_timetable`` 表示本班次是來源那一組的
            一部分（同一組裡還有別的營運模式），時刻是真的，歸屬是推得的。
    """

    service_days: str = ""
    first_departure: str = ""
    last_departure: str = ""
    departures_per_day: int = 0
    run_time_min: int | None = None
    effective_date: str = ""
    verification_status: str = "unknown"
    source: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Schedule:
        return cls(
            service_days=raw.get("service_days", ""),
            first_departure=raw.get("first_departure", ""),
            last_departure=raw.get("last_departure", ""),
            departures_per_day=int(raw.get("departures_per_day", 0)),
            run_time_min=raw.get("run_time_min"),
            effective_date=raw.get("effective_date", ""),
            verification_status=raw.get("verification_status", "unknown"),
            source=raw.get("source", ""),
        )


@dataclass(frozen=True)
class Service:
    """一個班次（車次）。

    停靠與通過一律以本班次的停靠表為準，不得由「列車行駛於這條路線」推論
    （規格 §9.1）。
    """

    train_number: str
    train_type: str
    """車種代碼（``local``／``local_express``／``tze_chiang``／``chu_kuang``）。"""

    route_id: str
    rolling_stock_id: str = ""
    """車輛型式代碼，對應 ``trains.json`` 的 ``train_types``。"""

    stop_station_ids: tuple[str, ...] = ()
    pass_station_ids: tuple[str, ...] = ()
    departure_times: dict[str, str] = field(default_factory=dict)
    arrival_times: dict[str, str] = field(default_factory=dict)
    name_zh_tw: str = ""
    verification_status: str = "test_data"
    schedule: Schedule | None = None
    """營運時刻摘要；``None`` 表示這個班次沒有可用的公布時刻表。

    沒有**不是**資料缺漏：文湖線、環狀線、三鶯線與機場捷運沒有逐班時刻的
    公開來源，補一份推估的只會讓人以為那是真的（§2.3）。
    """

    @property
    def display_name(self) -> str:
        return self.name_zh_tw or f"{self.train_type}{self.train_number}次"

    def scheduled_stations(self) -> tuple[str, ...]:
        return self.stop_station_ids

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Service:
        return cls(
            train_number=raw["train_number"],
            train_type=raw["train_type"],
            route_id=raw["route_id"],
            rolling_stock_id=raw.get("rolling_stock_id", ""),
            stop_station_ids=tuple(raw.get("stop_station_ids", ())),
            pass_station_ids=tuple(raw.get("pass_station_ids", ())),
            departure_times=dict(raw.get("departure_times", {})),
            arrival_times=dict(raw.get("arrival_times", {})),
            name_zh_tw=raw.get("name_zh_tw", ""),
            verification_status=raw.get("verification_status", "test_data"),
            schedule=(
                Schedule.from_dict(raw["schedule"]) if raw.get("schedule") else None
            ),
        )
