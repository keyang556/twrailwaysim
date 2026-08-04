"""班次資料模型（規格 §6.5）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Service"]


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
        )
