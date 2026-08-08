"""wxPython 介面（規格 §5.1）。

版面刻意極簡：兩個唯讀多行文字欄位，一個放運轉播報，一個放列車狀態。
理由是螢幕閱讀器可以直接用方向鍵逐行閱讀文字欄位內容，不需要任何自訂
繪圖或視覺元素；所有資訊也同時存在於文字中（§25.5）。

鍵盤事件以 ``EVT_CHAR_HOOK`` 在視窗層級攔截，因此焦點在哪個欄位都能操作，
焦點移動可預測（§2.1）。

語音方面：若系統可用 NVDA Controller Client，會直接送出語音；否則仍以
文字呈現，遊戲功能不受影響（見 :mod:`railway_sim.accessibility.speech`）。

與主控台介面的關係
------------------

兩個介面必須一樣能玩，差別只在輸出方式。因此：

- 行前提要、列車狀態、播報文字全部來自 :class:`~railway_sim.roles.driver.DriverSession`，
  沒有任何一邊自己組字串。
- 未綁定的按鍵一樣有文字回饋（§7.2）。
- 暫停選單的項目與主控台相同，並且多一個「選擇其他車次」——主控台可以用
  ``--service`` 指定車次，視窗版沒有命令列，因此改由開場的車次選擇視窗與
  暫停選單提供同樣的能力。
- 模擬時間依**實際經過時間**推進。``wx.Timer`` 不保證準時，若直接把計時器
  間隔當成經過時間，列車會跑得比真實時間慢。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from railway_sim.accessibility.announcer import Announcement, Announcer, Priority
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession

__all__ = ["DriverFrame", "ServicePicker", "StartChoice", "run_wx"]

#: 計時器間隔（毫秒）。實際推進量仍以 ``time.perf_counter()`` 為準。
_TIMER_MS = 50

#: 單次計時器最多補推的模擬秒數。
#:
#: 拖動視窗、系統忙碌或機器休眠時，計時器可能停很久。若照實補推，列車會
#: 在一個畫面更新內衝過好幾個車站，等於玩家什麼都沒做就被判應停未停。
_MAX_CATCH_UP_S = 1.0

#: 播報欄位保留的行數。
_LOG_LIMIT = 300

#: 未綁定按鍵的回饋（與主控台相同，§7.2）。
_UNBOUND_KEY_TEXT = "此按鍵未設定功能，按 F1 查看快捷鍵說明。"


@dataclass(frozen=True)
class StartChoice:
    """開場車次選擇視窗的一個選項。

    Attributes:
        key: 交給 ``make_session`` 的識別字串。本模組不解讀它的內容，
            因此 wx 介面不需要知道「情境」與「車次」的差別。
        label: 清單中顯示的一行文字。
        detail: 選取後顯示的補充說明。
        search_text: 額外的搜尋比對字串（例如沿途站名），不會顯示出來。
    """

    key: str
    label: str
    detail: str = ""
    search_text: str = ""

    def matches(self, keyword: str) -> bool:
        if not keyword:
            return True
        return keyword in self.label or keyword in self.detail or keyword in self.search_text


def _keycode_to_token(event) -> str | None:
    """把 wx 鍵盤事件轉成鍵位表使用的按鍵代碼。

    與主控台不同，wx 收得到真正的修飾鍵狀態，因此 ``Ctrl+Shift+S`` 這類
    與 OpenBVE 相同的鍵位在這裡是完整可用的。
    """
    import wx

    code = event.GetKeyCode()
    special = {
        wx.WXK_SPACE: "SPACE",
        wx.WXK_ESCAPE: "ESC",
        wx.WXK_RETURN: "ENTER",
        wx.WXK_NUMPAD_ENTER: "ENTER",
        wx.WXK_TAB: "TAB",
        wx.WXK_BACK: "BACKSPACE",
        wx.WXK_F1: "F1",
        wx.WXK_F2: "F2",
    }
    if code in special:
        base = special[code]
    elif 33 <= code <= 126:
        base = chr(code).upper()
    else:
        return None

    modifiers = []
    if event.ControlDown():
        modifiers.append("CTRL")
    if event.ShiftDown():
        modifiers.append("SHIFT")
    if event.AltDown():
        modifiers.append("ALT")
    if not modifiers:
        return base
    return "+".join([*modifiers, base])


class ServicePicker:  # pragma: no cover - 需要圖形環境
    """開場的車次選擇視窗。

    視窗版沒有命令列可以下 ``--service``，若不提供選擇畫面，安裝版就永遠
    只能開同一個預設車次。清單支援關鍵字過濾，只用鍵盤即可完成選擇。
    """

    def __init__(self, choices: Sequence[StartChoice], parent=None) -> None:
        import wx

        self.wx = wx
        self.choices = list(choices)
        self.visible: list[StartChoice] = list(choices)

        self.dialog = wx.Dialog(
            parent,
            title="選擇車次",
            size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        search_label = wx.StaticText(
            self.dialog, label="搜尋（車次、車種、車輛型式、起訖站、沿途車站）"
        )
        self.search = wx.TextCtrl(self.dialog)
        self.search.SetName("搜尋")

        list_label = wx.StaticText(self.dialog, label="可駕駛車次（上下鍵選擇，Enter 開始）")
        self.listbox = wx.ListBox(self.dialog, style=wx.LB_SINGLE)
        self.listbox.SetName("可駕駛車次")

        self.detail = wx.StaticText(self.dialog, label="")

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self.dialog, wx.ID_OK, "開始運轉")
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self.dialog, wx.ID_CANCEL, "離開"))
        buttons.Realize()

        sizer.Add(search_label, 0, wx.ALL, 6)
        sizer.Add(self.search, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(list_label, 0, wx.ALL, 6)
        sizer.Add(self.listbox, 1, wx.EXPAND | wx.ALL, 6)
        sizer.Add(self.detail, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.ALL, 8)
        self.dialog.SetSizer(sizer)

        self.search.Bind(wx.EVT_TEXT, self._on_search)
        self.listbox.Bind(wx.EVT_LISTBOX, self._on_select)
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self.dialog.EndModal(wx.ID_OK))

        self._refresh()
        self.search.SetFocus()

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        keyword = self.search.GetValue().strip()
        self.visible = [c for c in self.choices if c.matches(keyword)]
        self.listbox.Set([c.label for c in self.visible])
        if self.visible:
            self.listbox.SetSelection(0)
        self._on_select(None)

    def _on_search(self, _event) -> None:
        self._refresh()

    def _on_select(self, _event) -> None:
        index = self.listbox.GetSelection()
        if 0 <= index < len(self.visible):
            self.detail.SetLabel(self.visible[index].detail)
        else:
            self.detail.SetLabel(f"沒有符合的車次（共 {len(self.choices)} 個）")

    # ------------------------------------------------------------------
    def ask(self) -> str | None:
        """顯示視窗，回傳選到的 ``key``；取消時回傳 ``None``。"""
        try:
            if self.dialog.ShowModal() != self.wx.ID_OK:
                return None
            index = self.listbox.GetSelection()
            if not (0 <= index < len(self.visible)):
                return None
            return self.visible[index].key
        finally:
            self.dialog.Destroy()


class DriverFrame:  # pragma: no cover - 需要圖形環境
    """司機員模式主視窗。

    以組合而非繼承包裝 ``wx.Frame``，讓本模組在沒有 wxPython 時仍可被匯入。
    """

    def __init__(
        self,
        session: DriverSession,
        keymap: Keymap,
        announcer: Announcer,
        speak: Callable[[str, bool], bool] | None = None,
        *,
        can_change_service: bool = False,
    ) -> None:
        import wx

        self.wx = wx
        self.session = session
        self.keymap = keymap
        self.announcer = announcer
        self.speak = speak
        self.can_change_service = can_change_service
        self._log_lines: list[str] = []
        self._status_text = ""
        self._last_tick_s = time.perf_counter()
        self._running = False

        #: 關窗之後由 :func:`run_wx` 讀取：是否要回到車次選擇視窗。
        self.change_service_requested = False

        service = session.service
        class_name = session.data.service_class_name(service.train_type)
        title = f"臺灣鐵路人員模擬器 — 司機員模式 — {class_name}{service.train_number}次"

        self.frame = wx.Frame(None, title=title, size=(760, 620))
        panel = wx.Panel(self.frame)
        sizer = wx.BoxSizer(wx.VERTICAL)

        log_label = wx.StaticText(panel, label="運轉播報（唯讀，可用方向鍵閱讀）")
        self.log_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.log_ctrl.SetName("運轉播報")

        status_label = wx.StaticText(panel, label="列車狀態（唯讀）")
        self.status_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.status_ctrl.SetName("列車狀態")

        hint = wx.StaticText(panel, label="F1：快捷鍵說明　F2：重複播報　Esc：暫停選單")

        sizer.Add(log_label, 0, wx.ALL, 6)
        sizer.Add(self.log_ctrl, 3, wx.EXPAND | wx.ALL, 6)
        sizer.Add(status_label, 0, wx.ALL, 6)
        sizer.Add(self.status_ctrl, 2, wx.EXPAND | wx.ALL, 6)
        sizer.Add(hint, 0, wx.ALL, 6)
        panel.SetSizer(sizer)

        self.announcer.sink = self._emit

        self.dispatcher = KeyDispatcher(keymap)
        self.dispatcher.register_all(session.action_handlers())  # type: ignore[arg-type]
        self.dispatcher.register_all(
            {
                "show_help": self.show_help,
                "repeat_last": self.repeat_last,
                "pause_menu": self.pause_menu,
            }
        )

        self.frame.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.frame.Bind(wx.EVT_CLOSE, self._on_close)

        self.timer = wx.Timer(self.frame)
        self.frame.Bind(wx.EVT_TIMER, self._on_timer, self.timer)

        self._announce_intro()
        self._refresh_status()
        self.log_ctrl.SetFocus()

    # ------------------------------------------------------------------
    def show(self) -> None:
        self.frame.Show()
        self._resume()

    # ------------------------------------------------------------------
    # 模擬推進
    # ------------------------------------------------------------------
    def _pause(self) -> None:
        """停止推進模擬。開啟強制回應視窗前必須呼叫。"""
        self.timer.Stop()
        self._running = False

    def _resume(self) -> None:
        """恢復推進模擬。

        一併重設計時基準點，否則暫停期間流逝的真實時間會在恢復的第一步
        全部補推進去，列車會突然向前跳一大段。
        """
        self._last_tick_s = time.perf_counter()
        self._running = True
        self.timer.Start(_TIMER_MS)

    # ------------------------------------------------------------------
    def _emit(self, announcement: Announcement) -> None:
        self._append_log(announcement.text)
        if self.speak is not None:
            self.speak(announcement.text, announcement.priority >= Priority.SAFETY)

    def _append_log(self, text: str) -> None:
        """把一行播報加到播報欄。

        用 ``AppendText`` 而不是每次重寫整個欄位：重寫會把插入點與選取範圍
        重設，正在用方向鍵逐行閱讀的螢幕閱讀器使用者會被拉回開頭。
        """
        self._log_lines.append(text)
        if len(self._log_lines) > _LOG_LIMIT:
            del self._log_lines[: len(self._log_lines) - _LOG_LIMIT]
            self.log_ctrl.SetValue("\n".join(self._log_lines))
        elif len(self._log_lines) == 1:
            self.log_ctrl.SetValue(text)
        else:
            self.log_ctrl.AppendText("\n" + text)

    def _announce_intro(self) -> None:
        for line in self.session.briefing_lines():
            self._append_log(line)
        self._append_log("按 F1 查看快捷鍵說明，按 Esc 開啟暫停選單。")

    def _refresh_status(self) -> None:
        """狀態欄只在內容真的變了才重寫。

        每 50 毫秒無條件 ``SetValue`` 會讓螢幕閱讀器一直重讀同一段文字，也
        會把插入點打回開頭，方向鍵逐行閱讀完全沒辦法用。
        """
        text = self.session.status_text()
        if text == self._status_text:
            return
        self._status_text = text
        insertion = self.status_ctrl.GetInsertionPoint()
        self.status_ctrl.ChangeValue(text)
        self.status_ctrl.SetInsertionPoint(min(insertion, len(text)))

    # ------------------------------------------------------------------
    def _on_timer(self, _event) -> None:
        now = time.perf_counter()
        elapsed = now - self._last_tick_s
        self._last_tick_s = now
        self.session.advance(min(elapsed, _MAX_CATCH_UP_S))
        self.announcer.flush()
        self._refresh_status()

    def _on_key(self, event) -> None:
        token = _keycode_to_token(event)
        if token is None:
            event.Skip()
            return
        result = self.dispatcher.dispatch(token)
        if not result.handled and result.reason == "unbound_key":
            # 每次按鍵都要有回饋（§7.2），與主控台一致。
            self.announcer.announce(_UNBOUND_KEY_TEXT, Priority.ACTION)
            self.announcer.flush()
            event.Skip()
            return
        # 已處理的按鍵不再往下傳，避免觸發預設控制項行為。

    def _on_close(self, _event) -> None:
        self._pause()
        self.frame.Destroy()

    # ------------------------------------------------------------------
    # 系統動作
    # ------------------------------------------------------------------
    def _show_text_dialog(self, title: str, text: str, name: str) -> None:
        """以唯讀多行欄位顯示一段文字，方向鍵可逐行閱讀。

        開啟期間暫停模擬：強制回應視窗會擋住所有操作，列車在玩家完全無法
        介入的情況下繼續跑並不合理。
        """
        wx = self.wx
        was_running = self._running
        self._pause()
        dialog = wx.Dialog(
            self.frame,
            title=title,
            size=(560, 540),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        ctrl = wx.TextCtrl(dialog, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        ctrl.SetName(name)
        close = wx.Button(dialog, wx.ID_CANCEL, "關閉")
        sizer.Add(ctrl, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(close, 0, wx.ALIGN_CENTER | wx.ALL, 8)
        dialog.SetSizer(sizer)
        ctrl.SetFocus()
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        if was_running:
            self._resume()
        self.log_ctrl.SetFocus()

    def show_help(self) -> None:
        """F1：快捷鍵說明（§2.1 所有快捷鍵必須可查詢）。"""
        self._show_text_dialog("快捷鍵說明", self.keymap.help_text(), "快捷鍵說明")

    def show_status(self) -> None:
        """列車狀態（與主控台暫停選單的第 3 項相同）。"""
        self._show_text_dialog("列車狀態", self.session.status_text(), "列車狀態")

    def repeat_last(self) -> None:
        """F2：重複播報最近一則訊息（§2.1）。"""
        if not self.announcer.repeat_last():
            self.announcer.announce("目前沒有可重複的訊息。", Priority.ACTION)
        self.announcer.flush()

    def pause_menu_actions(self) -> list[tuple[str, str]]:
        """暫停選單的項目 ``(代碼, 顯示文字)``。

        前四項與主控台的 1～4 相同；「選擇其他車次」是視窗版特有的，因為
        視窗版沒有命令列可以下 ``--service``。
        """
        actions = [
            ("resume", "繼續運轉"),
            ("help", "快捷鍵說明"),
            ("status", "列車狀態"),
        ]
        if self.can_change_service:
            actions.append(("change", "選擇其他車次"))
        actions.append(("quit", "離開遊戲"))
        return actions

    def pause_menu(self) -> None:
        """Esc：暫停選單。項目與主控台一致。

        看完說明或狀態之後回到選單而不是直接繼續運轉，與主控台的行為相同：
        主控台的暫停選單也是一直等到玩家選「繼續運轉」或「離開遊戲」為止。
        """
        wx = self.wx
        self._pause()
        actions = self.pause_menu_actions()
        labels = [label for _, label in actions]

        while True:
            dialog = wx.SingleChoiceDialog(self.frame, "暫停選單", "暫停", labels)
            try:
                chosen = "resume"
                if dialog.ShowModal() == wx.ID_OK:
                    chosen = actions[dialog.GetSelection()][0]
            finally:
                dialog.Destroy()

            if chosen == "help":
                self.show_help()
            elif chosen == "status":
                self.show_status()
            elif chosen in ("change", "quit"):
                self.change_service_requested = chosen == "change"
                self.frame.Close()
                return
            else:
                break

        if not self._running:
            self._resume()
        self.log_ctrl.SetFocus()


def run_wx(
    choices: Sequence[StartChoice],
    make_session: Callable[[str], tuple[DriverSession, Announcer]],
    keymap: Keymap,
    speak: Callable[[str, bool], bool] | None = None,
    *,
    initial_key: str | None = None,
) -> int:  # pragma: no cover - 需要圖形環境
    """啟動 wx 介面。

    Args:
        choices: 車次選擇視窗的選項。
        make_session: 由 ``StartChoice.key`` 建立 ``(工作階段, 播報器)``。
        keymap: 鍵位表。
        speak: 選用的語音輸出。
        initial_key: 直接開始的車次；``None`` 表示先顯示車次選擇視窗。

    Returns:
        結束碼。玩家在選擇視窗按離開也算正常結束。
    """
    import wx

    app = wx.App(False)
    key = initial_key
    while True:
        if key is None:
            key = ServicePicker(choices).ask()
            if key is None:
                return 0

        session, announcer = make_session(key)
        frame = DriverFrame(
            session, keymap, announcer, speak, can_change_service=bool(choices)
        )
        frame.show()
        app.MainLoop()

        if not frame.change_service_requested:
            return 0
        key = None
