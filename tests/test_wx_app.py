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
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession
from railway_sim.ui.console_app import ConsoleApp
from railway_sim.ui.wx_app import (
    _MAX_CATCH_UP_S,
    _UNBOUND_KEY_TEXT,
    DriverFrame,
    ServicePicker,
    StartChoice,
)
from tests.conftest import LOCAL_SERVICE, make_session

_SPECIAL_KEYS = {"F1": wx.WXK_F1, "F2": wx.WXK_F2, "ESC": wx.WXK_ESCAPE}


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


def press(frame: DriverFrame, key: str) -> None:
    """送出一次按鍵，並像計時器一樣把播報沖出來。"""
    event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
    event.SetEventObject(frame.frame)
    event.SetKeyCode(_SPECIAL_KEYS.get(key, ord(key) if len(key) == 1 else 0))
    frame._on_key(event)
    frame.announcer.flush()


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
        shown = frame.log_ctrl.GetValue().splitlines()
        assert shown[: len(frame.session.briefing_lines())] == frame.session.briefing_lines()

    def test_status_shows_the_vehicle_type(self, frame: DriverFrame) -> None:
        assert frame.session.spec.name_zh_tw in frame.status_ctrl.GetValue()


class TestDriving:
    """實際可操作：按鍵真的會改變列車狀態（§7.2、§8.2）。"""

    def test_power_key_adds_a_notch(self, frame: DriverFrame) -> None:
        press(frame, "Z")
        assert frame.session.train.power_notch == 1
        assert "電門一段。" in frame.log_ctrl.GetValue()

    def test_brake_key_adds_a_notch(self, frame: DriverFrame) -> None:
        """OpenBVE 的制軔鍵是句號，wx 必須把它正規化成 PERIOD。"""
        press(frame, ".")
        assert frame.session.train.brake_notch == 1
        assert "制軔一段。" in frame.log_ctrl.GetValue()

    def test_emergency_key_applies_emergency_brake(self, frame: DriverFrame) -> None:
        press(frame, "/")
        assert frame.session.train.emergency_brake is True

    def test_query_keys_announce(self, frame: DriverFrame) -> None:
        press(frame, "V")
        assert "目前速度" in frame.log_ctrl.GetValue()

    def test_unbound_key_still_gives_feedback(self, frame: DriverFrame) -> None:
        press(frame, "Q")
        assert frame.log_ctrl.GetValue().splitlines()[-1] == _UNBOUND_KEY_TEXT

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


class TestTextFields:
    """唯讀欄位的更新方式（螢幕閱讀器要能用方向鍵逐行閱讀）。"""

    def test_log_appends_instead_of_rewriting(self, frame: DriverFrame) -> None:
        before = frame.log_ctrl.GetValue()
        press(frame, "Z")
        after = frame.log_ctrl.GetValue()
        assert after.startswith(before)
        assert after.endswith("電門一段。")

    def test_log_is_trimmed_to_the_limit(self, frame: DriverFrame) -> None:
        from railway_sim.ui.wx_app import _LOG_LIMIT

        for index in range(_LOG_LIMIT + 20):
            frame._append_log(f"第{index}行")
        lines = frame.log_ctrl.GetValue().splitlines()
        assert len(lines) == _LOG_LIMIT
        assert lines[-1] == f"第{_LOG_LIMIT + 19}行"

    def test_status_is_not_rewritten_when_unchanged(self, frame: DriverFrame) -> None:
        frame._refresh_status()
        frame.status_ctrl.SetInsertionPoint(5)
        frame._refresh_status()
        assert frame.status_ctrl.GetInsertionPoint() == 5


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
        assert labels[:3] == ["繼續運轉", "快捷鍵說明", "列車狀態"]
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
            assert labels == ["繼續運轉", "快捷鍵說明", "列車狀態", "離開遊戲"]
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
