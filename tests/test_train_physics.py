"""列車物理測試（規格 §8、§23.1）。"""

from __future__ import annotations

import pytest

from railway_sim.data_loader import GameData
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


#: 各車輛型式來源標示的性能，用來擋住資料被改回無來源的測試值。
#:
#: 值為 ``(營運速度, 常用減速度 km/h/s, 緊急減速度 km/h/s)``；減速度沒有
#: 來源的車型（EMU900、CK）以 ``None`` 表示不檢查。
SOURCED_PERFORMANCE = {
    "EMU3000": (130.0, 3.6, 4.32),
    "EMU900": (130.0, None, None),
    "TEMU1000": (130.0, 3.6, 4.32),
    "TEMU2000": (130.0, 3.6, 4.32),
    "PP": (130.0, 3.6, 5.5),
    "DR3100": (110.0, 3.0, 3.0),
    "DR1000": (110.0, 2.448, 2.448),
}


class TestRealStockPerformance:
    """``data/trains.json`` 的實車性能（見該檔 meta.sources）。"""

    def test_every_sourced_type_exists(self, game_data: GameData) -> None:
        missing = sorted(set(SOURCED_PERFORMANCE) - set(game_data.train_types))
        assert not missing, f"trains.json 缺少車輛型式：{missing}"

    @pytest.mark.parametrize("type_id", sorted(SOURCED_PERFORMANCE))
    def test_matches_source_figures(self, game_data: GameData, type_id: str) -> None:
        """最高速度與減速度必須等於來源數值（減速度換算成 m/s²）。"""
        spec = game_data.train_type(type_id)
        max_speed, service_kmhs, emergency_kmhs = SOURCED_PERFORMANCE[type_id]
        assert spec.max_speed_kmh == pytest.approx(max_speed)
        if service_kmhs is not None:
            assert spec.max_service_brake_ms2 == pytest.approx(service_kmhs / 3.6, abs=0.001)
        if emergency_kmhs is not None:
            assert spec.emergency_brake_ms2 == pytest.approx(emergency_kmhs / 3.6, abs=0.001)

    @pytest.mark.parametrize("type_id", sorted(SOURCED_PERFORMANCE))
    def test_reaches_its_operating_speed(self, game_data: GameData, type_id: str) -> None:
        """全電門、平直線上必須真的能加速到來源標示的營運速度。

        牽引力與行駛阻力都會隨速度變化，兩者交會處就是實際的極速。阻力
        係數沒有公開來源（``resistance_ms2`` 為測試值），因此這裡把「達得到
        營運速度」當成校驗條件：達不到就表示阻力係數配得不對，而不是車輛
        真的跑不到那個速度。
        """
        spec = game_data.train_type(type_id)
        train = Train(id=f"T_{type_id}", train_type=type_id, length_m=spec.length_m)
        train.power_notch = spec.power_notches
        for _ in range(15 * 60 * 10):  # 15 分鐘、每步 0.1 秒
            physics.step(train, spec, 0.1)
        assert train.current_speed_kmh == pytest.approx(spec.max_speed_kmh, abs=0.1)

    def test_branch_stock_is_slower_than_trunk_stock(self, game_data: GameData) -> None:
        """柴油客車的最高速度低於城際電聯車，資料弄反時會被擋下。"""
        assert (
            game_data.train_type("DR1000").max_speed_kmh
            < game_data.train_type("EMU3000").max_speed_kmh
        )
