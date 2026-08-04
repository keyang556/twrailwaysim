"""簡化離散時間列車物理（規格 §8）。

加速度 = 牽引加速度 − 制軔減速度 − 阻力（§8.2）。

約束（§8.2、§8.3）：

- 速度不可低於 0
- 速度不可高於車種最高速度
- 緊急制軔時電門立即歸零、套用最大制軔力，且在列車停止前不可恢復牽引
"""

from __future__ import annotations

from railway_sim.simulation.train import Train, TrainType

__all__ = [
    "KMH_PER_MS",
    "MS_PER_KMH",
    "brake_decel_ms2",
    "net_accel_ms2",
    "resistance_ms2",
    "step",
    "traction_accel_ms2",
]

MS_PER_KMH = 1.0 / 3.6
KMH_PER_MS = 3.6


def traction_accel_ms2(spec: TrainType, notch: int, speed_kmh: float) -> float:
    """牽引加速度。

    低於轉折速度為定加速度；超過後依定功率遞減，避免列車在高速時仍以
    起步加速度加速。
    """
    if notch <= 0 or spec.power_notches <= 0:
        return 0.0
    ratio = min(notch, spec.power_notches) / spec.power_notches
    base = spec.max_traction_ms2 * ratio
    if speed_kmh <= spec.power_corner_speed_kmh or speed_kmh <= 0.0:
        return base
    return base * (spec.power_corner_speed_kmh / speed_kmh)


def brake_decel_ms2(spec: TrainType, notch: int, emergency: bool) -> float:
    """制軔減速度（正值）。緊急制軔為獨立狀態，不受段位影響（§8.2）。"""
    if emergency:
        return spec.emergency_brake_ms2
    if notch <= 0 or spec.brake_notches <= 0:
        return 0.0
    ratio = min(notch, spec.brake_notches) / spec.brake_notches
    return spec.max_service_brake_ms2 * ratio


def resistance_ms2(spec: TrainType, speed_kmh: float) -> float:
    """行駛阻力造成的減速度（正值）。列車靜止時為 0。"""
    if speed_kmh <= 0.0:
        return 0.0
    v = speed_kmh * MS_PER_KMH
    r0, r1, r2 = spec.resistance_ms2
    return r0 + r1 * v + r2 * v * v


def net_accel_ms2(train: Train, spec: TrainType) -> float:
    """目前的淨加速度（公尺／秒平方，正值為加速）。"""
    power = 0 if train.emergency_brake else train.power_notch
    traction = traction_accel_ms2(spec, power, train.current_speed_kmh)
    braking = brake_decel_ms2(spec, train.brake_notch, train.emergency_brake)
    drag = resistance_ms2(spec, train.current_speed_kmh)
    return traction - braking - drag


def step(train: Train, spec: TrainType, dt_s: float) -> float:
    """推進一個模擬步長，回傳本步移動的距離（公尺）。

    速度會被限制在 ``0`` 與車種最高速度之間；緊急制軔時電門強制歸零。
    """
    if dt_s <= 0:
        return 0.0

    if train.emergency_brake:
        train.power_notch = 0  # §8.3：電門立即歸零

    accel = net_accel_ms2(train, spec)
    v0 = train.current_speed_kmh * MS_PER_KMH
    v1 = v0 + accel * dt_s

    if v1 <= 0.0:
        # 本步之內停妥：只走到停止點為止，速度不可低於 0（§8.2）。
        distance = (v0 * v0) / (2.0 * -accel) if accel < 0 else 0.0
        distance = max(0.0, min(distance, v0 * dt_s))
        train.current_speed_kmh = 0.0
    else:
        max_ms = spec.max_speed_kmh * MS_PER_KMH
        v1 = min(v1, max_ms)
        distance = (v0 + v1) * 0.5 * dt_s
        train.current_speed_kmh = v1 * KMH_PER_MS

    train.position_m += distance
    train.distance_travelled_m += distance
    return distance
