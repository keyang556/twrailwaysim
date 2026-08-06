"""停靠站、通過站與應停未停測試（規格 §9、§23.1、chat.md 成功站資料修正）。"""

from __future__ import annotations

from conftest import LOCAL_SERVICE, TZE_CHIANG_SERVICE, drive_to, make_session

from railway_sim.data_loader import GameData
from railway_sim.roles.driver import STOP_WINDOW_M, DriverSession
from railway_sim.timetable.service import Service
from railway_sim.timetable.stop_pattern import resolve_stop_kind, validate_service


class TestStopKindResolution:
    """停靠判斷一律依班次停靠表，不得由路線推論（規格 §9.1）。"""

    def test_local_stops_at_chenggong(self, game_data: GameData) -> None:
        service = game_data.service(LOCAL_SERVICE)
        station = game_data.stations["CHENGGONG"]
        assert resolve_stop_kind(service, station) == "stop"

    def test_tze_chiang_passes_chenggong(self, game_data: GameData) -> None:
        """自強號不停靠成功站（規格 §25.7）。"""
        service = game_data.service(TZE_CHIANG_SERVICE)
        station = game_data.stations["CHENGGONG"]
        assert resolve_stop_kind(service, station) == "pass"

    def test_station_rules_reject_tze_chiang_at_chenggong(
        self, game_data: GameData
    ) -> None:
        station = game_data.stations["CHENGGONG"]
        assert station.allows_train_type("local") is True
        assert station.allows_train_type("tze_chiang") is False
        assert station.allows_train_type("chu_kuang") is False

    def test_unlisted_station_is_not_a_stop(self, game_data: GameData) -> None:
        """未列於停靠表的車站不得因為「列車經過這條路線」而判定停靠。"""
        service = Service(
            train_number="TEST",
            train_type="local",
            route_id="R_TEST",
            rolling_stock_id="EMU900",
            stop_station_ids=("TAICHUNG", "CHANGHUA"),
        )
        assert resolve_stop_kind(service, game_data.stations["LILIN"]) == "pass"

    def test_conditional_kept_when_not_resolved(self, game_data: GameData) -> None:
        """車站辦理該車種、但班次未列出時，屬「依班次判斷」。

        未收斂時保留 ``conditional`` 供資料檢查與播報使用，收斂後預設通過。
        """
        service = Service(
            train_number="TEST",
            train_type="local",
            route_id="R_TEST",
            rolling_stock_id="EMU900",
            stop_station_ids=("TAICHUNG",),
        )
        station = game_data.stations["LILIN"]
        assert station.allows_train_type("local")
        assert resolve_stop_kind(service, station, resolve_conditional=False) == "conditional"
        assert resolve_stop_kind(service, station) == "pass"

    def test_local_express_does_not_serve_chenggong(self, game_data: GameData) -> None:
        """115 年 7 月 1 日的時刻表中沒有任何區間快停靠成功站。

        規格與 chat.md 當時寫的是「區間快依班次設定，一般通過」；實際時刻表
        匯入後可以確定為完全不停，因此在本站不是「依班次判斷」而是通過。
        """
        station = game_data.stations["CHENGGONG"]
        assert not station.allows_train_type("local_express")

        service = Service(
            train_number="TEST",
            train_type="local_express",
            route_id="R_TEST",
            rolling_stock_id="EMU900",
            stop_station_ids=("TAICHUNG",),
        )
        assert resolve_stop_kind(service, station, resolve_conditional=False) == "pass"


class TestServiceValidation:
    """停靠表與車站規則的一致性檢查（規格 §25.7）。"""

    def test_tze_chiang_stopping_at_chenggong_is_rejected(
        self, game_data: GameData
    ) -> None:
        bad = Service(
            train_number="999",
            train_type="tze_chiang",
            route_id="R_TEST",
            rolling_stock_id="EMU3000",
            stop_station_ids=("TAICHUNG", "CHENGGONG", "CHANGHUA"),
        )
        errors = validate_service(bad, game_data.stations)
        assert any("成功" in e for e in errors)

    def test_shipped_services_are_valid(self, game_data: GameData) -> None:
        for service in game_data.services.values():
            assert validate_service(service, game_data.stations) == []

    def test_station_listed_as_both_stop_and_pass_is_rejected(
        self, game_data: GameData
    ) -> None:
        bad = Service(
            train_number="998",
            train_type="local",
            route_id="R_TEST",
            rolling_stock_id="EMU900",
            stop_station_ids=("LILIN",),
            pass_station_ids=("LILIN",),
        )
        assert validate_service(bad, game_data.stations) != []


class TestSessionStopPattern:
    """工作階段依停靠表建立每站的停靠別。"""

    def test_local_session_stops_at_chenggong(self, local_session: DriverSession) -> None:
        progress = next(p for p in local_session.stations if p.station_id == "CHENGGONG")
        assert progress.stop_kind == "stop"
        assert progress.must_stop

    def test_express_session_passes_chenggong(
        self, express_session: DriverSession
    ) -> None:
        progress = next(p for p in express_session.stations if p.station_id == "CHENGGONG")
        assert progress.stop_kind == "pass"
        assert not progress.must_stop

    def test_origin_station_starts_served(self, local_session: DriverSession) -> None:
        origin = local_session.stations[0]
        assert origin.station_id == local_session.route.station_ids[0]
        assert origin.served


class TestPassingStation:
    """通過站：不啟動停站流程，只播報通過（規格 §9.3）。"""

    def test_express_announces_passing_chenggong(
        self, express_session: DriverSession
    ) -> None:
        chenggong = express_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        drive_to(express_session, chenggong.position_m + 50)
        express_session.announcer.flush()

        progress = next(
            p for p in express_session.stations if p.station_id == "CHENGGONG"
        )
        assert progress.passed
        assert not progress.missed
        assert any("通過成功站" in t for t in express_session.announcer.texts())

    def test_passing_station_records_no_violation(
        self, express_session: DriverSession
    ) -> None:
        chenggong = express_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        drive_to(express_session, chenggong.position_m + 200)

        progress = next(
            p for p in express_session.stations if p.station_id == "CHENGGONG"
        )
        assert not progress.missed
        assert all(
            "成功" not in v.description for v in express_session.incidents.violations
        )


class TestMissedStop:
    """應停未停（規格 §9.2、§23.1）。"""

    def test_missed_stop_is_recorded(self, local_session: DriverSession) -> None:
        chenggong = local_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        drive_to(local_session, chenggong.position_m + STOP_WINDOW_M + 100)
        local_session.announcer.flush()

        progress = next(p for p in local_session.stations if p.station_id == "CHENGGONG")
        assert progress.missed
        assert local_session.incidents.count_of("missed_stop") >= 1

    def test_missed_stop_is_announced(self, local_session: DriverSession) -> None:
        chenggong = local_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        drive_to(local_session, chenggong.position_m + STOP_WINDOW_M + 100)
        local_session.announcer.flush()
        assert any("應停未停" in t for t in local_session.announcer.texts())

    def test_missed_stop_does_not_reverse_train(
        self, local_session: DriverSession
    ) -> None:
        """不可自動倒車（規格 §9.2）。"""
        chenggong = local_session.route.stop_for_station("CHENGGONG")
        assert chenggong is not None
        drive_to(local_session, chenggong.position_m + STOP_WINDOW_M + 100)
        position_after_miss = local_session.train.position_m

        local_session.train.power_notch = 0
        for _ in range(200):
            local_session.tick(0.1)
        assert local_session.train.position_m >= position_after_miss

    def test_stopped_past_the_window_is_still_a_miss(
        self, game_data: GameData
    ) -> None:
        """列車停在停車範圍外側同樣屬於應停未停（規格 §9.2）。

        單一步長內列車可能同時越過停車範圍並停妥（例如緊急制軔），判定不可
        取決於列車剛好在越站前或越站後停下。
        """
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m + STOP_WINDOW_M + 1.0
        session.train.current_speed_kmh = 0.0
        session.tick(0.1)
        session.announcer.flush()

        progress = next(p for p in session.stations if p.station_id == "LILIN")
        assert progress.missed
        assert not progress.served
        assert session.incidents.count_of("missed_stop") == 1
        assert any("應停未停" in t for t in session.announcer.texts())

    def test_missed_stop_verdict_matches_whether_moving_or_stopped(
        self, game_data: GameData
    ) -> None:
        """同一位置的行駛中與停止列車必須得到相同判定。"""
        verdicts = []
        for speed in (0.0, 40.0):
            session = make_session(game_data, LOCAL_SERVICE)
            lilin = session.route.stop_for_station("LILIN")
            assert lilin is not None
            session.train.position_m = lilin.position_m + STOP_WINDOW_M + 1.0
            session.train.current_speed_kmh = speed
            session.tick(0.1)
            progress = next(p for p in session.stations if p.station_id == "LILIN")
            verdicts.append(progress.missed)
        assert verdicts == [True, True]

    def test_emergency_stop_just_past_the_window_is_a_miss(
        self, game_data: GameData
    ) -> None:
        """緊急制軔在越過停車範圍的同時停妥，仍須判定應停未停。"""
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m + STOP_WINDOW_M - 2.0
        session.train.current_speed_kmh = 12.0
        session.emergency_brake()
        for _ in range(300):
            session.tick(0.1)

        progress = next(p for p in session.stations if p.station_id == "LILIN")
        assert session.train.is_stopped
        assert session.train.position_m > lilin.position_m + STOP_WINDOW_M
        assert progress.missed
        assert not progress.served

    def test_stopping_within_window_is_not_a_miss(
        self, game_data: GameData
    ) -> None:
        """在停車範圍內停妥即為正常到站。"""
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m - 10.0
        session.train.current_speed_kmh = 0.0
        session.tick(0.1)

        progress = next(p for p in session.stations if p.station_id == "LILIN")
        assert progress.served
        assert not progress.missed
        assert session.incidents.count_of("missed_stop") == 0

    def test_arrival_reports_stop_offset(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m + 20.0
        session.tick(0.1)
        session.announcer.flush()

        progress = next(p for p in session.stations if p.station_id == "LILIN")
        assert progress.stop_offset_m == 20.0
        assert any("超出停車位置" in t for t in session.announcer.texts())
