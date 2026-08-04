"""制軔與緊急制軔測試（規格 §8.2–§8.4、§23.1）。"""

from __future__ import annotations

import pytest

from railway_sim.simulation import braking, physics
from railway_sim.simulation.train import Train, TrainType


class TestServiceBrake:
    """制軔增加與鬆軔（規格 §23.1）。"""

    def test_brake_up_increases_one_notch(self, train: Train, train_spec: TrainType) -> None:
        result = braking.brake_up(train, train_spec)
        assert result.accepted
        assert train.brake_notch == 1

    def test_brake_up_stops_at_max_notch(self, train: Train, train_spec: TrainType) -> None:
        for _ in range(train_spec.brake_notches):
            braking.brake_up(train, train_spec)
        assert train.brake_notch == 7

        result = braking.brake_up(train, train_spec)
        assert not result.accepted
        assert result.reason == "max_brake"
        assert train.brake_notch == 7

    def test_brake_up_cuts_power(self, train: Train, train_spec: TrainType) -> None:
        train.power_notch = 4
        braking.brake_up(train, train_spec)
        assert train.power_notch == 0

    def test_release_brake_clears_all_notches(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.brake_notch = 5
        result = braking.release_brake(train, train_spec)
        assert result.accepted
        assert train.brake_notch == 0

    def test_release_brake_does_not_add_power(
        self, train: Train, train_spec: TrainType
    ) -> None:
        """鬆軔不得自動增加電門（規格 §8.4）。"""
        train.brake_notch = 5
        train.power_notch = 0
        braking.release_brake(train, train_spec)
        assert train.power_notch == 0

    def test_brake_decel_scales_with_notch(self, train_spec: TrainType) -> None:
        full = braking.brake_decel_ms2(train_spec, 7, False)
        assert full == pytest.approx(train_spec.max_service_brake_ms2)
        assert braking.brake_decel_ms2(train_spec, 0, False) == 0.0


class TestEmergencyBrake:
    """緊急制軔（規格 §8.3、§23.1）。"""

    def test_emergency_zeroes_power_immediately(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.power_notch = 5
        braking.apply_emergency(train, train_spec)
        assert train.emergency_brake is True
        assert train.power_notch == 0

    def test_emergency_applies_maximum_braking(
        self, train: Train, train_spec: TrainType
    ) -> None:
        braking.apply_emergency(train, train_spec)
        decel = braking.brake_decel_ms2(train_spec, train.brake_notch, train.emergency_brake)
        assert decel == pytest.approx(train_spec.emergency_brake_ms2)
        assert decel > train_spec.max_service_brake_ms2

    def test_traction_cannot_be_restored_while_moving(
        self, train: Train, train_spec: TrainType
    ) -> None:
        """列車停止前不可恢復牽引（規格 §8.3）。"""
        train.current_speed_kmh = 60.0
        braking.apply_emergency(train, train_spec)

        result = braking.power_up(train, train_spec)
        assert not result.accepted
        assert result.reason == "emergency"
        assert train.power_notch == 0

    def test_release_requires_full_stop(self, train: Train, train_spec: TrainType) -> None:
        train.current_speed_kmh = 30.0
        braking.apply_emergency(train, train_spec)

        result = braking.release_emergency(train, train_spec)
        assert not result.accepted
        assert result.reason == "not_stopped"
        assert train.emergency_brake is True

    def test_release_after_stop_succeeds(self, train: Train, train_spec: TrainType) -> None:
        train.current_speed_kmh = 30.0
        braking.apply_emergency(train, train_spec)
        for _ in range(300):
            physics.step(train, train_spec, 0.1)
        assert train.is_stopped

        result = braking.release_emergency(train, train_spec)
        assert result.accepted
        assert train.emergency_brake is False
        assert train.brake_notch == 0

    def test_power_zeroed_every_tick_during_emergency(
        self, train: Train, train_spec: TrainType
    ) -> None:
        """即使直接改寫段位，緊急制軔仍會在每個步長把電門歸零。"""
        train.current_speed_kmh = 50.0
        braking.apply_emergency(train, train_spec)
        train.power_notch = 5  # 模擬外部誤寫
        physics.step(train, train_spec, 0.1)
        assert train.power_notch == 0

    def test_release_brake_blocked_during_emergency(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.current_speed_kmh = 50.0
        braking.apply_emergency(train, train_spec)
        result = braking.release_brake(train, train_spec)
        assert not result.accepted
        assert result.reason == "emergency"


class TestBrakingDistance:
    """制動距離計算（規格 §14.3 提前警告的基礎）。"""

    def test_braking_distance_to_stop(self) -> None:
        # 36 公里／小時 = 10 m/s，1 m/s² → 50 公尺
        assert braking.braking_distance_m(36.0, 0.0, 1.0) == pytest.approx(50.0)

    def test_braking_distance_to_lower_limit(self) -> None:
        # 由 10 m/s 減到 6 m/s，1 m/s² → (100-36)/2 = 32 公尺
        assert braking.braking_distance_m(36.0, 21.6, 1.0) == pytest.approx(32.0)

    def test_reaction_time_adds_distance(self) -> None:
        without = braking.braking_distance_m(36.0, 0.0, 1.0)
        with_reaction = braking.braking_distance_m(36.0, 0.0, 1.0, reaction_time_s=2.0)
        assert with_reaction == pytest.approx(without + 20.0)

    def test_zero_when_already_below_target(self) -> None:
        assert braking.braking_distance_m(20.0, 60.0, 1.0) == 0.0

    def test_required_decel(self) -> None:
        assert braking.required_decel_ms2(36.0, 0.0, 50.0) == pytest.approx(1.0)

    def test_service_brake_notch_selection(self, train_spec: TrainType) -> None:
        assert braking.service_brake_notch_for(train_spec, 0.0) == 0
        # 1.0 m/s² 全制軔 / 7 段 → 每段 0.1428…，0.5 需要 4 段
        assert braking.service_brake_notch_for(train_spec, 0.5) == 4
        assert braking.service_brake_notch_for(train_spec, 5.0) == 7
