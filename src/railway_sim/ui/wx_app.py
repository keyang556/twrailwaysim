"""wxPython 介面（規格 §5.1）。

版面刻意極簡：兩個清單，一個放運轉播報，一個放最近一次的狀態查詢結果。
不需要任何自訂繪圖或視覺元素；所有資訊也同時存在於文字中（§25.5）。

儀表板不是編輯區
----------------

先前這兩個欄位是唯讀的多行文字框。文字框對螢幕閱讀器而言是**編輯區**：
焦點一進去就被報成「編輯 唯讀 多行」，方向鍵讀的是游標所在的行或字元，
而駕駛台上根本沒有東西可以編輯。現在改用清單：

- 焦點落在清單上時報的是「清單」與目前這一項，不是「編輯」。
- 上下鍵一次讀完整一則播報，不會停在半句話中間。
- 字母鍵不會被當成輸入吞掉（未綁定的字母鍵也不再往下傳，否則清單會把它
  當成快速尋找而跳走）。

新播報進來時**不會**移動選取項目。播報同時已經直接送到螢幕閱讀器（見
下一節），再移動選取只會讓同一句被唸兩次，而且會把正在往回查看的人拉走。
清單只把捲軸移到最新一則，讓看得見的人也跟得上。

說明與完整狀態那類「一整段文字」仍然用唯讀文字框顯示：那時要的正是逐字
逐行的檢閱，編輯區的游標導覽是對的工具。

直接送到螢幕閱讀器
------------------

播報除了寫進清單，也**直接送給 NVDA 的語音與點字**，不必等螢幕閱讀器自己
發現畫面變了。好處是時機準確（列車是即時的，慢半拍的警告沒有用），而且
可以帶優先級：超速警告會插播，插播完 NVDA 會把被打斷的內容接回去。

Alt＋Shift＋T 另外開啟**點字即時顯示**：在點字顯示器上持續顯示距離下一站
還有多遠，接近停靠站時改顯示距離停車位置多遠。看不見月台標記的人終於有
一個連續的、不必一直按鍵去問的資訊來源。

狀態改為「查詢才出現」
----------------------

先前的版本常駐顯示整份列車狀態，玩家得自己在十幾行文字裡找需要的那一
行，而且每次內容變動螢幕閱讀器都會重讀。現在改成與 OpenBVE 無障礙模式
相同的做法：**按快捷鍵或選單選一項，才會播報並顯示那一項**，例如 V 報
速度、G 報前方號誌。狀態欄顯示的就是剛剛播出去的那一句，看到的與聽到的
完全一致。

顯示的是查詢當下的快照，不會自己更新——播出去的語音也是快照，兩者若不
一致反而更難判斷。要最新的數值就再按一次。

鍵盤事件以 ``EVT_CHAR_HOOK`` 在視窗層級攔截，因此焦點在哪個欄位都能操作，
焦點移動可預測（§2.1）。選單項目刻意**不**設 wx 加速鍵，改在標籤裡寫出
按鍵名稱，避免同一個按鍵被加速鍵與 ``EVT_CHAR_HOOK`` 各處理一次。

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
from railway_sim.accessibility.speech import speech_priority_for
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import STATUS_ITEM_ACTIONS, DriverSession
from railway_sim.systems import DEFAULT_SYSTEM

__all__ = [
    "DriverFrame",
    "ServicePicker",
    "StartChoice",
    "initial_system_choice",
    "run_wx",
]

#: 計時器間隔（毫秒）。實際推進量仍以 ``time.perf_counter()`` 為準。
_TIMER_MS = 50

#: 單次計時器最多補推的模擬秒數。
#:
#: 拖動視窗、系統忙碌或機器休眠時，計時器可能停很久。若照實補推，列車會
#: 在一個畫面更新內衝過好幾個車站，等於玩家什麼都沒做就被判應停未停。
_MAX_CATCH_UP_S = 1.0

#: 播報清單保留的行數。
_LOG_LIMIT = 300

#: 點字即時顯示的更新間隔（毫秒）。
#:
#: NVDA 把點字訊息當成**暫時**訊息，預設四秒後就換回焦點的內容，因此必須
#: 定期重送。間隔要明顯短於那個逾時，又不必短到每一格都重畫。
_BRAILLE_MS = 700

#: 一則播報在點字上停留多久（秒），期間不被即時顯示蓋掉。
#:
#: 沒有這段保留時間的話，緊急制軔之類的訊息會在下一次更新（不到一秒）就
#: 被距離蓋掉，摸讀的人根本來不及讀到。
_BRAILLE_HOLD_S = 2.5

#: 未綁定時要交還給控制項的按鍵。
#:
#: Tab 與 F10 是視窗本身的巡覽鍵。先前它們會被當成「未設定功能的按鍵」而
#: 播報一句說明，等於每次換焦點都被唸一次無關的話。方向鍵、Home、End 這些
#: 不在這裡，因為 :func:`_keycode_to_token` 本來就不會把它們轉成代碼。
_NAVIGATION_KEYS = frozenset({"TAB", "SHIFT+TAB", "CTRL+TAB", "F10", "SHIFT+F10"})

#: 未綁定按鍵的回饋（與主控台相同，§7.2）。
_UNBOUND_KEY_TEXT = "此按鍵未設定功能，按 F1 查看快捷鍵說明。"

#: 尚未查詢任何狀態時，狀態欄顯示的說明。
_STATUS_HINT_TEXT = (
    "尚未查詢。按快捷鍵或用「狀態查詢」選單查詢單一項目，查到的內容會顯示在"
    "這裡，同時播報出去。"
)



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
    }
    # F1 至 F12 一次列出：車門用的 F5／F6 取自 OpenBVE，只補這兩個會讓
    # 下一個要用功能鍵的動作又得回來改一次。
    special.update({getattr(wx, f"WXK_F{n}"): f"F{n}" for n in range(1, 13)})
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
    """開場的選擇視窗（車次，也用來選鐵路系統）。

    視窗版沒有命令列可以下 ``--service``，若不提供選擇畫面，安裝版就永遠
    只能開同一個預設車次。清單支援關鍵字過濾，只用鍵盤即可完成選擇。

    系統選擇沿用同一個視窗而不是另做一個：兩者要做的事完全一樣（從一份清單
    挑一項），共用之後鍵盤操作、搜尋與螢幕閱讀器行為也一定一致。

    補充說明直接送給螢幕閱讀器
    --------------------------

    每一項的補充說明（路線、停靠幾站）顯示在清單下方的靜態文字裡。螢幕閱讀器
    在清單裡上下移動時只會唸出項目本身，那段說明**看得到卻聽不到**，除非使用者
    自己 Tab 過去確認——選十幾個車次就要 Tab 十幾次。因此選取變動時直接把說明
    送出去唸，搜尋時也直接說出還剩幾個符合，不必自己數。
    """

    def __init__(
        self,
        choices: Sequence[StartChoice],
        parent=None,
        *,
        title: str = "選擇車次",
        list_label: str = "可駕駛車次（上下鍵選擇，Enter 開始）",
        search_label: str = "搜尋（車次、車種、車輛型式、起訖站、沿途車站）",
        accept_label: str = "開始運轉",
        speak: Callable[[str, bool], bool] | None = None,
    ) -> None:
        import wx

        self.wx = wx
        self.choices = list(choices)
        self.visible: list[StartChoice] = list(choices)
        self.speak = speak

        self.dialog = wx.Dialog(
            parent,
            title=title,
            size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        search_text = wx.StaticText(self.dialog, label=search_label)
        self.search = wx.TextCtrl(self.dialog)
        self.search.SetName("搜尋")

        list_text = wx.StaticText(self.dialog, label=list_label)
        self.listbox = wx.ListBox(self.dialog, style=wx.LB_SINGLE)
        self.listbox.SetName(list_label)

        self.detail = wx.StaticText(self.dialog, label="")

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self.dialog, wx.ID_OK, accept_label)
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self.dialog, wx.ID_CANCEL, "離開"))
        buttons.Realize()

        sizer.Add(search_text, 0, wx.ALL, 6)
        sizer.Add(self.search, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(list_text, 0, wx.ALL, 6)
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
    def _say(self, text: str) -> None:
        """把一段文字直接送去唸。沒有語音後端時什麼都不做。

        不經過 :class:`~railway_sim.accessibility.announcer.Announcer`：這裡
        是選擇視窗，還沒有工作階段，也沒有播報歷史可言。
        """
        if self.speak is not None and text:
            self.speak(text, False)

    def _refresh(self) -> None:
        keyword = self.search.GetValue().strip()
        self.visible = [c for c in self.choices if c.matches(keyword)]
        self.listbox.Set([c.label for c in self.visible])
        if self.visible:
            self.listbox.SetSelection(0)
        self._update_detail()

    def _on_search(self, _event) -> None:
        self._refresh()
        # 邊打字邊知道還剩幾個，不必切到清單自己數。
        self._say(self._match_summary())

    def _match_summary(self) -> str:
        if not self.visible:
            return f"沒有符合的項目，共 {len(self.choices)} 個。"
        if len(self.visible) == len(self.choices):
            return f"共 {len(self.choices)} 個。"
        return f"符合 {len(self.visible)} 個。"

    def _update_detail(self) -> str:
        """更新補充說明並回傳目前顯示的文字。"""
        index = self.listbox.GetSelection()
        if 0 <= index < len(self.visible):
            text = self.visible[index].detail
        else:
            text = f"沒有符合的車次（共 {len(self.choices)} 個）"
        self.detail.SetLabel(text)
        return text

    def _on_select(self, _event) -> None:
        # 螢幕閱讀器只會唸出項目本身，說明看得到卻聽不到，因此直接送出去。
        self._say(self._update_detail())

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
        can_change_system: bool = False,
    ) -> None:
        import wx

        self.wx = wx
        self.session = session
        self.keymap = keymap
        self.announcer = announcer
        self.speak = speak
        self.can_change_service = can_change_service
        self.can_change_system = can_change_system
        self._log_lines: list[str] = []
        self._status_text = ""
        self._last_tick_s = time.perf_counter()
        self._running = False

        # 螢幕閱讀器輸出可能只有語音（單純的 speak sink），也可能連點字與
        # 優先級一起（:class:`~railway_sim.accessibility.speech.ScreenReader`）。
        # 用能力探測而不是型別判斷，測試才能塞一個只有其中一半的替身進來。
        self._braille: Callable[[str], bool] | None = getattr(speak, "braille", None)
        self._speak_with_priority = getattr(speak, "speak", None)

        #: 點字即時顯示是否開啟（Alt＋Shift＋T）。
        self.braille_monitor = False
        self._braille_hold_until = 0.0
        self._braille_text = ""

        #: 關窗之後由 :func:`run_wx` 讀取：是否要回到車次選擇視窗。
        self.change_service_requested = False

        #: 關窗之後由 :func:`run_wx` 讀取：是否要回到鐵路系統選擇視窗。
        self.change_system_requested = False

        service = session.service
        class_name = session.data.service_class_name(service.train_type)
        title = (
            f"臺灣鐵路人員模擬器 — 司機員模式 — {session.data.system.name_zh_tw} — "
            f"{class_name}{service.train_number}次"
        )

        self.frame = wx.Frame(None, title=title, size=(760, 620))
        panel = wx.Panel(self.frame)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # 清單而不是唯讀文字框：文字框會被螢幕閱讀器報成「編輯區」，而駕駛台
        # 上沒有東西可以編輯；清單的上下鍵還會一次讀完整一則播報。
        log_label = wx.StaticText(panel, label="運轉播報（清單，可用上下鍵逐則閱讀）")
        self.log_ctrl = wx.ListBox(panel, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
        self.log_ctrl.SetName("運轉播報")

        status_label = wx.StaticText(
            panel, label="狀態查詢結果（顯示最近一次查詢的項目）"
        )
        self.status_ctrl = wx.ListBox(panel, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
        self.status_ctrl.SetName("狀態查詢結果")

        hint = wx.StaticText(
            panel,
            label=(
                "F1：快捷鍵說明　F2：重複播報　Esc：暫停選單　"
                "Alt＋Shift＋T：點字即時顯示"
            ),
        )

        sizer.Add(log_label, 0, wx.ALL, 6)
        sizer.Add(self.log_ctrl, 4, wx.EXPAND | wx.ALL, 6)
        sizer.Add(status_label, 0, wx.ALL, 6)
        sizer.Add(self.status_ctrl, 1, wx.EXPAND | wx.ALL, 6)
        sizer.Add(hint, 0, wx.ALL, 6)
        panel.SetSizer(sizer)

        self._build_menu_bar()
        self.announcer.sink = self._emit

        self.dispatcher = KeyDispatcher(keymap)
        self.dispatcher.register_all(session.action_handlers())  # type: ignore[arg-type]
        self.dispatcher.register_all(
            {
                "show_help": self.show_help,
                "repeat_last": self.repeat_last,
                "pause_menu": self.pause_menu,
                "toggle_braille_monitor": self.toggle_braille_monitor,
            }
        )

        self.frame.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.frame.Bind(wx.EVT_CLOSE, self._on_close)

        self.timer = wx.Timer(self.frame)
        self.frame.Bind(wx.EVT_TIMER, self._on_timer, self.timer)

        # 點字用自己的計時器：更新頻率與模擬步進無關，而且暫停時仍應繼續
        # 顯示（停在那裡看距離也是有意義的），兩者不該綁在一起。
        self.braille_timer = wx.Timer(self.frame)
        self.frame.Bind(wx.EVT_TIMER, self._on_braille_timer, self.braille_timer)

        self._announce_intro()
        self._refresh_status()
        # 焦點落在播報清單上：這是儀表板的主體，而且不是編輯區。
        self.log_ctrl.SetFocus()

    # ------------------------------------------------------------------
    # 選單
    # ------------------------------------------------------------------
    def _keys_text_for(self, action: str) -> str:
        """動作對應按鍵的顯示文字，取自鍵位表而不是寫死在選單裡。"""
        binding = self.keymap.binding_for(action)
        return binding.keys_text if binding is not None else ""

    def _status_menu_label(self, code: str, label: str) -> str:
        """狀態選單項目的文字，例如「速度（Ｖ、Ctrl＋Shift＋S）」。

        刻意不使用 wx 的加速鍵（``\\t``）：按鍵已由 ``EVT_CHAR_HOOK`` 統一
        處理，再設一次加速鍵會讓同一次按鍵被處理兩遍。
        """
        keys = self._keys_text_for(STATUS_ITEM_ACTIONS.get(code, ""))
        return f"{label}（{keys}）" if keys else label

    def _build_menu_bar(self) -> None:
        """建立選單列。

        鍵盤操作已經夠用，選單存在的理由是**不必先記住快捷鍵**：查得到有
        哪些狀態可以問，也看得到每一項對應哪個鍵（§2.1 所有快捷鍵必須可
        查詢）。選單項目與快捷鍵走的是同一條路徑，因此兩者結果一定一致。
        """
        wx = self.wx
        self._menu_status_codes = {}

        status_menu = wx.Menu()
        for item in self.session.status_items():
            entry = status_menu.Append(
                wx.ID_ANY, self._status_menu_label(item.code, item.label)
            )
            self._menu_status_codes[entry.GetId()] = item.code
            self.frame.Bind(wx.EVT_MENU, self._on_status_menu, entry)

        system_menu = wx.Menu()
        help_item = system_menu.Append(wx.ID_ANY, f"快捷鍵說明（{self._keys_text_for('show_help')}）")
        repeat_item = system_menu.Append(
            wx.ID_ANY, f"重複播報最近一則（{self._keys_text_for('repeat_last')}）"
        )
        full_status_item = system_menu.Append(wx.ID_ANY, "完整列車狀態")
        braille_item = system_menu.Append(
            wx.ID_ANY,
            "點字即時顯示（距離下一站，"
            f"{self._keys_text_for('toggle_braille_monitor')}）",
        )
        pause_item = system_menu.Append(
            wx.ID_ANY, f"暫停選單（{self._keys_text_for('pause_menu')}）"
        )
        self.frame.Bind(wx.EVT_MENU, lambda _e: self.show_help(), help_item)
        self.frame.Bind(wx.EVT_MENU, lambda _e: self.repeat_last(), repeat_item)
        self.frame.Bind(wx.EVT_MENU, lambda _e: self.show_status(), full_status_item)
        self.frame.Bind(
            wx.EVT_MENU, lambda _e: self.toggle_braille_monitor(), braille_item
        )
        self.frame.Bind(wx.EVT_MENU, lambda _e: self.pause_menu(), pause_item)

        bar = wx.MenuBar()
        bar.Append(status_menu, "狀態查詢(&S)")
        bar.Append(system_menu, "系統(&Y)")
        self.frame.SetMenuBar(bar)

    def _on_status_menu(self, event) -> None:
        code = self._menu_status_codes.get(event.GetId())
        if code is not None:
            self.query_status(code)

    def query_status(self, code: str) -> None:
        """查詢並播報一個狀態項目，同時更新狀態欄。"""
        self.session.announce_status(code)
        self.announcer.flush()
        self._refresh_status()

    # ------------------------------------------------------------------
    def show(self) -> None:
        self.frame.Show()
        self._resume()

    # ------------------------------------------------------------------
    # 模擬推進
    # ------------------------------------------------------------------
    def _pause(self) -> None:
        """停止推進模擬。開啟強制回應視窗前必須呼叫。

        點字即時顯示一併停下來：說明與狀態視窗是拿來讀的，每 0.7 秒把顯示器
        蓋成距離會讓摸讀的人完全讀不到那些文字。
        """
        self.timer.Stop()
        self.braille_timer.Stop()
        self._running = False

    def _resume(self) -> None:
        """恢復推進模擬。

        一併重設計時基準點，否則暫停期間流逝的真實時間會在恢復的第一步
        全部補推進去，列車會突然向前跳一大段。
        """
        self._last_tick_s = time.perf_counter()
        self._running = True
        self.timer.Start(_TIMER_MS)
        if self.braille_monitor:
            self.braille_timer.Start(_BRAILLE_MS)

    def _emit(self, announcement: Announcement) -> None:
        """一則播報同時進到清單、語音與點字。

        直接送給螢幕閱讀器而不是等它自己發現畫面變了：列車是即時的，慢半拍
        的警告沒有用。優先級一併交給 NVDA，超速警告因此會插播，而且插播完
        被打斷的內容會由 NVDA 自己接回去。
        """
        self._append_log(announcement.text)
        self._speak(announcement.text, announcement.priority)
        self._braille_announcement(announcement.text)

    def _speak(self, text: str, priority: Priority) -> None:
        """把一段文字送去朗讀，能帶優先級就帶。"""
        if self._speak_with_priority is not None:
            self._speak_with_priority(text, priority=speech_priority_for(priority))
        elif self.speak is not None:
            self.speak(text, priority >= Priority.SAFETY)

    def _screen_reader_available(self) -> bool:
        """查詢目前連線狀態，而非把 backend 物件存在當成已連線。"""
        if self.speak is None:
            return False
        available = getattr(self.speak, "available", None)
        return (
            True
            if available is None
            else bool(available() if callable(available) else available)
        )

    def _append_log(self, text: str) -> None:
        """把一則播報加到播報清單。

        **不移動選取項目**：播報已經直接送到螢幕閱讀器了，再移動選取會讓同
        一句被唸第二次，也會把正在往回查看的人拉走。只把捲軸帶到最新一則，
        讓看得見的人跟得上。
        """
        self._log_lines.append(text)
        self.log_ctrl.Append(text)
        if len(self._log_lines) > _LOG_LIMIT:
            excess = len(self._log_lines) - _LOG_LIMIT
            del self._log_lines[:excess]
            for _ in range(excess):
                self.log_ctrl.Delete(0)
        self.log_ctrl.SetFirstItem(self.log_ctrl.GetCount() - 1)

    def _announce_intro(self) -> None:
        for line in self.session.briefing_lines():
            self._append_log(line)
        self._append_log("按 F1 查看快捷鍵說明，按 Esc 開啟暫停選單。")
        self._append_log(
            "狀態不再常駐顯示：按快捷鍵或用「狀態查詢」選單問一項，"
            f"例如{self._keys_text_for('announce_speed')}報速度。"
        )
        self._append_log(
            f"對準停車位置：{self._keys_text_for('announce_stop_point')}"
            "隨時可問距離停車位置多遠；接近停靠站時會自動由疏而密報出剩餘距離。"
        )
        if self._braille is not None:
            self._append_log(
                f"{self._keys_text_for('toggle_braille_monitor')}："
                "在點字顯示器上即時顯示距離下一站還有多遠。"
            )

    def _refresh_status(self) -> None:
        """狀態欄只在內容真的變了才重寫。

        每 50 毫秒無條件重設清單內容會讓螢幕閱讀器一直重讀同一句，也會把
        選取打回開頭。現在內容只在玩家查詢時變動，因此實際上幾乎不會重寫。
        """
        status = self.session.last_status
        text = (
            _STATUS_HINT_TEXT
            if status is None
            else f"{status.label}：{status.text}"
        )
        if text == self._status_text:
            return
        self._status_text = text
        self.status_ctrl.Set([text])

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
            # 方向鍵、Home、End…：清單自己處理，玩家正在閱讀播報。
            event.Skip()
            return
        if token in _NAVIGATION_KEYS and self.keymap.action_for(token) is None:
            # Tab 與 F10 是視窗的巡覽鍵。把它們當成「未設定功能的按鍵」等於
            # 每次換焦點都被唸一句無關的話。
            event.Skip()
            return
        result = self.dispatcher.dispatch(token)
        if not result.handled and result.reason == "unbound_key":
            # 每次按鍵都要有回饋（§7.2），與主控台一致。
            self.announcer.announce(_UNBOUND_KEY_TEXT, Priority.ACTION)
            self.announcer.flush()
            # 刻意不往下傳：清單會把字母鍵當成快速尋找而跳到別的項目，
            # 玩家只是按錯鍵，不該因此失去閱讀位置。
            return
        # 狀態查詢鍵（V、G、N…）按下之後，狀態欄要換成剛剛播出去的那一項。
        self._refresh_status()
        # 已處理的按鍵不再往下傳，避免觸發預設控制項行為。

    def _on_close(self, _event) -> None:
        self._pause()
        self.braille_timer.Stop()
        self.frame.Destroy()

    # ------------------------------------------------------------------
    # 點字即時顯示（Alt＋Shift＋T）
    # ------------------------------------------------------------------
    def toggle_braille_monitor(self) -> None:
        """開啟或關閉點字即時顯示。

        沒有連接 NVDA 時明確說出原因，而不是靜靜地沒反應——每次按鍵都要有
        回饋（§7.2），而且「沒有點字顯示器」與「功能壞了」是兩回事。
        """
        if self._braille is None or not self._screen_reader_available():
            self.announcer.announce(
                "沒有連接 NVDA，無法使用點字顯示。所有資訊仍以文字提供。",
                Priority.ACTION,
            )
            self.announcer.flush()
            return

        self.braille_monitor = not self.braille_monitor
        if self.braille_monitor:
            self.braille_timer.Start(_BRAILLE_MS)
            self.announcer.announce(
                "點字即時顯示已開啟：顯示距離下一站，接近停靠站時改顯示距離停車位置。",
                Priority.NOTICE,
            )
            self.announcer.flush()
            # 開啟的那一句才剛送去點字，等保留時間過了再換成即時內容。
            self._update_braille_monitor()
        else:
            self.braille_timer.Stop()
            self._braille_text = ""
            self.announcer.announce("點字即時顯示已關閉。", Priority.NOTICE)
            self.announcer.flush()

    def _braille_announcement(self, text: str) -> None:
        """把一則播報送到點字顯示器，並讓它停留一段時間。

        沒有這段保留時間的話，即時顯示會在不到一秒內把訊息蓋掉，摸讀的人
        根本來不及讀完。
        """
        if self._braille is None or not self._screen_reader_available():
            return
        self._braille(text)
        self._braille_hold_until = time.perf_counter() + _BRAILLE_HOLD_S

    def _on_braille_timer(self, _event) -> None:
        self._update_braille_monitor()

    def _update_braille_monitor(self) -> None:
        """重送即時顯示的內容。

        每次都重送而不是只在文字變了才送：NVDA 把點字訊息當成暫時訊息，
        過幾秒就會換回焦點的內容，不重送就消失了。
        """
        if (
            not self.braille_monitor
            or self._braille is None
            or not self._screen_reader_available()
        ):
            return
        if time.perf_counter() < self._braille_hold_until:
            return
        self._braille_text = self.session.braille_line()
        self._braille(self._braille_text)

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
        """完整列車狀態（與主控台暫停選單的第 3 項相同）。

        平常的狀態欄只顯示查詢到的單一項目；要一次看完整份時用這個。
        """
        self._show_text_dialog("列車狀態", self.session.status_text(), "列車狀態")

    def ask_status_item(self) -> None:
        """狀態查詢選單（與主控台暫停選單的第 4 項相同）。"""
        wx = self.wx
        items = self.session.status_items()
        labels = [self._status_menu_label(i.code, i.label) for i in items]
        dialog = wx.SingleChoiceDialog(self.frame, "要查詢哪一項？", "狀態查詢", labels)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            code = items[dialog.GetSelection()].code
        finally:
            dialog.Destroy()
        self.query_status(code)
        self._show_text_dialog(
            "狀態查詢結果", self._status_text, "狀態查詢結果"
        )

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
            ("status_item", "狀態查詢（單一項目）"),
        ]
        if self.can_change_service:
            actions.append(("change", "選擇其他車次"))
        if self.can_change_system:
            actions.append(("change_system", "選擇其他鐵路系統"))
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
            elif chosen == "status_item":
                self.ask_status_item()
            elif chosen in ("change", "change_system", "quit"):
                self.change_service_requested = chosen == "change"
                self.change_system_requested = chosen == "change_system"
                self.frame.Close()
                return
            else:
                break

        if not self._running:
            self._resume()
        self.log_ctrl.SetFocus()


def initial_system_choice(
    initial_system: str | None, systems: Sequence[StartChoice]
) -> tuple[bool, str | None]:
    """開場要不要問系統、以及一開始用哪一個。

    回傳 ``(要不要問, 一開始的系統)``。要問的時候系統一定是 ``None``——
    :func:`run_wx` 的迴圈就是用「系統還沒決定」當作該開選擇視窗的條件，
    先填一個預設值進去會讓選擇視窗永遠不出現，直接跳進第一個系統的車次清單。

    這段判斷抽出來是為了測得到：``run_wx`` 本身要有圖形環境才跑得起來。
    """
    if initial_system is not None:
        return False, initial_system
    if len(systems) > 1:
        return True, None
    return False, systems[0].key if systems else DEFAULT_SYSTEM


def run_wx(
    open_system: Callable[
        [str], tuple[Sequence[StartChoice], Callable[[str], tuple[DriverSession, Announcer]]]
    ],
    keymap: Keymap,
    speak: Callable[[str, bool], bool] | None = None,
    *,
    systems: Sequence[StartChoice] = (),
    initial_system: str | None = None,
    initial_key: str | None = None,
) -> int:  # pragma: no cover - 需要圖形環境
    """啟動 wx 介面。

    Args:
        open_system: 由系統代碼取得 ``(車次選項, 建立工作階段的函式)``。
            資料是按系統分開載入的，因此這裡用回呼而不是先把兩套都讀進來——
            只玩臺鐵的人不必為了捷運多等一次載入。
        keymap: 鍵位表。
        speak: 選用的語音輸出。
        systems: 鐵路系統選項。只有一個（或已用 ``--system`` 指定）時不會問。
        initial_system: 直接使用的系統；``None`` 表示先問。
        initial_key: 直接開始的車次；``None`` 表示先顯示車次選擇視窗。

    Returns:
        結束碼。玩家在選擇視窗按離開也算正常結束。
    """
    import wx

    app = wx.App(False)
    ask_system, system = initial_system_choice(initial_system, systems)
    key = initial_key

    while True:
        if ask_system and system is None:
            system = ServicePicker(
                systems,
                title="選擇鐵路系統",
                list_label="可駕駛的鐵路系統（上下鍵選擇，Enter 確認）",
                search_label="搜尋",
                accept_label="選擇",
                speak=speak,
            ).ask()
            if system is None:
                return 0
            key = None

        choices, make_session = open_system(system)
        if key is None:
            key = ServicePicker(choices, speak=speak).ask()
            if key is None:
                if not ask_system:
                    return 0
                # 選錯系統的人應該回得去，而不是被迫離開遊戲重開。
                system = None
                continue

        session, announcer = make_session(key)
        frame = DriverFrame(
            session,
            keymap,
            announcer,
            speak,
            can_change_service=bool(choices),
            can_change_system=ask_system,
        )
        frame.show()
        app.MainLoop()

        if frame.change_system_requested:
            system = None
            key = None
            continue
        if not frame.change_service_requested:
            return 0
        key = None
