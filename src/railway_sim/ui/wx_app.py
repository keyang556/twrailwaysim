"""wxPython 介面（規格 §5.1）。

版面刻意極簡：兩個唯讀多行文字欄位，一個放運轉播報，一個放列車狀態。
理由是螢幕閱讀器可以直接用方向鍵逐行閱讀文字欄位內容，不需要任何自訂
繪圖或視覺元素；所有資訊也同時存在於文字中（§25.5）。

鍵盤事件以 ``EVT_CHAR_HOOK`` 在視窗層級攔截，因此焦點在哪個欄位都能操作，
焦點移動可預測（§2.1）。

語音方面：若系統可用 NVDA Controller Client，會直接送出語音；否則仍以
文字呈現，遊戲功能不受影響（見 :mod:`railway_sim.accessibility.speech`）。
"""

from __future__ import annotations

from collections.abc import Callable

from railway_sim.accessibility.announcer import Announcement, Announcer, Priority
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession

__all__ = ["DriverFrame", "run_wx"]

#: 計時器間隔（毫秒）。
_TIMER_MS = 50

#: 播報欄位保留的行數。
_LOG_LIMIT = 300


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
    ) -> None:
        import wx

        self.wx = wx
        self.session = session
        self.keymap = keymap
        self.announcer = announcer
        self.speak = speak
        self._log_lines: list[str] = []

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
        self.timer.Start(_TIMER_MS)

    # ------------------------------------------------------------------
    def _emit(self, announcement: Announcement) -> None:
        self._log_lines.append(announcement.text)
        if len(self._log_lines) > _LOG_LIMIT:
            del self._log_lines[: len(self._log_lines) - _LOG_LIMIT]
        self.log_ctrl.SetValue("\n".join(self._log_lines))
        self.log_ctrl.SetInsertionPointEnd()
        if self.speak is not None:
            self.speak(announcement.text, announcement.priority >= Priority.SAFETY)

    def _announce_intro(self) -> None:
        session = self.session
        class_name = session.data.service_class_name(session.service.train_type)
        stops = "、".join(p.name_zh_tw for p in session.stations if p.stop_kind == "stop")
        passes = "、".join(p.name_zh_tw for p in session.stations if p.stop_kind != "stop")
        for line in (
            f"車次：{class_name}{session.service.train_number}次",
            f"路線：{session.route.name_zh_tw}",
            f"停靠站：{stops or '無'}",
            f"通過站：{passes or '無'}",
            "按 F1 查看快捷鍵說明。",
        ):
            self._log_lines.append(line)
        self.log_ctrl.SetValue("\n".join(self._log_lines))

    def _refresh_status(self) -> None:
        self.status_ctrl.SetValue(self.session.status_text())

    # ------------------------------------------------------------------
    def _on_timer(self, _event) -> None:
        self.session.advance(_TIMER_MS / 1000.0)
        self.announcer.flush()
        self._refresh_status()

    def _on_key(self, event) -> None:
        token = _keycode_to_token(event)
        if token is None:
            event.Skip()
            return
        result = self.dispatcher.dispatch(token)
        if not result.handled and result.reason == "unbound_key":
            event.Skip()
            return
        # 已處理的按鍵不再往下傳，避免觸發預設控制項行為。

    def _on_close(self, _event) -> None:
        self.timer.Stop()
        self.frame.Destroy()

    # ------------------------------------------------------------------
    # 系統動作
    # ------------------------------------------------------------------
    def show_help(self) -> None:
        wx = self.wx
        dialog = wx.Dialog(self.frame, title="快捷鍵說明", size=(520, 520))
        sizer = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(
            dialog,
            value=self.keymap.help_text(),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        text.SetName("快捷鍵說明")
        close = wx.Button(dialog, wx.ID_CANCEL, "關閉")
        sizer.Add(text, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(close, 0, wx.ALIGN_CENTER | wx.ALL, 8)
        dialog.SetSizer(sizer)
        text.SetFocus()
        dialog.ShowModal()
        dialog.Destroy()
        self.log_ctrl.SetFocus()

    def repeat_last(self) -> None:
        if not self.announcer.repeat_last():
            self.announcer.announce("目前沒有可重複的訊息。", Priority.ACTION)
            self.announcer.flush()

    def pause_menu(self) -> None:
        wx = self.wx
        self.timer.Stop()
        choices = ["繼續運轉", "快捷鍵說明", "離開遊戲"]
        dialog = wx.SingleChoiceDialog(self.frame, "暫停選單", "暫停", choices)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                index = dialog.GetSelection()
                if index == 1:
                    self.show_help()
                elif index == 2:
                    dialog.Destroy()
                    self.frame.Close()
                    return
        finally:
            if dialog:
                dialog.Destroy()
        self.timer.Start(_TIMER_MS)
        self.log_ctrl.SetFocus()


def run_wx(
    session: DriverSession,
    keymap: Keymap,
    announcer: Announcer,
    speak: Callable[[str, bool], bool] | None = None,
) -> int:  # pragma: no cover - 需要圖形環境
    """啟動 wx 介面。"""
    import wx

    app = wx.App(False)
    frame = DriverFrame(session, keymap, announcer, speak)
    frame.show()
    app.MainLoop()
    return 0
