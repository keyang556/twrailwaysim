"""主控台介面的螢幕閱讀器輸出（規格 §20.1、§25.5）。

主控台是預設介面，因此「送給 NVDA 的東西」不能只有視窗版有。這一份守的是
兩件事：播報同時送到語音與點字，以及點字即時顯示在兩個介面都開得起來——
終端機收不到 Alt 組合鍵，主控台改由暫停選單提供，但走的是同一個動作代碼。

不需要 wxPython，因此在只裝 ``[dev]`` 的環境也跑得起來。
"""

from __future__ import annotations

from conftest import LOCAL_SERVICE, make_session

from railway_sim.accessibility.announcer import Announcer
from railway_sim.accessibility.speech import SpeechPriority
from railway_sim.data_loader import GameData
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession
from railway_sim.ui import console_app
from railway_sim.ui.console_app import ConsoleApp


class FakeScreenReader:
    """假的螢幕閱讀器：記錄送出去的語音與點字。"""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, object]] = []
        self.brailled: list[str] = []

    def __call__(self, text: str, interrupt: bool = False) -> bool:
        return self.speak(text, interrupt=interrupt)

    def speak(self, text: str, *, interrupt: bool = False, priority=None) -> bool:
        self.spoken.append((text, priority))
        return True

    def braille(self, text: str) -> bool:
        self.brailled.append(text)
        return True


class FakeSpeakSink:
    """只有語音、沒有點字的後端（舊的 speak sink 形狀）。"""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, bool]] = []

    def __call__(self, text: str, interrupt: bool = False) -> bool:
        self.spoken.append((text, interrupt))
        return True


def build_app(
    game_data: GameData, speak=None
) -> tuple[ConsoleApp, DriverSession, Announcer]:
    announcer = Announcer(dedupe_seconds=0.0)
    session = make_session(game_data, LOCAL_SERVICE, announcer)
    keymap = Keymap.from_dict(game_data.keymap_raw, "driver")
    return ConsoleApp(session, keymap, announcer, speak), session, announcer


class TestScreenReaderOutput:
    def test_announcements_go_to_speech_and_braille(self, game_data: GameData) -> None:
        reader = FakeScreenReader()
        app, session, announcer = build_app(game_data, reader)
        session.power_up()
        announcer.flush()

        assert "電門一段。" in [text for text, _ in reader.spoken]
        assert "電門一段。" in reader.brailled
        assert app.braille_monitor is False

    def test_priority_is_handed_to_the_screen_reader(self, game_data: GameData) -> None:
        """緊急訊息要插播，插播完 NVDA 會把被打斷的內容接回去。"""
        reader = FakeScreenReader()
        _, session, announcer = build_app(game_data, reader)
        session.emergency_brake()
        announcer.flush()

        assert dict(reader.spoken)["緊急制軔。"] == SpeechPriority.NOW

    def test_ordinary_feedback_does_not_interrupt(self, game_data: GameData) -> None:
        reader = FakeScreenReader()
        _, session, announcer = build_app(game_data, reader)
        session.power_up()
        announcer.flush()

        assert dict(reader.spoken)["電門一段。"] == SpeechPriority.NORMAL

    def test_plain_speak_sink_still_works(self, game_data: GameData) -> None:
        """只有語音的後端不該讓介面壞掉（§20.1：語音是加分，不是必要）。"""
        sink = FakeSpeakSink()
        app, session, announcer = build_app(game_data, sink)
        session.power_up()
        announcer.flush()

        assert app._braille is None
        assert ("電門一段。", False) in sink.spoken

    def test_no_backend_at_all_still_runs(self, game_data: GameData) -> None:
        app, session, announcer = build_app(game_data)
        session.power_up()
        announcer.flush()

        assert app._braille is None
        assert "電門一段。" in announcer.texts()


class TestBrailleMonitor:
    """點字即時顯示：兩個介面都要有（§25.5）。"""

    def test_action_is_registered_like_any_other(self, game_data: GameData) -> None:
        """終端機按不到 Alt＋Shift＋T，但動作本身仍註冊在同一個代碼上。"""
        app, _, _ = build_app(game_data, FakeScreenReader())
        assert "toggle_braille_monitor" in app.dispatcher.handlers
        assert app.dispatcher.unbound_actions() == []

    def test_toggle_turns_it_on_and_off(self, game_data: GameData) -> None:
        app, _, _ = build_app(game_data, FakeScreenReader())
        app.toggle_braille_monitor()
        assert app.braille_monitor is True
        app.toggle_braille_monitor()
        assert app.braille_monitor is False

    def test_monitor_sends_the_distance_to_the_next_station(
        self, game_data: GameData
    ) -> None:
        reader = FakeScreenReader()
        app, session, _ = build_app(game_data, reader)
        app.toggle_braille_monitor()
        app._braille_hold_until = 0.0
        app._braille_next_s = 0.0
        app._update_braille_monitor()

        assert reader.brailled[-1] == session.braille_line()

    def test_an_announcement_holds_the_display_for_a_moment(
        self, game_data: GameData
    ) -> None:
        """不留一段時間的話，摸讀的人來不及讀完就被距離蓋掉了。"""
        reader = FakeScreenReader()
        app, session, announcer = build_app(game_data, reader)
        app.toggle_braille_monitor()
        session.emergency_brake()
        announcer.flush()
        app._braille_next_s = 0.0
        app._update_braille_monitor()

        assert reader.brailled[-1] == "緊急制軔。"

    def test_without_nvda_it_says_why_instead_of_doing_nothing(
        self, game_data: GameData
    ) -> None:
        """每次按鍵都要有回饋（§7.2）；「沒有點字顯示器」不是故障。"""
        app, _, announcer = build_app(game_data)
        app.toggle_braille_monitor()

        assert app.braille_monitor is False
        assert "沒有連接 NVDA" in announcer.texts()[-1]

    def test_braille_monitor_rechecks_a_dynamic_reader_connection(
        self, game_data: GameData
    ) -> None:
        """已載入的 Controller Client 不能因 NVDA 晚啟動而被當成永久離線。"""
        reader = FakeScreenReader()
        reader.available = False
        app, _, announcer = build_app(game_data, reader)

        app.toggle_braille_monitor()
        assert app.braille_monitor is False
        assert "沒有連接 NVDA" in announcer.texts()[-1]

        reader.available = True
        app.toggle_braille_monitor()
        assert app.braille_monitor is True


class TestPauseMenu:
    """暫停選單多了點字那一項，離開遊戲跟著換號碼。"""

    def test_menu_lists_the_braille_option(
        self, game_data: GameData, monkeypatch, capsys
    ) -> None:
        """終端機按不到 Alt＋Shift＋T，選單是主控台唯一開得起來的路。"""
        app, _, _ = build_app(game_data, FakeScreenReader())
        monkeypatch.setattr(console_app, "read_key_nonblocking", lambda: "1")
        app.pause_menu()

        printed = capsys.readouterr().out
        assert "5：點字即時顯示（目前關閉）" in printed
        assert "6：離開遊戲" in printed

    def test_menu_item_toggles_the_monitor(
        self, game_data: GameData, monkeypatch, capsys
    ) -> None:
        app, _, _ = build_app(game_data, FakeScreenReader())
        keys = iter(["5", "1"])
        monkeypatch.setattr(console_app, "read_key_nonblocking", lambda: next(keys))
        app.pause_menu()

        assert app.braille_monitor is True

    def test_quitting_still_works_at_its_new_number(
        self, game_data: GameData, monkeypatch, capsys
    ) -> None:
        app, _, _ = build_app(game_data, FakeScreenReader())
        monkeypatch.setattr(console_app, "read_key_nonblocking", lambda: "6")
        app.pause_menu()

        assert app.running is False

    def test_status_menu_includes_the_stop_point_item(
        self, game_data: GameData
    ) -> None:
        """對準停車位置的查詢兩個介面都要查得到（§25.5）。"""
        app, session, _ = build_app(game_data, FakeScreenReader())
        codes = [item.code for item in session.status_items()]
        assert "stop_point" in codes
        assert app._keys_text_for("announce_stop_point")
