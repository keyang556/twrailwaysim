"""wx 介面測試（規格 §5.1、§25.5）。

重點是「視窗版與主控台版一樣能玩」：同一組動作、同一份行前提要與狀態
文字、每次按鍵都有回饋，而且模擬時間依實際經過時間推進。

沒有安裝 wxPython（或沒有圖形環境）時整個檔案會被跳過，因此在只裝
``[dev]`` 的 CI 上不會失敗。
"""

from __future__ import annotations

import time

import pytest

wx = pytest.importorskip("wx", reason="需要 wxPython 才能測試視窗介面")

from railway_sim.accessibility.announcer import Announcer
from railway_sim.app import start_choices, start_session
from railway_sim.data_loader import GameData
from railway_sim.input.keymap import Keymap, display_key
from railway_sim.roles.driver import DriverSession
from railway_sim.ui.console_app import ConsoleApp
from railway_sim.ui.wx_app import (
    _MAX_CATCH_UP_S,
    _STATUS_HINT_TEXT,
    _UNBOUND_KEY_TEXT,
    DriverFrame,
    ServicePicker,
    StartChoice,
)
from tests.conftest import LOCAL_SERVICE, make_session

_SPECIAL_KEYS = {
    "F1": wx.WXK_F1,
    "F2": wx.WXK_F2,
    "F5": wx.WXK_F5,
    "F6": wx.WXK_F6,
    "F10": wx.WXK_F10,
    "ESC": wx.WXK_ESCAPE,
    "TAB": wx.WXK_TAB,
}


def log_lines(frame: DriverFrame) -> list[str]:
    """播報清單目前的每一則。"""
    return list(frame.log_ctrl.GetStrings())


def log_text(frame: DriverFrame) -> str:
    """播報清單的全部內容，方便用 ``in`` 檢查。"""
    return chr(10).join(log_lines(frame))


def status_text(frame: DriverFrame) -> str:
    """狀態欄目前顯示的那一句。"""
    return frame.status_ctrl.GetString(0) if frame.status_ctrl.GetCount() else ""


class FakeScreenReader:
    """假的螢幕閱讀器：記錄送出去的語音與點字。

    刻意做成與真的
    :class:`~railway_sim.accessibility.speech.ScreenReader` 一樣的形狀
    （可呼叫、有 ``speak``、有 ``braille``），介面層才是用能力探測而不是
    型別判斷在挑路徑。
    """

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


@pytest.fixture(scope="module")
def wx_app():
    """建立 wx 應用程式物件；沒有圖形環境時跳過整組測試。

    wxPython 在沒有顯示器時丟出的例外種類依平台而異（Windows 上是
    ``SystemExit``、GTK 上是 ``wx.PyNoAppError`` 或 ``SystemError``），
    因此這裡刻意攔全部：測不了就跳過，不是測試失敗。
    """
    try:
        app = wx.App(False)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - 見上方說明
        pytest.skip(f"無法建立 wx 應用程式：{exc}")
    yield app


@pytest.fixture
def keymap(game_data: GameData) -> Keymap:
    return Keymap.from_dict(game_data.keymap_raw, "driver")


@pytest.fixture
def frame(wx_app, game_data: GameData, keymap: Keymap):
    """一個未顯示、未啟動計時器的主視窗，由測試自行推進。"""
    announcer = Announcer(dedupe_seconds=0.0)
    session = make_session(game_data, LOCAL_SERVICE, announcer)
    built = DriverFrame(session, keymap, announcer, can_change_service=True)
    yield built
    built.frame.Destroy()


def press(frame: DriverFrame, key: str) -> bool:
    """送出一次按鍵，並像計時器一樣把播報沖出來。

    回傳事件有沒有被交還給控制項（``event.Skip()``）——巡覽鍵必須交還，
    未綁定的字母鍵則不能，否則清單會把它當成快速尋找而跳走。
    """
    event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
    event.SetEventObject(frame.frame)
    event.SetKeyCode(_SPECIAL_KEYS.get(key, ord(key) if len(key) == 1 else 0))
    event.Skip(False)
    frame._on_key(event)
    frame.announcer.flush()
    return event.GetSkipped()


class TestParityWithConsole:
    """兩個介面必須提供一樣的操作與一樣的文字（§25.5）。"""

    def test_same_actions_are_bound(
        self, frame: DriverFrame, game_data: GameData, keymap: Keymap
    ) -> None:
        console = ConsoleApp(
            make_session(game_data, LOCAL_SERVICE), keymap, Announcer()
        )
        assert set(frame.dispatcher.handlers) == set(console.dispatcher.handlers)

    def test_no_action_is_left_without_a_handler(self, frame: DriverFrame) -> None:
        assert frame.dispatcher.unbound_actions() == []

    def test_briefing_matches_the_session(self, frame: DriverFrame) -> None:
        """行前提要來自 DriverSession，不是介面自己組的字串。"""
        shown = log_lines(frame)
        assert shown[: len(frame.session.briefing_lines())] == frame.session.briefing_lines()

    def test_status_starts_empty_and_explains_how_to_query(
        self, frame: DriverFrame
    ) -> None:
        """狀態欄不再常駐顯示整份狀態，改為查詢後才出現（與 OpenBVE 相同）。"""
        assert status_text(frame) == _STATUS_HINT_TEXT

    def test_full_status_is_still_reachable(self, frame: DriverFrame) -> None:
        """完整狀態沒有被拿掉，只是移到選單裡，兩個介面都拿得到（§25.5）。"""
        assert frame.session.spec.name_zh_tw in frame.session.status_text()
        assert "status_item" in [code for code, _ in frame.pause_menu_actions()]


class TestDriving:
    """實際可操作：按鍵真的會改變列車狀態（§7.2、§8.2）。"""

    def test_power_key_adds_a_notch(self, frame: DriverFrame) -> None:
        press(frame, "Z")
        assert frame.session.train.power_notch == 1
        assert "電門一段。" in log_text(frame)

    def test_brake_key_adds_a_notch(self, frame: DriverFrame) -> None:
        """OpenBVE 的制軔鍵是句號，wx 必須把它正規化成 PERIOD。"""
        press(frame, ".")
        assert frame.session.train.brake_notch == 1
        assert "制軔一段。" in log_text(frame)

    def test_emergency_key_applies_emergency_brake(self, frame: DriverFrame) -> None:
        press(frame, "/")
        assert frame.session.train.emergency_brake is True

    def test_query_keys_announce(self, frame: DriverFrame) -> None:
        press(frame, "S")
        assert "目前速度" in log_text(frame)

    def test_query_key_shows_only_that_item(self, frame: DriverFrame) -> None:
        """按 S 之後狀態欄只顯示速度，顯示的與播報的是同一句。"""
        press(frame, "S")
        shown = status_text(frame)
        assert shown.startswith("速度：")
        assert frame.session.last_status is not None
        assert frame.session.last_status.text in shown
        assert "前方號誌" not in shown

    def test_query_key_replaces_the_previous_item(self, frame: DriverFrame) -> None:
        press(frame, "S")
        press(frame, "P")
        shown = status_text(frame)
        assert shown.startswith("位置：")
        assert "目前速度" not in shown

    def test_single_brake_key_follows_openbve(self, frame: DriverFrame) -> None:
        """Q：有電門先減電門，電門為零之後改為加制軔。"""
        press(frame, "Z")
        press(frame, "Z")
        press(frame, "Q")
        assert frame.session.train.power_notch == 1
        press(frame, "Q")
        assert frame.session.train.power_notch == 0
        press(frame, "Q")
        assert frame.session.train.brake_notch == 1

    def test_door_keys_open_and_close_each_side(self, frame: DriverFrame) -> None:
        """F5／F6 開關左右側車門，同一個鍵開也關（與 OpenBVE 相同）。"""
        press(frame, "F5")
        assert frame.session.train.left_doors_open is True
        assert frame.session.train.right_doors_open is False
        press(frame, "F6")
        assert frame.session.train.right_doors_open is True
        press(frame, "F5")
        assert frame.session.train.left_doors_open is False

    def test_unbound_key_still_gives_feedback(self, frame: DriverFrame) -> None:
        press(frame, "X")
        assert log_lines(frame)[-1] == _UNBOUND_KEY_TEXT

    def test_train_actually_moves(self, frame: DriverFrame) -> None:
        for _ in range(5):
            press(frame, "Z")
        for _ in range(600):
            frame.session.advance(0.1)
        assert frame.session.train.current_speed_kmh > 0.0
        assert frame.session.train.position_m > 0.0


class TestSimulationTiming:
    """模擬時間依實際經過時間推進，不是照計時器間隔猜。"""

    def test_timer_uses_real_elapsed_time(self, frame: DriverFrame) -> None:
        frame._resume()
        frame.timer.Stop()  # 由測試自己叫 _on_timer，不讓計時器插隊
        frame._last_tick_s = time.perf_counter() - 0.5
        frame._on_timer(None)
        assert frame.session.clock.elapsed_s == pytest.approx(0.5, abs=0.15)

    def test_long_stall_is_capped(self, frame: DriverFrame) -> None:
        """拖動視窗或機器休眠之後，不可以一口氣把列車補推出去。"""
        frame._resume()
        frame.timer.Stop()
        frame._last_tick_s = time.perf_counter() - 3600.0
        frame._on_timer(None)
        assert frame.session.clock.elapsed_s <= _MAX_CATCH_UP_S + 0.001

    def test_pause_does_not_accumulate_time(self, frame: DriverFrame) -> None:
        frame._resume()
        frame.timer.Stop()
        frame._pause()
        time.sleep(0.3)
        frame._resume()
        frame.timer.Stop()
        frame._on_timer(None)
        assert frame.session.clock.elapsed_s < 0.2

    def test_simulation_clock_matches_real_time(self, frame: DriverFrame) -> None:
        """運轉時間不可以走得比真實時間快（先前 advance 與 tick 各加一次）。"""
        frame.session.advance(10.0)
        assert frame.session.clock.elapsed_s == pytest.approx(10.0, abs=0.1)


class TestStatusMenu:
    """狀態查詢選單：不必先記住快捷鍵也查得到（§2.1）。"""

    def test_menu_lists_every_status_item(self, frame: DriverFrame) -> None:
        codes = list(frame._menu_status_codes.values())
        assert codes == [item.code for item in frame.session.status_items()]

    def test_menu_labels_show_the_key(self, frame: DriverFrame) -> None:
        """選單看得到每一項對應哪個鍵，鍵名取自鍵位表而不是寫死。"""
        keys = frame.keymap.keys_for("announce_speed")
        label = frame._status_menu_label("speed", "速度")
        assert all(display_key(key) in label for key in keys)

    def test_items_without_a_hotkey_show_no_key(self, frame: DriverFrame) -> None:
        assert frame._status_menu_label("summary", "運轉摘要") == "運轉摘要"

    def test_menu_and_hotkey_produce_the_same_result(
        self, frame: DriverFrame
    ) -> None:
        frame.query_status("speed")
        from_menu = status_text(frame)

        frame.session.last_status = None
        frame._status_text = ""
        press(frame, "S")
        assert status_text(frame).split("：", 1)[0] == from_menu.split("：", 1)[0]

    def test_menu_bar_is_attached(self, frame: DriverFrame) -> None:
        bar = frame.frame.GetMenuBar()
        assert bar is not None
        assert [bar.GetMenuLabelText(i) for i in range(bar.GetMenuCount())] == [
            "狀態查詢",
            "系統",
        ]


class TestDashboardControls:
    """儀表板是清單，不是編輯區。

    唯讀文字框會被螢幕閱讀器報成「編輯 唯讀 多行」，方向鍵讀的是游標所在
    的行或字元——而駕駛台上根本沒有東西可以編輯。清單報的是「清單」與目前
    這一項，上下鍵一次讀完整一則播報。
    """

    def test_dashboard_fields_are_lists_not_edit_areas(
        self, frame: DriverFrame
    ) -> None:
        assert isinstance(frame.log_ctrl, wx.ListBox)
        assert isinstance(frame.status_ctrl, wx.ListBox)
        assert not isinstance(frame.log_ctrl, wx.TextCtrl)
        assert not isinstance(frame.status_ctrl, wx.TextCtrl)

    def test_each_announcement_is_one_list_item(self, frame: DriverFrame) -> None:
        before = log_lines(frame)
        press(frame, "Z")
        assert log_lines(frame) == [*before, "電門一段。"]

    def test_new_announcements_do_not_move_the_selection(
        self, frame: DriverFrame
    ) -> None:
        """播報已經直接送給螢幕閱讀器了。再移動選取會讓同一句被唸兩次，
        也會把正在往回查看的人拉走。
        """
        frame.log_ctrl.SetSelection(1)
        press(frame, "Z")
        assert frame.log_ctrl.GetSelection() == 1

    def test_log_is_trimmed_to_the_limit(self, frame: DriverFrame) -> None:
        from railway_sim.ui.wx_app import _LOG_LIMIT

        for index in range(_LOG_LIMIT + 20):
            frame._append_log(f"第{index}行")
        lines = log_lines(frame)
        assert len(lines) == _LOG_LIMIT
        assert lines[-1] == f"第{_LOG_LIMIT + 19}行"
        assert frame.log_ctrl.GetCount() == _LOG_LIMIT

    def test_status_is_not_rewritten_when_unchanged(self, frame: DriverFrame) -> None:
        """內容沒變還重寫的話，螢幕閱讀器會一直重讀同一句。"""
        frame.query_status("speed")
        first = status_text(frame)
        frame._refresh_status()
        assert status_text(frame) == first
        assert frame.status_ctrl.GetCount() == 1

    def test_status_shows_only_the_latest_query(self, frame: DriverFrame) -> None:
        frame.query_status("speed")
        frame.query_status("position")
        assert frame.status_ctrl.GetCount() == 1
        assert status_text(frame).startswith("位置：")


class TestKeyboardNavigation:
    """巡覽鍵要能巡覽，未綁定的字母鍵不能把閱讀位置弄丟。"""

    def test_tab_is_left_to_the_window(self, frame: DriverFrame) -> None:
        """先前 Tab 會被當成「未設定功能的按鍵」，換一次焦點被唸一句廢話。"""
        before = len(log_lines(frame))
        assert press(frame, "TAB") is True
        assert len(log_lines(frame)) == before

    def test_menu_key_is_left_to_the_window(self, frame: DriverFrame) -> None:
        assert press(frame, "F10") is True

    def test_unbound_letters_are_not_passed_to_the_list(
        self, frame: DriverFrame
    ) -> None:
        """清單會把字母鍵當成快速尋找而跳到別的項目；按錯鍵不該失去閱讀位置。"""
        assert press(frame, "X") is False
        assert log_lines(frame)[-1] == _UNBOUND_KEY_TEXT


def build_frame(game_data: GameData, keymap: Keymap, reader=None):
    """建立一個接上假螢幕閱讀器的主視窗。"""
    announcer = Announcer(dedupe_seconds=0.0)
    session = make_session(game_data, LOCAL_SERVICE, announcer)
    reader = FakeScreenReader() if reader is None else reader
    return DriverFrame(session, keymap, announcer, reader), reader


class TestScreenReaderOutput:
    """播報直接送給 NVDA，不必等螢幕閱讀器自己發現畫面變了。"""

    def test_announcements_go_to_speech_and_braille(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        built, reader = build_frame(game_data, keymap)
        try:
            press(built, "Z")
            assert "電門一段。" in [text for text, _ in reader.spoken]
            assert "電門一段。" in reader.brailled
        finally:
            built.frame.Destroy()

    def test_priority_is_handed_to_the_screen_reader(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        """緊急訊息要插播，而不是排在一串電門段位確認後面。"""
        from railway_sim.accessibility.speech import SpeechPriority

        built, reader = build_frame(game_data, keymap)
        try:
            press(built, "/")  # 緊急制軔
            assert dict(reader.spoken)["緊急制軔。"] == SpeechPriority.NOW
        finally:
            built.frame.Destroy()

    def test_plain_speak_sink_still_works(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        """只有語音、沒有點字的後端不該讓介面壞掉。"""
        built, reader = build_frame(game_data, keymap, FakeSpeakSink())
        try:
            press(built, "Z")
            assert built._braille is None
            assert ("電門一段。", False) in reader.spoken
        finally:
            built.frame.Destroy()


class TestBrailleMonitor:
    """Alt＋Shift＋T：點字顯示器上持續顯示距離下一站還有多遠。"""

    def test_toggle_turns_it_on_and_off(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        built, _ = build_frame(game_data, keymap)
        try:
            assert built.braille_monitor is False
            built.toggle_braille_monitor()
            assert built.braille_monitor is True
            built.toggle_braille_monitor()
            assert built.braille_monitor is False
        finally:
            built.braille_timer.Stop()
            built.frame.Destroy()

    def test_monitor_shows_the_distance_to_the_next_station(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        built, reader = build_frame(game_data, keymap)
        try:
            built.toggle_braille_monitor()
            built._braille_hold_until = 0.0
            built._update_braille_monitor()
            assert built._braille_text == built.session.braille_line()
            assert reader.brailled[-1] == built._braille_text
        finally:
            built.braille_timer.Stop()
            built.frame.Destroy()

    def test_an_announcement_holds_the_display_for_a_moment(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        """不留一段時間的話，緊急制軔會在不到一秒內被距離蓋掉。"""
        built, reader = build_frame(game_data, keymap)
        try:
            built.toggle_braille_monitor()
            press(built, "/")
            built._update_braille_monitor()
            assert reader.brailled[-1] == "緊急制軔。"
        finally:
            built.braille_timer.Stop()
            built.frame.Destroy()

    def test_without_nvda_it_says_why_instead_of_doing_nothing(
        self, frame: DriverFrame
    ) -> None:
        """每次按鍵都要有回饋（§7.2）；「沒有點字顯示器」不是故障。"""
        frame.toggle_braille_monitor()
        frame.announcer.flush()
        assert frame.braille_monitor is False
        assert "沒有連接 NVDA" in log_lines(frame)[-1]

    def test_disconnect_does_not_prevent_turning_the_monitor_off(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        """已開啟後斷線時，仍可停止計時器並取消稍後恢復的重送。"""
        built, reader = build_frame(game_data, keymap)
        try:
            built.toggle_braille_monitor()
            built._braille_text = "下一站 板橋"
            assert built.braille_monitor is True
            assert built.braille_timer.IsRunning()

            reader.available = False
            built.toggle_braille_monitor()

            assert built.braille_monitor is False
            assert not built.braille_timer.IsRunning()
            assert built._braille_text == ""
        finally:
            built.braille_timer.Stop()
            built.frame.Destroy()

    def test_pausing_stops_overwriting_the_display(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        """說明視窗是拿來讀的，每 0.7 秒蓋成距離就讀不到了。"""
        built, _ = build_frame(game_data, keymap)
        try:
            built.toggle_braille_monitor()
            built._pause()
            assert not built.braille_timer.IsRunning()
            built._resume()
            assert built.braille_timer.IsRunning()
        finally:
            built.braille_timer.Stop()
            built.frame.Destroy()


class TestServicePicker:
    """開場車次選擇：視窗版沒有命令列，選車次只能靠它。"""

    def test_choices_cover_scenarios_and_every_service(
        self, game_data: GameData
    ) -> None:
        choices = start_choices(game_data)
        keys = {c.key for c in choices}
        assert "scenario:red_signal" in keys
        assert f"service:{LOCAL_SERVICE}" in keys
        assert len(choices) == len(game_data.services) + 5

    def test_keyword_filters_by_label(self, wx_app, game_data: GameData) -> None:
        picker = ServicePicker(start_choices(game_data))
        try:
            picker.search.SetValue("太魯閣")
            picker._refresh()
            assert picker.visible
            assert all("TEMU1000" in c.label for c in picker.visible)
        finally:
            picker.dialog.Destroy()

    def test_keyword_filters_by_station_along_the_way(
        self, wx_app, game_data: GameData
    ) -> None:
        """集集站不是任何一班車的起訖站，只出現在沿途站名裡。"""
        picker = ServicePicker(start_choices(game_data))
        try:
            picker.search.SetValue("集集")
            picker._refresh()
            assert picker.visible
            assert all("DR1000" in c.label for c in picker.visible)
        finally:
            picker.dialog.Destroy()

    def test_unknown_keyword_gives_an_empty_list(
        self, wx_app, game_data: GameData
    ) -> None:
        picker = ServicePicker(start_choices(game_data))
        try:
            picker.search.SetValue("這個字串不會出現")
            picker._refresh()
            assert picker.visible == []
        finally:
            picker.dialog.Destroy()

    def test_start_choice_matching(self) -> None:
        choice = StartChoice(key="k", label="標籤", detail="說明", search_text="沿途")
        assert choice.matches("")
        assert choice.matches("標籤")
        assert choice.matches("說明")
        assert choice.matches("沿途")
        assert not choice.matches("其他")


class TestChangeService:
    """遊戲中換車次：``run_wx`` 關掉舊視窗後再建一個新的。

    這裡不真的跑 ``MainLoop``：在 pytest 裡起巢狀訊息迴圈會讓 wxWidgets 印出
    一大段 COM 警告，測到的東西卻只是 wx 自己的事件迴圈。有價值的是「換車次
    之後每一段都是獨立的工作階段」，那部分不需要事件迴圈就能驗。
    """

    def test_each_service_gets_a_fresh_session(
        self, wx_app, game_data: GameData, keymap: Keymap
    ) -> None:
        frames = []
        try:
            for train_number in ("1801", "2705"):
                session, announcer = start_session(game_data, f"service:{train_number}")
                built = DriverFrame(session, keymap, announcer, can_change_service=True)
                frames.append(built)
                session.advance(5.0)

            first, second = (f.session for f in frames)
            assert first is not second
            assert first.spec.id == "DR1000"
            assert second.spec.id == "DR1000"
            assert first.route.id != second.route.id
            # 第二段不會把第一段的運轉時間帶過來。
            assert second.clock.elapsed_s == pytest.approx(5.0, abs=0.1)
        finally:
            for built in frames:
                built.frame.Destroy()

    def test_closing_records_whether_to_reopen_the_picker(
        self, frame: DriverFrame
    ) -> None:
        """``run_wx`` 靠這個旗標決定要回到選擇視窗還是結束。"""
        assert frame.change_service_requested is False

    def test_pause_menu_matches_the_console_menu(self, frame: DriverFrame) -> None:
        """前四項與主控台暫停選單的 1～4 相同。"""
        labels = [label for _, label in frame.pause_menu_actions()]
        assert labels[:4] == [
            "繼續運轉",
            "快捷鍵說明",
            "列車狀態",
            "狀態查詢（單一項目）",
        ]
        assert labels[-1] == "離開遊戲"
        assert "選擇其他車次" in labels

    def test_pause_menu_hides_change_service_when_not_allowed(
        self, game_data: GameData, keymap: Keymap, wx_app
    ) -> None:
        announcer = Announcer(dedupe_seconds=0.0)
        session = make_session(game_data, LOCAL_SERVICE, announcer)
        built = DriverFrame(session, keymap, announcer, can_change_service=False)
        try:
            labels = [label for _, label in built.pause_menu_actions()]
            assert labels == [
                "繼續運轉",
                "快捷鍵說明",
                "列車狀態",
                "狀態查詢（單一項目）",
                "離開遊戲",
            ]
        finally:
            built.frame.Destroy()


class TestStartSession:
    """``start_session`` 讓視窗介面可以在遊戲中換車次。"""

    def test_builds_from_a_service_key(self, game_data: GameData) -> None:
        session, announcer = start_session(game_data, f"service:{LOCAL_SERVICE}")
        assert isinstance(session, DriverSession)
        assert session.service.train_number == LOCAL_SERVICE
        assert announcer is session.announcer

    def test_builds_from_a_scenario_key(self, game_data: GameData) -> None:
        session, _ = start_session(game_data, "scenario:red_signal")
        # red_signal 情境會在前方放一列故障車占用區間（§12.3）。
        assert [i.kind for i in session.incidents.incidents] == ["train_failure"]

    def test_unknown_service_raises(self, game_data: GameData) -> None:
        with pytest.raises(KeyError):
            start_session(game_data, "service:沒有這班車")


class TestServicePickerAnnouncements:
    """選擇視窗：補充說明看得到，也要聽得到。

    螢幕閱讀器在清單裡上下移動時只會唸出項目本身。下方那段說明（路線、
    停靠幾站）不 Tab 過去就聽不到，選十幾個車次要 Tab 十幾次。
    """

    def test_selection_speaks_the_detail(self, wx_app, game_data: GameData) -> None:
        reader = FakeScreenReader()
        picker = ServicePicker(start_choices(game_data), speak=reader)
        try:
            picker.listbox.SetSelection(0)
            picker._on_select(None)
            assert reader.spoken
            assert reader.spoken[-1][0] == picker.visible[0].detail
        finally:
            picker.dialog.Destroy()

    def test_search_says_how_many_matched(self, wx_app, game_data: GameData) -> None:
        """邊打字邊知道還剩幾個，不必切到清單自己數。"""
        reader = FakeScreenReader()
        picker = ServicePicker(start_choices(game_data), speak=reader)
        try:
            picker.search.SetValue("太魯閣")
            picker._on_search(None)
            assert f"符合 {len(picker.visible)} 個" in reader.spoken[-1][0]
        finally:
            picker.dialog.Destroy()

    def test_no_match_is_said_out_loud(self, wx_app, game_data: GameData) -> None:
        reader = FakeScreenReader()
        picker = ServicePicker(start_choices(game_data), speak=reader)
        try:
            picker.search.SetValue("這個字串不會出現")
            picker._on_search(None)
            assert "沒有符合" in reader.spoken[-1][0]
        finally:
            picker.dialog.Destroy()

    def test_works_without_a_speech_backend(self, wx_app, game_data: GameData) -> None:
        """沒有 NVDA 時一切照舊，說明仍然顯示得出來（§20.1）。"""
        picker = ServicePicker(start_choices(game_data))
        try:
            picker.listbox.SetSelection(0)
            picker._on_select(None)
            assert picker.detail.GetLabel() == picker.visible[0].detail
        finally:
            picker.dialog.Destroy()
