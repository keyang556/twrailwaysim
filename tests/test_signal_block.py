"""號誌、閉塞與紅燈防護測試（規格 §11、§12、§14.2、§23.1）。"""

from __future__ import annotations

import pytest
from conftest import (
    LOCAL_SERVICE,
    block_midpoint,
    drive_to,
    make_session,
    signal_position,
    slowest_segment,
)

from railway_sim.data_loader import GameData
from railway_sim.railway.block import Block, BlockSystem
from railway_sim.railway.signal import SignalAspect
from railway_sim.roles.driver import DriverSession


class TestBlockOccupancy:
    """閉塞占用（規格 §12.1、§23.1）。"""

    def test_blocks_are_built_from_route_nodes(self, local_session: DriverSession) -> None:
        blocks = local_session.blocks.blocks
        assert len(blocks) == len(local_session.route.node_ids) - 1
        assert blocks[0].start_m == 0.0
        assert blocks[-1].end_m == pytest.approx(local_session.route.length_m)

    def test_one_train_occupies_the_block_it_is_in(self) -> None:
        system = BlockSystem(
            blocks=[
                Block(id="B1", start_m=0, end_m=1000),
                Block(id="B2", start_m=1000, end_m=2000),
            ]
        )
        system.update_occupancy("T1", 100.0, 300.0)
        assert system.occupied_summary() == {"B1": "T1"}

    def test_train_spanning_boundary_occupies_both_blocks(self) -> None:
        system = BlockSystem(
            blocks=[
                Block(id="B1", start_m=0, end_m=1000),
                Block(id="B2", start_m=1000, end_m=2000),
            ]
        )
        system.update_occupancy("T1", 950.0, 1050.0)
        assert set(system.occupied_summary()) == {"B1", "B2"}

    def test_block_released_when_train_leaves(self) -> None:
        system = BlockSystem(
            blocks=[
                Block(id="B1", start_m=0, end_m=1000),
                Block(id="B2", start_m=1000, end_m=2000),
            ]
        )
        system.update_occupancy("T1", 100.0, 300.0)
        system.update_occupancy("T1", 1100.0, 1300.0)
        assert system.occupied_summary() == {"B2": "T1"}

    def test_is_free_ignores_own_train(self) -> None:
        system = BlockSystem(blocks=[Block(id="B1", start_m=0, end_m=1000)])
        system.update_occupancy("T1", 100.0, 300.0)
        assert system.is_free("B1", ignore_train_id="T1")
        assert not system.is_free("B1", ignore_train_id="T2")

    def test_session_tracks_current_block(self, local_session: DriverSession) -> None:
        local_session.train.position_m = 4000.0
        local_session._update_occupancy()
        assert local_session.train.current_block_id is not None
        block = local_session.blocks.block(local_session.train.current_block_id)
        assert block.contains(4000.0)


class TestSignalAspects:
    """三顯示號誌由閉塞占用推導（規格 §11.1、§11.2）。"""

    def test_clear_when_two_blocks_ahead_are_free(
        self, local_session: DriverSession
    ) -> None:
        signal = local_session.signals.next_signal_ahead(0.0)
        assert signal is not None
        assert local_session.signals.aspect_of(
            signal, ignore_train_id=local_session.train.id
        ) is SignalAspect.CLEAR

    def test_stop_when_protected_block_occupied(
        self, local_session: DriverSession
    ) -> None:
        local_session.add_obstruction(
            "T_OTHER", block_midpoint(local_session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")
        )
        signal = local_session.signals.signal("SIG_BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")
        assert local_session.signals.aspect_of(
            signal, ignore_train_id=local_session.train.id
        ) is SignalAspect.STOP

    def test_caution_when_the_block_beyond_is_occupied(
        self, local_session: DriverSession
    ) -> None:
        local_session.add_obstruction(
            "T_OTHER", block_midpoint(local_session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")
        )
        signal = local_session.signals.signal("SIG_BLK_STA_CHENGGONG_JCT_CHENGZHUI")
        assert local_session.signals.aspect_of(
            signal, ignore_train_id=local_session.train.id
        ) is SignalAspect.CAUTION

    def test_forced_stop_overrides_free_blocks(
        self, local_session: DriverSession
    ) -> None:
        """未鎖閉道岔不可開放號誌（規格 §13.2）。"""
        signal = local_session.signals.signals[2]
        signal.forced_stop = True
        assert local_session.signals.aspect_of(signal) is SignalAspect.STOP

    def test_permitted_speed_by_aspect(self, local_session: DriverSession) -> None:
        signal = local_session.signals.signals[2]
        assert local_session.signals.permitted_speed_kmh(signal, 110.0) == 110.0

        signal.forced_stop = True
        assert local_session.signals.permitted_speed_kmh(signal, 110.0) == 0.0


class TestSignalProtection:
    """紅燈防護（規格 §14.2、§23.1）。"""

    def test_atp_reduces_permitted_speed_before_a_stop_signal(
        self, game_data: GameData
    ) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        session.add_obstruction("T_FAULT", block_midpoint(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"))
        stop_signal_m = signal_position(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")

        # 停在停止號誌前 200 公尺
        session.train.position_m = stop_signal_m - 200.0
        state, _ = session.atp.evaluate(session.train, 0.0)
        assert state.next_signal_aspect is SignalAspect.STOP
        assert state.permitted_kmh < state.line_limit_kmh

    def test_train_can_stop_before_a_red_signal(self, game_data: GameData) -> None:
        """驗收測試第 13 項：在紅燈前停車。"""
        session = make_session(game_data, LOCAL_SERVICE)
        session.add_obstruction("T_FAULT", block_midpoint(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"))
        stop_signal_m = signal_position(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")

        drive_to(session, stop_signal_m - 50.0, respect_stops=True, max_seconds=3600.0)

        assert session.train.position_m <= stop_signal_m
        assert session.incidents.count_of("spad") == 0

    def test_passing_a_stop_signal_is_a_violation(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        session.add_obstruction("T_FAULT", block_midpoint(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"))
        stop_signal_m = signal_position(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")

        # 直接把列車放到號誌之後，模擬冒進
        session.train.position_m = stop_signal_m + 50.0
        session.train.current_speed_kmh = 60.0
        session.tick(0.1)
        session.announcer.flush()

        assert session.incidents.count_of("spad") == 1
        assert any("冒進號誌" in t for t in session.announcer.texts())

    def test_spad_applies_emergency_brake(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        session.add_obstruction("T_FAULT", block_midpoint(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"))
        stop_signal_m = signal_position(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")
        session.train.position_m = stop_signal_m + 50.0
        session.train.current_speed_kmh = 60.0
        session.tick(0.1)

        assert session.train.emergency_brake is True

    def test_spad_recorded_only_once(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        session.add_obstruction("T_FAULT", block_midpoint(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"))
        stop_signal_m = signal_position(session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N")
        session.train.position_m = stop_signal_m + 50.0
        session.train.current_speed_kmh = 60.0
        for _ in range(50):
            session.tick(0.1)
        assert session.incidents.count_of("spad") == 1


class TestOverspeed:
    """超速處理（規格 §14.2）。

    測試在路線上速限最低的區間進行：山線速限與 EMU900 的最高速度同為 110，
    在那裡列車根本開不到超速。成追線這類低速區間才有超速的空間。
    """

    def test_warning_on_first_overspeed(
        self, chengzhui_session: DriverSession
    ) -> None:
        position, limit = slowest_segment(chengzhui_session)
        chengzhui_session.train.position_m = position
        chengzhui_session.train.current_speed_kmh = limit + 10.0
        chengzhui_session.tick(0.1)
        chengzhui_session.announcer.flush()
        assert any("超速" in t for t in chengzhui_session.announcer.texts())

    def test_sustained_overspeed_records_violation(
        self, chengzhui_session: DriverSession
    ) -> None:
        position, limit = slowest_segment(chengzhui_session)
        chengzhui_session.train.position_m = position
        for _ in range(80):
            chengzhui_session.train.current_speed_kmh = limit + 10.0
            chengzhui_session.tick(0.1)
        assert chengzhui_session.incidents.count_of("overspeed") >= 1

    def test_severe_overspeed_triggers_emergency_brake(
        self, chengzhui_session: DriverSession
    ) -> None:
        position, limit = slowest_segment(chengzhui_session)
        chengzhui_session.train.position_m = position
        chengzhui_session.train.current_speed_kmh = limit + 20.0
        chengzhui_session.tick(0.1)
        assert chengzhui_session.train.emergency_brake is True

    def test_no_violation_within_limit(
        self, chengzhui_session: DriverSession
    ) -> None:
        position, limit = slowest_segment(chengzhui_session)
        chengzhui_session.train.position_m = position
        for _ in range(100):
            chengzhui_session.train.current_speed_kmh = limit - 5.0
            chengzhui_session.tick(0.1)
        assert chengzhui_session.incidents.count_of("overspeed") == 0
        assert chengzhui_session.train.emergency_brake is False

    def test_station_stop_does_not_trigger_enforcement(
        self, local_session: DriverSession
    ) -> None:
        """營業停靠不是保安功能，ATP 不因停不下來而介入（規格 §9.2）。"""
        daqing = local_session.route.stop_for_station("DAQING")
        assert daqing is not None
        local_session.train.position_m = daqing.position_m - 100.0
        local_session.train.current_speed_kmh = 80.0
        local_session.tick(0.1)

        assert local_session.train.emergency_brake is False
        assert local_session.last_state is not None
        assert local_session.last_state.advisory_kmh < local_session.last_state.permitted_kmh
