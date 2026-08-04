"""列車物理測試（規格 §8、§23.1）。"""

from __future__ import annotations

import pytest

from railway_sim.simulation import physics
from railway_sim.simulation.braking import notch_down, power_up
from railway_sim.simulation.train import Train, TrainType


class TestPowerNotch:
    """電門增加與降低（規格 §23.1）。"""

    def test_power_up_increases_one_notch(self, train: Train, train_spec: TrainType) -> None:
        result = power_up(train, train_spec)
        assert result.accepted
        assert train.power_notch == 1

    def test_power_up_stops_at_max_notch(self, train: Train, train_spec: TrainType) -> None:
        for _ in range(train_spec.power_notches):
            assert power_up(train, train_spec).accepted
        assert train.power_notch == 5

        result = power_up(train, train_spec)
        assert not result.accepted
        assert result.reason == "max_power"
        assert train.power_notch == 5

    def test_notch_down_reduces_power_before_brake(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.power_notch = 3
        notch_down(train, train_spec)
        assert train.power_notch == 2
        assert train.brake_notch == 0

    def test_notch_down_to_coasting(self, train: Train, train_spec: TrainType) -> None:
        train.power_notch = 1
        notch_down(train, train_spec)
        assert train.power_notch == 0
        assert train.brake_notch == 0

        result = notch_down(train, train_spec)
        assert not result.accepted
        assert result.reason == "already_coasting"

    def test_power_up_cuts_brake(self, train: Train, train_spec: TrainType) -> None:
        """加電門時不得同時施加制軔。"""
        train.brake_notch = 4
        power_up(train, train_spec)
        assert train.brake_notch == 0
        assert train.power_notch == 1


class TestAcceleration:
    """加速度 = 牽引 − 制軔 − 阻力（規格 §8.2）。"""

    def test_traction_scales_with_notch(self, train_spec: TrainType) -> None:
        full = physics.traction_accel_ms2(train_spec, 5, 0.0)
        half = physics.traction_accel_ms2(train_spec, 1, 0.0)
        assert full == pytest.approx(1.0)
        assert half == pytest.approx(0.2)

    def test_traction_falls_off_above_corner_speed(self) -> None:
        spec = TrainType(
            id="C",
            name_zh_tw="轉折測試",
            max_speed_kmh=200.0,
            length_m=100.0,
            max_traction_ms2=1.0,
            max_service_brake_ms2=1.0,
            emergency_brake_ms2=2.0,
            power_corner_speed_kmh=50.0,
            resistance_ms2=(0.0, 0.0, 0.0),
        )
        assert physics.traction_accel_ms2(spec, 5, 50.0) == pytest.approx(1.0)
        assert physics.traction_accel_ms2(spec, 5, 100.0) == pytest.approx(0.5)

    def test_net_accel_subtracts_brake_and_resistance(self, train: Train) -> None:
        spec = TrainType(
            id="R",
            name_zh_tw="阻力測試",
            max_speed_kmh=200.0,
            length_m=100.0,
            max_traction_ms2=1.0,
            max_service_brake_ms2=0.7,
            emergency_brake_ms2=2.0,
            power_corner_speed_kmh=1000.0,
            resistance_ms2=(0.1, 0.0, 0.0),
        )
        train.power_notch = 5
        train.brake_notch = 0
        train.current_speed_kmh = 10.0
        assert physics.net_accel_ms2(train, spec) == pytest.approx(0.9)

    def test_no_resistance_when_stationary(self, train_spec: TrainType) -> None:
        assert physics.resistance_ms2(train_spec, 0.0) == 0.0


class TestSpeedLimits:
    """速度不可低於零、不可超過最高速度（規格 §8.2、§23.1）。"""

    def test_speed_never_below_zero(self, train: Train, train_spec: TrainType) -> None:
        train.current_speed_kmh = 5.0
        train.brake_notch = 7
        for _ in range(200):
            physics.step(train, train_spec, 0.1)
        assert train.current_speed_kmh == 0.0

    def test_position_does_not_move_backwards_when_stopping(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.current_speed_kmh = 1.0
        train.brake_notch = 7
        start = train.position_m
        for _ in range(100):
            physics.step(train, train_spec, 0.1)
        assert train.position_m >= start
        assert train.current_speed_kmh == 0.0

    def test_speed_never_above_max(self, train: Train, train_spec: TrainType) -> None:
        train.power_notch = 5
        for _ in range(2000):
            physics.step(train, train_spec, 0.1)
        assert train.current_speed_kmh <= train_spec.max_speed_kmh
        assert train.current_speed_kmh == pytest.approx(train_spec.max_speed_kmh)

    def test_constant_acceleration_matches_kinematics(
        self, train: Train, train_spec: TrainType
    ) -> None:
        """全電門 1 m/s²、無阻力：10 秒後應為 36 公里／小時。"""
        train.power_notch = 5
        for _ in range(100):
            physics.step(train, train_spec, 0.1)
        assert train.current_speed_kmh == pytest.approx(36.0, abs=0.1)
        assert train.position_m == pytest.approx(50.0, abs=0.5)

    def test_stopping_distance_matches_kinematics(
        self, train: Train, train_spec: TrainType
    ) -> None:
        """36 公里／小時、1 m/s² 減速：制動距離應為 50 公尺。"""
        train.current_speed_kmh = 36.0
        train.brake_notch = 7
        for _ in range(300):
            physics.step(train, train_spec, 0.1)
        assert train.current_speed_kmh == 0.0
        assert train.position_m == pytest.approx(50.0, abs=0.5)
