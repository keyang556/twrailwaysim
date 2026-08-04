"""司機員模式整合與驗收測試（規格 §4.1、§23.2）。

規格 §23.2 列出第一階段完成時玩家必須能做到的十五件事。本檔逐項驗證，
且**全部透過鍵盤派送**執行，藉此證明所有操作都不需要滑鼠。
"""

from __future__ import annotations

import io

import pytest
from conftest import drive_to, make_session

from railway_sim.accessibility.announcer import Announcer, Priority
from railway_sim import app
from railway_sim.app import SCENARIOS, build_session, main
from railway_sim.data_loader import GameData
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession


@pytest.fixture
def dispatcher(game_data: GameData, local_session: DriverSession) -> KeyDispatcher:
    """只綁定司機員動作的派送器，用於「純鍵盤操作」驗證。"""
    keymap = Keymap.from_dict(game_data.keymap_raw, "driver")
    dispatcher = KeyDispatcher(keymap)
    dispatcher.register_all(local_session.action_handlers())  # type: ignore[arg-type]
    dispatcher.register_all(
        {"show_help": lambda: None, "repeat_last": lambda: None, "pause_menu": lambda: None}
    )
    return dispatcher


def spoken(session: DriverSession) -> list[str]:
    session.announcer.flush()
    return session.announcer.texts()


class TestKeyboardOnlyOperation:
    """驗收第 2 項：完全不使用滑鼠。"""

    def test_accelerate_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 5 項：加速。"""
        assert dispatcher.dispatch("D").handled
        assert local_session.train.power_notch == 1
        assert "電門一段。" in spoken(local_session)

    def test_decelerate_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 6 項：減速。"""
        assert dispatcher.dispatch("A").handled
        assert local_session.train.brake_notch == 1
        assert "制軔一段。" in spoken(local_session)

    def test_horn_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 7 項：鳴笛。"""
        assert dispatcher.dispatch("H").handled
        assert "鳴笛。" in spoken(local_session)

    def test_query_speed_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 8 項：查詢速度。"""
        dispatcher.dispatch("V")
        assert any("目前速度" in t for t in spoken(local_session))

    def test_query_position_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 9 項：查詢位置。"""
        dispatcher.dispatch("P")
        assert any("臺中至大慶間" in t for t in spoken(local_session))

    def test_query_next_station_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 10 項：查詢前方車站。"""
        dispatcher.dispatch("N")
        assert any("前方車站大慶" in t for t in spoken(local_session))

    def test_query_signal_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        dispatcher.dispatch("G")
        assert any("前方號誌" in t for t in spoken(local_session))

    def test_query_train_status_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        dispatcher.dispatch("T")
        assert any("區間車2701次" in t for t in spoken(local_session))

    def test_emergency_brake_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """驗收第 14 項：使用緊急制軔。"""
        assert dispatcher.dispatch("SPACE").handled
        assert local_session.train.emergency_brake is True
        assert "緊急制軔。" in spoken(local_session)

    def test_release_brake_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        dispatcher.dispatch("A")
        dispatcher.dispatch("A")
        dispatcher.dispatch("R")
        assert local_session.train.brake_notch == 0
        assert "制軔緩解。" in spoken(local_session)

    def test_notch_down_by_key(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        dispatcher.dispatch("D")
        dispatcher.dispatch("D")
        dispatcher.dispatch("S")
        assert local_session.train.power_notch == 1

    def test_every_key_press_produces_feedback(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        """規格 §7.2：每次按鍵都應提供文字回饋。"""
        for key in ("D", "A", "S", "R", "H", "V", "P", "N", "G", "T", "SPACE"):
            local_session.announcer.clear_history()
            dispatcher.dispatch(key)
            assert spoken(local_session), f"按鍵 {key} 沒有任何文字回饋"


class TestEmergencyBrakeFlow:
    """規格 §8.3 的緊急制軔完整流程。"""

    def test_emergency_stops_the_train(self, local_session: DriverSession) -> None:
        local_session.train.current_speed_kmh = 80.0
        local_session.emergency_brake()
        for _ in range(400):
            local_session.tick(0.1)
        assert local_session.train.is_stopped

    def test_cannot_accelerate_during_emergency(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        local_session.train.current_speed_kmh = 80.0
        dispatcher.dispatch("SPACE")
        local_session.announcer.clear_history()

        dispatcher.dispatch("D")
        assert local_session.train.power_notch == 0
        assert any("無法加電門" in t for t in spoken(local_session))

    def test_release_flow_requires_stop_then_succeeds(
        self, dispatcher: KeyDispatcher, local_session: DriverSession
    ) -> None:
        local_session.train.current_speed_kmh = 80.0
        dispatcher.dispatch("SPACE")

        dispatcher.dispatch("E")
        assert local_session.train.emergency_brake is True

        for _ in range(400):
            local_session.tick(0.1)
        dispatcher.dispatch("E")
        assert local_session.train.emergency_brake is False
        assert any("緊急制軔解除" in t for t in spoken(local_session))


class TestFullServiceRun:
    """驗收第 11、12 項：在停靠站正確停車、通過非停靠站。"""

    def test_local_service_stops_at_every_scheduled_station(
        self, game_data: GameData
    ) -> None:
        session = make_session(game_data, "2701")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=3600.0)

        unserved = [p.name_zh_tw for p in session.stations if p.must_stop and not p.served]
        assert unserved == []
        assert session.incidents.count_of("missed_stop") == 0

    def test_local_service_stops_at_chenggong(self, game_data: GameData) -> None:
        """成功站僅停靠區間車（chat.md 成功站資料修正）。"""
        session = make_session(game_data, "2701")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=3600.0)

        chenggong = next(p for p in session.stations if p.station_id == "CHENGGONG")
        assert chenggong.served
        assert chenggong.stop_offset_m is not None
        assert abs(chenggong.stop_offset_m) <= 50.0

    def test_express_service_passes_chenggong_and_stops_at_changhua(
        self, game_data: GameData
    ) -> None:
        """自強號通過成功站，停靠彰化（規格 §25.7）。"""
        session = make_session(game_data, "121")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=3600.0)

        chenggong = next(p for p in session.stations if p.station_id == "CHENGGONG")
        changhua = next(p for p in session.stations if p.station_id == "CHANGHUA")
        assert chenggong.passed
        assert not chenggong.served
        assert changhua.served

    def test_service_completion_is_announced(self, game_data: GameData) -> None:
        session = make_session(game_data, "2701")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=3600.0)
        assert session.finished
        assert any("運轉結束" in t for t in spoken(session))

    def test_no_violations_on_a_clean_run(self, game_data: GameData) -> None:
        session = make_session(game_data, "2701")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=3600.0)
        assert session.incidents.violations == []


class TestStatusText:
    """規格 §25.5：必要資訊不得只放在視覺介面。"""

    def test_status_contains_all_required_fields(
        self, local_session: DriverSession
    ) -> None:
        local_session.tick(0.1)
        text = local_session.status_text()
        for label in (
            "目前速度",
            "允許速度",
            "電門段位",
            "制軔段位",
            "緊急制軔",
            "位置",
            "前方車站",
            "前方號誌",
            "所在閉塞",
            "行車違規",
        ):
            assert label in text, f"狀態文字缺少：{label}"

    def test_status_marks_chenggong_as_pass_for_express(
        self, express_session: DriverSession
    ) -> None:
        express_session.train.position_m = 10000.0
        express_session.tick(0.1)
        assert "成功（通過站）" in express_session.status_text()


class TestApproachAnnouncements:
    """規格 §14.3：提前警告，不在已無法制動時才警告。"""

    def test_approach_announced_before_a_stop_station(
        self, local_session: DriverSession
    ) -> None:
        daging = local_session.route.stop_for_station("DAGING")
        assert daging is not None
        local_session.train.position_m = daging.position_m - 700.0
        local_session.tick(0.1)
        assert any("接近大慶" in t for t in spoken(local_session))

    def test_stop_point_warning_names_the_station(
        self, local_session: DriverSession
    ) -> None:
        """接近停車點的警告要說出地點，不可播成「前方速限零公里」。"""
        daging = local_session.route.stop_for_station("DAGING")
        assert daging is not None
        # 80 公里／小時的制動距離約 440 公尺，因此在 400 公尺處必定已發出警告。
        local_session.train.position_m = daging.position_m - 400.0
        local_session.train.current_speed_kmh = 80.0
        local_session.tick(0.1)

        warnings = [t for t in spoken(local_session) if "請減速" in t]
        assert warnings
        assert all("速限零公里" not in t for t in warnings)
        assert any("大慶站停車位置" in t for t in warnings)

    def test_pass_station_announced_in_advance(
        self, express_session: DriverSession
    ) -> None:
        chenggong = express_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        express_session.train.position_m = chenggong.position_m - 700.0
        express_session.tick(0.1)
        assert any("成功站為通過站" in t for t in spoken(express_session))


class TestEventBus:
    """事件必須發佈到匯流排，介面才不會是唯一資訊來源。"""

    def test_actions_publish_events(self, local_session: DriverSession) -> None:
        local_session.power_up()
        local_session.horn()
        local_session.emergency_brake()

        assert local_session.bus.events_of("power_notch")
        assert local_session.bus.events_of("horn")
        assert local_session.bus.events_of("emergency_brake")

    def test_station_events_published(self, express_session: DriverSession) -> None:
        chenggong = express_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        drive_to(express_session, chenggong.position_m + 100)
        events = express_session.bus.events_of("station_passed")
        assert any(e.get("station_id") == "CHENGGONG" for e in events)


class TestScenarios:
    """驗收第 1、3 項：啟動遊戲、選擇司機員模式。"""

    def test_all_scenarios_build(self, game_data: GameData) -> None:
        for scenario in SCENARIOS.values():
            session = build_session(game_data, scenario, Announcer())
            assert session.route.length_m > 0
            assert session.train.current_speed_kmh == 0.0

    def test_red_signal_scenario_creates_a_stop_signal(
        self, game_data: GameData
    ) -> None:
        session = build_session(game_data, SCENARIOS["red_signal"], Announcer())
        session.train.position_m = 11000.0
        state, _ = session.atp.evaluate(session.train, 0.0)
        assert str(state.next_signal_aspect) == "stop"

    def test_obstruction_is_recorded_as_a_special_incident(
        self, game_data: GameData
    ) -> None:
        """規格 §12.3：站外等待屬特殊事件，必須明確記錄。"""
        session = build_session(game_data, SCENARIOS["red_signal"], Announcer())
        assert len(session.incidents.incidents) == 1
        assert session.incidents.incidents[0].kind == "train_failure"

    def test_normal_scenario_has_no_incidents(self, game_data: GameData) -> None:
        """規格 §12.2：正常運轉不應出現站外等待。"""
        session = build_session(game_data, SCENARIOS["local"], Announcer())
        assert session.incidents.incidents == []


class TestCommandLine:
    """驗收第 1、15 項：可啟動、可離開（回傳結束碼）。"""

    def test_check_mode_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--check"]) == 0
        assert "驗證通過" in capsys.readouterr().out

    def test_list_scenarios(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--list-scenarios"]) == 0
        assert "區間車 2701 次" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            (["--check"], "資料驗證通過"),
            (["--list-scenarios"], "區間車 2701 次"),
        ],
    )
    def test_cli_reconfigures_incompatible_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        arguments: list[str],
        expected: str,
    ) -> None:
        output = io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding="cp1252")
        monkeypatch.setattr(app.sys, "stdout", stdout)

        assert main(arguments) == 0

        stdout.flush()
        assert expected in output.getvalue().decode("utf-8")

    def test_cli_reconfigures_incompatible_stderr(self, monkeypatch, tmp_path) -> None:
        output = io.BytesIO()
        stderr = io.TextIOWrapper(output, encoding="cp1252")
        monkeypatch.setattr(app.sys, "stderr", stderr)

        assert main(["--check", "--data-dir", str(tmp_path)]) == 2

        stderr.flush()
        assert "資料載入失敗" in output.getvalue().decode("utf-8")

    def test_missing_data_dir_returns_error(self, tmp_path) -> None:
        assert main(["--check", "--data-dir", str(tmp_path)]) == 2


class TestAnnouncementPriorities:
    """規格 §21.1：安全警告可中斷一般播報。"""

    def test_emergency_interrupts_pending_status(
        self, local_session: DriverSession
    ) -> None:
        local_session.announce_speed()
        local_session.announce_position()
        local_session.emergency_brake()

        pending = local_session.announcer.pending
        assert [a.text for a in pending] == ["緊急制軔。"]
        assert pending[0].priority is Priority.EMERGENCY
