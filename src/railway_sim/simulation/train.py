"""列車與車種資料模型（規格 §6.4、§8.2）。

段位範圍依規格 §8.2：電門 0 至 5、制軔 0 至 7，緊急制軔為獨立狀態。
所有性能數值均為第一版**測試資料**，非臺鐵實際制動性能（§27）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "REVERSER_BACKWARD",
    "REVERSER_FORWARD",
    "REVERSER_NEUTRAL",
    "Train",
    "TrainType",
]

#: 方向把手的位置。數值與 OpenBVE 的 ``ReverserPosition`` 一致
#: （``source/TrainManager/Handles/Reverser/Reverser.Positions.cs``）。
REVERSER_FORWARD = 1
REVERSER_NEUTRAL = 0
REVERSER_BACKWARD = -1


@dataclass(frozen=True)
class TrainType:
    """車種性能。

    Attributes:
        max_traction_ms2: 最大牽引加速度（公尺／秒平方，全電門、低速時）。
        power_corner_speed_kmh: 定加速度轉為定功率的轉折速度。超過此速度後
            牽引力隨速度遞減。
        max_service_brake_ms2: 全常用制軔的減速度。
        emergency_brake_ms2: 緊急制軔的減速度。
        resistance_ms2: 行駛阻力係數 ``(r0, r1, r2)``，
            阻力 = ``r0 + r1 * v + r2 * v²``（``v`` 單位為公尺／秒）。
        has_broadcast: 有無車上自動廣播設備。DR1000 型柴油客車沒有，因此
            該型車不播放到站與下一站廣播（§20.2）。以資料欄位表示而不是在
            程式裡寫死車型代碼，日後多一型沒有廣播的車只要改資料。
        boarding_notice: 車門開啟中是否持續播放「請勿上車」。全車對號的
            TEMU1000、TEMU2000、EMU3000 需要，用來提醒沒有買這班列車車票的
            旅客不要上車；關門動作一開始就立即停止。同樣以資料欄位表示。
    """

    id: str
    name_zh_tw: str
    max_speed_kmh: float
    length_m: float
    max_traction_ms2: float
    max_service_brake_ms2: float
    emergency_brake_ms2: float
    power_notches: int = 5
    brake_notches: int = 7
    power_corner_speed_kmh: float = 45.0
    resistance_ms2: tuple[float, float, float] = (0.02, 0.0005, 0.00035)
    verification_status: str = "test_data"
    has_broadcast: bool = True
    boarding_notice: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrainType:
        return cls(
            id=raw["id"],
            name_zh_tw=raw["name_zh_tw"],
            max_speed_kmh=float(raw["max_speed_kmh"]),
            length_m=float(raw["length_m"]),
            max_traction_ms2=float(raw["max_traction_ms2"]),
            max_service_brake_ms2=float(raw["max_service_brake_ms2"]),
            emergency_brake_ms2=float(raw["emergency_brake_ms2"]),
            power_notches=int(raw.get("power_notches", 5)),
            brake_notches=int(raw.get("brake_notches", 7)),
            power_corner_speed_kmh=float(raw.get("power_corner_speed_kmh", 45.0)),
            resistance_ms2=tuple(raw.get("resistance_ms2", (0.02, 0.0005, 0.00035))),  # type: ignore[arg-type]
            verification_status=raw.get("verification_status", "test_data"),
            has_broadcast=bool(raw.get("has_broadcast", True)),
            boarding_notice=bool(raw.get("boarding_notice", False)),
        )


@dataclass
class Train:
    """列車即時狀態（規格 §6.4）。

    ``position_m`` 為**車頭**在目前路線上的里程。
    """

    id: str
    train_type: str
    """車輛型式代碼，對應 :class:`TrainType`（例如 ``EMU3000``）。"""

    current_speed_kmh: float = 0.0
    """速度**大小**（公里／小時），恆為非負；行進方向看 :attr:`motion_sign`。"""

    power_notch: int = 0
    brake_notch: int = 0
    emergency_brake: bool = False

    reverser: int = REVERSER_FORWARD
    """方向把手：``1`` 前進、``0`` 切、``-1`` 後退。

    位置與 OpenBVE 的 ``ReverserPosition`` 相同（F／N／R），操作鍵也一樣是
    ``F``／``V``。位於「切」時電門不產生牽引力——這正是方向把手的作用。

    預設是**前進**位：本專案的工作階段是從「列車已備妥、等著發車」開始，
    不含出庫與換端，因此把手一開始就在行車方向上。要倒退（例如停過頭了
    退回停車位置）才需要動它。
    """

    motion_sign: int = 1
    """列車目前滾動的方向：``1`` 前進、``-1`` 後退。

    與 :attr:`reverser` 分開，是因為把方向把手推回「切」的瞬間列車還在滑行，
    它不會就此改往反方向走。靜止時由方向把手決定，滑行時維持不變。
    """

    left_doors_open: bool = False
    right_doors_open: bool = False
    position_m: float = 0.0
    direction: str = "southbound"
    current_route_id: str = ""
    current_block_id: str | None = None
    train_number: str = ""
    length_m: float = 100.0
    distance_travelled_m: float = 0.0

    service_class: str = ""
    """車種代碼（``local``／``tze_chiang`` 等），停靠規則以此判斷。

    車種與車輛型式分開的理由：停靠規則看的是車種（自強號不停成功站），
    運轉性能看的是車輛型式（EMU3000 與 PP 的加減速不同）。
    """

    @property
    def is_stopped(self) -> bool:
        """速度低於 0.1 公里／小時即視為停妥。"""
        return self.current_speed_kmh < 0.1

    @property
    def any_door_open(self) -> bool:
        """任一側車門開啟中。出發前必須為 ``False``（§16.2）。"""
        return self.left_doors_open or self.right_doors_open

    @property
    def rear_position_m(self) -> float:
        """車尾里程，可能為負值（列車尚未完全進入路線起點）。"""
        return self.position_m - self.length_m

    @property
    def reversing(self) -> bool:
        """列車正在後退（方向把手在後退位且列車在動）。"""
        return self.motion_sign < 0 and not self.is_stopped

    @property
    def speed_ms(self) -> float:
        return self.current_speed_kmh / 3.6
