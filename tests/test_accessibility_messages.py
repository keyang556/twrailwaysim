"""無障礙訊息輸出測試（規格 §2.1、§7.2、§21、§23.1）。"""

from __future__ import annotations

import pytest
from conftest import ManualClock

from railway_sim.accessibility import messages as msg
from railway_sim.accessibility.announcer import Announcer, Priority


class TestChineseNumbers:
    """數字唸法必須符合規格 §21.2 的範例。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "零"),
            (3, "三"),
            (10, "十"),
            (15, "十五"),
            (20, "二十"),
            (60, "六十"),
            (82, "八十二"),
            (100, "一百"),
            (105, "一百零五"),
            (110, "一百一十"),
            (800, "八百"),
            (1200, "一千二百"),
            (10005, "一萬零五"),
        ],
    )
    def test_integers(self, value: int, expected: str) -> None:
        assert msg.num_to_zh(value) == expected

    def test_negative(self) -> None:
        assert msg.num_to_zh(-5) == "負五"

    @pytest.mark.parametrize(
        ("value", "expected"), [(1.2, "一點二"), (0.5, "零點五"), (12.0, "十二")]
    )
    def test_decimals(self, value: float, expected: str) -> None:
        assert msg.num_to_zh(value, decimals=1) == expected

    def test_distance_uses_metres_below_one_kilometre(self) -> None:
        assert msg.distance_phrase(800) == "八百公尺"
        assert msg.distance_phrase(804) == "八百公尺"

    def test_distance_uses_kilometres_above_one_kilometre(self) -> None:
        assert msg.distance_phrase(1200) == "一點二公里"

    def test_distance_never_negative(self) -> None:
        assert msg.distance_phrase(-50) == "零公尺"


class TestSpecExampleMessages:
    """規格 §21.2 明列的訊息範例。"""

    def test_power_notch(self) -> None:
        assert msg.power_notch(3) == "電門三段。"

    def test_power_notch_zero(self) -> None:
        assert msg.power_notch(0) == "電門切斷。"

    def test_speed_report(self) -> None:
        assert msg.speed_report(82).startswith("目前速度八十二公里。")

    def test_next_station(self) -> None:
        text = msg.next_station("成功", 1200, "pass")
        assert "前方車站成功，距離一點二公里。" in text
        assert "成功站為通過站。" in text

    def test_next_station_stop_kind(self) -> None:
        assert "成功站為停靠站。" in msg.next_station("成功", 1200, "stop")

    def test_signal_report(self) -> None:
        """規格 §11.3 的號誌播報格式。"""
        text = msg.signal_report("caution", 800, 60)
        assert text == "前方號誌：注意。距離：八百公尺。目前允許速度：六十公里。"

    def test_overspeed(self) -> None:
        assert msg.overspeed(65, 60) == "超速五公里。"

    def test_emergency_brake(self) -> None:
        assert msg.emergency_brake_applied() == "緊急制軔。"


class TestOperationalMessages:
    """狀態變化都必須有文字（規格 §2.1）。"""

    def test_brake_notch(self) -> None:
        assert msg.brake_notch(4) == "制軔四段。"
        assert msg.brake_notch(0) == "制軔緩解。"

    def test_missed_stop_mentions_no_reversing(self) -> None:
        """應停未停必須說明不可倒車（規格 §9.2）。"""
        text = msg.missed_stop("成功")
        assert "應停未停" in text
        assert "不可倒車" in text

    def test_station_arrival_reports_offset(self) -> None:
        assert "停車位置準確" in msg.station_arrival("成功", 0.5)
        assert "超出停車位置" in msg.station_arrival("成功", 20.0)
        assert "未達停車位置" in msg.station_arrival("成功", -20.0)

    def test_station_arrival_offset_is_metre_precise(self) -> None:
        """小誤差不可被整十化簡成「零公尺」。"""
        assert msg.station_arrival("成功", -4.0) == "成功站停妥，未達停車位置四公尺。"
        assert msg.station_arrival("成功", 7.0) == "成功站停妥，超出停車位置七公尺。"

    def test_stop_point_warning_does_not_say_zero_speed_limit(self) -> None:
        """停車點的提醒不可播成「前方速限零公里」。"""
        text = msg.approaching_stop_point("大慶站停車位置", 710)
        assert "速限" not in text
        assert "大慶站停車位置" in text
        assert "七百一十公尺" in text

    def test_speed_limit_warning_still_uses_speed_wording(self) -> None:
        text = msg.approaching_speed_limit(60, 800)
        assert "前方速限六十公里" in text

    def test_train_status_includes_direction_and_speed(self) -> None:
        text = msg.train_status("2701", "區間車", 80, 3, 0, False, "southbound")
        assert "區間車2701次" in text
        assert "南下" in text
        assert "八十公里" in text
        assert "電門三段" in text

    def test_train_status_reports_emergency(self) -> None:
        text = msg.train_status("2701", "區間車", 0, 0, 7, True, "southbound")
        assert "緊急制軔中" in text

    def test_position_report(self) -> None:
        text = msg.position_report("山線", "臺中", "大慶", 1200)
        assert "臺中至大慶間" in text
        assert "距離大慶一點二公里" in text


class TestAnnouncerPriority:
    """優先級與中斷（規格 §21.1）。"""

    def test_high_priority_interrupts_pending_low_priority(self) -> None:
        announcer = Announcer(clock=ManualClock())
        announcer.announce("一般狀態", Priority.STATUS)
        announcer.announce("操作確認", Priority.ACTION)
        announcer.announce("緊急制軔。", Priority.EMERGENCY)

        assert [a.text for a in announcer.pending] == ["緊急制軔。"]

    def test_safety_messages_are_kept_together(self) -> None:
        announcer = Announcer(clock=ManualClock())
        announcer.announce("超速五公里。", Priority.SAFETY)
        announcer.announce("緊急制軔。", Priority.EMERGENCY)
        assert len(announcer.pending) == 2

    def test_low_priority_does_not_interrupt(self) -> None:
        announcer = Announcer(clock=ManualClock())
        announcer.announce("緊急制軔。", Priority.EMERGENCY)
        announcer.announce("電門一段。", Priority.ACTION)
        assert len(announcer.pending) == 2


class TestAnnouncerThrottling:
    """連續按鍵不得造成語音塞車（規格 §7.2）。"""

    def test_same_dedupe_key_suppressed_within_cooldown(self) -> None:
        clock = ManualClock()
        announcer = Announcer(clock=clock, dedupe_seconds=3.0)

        assert announcer.announce("超速五公里。", Priority.SAFETY, dedupe_key="overspeed")
        assert not announcer.announce(
            "超速六公里。", Priority.SAFETY, dedupe_key="overspeed"
        )

        clock.advance(3.5)
        assert announcer.announce("超速七公里。", Priority.SAFETY, dedupe_key="overspeed")

    def test_messages_without_dedupe_key_always_pass(self) -> None:
        announcer = Announcer(clock=ManualClock(), dedupe_seconds=10.0)
        assert announcer.announce("電門一段。", Priority.ACTION)
        assert announcer.announce("電門一段。", Priority.ACTION)

    def test_empty_message_rejected(self) -> None:
        announcer = Announcer(clock=ManualClock())
        assert not announcer.announce("")


class TestAnnouncerRepeat:
    """重要訊息需支援重複播報（規格 §2.1）。"""

    def test_repeat_last_resends_to_sink(self) -> None:
        spoken: list[str] = []
        announcer = Announcer(clock=ManualClock(), sink=lambda a: spoken.append(a.text))
        announcer.announce("前方號誌：注意。", Priority.NOTICE)
        announcer.flush()
        assert spoken == ["前方號誌：注意。"]

        announcer.repeat_last()
        assert spoken == ["前方號誌：注意。", "前方號誌：注意。"]

    def test_repeat_last_with_no_history(self) -> None:
        announcer = Announcer(clock=ManualClock())
        assert announcer.repeat_last() == []

    def test_repeat_multiple(self) -> None:
        announcer = Announcer(clock=ManualClock())
        announcer.announce("第一則", Priority.STATUS)
        announcer.announce("第二則", Priority.STATUS)
        announcer.flush()
        assert [a.text for a in announcer.repeat_last(2)] == ["第一則", "第二則"]


class TestAnnouncerFlush:
    def test_flush_moves_pending_to_history(self) -> None:
        announcer = Announcer(clock=ManualClock())
        announcer.announce("電門一段。", Priority.ACTION)
        batch = announcer.flush()

        assert [a.text for a in batch] == ["電門一段。"]
        assert announcer.pending == ()
        assert announcer.texts() == ["電門一段。"]

    def test_history_is_trimmed(self) -> None:
        announcer = Announcer(clock=ManualClock(), history_limit=3)
        for index in range(10):
            announcer.announce(f"訊息{index}", Priority.STATUS)
            announcer.flush()
        assert len(announcer.history) == 3
