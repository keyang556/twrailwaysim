"""自動列車運轉裝置（ATO）與自動駕駛。

只有捷運有
----------

臺鐵沒有 ATO，因此臺鐵模式下這個功能連開都開不起來（會明確回覆「本系統
沒有這個功能」，而不是靜靜地沒反應）。捷運各線的號誌系統都含 ATO，資料裡
以 ``lines[*].ato`` 標記。

兩種自動化程度
--------------

``ATO``（一般捷運線）
    電腦負責加速、巡航、進站停車與停妥後保持制軔；駕駛只負責**開關車門**，
    以及關門後按下發車鍵。這就是台北捷運高運量線的實際作業方式。

``無人駕駛``（文湖線、環狀線、三鶯線）
    連車門與發車都由電腦控制，駕駛不需要任何動作。資料裡以
    ``lines[*].driverless`` 標記，依據是各線條目的號誌系統欄位（GoA 4 UTO）。

控制律
------

每一個步長算出「現在該用幾段電門或幾段制軔」，直接寫進列車狀態——ATO 是
電腦，不必像人一樣一段一段推手把。

1. 緊急制軔動作中：完全不介入。緊急制軔是駕駛或 ATP 的決定，ATO 不得覆蓋。
2. 車門開啟中：不加電門並施加制軔（§16.2）。
3. 前方有停車點：算出停在該點所需的減速度，換成最小的常用制軔段位。
   減速度用**常用制軔的七成**當作舒適上限，剩下的三成留給誤差與坡度。
4. 其餘情況：巡航在允許速度以下一點點，低於目標就加速、超過就減速。
   超出得愈多煞得愈重——允許速度往下走的時候（前方速限、通過月台的速限），
   微調的力道跟不上 ATP 的監控曲線，速度會壓在允許速度之上累積超速警告。

停車精度
--------

停車點取自班次的停靠站位置，容許誤差遠小於 :data:`~railway_sim.roles.driver.STOP_WINDOW_M`。
低速時改用固定的緩解段位慢慢貼上去，避免「算出來的減速度趨近無限大」導致
在最後幾公尺猛煞。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from railway_sim.simulation.braking import required_decel_ms2, service_brake_notch_for
from railway_sim.simulation.train import Train, TrainType

__all__ = [
    "CRUISE_MARGIN_KMH",
    "DEFAULT_DOOR_SIDE",
    "DWELL_S",
    "AtoController",
    "AtoDecision",
]

#: 巡航時比允許速度低多少（公里／小時）。
#:
#: 留一點餘裕才不會因為計算誤差在允許速度上下抖動而累積超速紀錄。
CRUISE_MARGIN_KMH = 3.0

#: 重新加速的遲滯（公里／小時）。低於「目標速度減去這個值」才再加電門。
_RESUME_MARGIN_KMH = 4.0

#: 進站停車使用的常用制軔比例。剩下的餘裕留給誤差與坡度。
_COMFORT_BRAKE_RATIO = 0.7

#: 低速貼近停車點與巡航微調時使用的固定段位比例。
_CREEP_BRAKE_RATIO = 0.35

#: 超出目標速度多少（公里／小時）以內算是巡航微調。
#:
#: 超過這個值就表示允許速度**正在往下走**（前方有速限、通過月台的速限或
#: 停止號誌），此時微調的力道不夠：ATP 的監控曲線是以常用制軔的七成畫的，
#: 只用三成五去追，速度會一路壓在允許速度之上累積超速警告。
_TRIM_EXCESS_KMH = 1.0

#: 進入低速貼近的速度（公里／小時）。
_CREEP_SPEED_KMH = 8.0

#: 停妥後保持的制軔段位比例，避免溜逸。
_HOLD_BRAKE_RATIO = 0.5

#: 無人駕駛的停站時間（秒）。
#:
#: 各線條目沒有公布停站時間（規格 §27），這是第一版測試值，取捷運常見的
#: 二十秒。改這個值不影響任何其他行為。
DWELL_S = 20.0

#: 無人駕駛開哪一側車門。
#:
#: 維基百科的車站表沒有月台配置與開門方向（規格 §22、§27：查不到的欄位不得
#: 自行推測），因此固定用左側並明說這是測試值，而不是假裝知道每一站的方向。
DEFAULT_DOOR_SIDE = "left"


@dataclass(frozen=True)
class AtoDecision:
    """一個步長的控制決定。

    Attributes:
        reason: 為什麼是這個決定，供播報與測試判讀。
    """

    power_notch: int
    brake_notch: int
    reason: str

    @property
    def braking(self) -> bool:
        return self.brake_notch > 0


@dataclass
class AtoController:
    """自動列車運轉裝置。

    Attributes:
        available: 這條線有沒有 ATO。沒有的話 :meth:`engage` 一律失敗。
        driverless: 是否為無人駕駛（車門與發車也自動）。
        engaged: 目前是否由 ATO 駕駛。
        departure_authorised: 駕駛已按下發車。無人駕駛時由停站計時自動給。
    """

    spec: TrainType
    available: bool = False
    driverless: bool = False
    engaged: bool = False
    departure_authorised: bool = False
    dwell_remaining_s: float = 0.0

    _held_at: str | None = field(default=None, init=False, repr=False)
    """目前停妥待發的車站；換一站就要重新取得發車授權。"""

    # ------------------------------------------------------------------
    def engage(self) -> bool:
        """啟動自動駕駛。這條線沒有 ATO 時回傳 ``False``。"""
        if not self.available:
            return False
        self.engaged = True
        return True

    def disengage(self) -> None:
        self.engaged = False
        self.departure_authorised = False
        self._held_at = None
        self.dwell_remaining_s = 0.0

    def authorise_departure(self) -> None:
        """駕駛按下發車鍵。"""
        self.departure_authorised = True

    def arrive_at(self, station_id: str | None) -> bool:
        """通知目前停妥在哪一站，回傳是否換了一站。

        換站時撤銷上一次的發車授權——否則在前一站按過的發車會讓列車一到下
        一站就直接開走，駕駛根本來不及開門。
        """
        if station_id == self._held_at:
            return False
        self._held_at = station_id
        self.departure_authorised = False
        self.dwell_remaining_s = DWELL_S if station_id is not None else 0.0
        return True

    # ------------------------------------------------------------------
    def decide(
        self,
        train: Train,
        *,
        permitted_kmh: float,
        distance_to_stop_m: float | None,
        holding: bool,
    ) -> AtoDecision:
        """算出這個步長該下什麼指令。

        Args:
            permitted_kmh: ATP 的允許速度。
            distance_to_stop_m: 到下一個停車點的距離；``None`` 表示前方沒有
                停車點（例如已完成全部停靠）。
            holding: 是否應該停在原地待發（停妥於車站且尚未取得發車授權）。
        """
        if train.emergency_brake:
            # 緊急制軔是駕駛或 ATP 的決定，ATO 不得覆蓋。
            return AtoDecision(0, train.brake_notch, "emergency")

        if train.any_door_open:
            return AtoDecision(0, self._notch(_HOLD_BRAKE_RATIO), "doors_open")

        if holding:
            return AtoDecision(0, self._notch(_HOLD_BRAKE_RATIO), "holding")

        if distance_to_stop_m is not None:
            braking = self._approach(train, distance_to_stop_m)
            if braking is not None:
                return braking

        target = min(permitted_kmh, self.spec.max_speed_kmh) - CRUISE_MARGIN_KMH
        if target <= 0.0:
            return AtoDecision(0, self._notch(_COMFORT_BRAKE_RATIO), "speed_zero")
        if train.current_speed_kmh < target - _RESUME_MARGIN_KMH:
            return AtoDecision(self.spec.power_notches, 0, "accelerate")
        if train.current_speed_kmh > target:
            # 差得愈多煞得愈重：巡航時的一點點誤差用輕微制軔就好，允許速度
            # 正在下降時則必須跟得上監控曲線（常用制軔的七成）。
            excess = train.current_speed_kmh - target
            ratio = (
                _CREEP_BRAKE_RATIO
                if excess <= _TRIM_EXCESS_KMH
                else _COMFORT_BRAKE_RATIO
            )
            return AtoDecision(0, self._notch(ratio), "trim")
        return AtoDecision(0, 0, "coast")

    def _approach(self, train: Train, distance_m: float) -> AtoDecision | None:
        """進站減速。不需要減速時回傳 ``None``，交給巡航段處理。"""
        if distance_m <= 0.0:
            return AtoDecision(0, self.spec.brake_notches, "stop")
        if train.current_speed_kmh <= _CREEP_SPEED_KMH:
            # 低速時 v²/(2d) 會在最後幾公尺爆增，改用固定段位慢慢貼上去；
            # 距離已經很近就直接全制軔停妥。
            if distance_m <= 1.0:
                return AtoDecision(0, self.spec.brake_notches, "stop")
            if distance_m <= _CREEP_SPEED_KMH:
                return AtoDecision(0, self._notch(_CREEP_BRAKE_RATIO), "creep")
            return None

        comfortable = self.spec.max_service_brake_ms2 * _COMFORT_BRAKE_RATIO
        required = required_decel_ms2(train.current_speed_kmh, 0.0, distance_m)
        if required < comfortable * 0.9:
            return None
        return AtoDecision(0, service_brake_notch_for(self.spec, required), "approach")

    def _notch(self, ratio: float) -> int:
        return max(1, round(self.spec.brake_notches * ratio))

    # ------------------------------------------------------------------
    def tick_dwell(self, dt_s: float) -> bool:
        """無人駕駛的停站計時。回傳這一步是否剛好數到零（該關門發車了）。"""
        if not (self.engaged and self.driverless) or self.dwell_remaining_s <= 0.0:
            return False
        self.dwell_remaining_s -= dt_s
        return self.dwell_remaining_s <= 0.0
