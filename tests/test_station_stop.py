"""停靠站、通過站與應停未停測試（規格 §9、§23.1、chat.md 成功站資料修正）。"""

from __future__ import annotations

import pytest
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


class TestStopAlignment:
    """對準停車位置的輔助（看不見月台標記時唯一的依據）。

    對位時司機手上沒有任何連續資訊，只有這幾條路：接近時自動由疏而密的
    倒數、隨時可按的查詢鍵、停妥後可再往前推的修正。三者都要能單獨用。
    """

    def _approach(self, game_data: GameData, from_m: float = 250.0) -> DriverSession:
        """把列車放在停車位置前 *from_m* 公尺，一路滑行到停車位置。"""
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m - from_m
        while session.train.position_m < target.position_m:
            remaining = target.position_m - session.train.position_m
            session.train.current_speed_kmh = max(2.0, min(60.0, remaining * 0.5))
            session.train.position_m += session.train.current_speed_kmh / 3.6 * 0.1
            session._handle_stations()
        session.announcer.flush()
        return session

    def test_countdown_says_what_it_counts_before_giving_bare_numbers(
        self, game_data: GameData
    ) -> None:
        spoken = self._approach(game_data).announcer.texts()
        countdown = [t for t in spoken if t.endswith("公尺。")]
        assert "停車位置" in countdown[0]
        assert countdown[1].rstrip("。").endswith("公尺")
        assert "停車位置" not in countdown[1]

    def test_countdown_gets_denser_as_the_train_closes_in(
        self, game_data: GameData
    ) -> None:
        """遠處一百公尺報一次就夠，最後十公尺內才是決定停得準不準的地方。"""
        spoken = self._approach(game_data).announcer.texts()
        far = sum(1 for t in spoken if "百公尺" in t)
        near = sum(
            1
            for t in spoken
            if t.rstrip("。") in ("十公尺", "七公尺", "五公尺", "三公尺", "二公尺", "一公尺")
        )
        assert near > far

    def test_reaching_the_mark_is_announced(self, game_data: GameData) -> None:
        spoken = self._approach(game_data).announcer.texts()
        assert "停車位置。" in spoken

    def test_countdown_reports_the_real_distance_not_the_threshold(
        self, game_data: GameData
    ) -> None:
        """一個步長內跨過好幾個門檻時只報一次，而且報的是當下的距離。"""
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        progress = next(p for p in session.stations if p.station_id == target.station_id)

        session.train.position_m = target.position_m - 120.0
        session._handle_stations()
        session.announcer.flush()
        countdown = [t for t in session.announcer.texts() if "停車位置" in t]
        assert len(countdown) == 1
        assert "一百二十公尺" in countdown[0]
        # 二百與一百五十兩個門檻同時跨過，但只開口一次。
        assert progress.countdown_index == 2

    def test_query_reports_distance_to_the_mark(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m - 17.0

        item = session.status_item("stop_point")
        assert item.label == "停車位置"
        assert "距離" in item.text
        assert "十七公尺" in item.text

    def test_query_says_past_the_mark_when_overshooting(
        self, game_data: GameData
    ) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m + 3.0
        assert "已超出" in session.status_item("stop_point").text

    def test_query_still_points_at_this_station_while_realigning(
        self, game_data: GameData
    ) -> None:
        """一停妥就跳到下一站的話，正想微調的人反而問不到自己在哪裡。"""
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None
        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)

        assert "栗林" in session.status_item("stop_point").text
        assert "四公尺" in session.status_item("stop_point").text

    def test_creeping_forward_after_stopping_updates_the_offset(
        self, game_data: GameData
    ) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None
        progress = next(p for p in session.stations if p.station_id == "LILIN")

        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)
        assert progress.stop_offset_m == -4.0

        session.train.position_m = lilin.position_m - 0.2
        session.tick(0.1)
        session.announcer.flush()
        assert progress.stop_offset_m == pytest.approx(-0.2)
        assert any("修正後" in t for t in session.announcer.texts())

    def test_realignment_does_not_change_the_stop_verdict(
        self, game_data: GameData
    ) -> None:
        """修正的是誤差記錄，不是判定：車站仍然是已服務，也不會多記違規。"""
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None
        progress = next(p for p in session.stations if p.station_id == "LILIN")

        session.train.position_m = lilin.position_m - 6.0
        session.tick(0.1)
        session.train.position_m = lilin.position_m
        session.tick(0.1)

        assert progress.served
        assert not progress.missed
        assert session.incidents.violation_count == 0

    def test_tiny_movements_do_not_re_announce(self, game_data: GameData) -> None:
        """滑行最後幾公分一直重播，反而蓋掉真正有用的那一句。"""
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)
        session.announcer.flush()
        session.announcer.clear_history()

        session.train.position_m += 0.1
        session.tick(0.1)
        session.announcer.flush()
        assert not any("修正後" in t for t in session.announcer.texts())

    def test_leaving_the_platform_ends_realignment(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)
        assert session._aligning_stop is not None

        session.train.position_m = lilin.position_m + STOP_WINDOW_M + 10.0
        session.tick(0.1)
        assert session._aligning_stop is None

    def test_departing_switches_the_target_before_leaving_the_window(
        self, game_data: GameData
    ) -> None:
        """一開始移動就該改看下一站，不必等走出五十公尺的停車範圍。

        `_aligning_stop` 仍要保留給 :meth:`_handle_realignment`，但查詢與
        點字不該繼續指向已經離開的車站——否則離站後這段距離裡問的都是
        剛剛停過的那一站，而不是接下來要停的站。
        """
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None
        progress = next(p for p in session.stations if p.station_id == "LILIN")

        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)
        assert session._aligning_stop is progress

        next_target = session.next_scheduled_stop()
        assert next_target is not None
        assert next_target.station_id != "LILIN"

        session.train.position_m = lilin.position_m + 0.5
        session.train.current_speed_kmh = 20.0
        session._handle_realignment()

        assert session.stop_alignment_target() is next_target
        assert "栗林" not in session.status_item("stop_point").text
        assert "栗林" not in session.braille_line()
        assert session._aligning_stop is progress

    def test_stopping_again_after_departing_does_not_revert_to_the_old_target(
        self, game_data: GameData
    ) -> None:
        """離站後若在停車範圍內因號誌或緊急制軔又停下，不該又跳回上一站。

        只看「目前是否靜止」在這裡會出錯：號誌或緊急制軔可能讓列車在還沒
        走出五十公尺的停車範圍前又停下來，那不是回到同一次對位，查詢與
        點字仍該看下一個預定停靠站，也不該再報一次「這一站修正後」。
        """
        session = make_session(game_data, LOCAL_SERVICE)
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)
        session.announcer.flush()
        session.announcer.clear_history()

        next_target = session.next_scheduled_stop()
        assert next_target is not None

        # 離站：短暫移動後又在範圍內停下（例如遇到號誌）。
        session.train.position_m = lilin.position_m + 0.5
        session.train.current_speed_kmh = 20.0
        session._handle_realignment()
        assert session.stop_alignment_target() is next_target

        session.train.current_speed_kmh = 0.0
        session._handle_realignment()
        session.announcer.flush()

        assert session.stop_alignment_target() is next_target
        assert "栗林" not in session.status_item("stop_point").text
        assert "栗林" not in session.braille_line()
        assert not any("修正後" in t for t in session.announcer.texts())


class TestBrailleLine:
    """點字即時顯示的內容（Alt＋Shift＋T）。

    點字顯示器一次只有二十到八十方，而且是用摸的——不能像螢幕那樣掃一眼就
    跳過不要的部分。因此這一行的每一個字都要有理由存在。
    """

    def test_far_away_it_shows_the_next_station(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m - 1500.0
        assert session.braille_line() == f"{target.name_zh_tw} 1.5km"

    def test_closing_in_it_switches_to_the_stop_mark(
        self, game_data: GameData
    ) -> None:
        """最後兩百公尺裡，車站中心的距離已經沒有意義了。"""
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m - 47.0
        assert session.braille_line() == f"{target.name_zh_tw} 停 47m"

    def test_last_metres_get_one_decimal(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m - 3.4
        assert session.braille_line() == f"{target.name_zh_tw} 停 3.4m"

    def test_overshooting_reads_differently(self, game_data: GameData) -> None:
        """不可倒車，「還差三公尺」與「過了三公尺」要一摸就分得出來。"""
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m + 3.0
        assert session.braille_line() == f"{target.name_zh_tw} 過3.0m"

    def test_end_of_route_says_so(self, game_data: GameData) -> None:
        session = make_session(game_data, LOCAL_SERVICE)
        session.train.position_m = session.route.length_m + 1000.0
        assert session.braille_line() == "路線終點"

    def test_numbers_stay_in_arabic_digits(self, game_data: GameData) -> None:
        """距離一直在變，長度浮動會讓摸讀的人每次都要重新找位置。"""
        session = make_session(game_data, LOCAL_SERVICE)
        target = session.next_scheduled_stop()
        assert target is not None
        session.train.position_m = target.position_m - 1200.0
        line = session.braille_line()
        assert "公尺" not in line and "公里" not in line
        assert line.endswith("km")
