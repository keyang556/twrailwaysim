"""制軔操作與制動距離計算（規格 §8.2–§8.4、§14）。

本模組只處理制軔的**狀態轉換規則**與**距離計算**，不負責播報；播報文字
統一由 :mod:`railway_sim.accessibility.messages` 提供。
"""

from __future__ import annotations

from dataclasses import dataclass

from railway_sim.simulation.physics import MS_PER_KMH, brake_decel_ms2
from railway_sim.simulation.train import Train, TrainType

__all__ = [
    "BrakeChange",
    "apply_emergency",
    "brake_decel_ms2",
    "brake_up",
    "braking_distance_m",
    "notch_down",
    "power_up",
    "release_brake",
    "release_emergency",
    "required_decel_ms2",
    "service_brake_notch_for",
]


@dataclass(frozen=True)
class BrakeChange:
    """一次操作的結果。

    Attributes:
        accepted: 操作是否被接受。被安全規則擋下時為 ``False``。
        power_notch: 操作後的電門段位。
        brake_notch: 操作後的制軔段位。
        emergency: 操作後的緊急制軔狀態。
        reason: 操作被擋下的原因。
    """

    accepted: bool
    power_notch: int
    brake_notch: int
    emergency: bool
    reason: str | None = None


def _snapshot(train: Train, *, accepted: bool, reason: str | None = None) -> BrakeChange:
    return BrakeChange(
        accepted=accepted,
        power_notch=train.power_notch,
        brake_notch=train.brake_notch,
        emergency=train.emergency_brake,
        reason=reason,
    )


# ----------------------------------------------------------------------
# 操作
# ----------------------------------------------------------------------
def power_up(train: Train, spec: TrainType) -> BrakeChange:
    """增加電門段位。

    緊急制軔中不可加電門（§8.3）。加電門前會先把制軔緩解到 0，避免同時
    施加牽引與制軔。
    """
    if train.emergency_brake:
        return _snapshot(train, accepted=False, reason="emergency")
    train.brake_notch = 0
    if train.power_notch >= spec.power_notches:
        return _snapshot(train, accepted=False, reason="max_power")
    train.power_notch += 1
    return _snapshot(train, accepted=True)


def brake_up(train: Train, spec: TrainType) -> BrakeChange:
    """增加常用制軔段位。加制軔時電門自動切斷。"""
    if train.emergency_brake:
        return _snapshot(train, accepted=False, reason="emergency")
    train.power_notch = 0
    if train.brake_notch >= spec.brake_notches:
        return _snapshot(train, accepted=False, reason="max_brake")
    train.brake_notch += 1
    return _snapshot(train, accepted=True)


def notch_down(train: Train, spec: TrainType) -> BrakeChange:
    """回到惰行或降低控制段位（規格 §7.1 的 S 鍵）。

    有電門時先減電門，電門為 0 時再減制軔；兩者皆為 0 即為惰行。
    """
    if train.emergency_brake:
        return _snapshot(train, accepted=False, reason="emergency")
    if train.power_notch > 0:
        train.power_notch -= 1
        return _snapshot(train, accepted=True)
    if train.brake_notch > 0:
        train.brake_notch -= 1
        return _snapshot(train, accepted=True)
    return _snapshot(train, accepted=False, reason="already_coasting")


def release_brake(train: Train, spec: TrainType) -> BrakeChange:
    """鬆軔（規格 §8.4）。

    立即把常用制軔降到 0，且**不得自動增加電門**。緊急制軔中無效，必須先
    依 §8.3 的解除流程解除緊急制軔。
    """
    if train.emergency_brake:
        return _snapshot(train, accepted=False, reason="emergency")
    if train.brake_notch == 0:
        return _snapshot(train, accepted=False, reason="already_released")
    train.brake_notch = 0
    # 不自動增加電門（§8.4）
    return _snapshot(train, accepted=True)


def apply_emergency(train: Train, spec: TrainType) -> BrakeChange:
    """施加緊急制軔（規格 §8.3）。"""
    already = train.emergency_brake
    train.emergency_brake = True
    train.power_notch = 0
    train.brake_notch = spec.brake_notches
    return _snapshot(train, accepted=not already, reason=None if not already else "already_applied")


def release_emergency(train: Train, spec: TrainType) -> BrakeChange:
    """解除緊急制軔（規格 §8.3 的明確解除流程）。

    只有在列車完全停妥後才可解除。
    """
    if not train.emergency_brake:
        return _snapshot(train, accepted=False, reason="not_applied")
    if not train.is_stopped:
        return _snapshot(train, accepted=False, reason="not_stopped")
    train.emergency_brake = False
    train.brake_notch = 0
    train.power_notch = 0
    return _snapshot(train, accepted=True)


# ----------------------------------------------------------------------
# 制動距離
# ----------------------------------------------------------------------
def braking_distance_m(
    speed_kmh: float,
    target_speed_kmh: float,
    decel_ms2: float,
    *,
    reaction_time_s: float = 0.0,
) -> float:
    """由目前速度減到目標速度所需的距離（公尺）。

    Args:
        speed_kmh: 目前速度。
        target_speed_kmh: 目標速度。
        decel_ms2: 可用減速度（正值）。
        reaction_time_s: 反應時間，這段時間內視為維持原速。

    Returns:
        所需距離；已達或低於目標速度時為 0。
    """
    if decel_ms2 <= 0:
        return float("inf")
    v0 = max(0.0, speed_kmh) * MS_PER_KMH
    v1 = max(0.0, target_speed_kmh) * MS_PER_KMH
    if v0 <= v1:
        return 0.0
    return v0 * reaction_time_s + (v0 * v0 - v1 * v1) / (2.0 * decel_ms2)


def required_decel_ms2(speed_kmh: float, target_speed_kmh: float, distance_m: float) -> float:
    """在指定距離內由目前速度減到目標速度所需的減速度（正值）。"""
    v0 = max(0.0, speed_kmh) * MS_PER_KMH
    v1 = max(0.0, target_speed_kmh) * MS_PER_KMH
    if v0 <= v1:
        return 0.0
    if distance_m <= 0:
        return float("inf")
    return (v0 * v0 - v1 * v1) / (2.0 * distance_m)


def service_brake_notch_for(spec: TrainType, decel_ms2: float) -> int:
    """達到指定減速度所需的最小常用制軔段位。

    超出常用制軔能力時回傳最大段位。
    """
    if decel_ms2 <= 0:
        return 0
    if spec.max_service_brake_ms2 <= 0:
        return spec.brake_notches
    ratio = decel_ms2 / spec.max_service_brake_ms2
    notch = int(-(-ratio * spec.brake_notches // 1))  # 無條件進位
    return max(1, min(spec.brake_notches, notch))
