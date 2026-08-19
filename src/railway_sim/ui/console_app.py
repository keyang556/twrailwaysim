"""主控台介面（規格 §26）。

設計為純文字、僅在有事件時輸出，因此：

- NVDA 等螢幕閱讀器會自動朗讀新出現的主控台文字。
- 不會有持續重繪造成的語音塞車（§7.2）。
- 完全不需要滑鼠（§23.2）。

按鍵讀取在 Windows 使用 ``msvcrt``，其他平台使用 ``termios``；兩者都是
單次按鍵即完成操作，不需要長按（§7.2）。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from railway_sim.accessibility.announcer import Announcement, Announcer, Priority
from railway_sim.accessibility.speech import speech_priority_for
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import STATUS_ITEM_ACTIONS, DriverSession

__all__ = ["ConsoleApp", "read_key_nonblocking"]

#: 主迴圈的實際更新間隔（秒）。
_LOOP_INTERVAL_S = 0.05

#: 點字即時顯示的更新間隔（秒）。
#:
#: NVDA 把點字訊息當成暫時訊息，過幾秒就換回焦點的內容，因此必須定期重送。
_BRAILLE_INTERVAL_S = 0.7

#: 一則播報在點字上停留多久（秒），期間不被即時顯示蓋掉。
_BRAILLE_HOLD_S = 2.5

#: 直接對應到具名按鍵的控制字元，不視為 Ctrl 組合。
_CONTROL_CHAR_NAMES = {
    "\r": "ENTER",
    "\n": "ENTER",
    "\t": "TAB",
    "\x1b": "ESC",
    "\x08": "BACKSPACE",
}


def _decode_control_char(char: str) -> str | None:
    """把終端機收到的控制字元轉成鍵位代碼。

    終端機只會送出 ``Ctrl+字母`` 的控制字元，**無法區分 Ctrl 與
    Ctrl+Shift**。本專案的鍵位表沒有使用單獨的 ``Ctrl+字母``，因此一律
    當成 ``CTRL+SHIFT+字母``，在本鍵位表內不會產生歧義。
    """
    if char in _CONTROL_CHAR_NAMES:
        return _CONTROL_CHAR_NAMES[char]
    code = ord(char)
    if 1 <= code <= 26:
        return f"CTRL+SHIFT+{chr(ord('A') + code - 1)}"
    return None


#: Windows 擴充鍵（0x00 / 0xE0 前綴）的掃描碼對應。
_WINDOWS_SCANCODES = {
    ";": "F1",
    "<": "F2",
    "=": "F3",
    ">": "F4",
    "?": "F5",
    "@": "F6",
    "A": "F7",
    "B": "F8",
    "C": "F9",
    "D": "F10",
    "H": "UP",
    "P": "DOWN",
    "K": "LEFT",
    "M": "RIGHT",
}


def read_key_nonblocking() -> str | None:
    """非阻塞讀取一個按鍵，沒有按鍵時回傳 ``None``。"""
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


def _read_key_windows() -> str | None:
    import msvcrt

    if not msvcrt.kbhit():
        return None
    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        scan = msvcrt.getwch()
        return _WINDOWS_SCANCODES.get(scan.upper())
    if char < " ":
        return _decode_control_char(char)
    return char


def _read_key_posix() -> str | None:  # pragma: no cover - 非 Windows 路徑
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if not select.select([sys.stdin], [], [], 0)[0]:
            return None
        char = sys.stdin.read(1)
        if char == "\x1b" and select.select([sys.stdin], [], [], 0.01)[0]:
            rest = sys.stdin.read(2)
            if rest == "OP":
                return "F1"
            if rest == "OQ":
                return "F2"
            return "ESC"
        if char < " ":
            return _decode_control_char(char)
        return char
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class ConsoleApp:
    """司機員模式的主控台介面。"""

    def __init__(
        self,
        session: DriverSession,
        keymap: Keymap,
        announcer: Announcer,
        speak: Callable[[str, bool], bool] | None = None,
    ) -> None:
        self.session = session
        self.keymap = keymap
        self.announcer = announcer
        self.speak = speak
        self.running = True

        # 螢幕閱讀器輸出可能只有語音，也可能連點字與優先級一起。用能力探測
        # 而不是型別判斷，與視窗版一致。
        self._braille: Callable[[str], bool] | None = getattr(speak, "braille", None)
        self._speak_with_priority = getattr(speak, "speak", None)

        #: 點字即時顯示是否開啟。
        self.braille_monitor = False
        self._braille_hold_until = 0.0
        self._braille_next_s = 0.0

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

    # ------------------------------------------------------------------
    def _emit(self, announcement: Announcement) -> None:
        """一則播報同時進到畫面、語音與點字。

        直接送給螢幕閱讀器而不是只印出來等它自己讀到：列車是即時的，而且
        這樣才帶得了優先級——超速警告會插播，插播完 NVDA 會把被打斷的內容
        接回去，不像 cancelSpeech 那樣整段丟掉。
        """
        print(announcement.text, flush=True)
        if self._speak_with_priority is not None:
            self._speak_with_priority(
                announcement.text,
                priority=speech_priority_for(announcement.priority),
            )
        elif self.speak is not None:
            self.speak(announcement.text, announcement.priority >= Priority.SAFETY)
        if self._braille is not None:
            self._braille(announcement.text)
            self._braille_hold_until = time.perf_counter() + _BRAILLE_HOLD_S

    def _say(self, text: str, priority: Priority = Priority.ACTION) -> None:
        self.announcer.announce(text, priority)
        self.announcer.flush()

    # ------------------------------------------------------------------
    # 系統動作
    # ------------------------------------------------------------------
    def show_help(self) -> None:
        """F1：快捷鍵說明（§2.1 所有快捷鍵必須可查詢）。"""
        print("\n===== 快捷鍵說明 =====", flush=True)
        for line in self.keymap.help_lines():
            print(line, flush=True)
        print("===== 說明結束 =====\n", flush=True)

    def repeat_last(self) -> None:
        """F2：重複播報最近一則訊息（§2.1）。"""
        if not self.announcer.repeat_last():
            self._say("目前沒有可重複的訊息。")

    def toggle_braille_monitor(self) -> None:
        """開啟或關閉點字即時顯示。

        終端機收不到 Alt 組合鍵，因此主控台這邊實際上是由暫停選單呼叫；
        動作本身仍然註冊在同一個代碼上，兩個介面提供的能力才一致（§25.5）。
        """
        if self._braille is None:
            self._say("沒有連接 NVDA，無法使用點字顯示。所有資訊仍以文字提供。")
            return
        self.braille_monitor = not self.braille_monitor
        if self.braille_monitor:
            self._say(
                "點字即時顯示已開啟：顯示距離下一站，接近停靠站時改顯示距離停車位置。",
                Priority.NOTICE,
            )
        else:
            self._say("點字即時顯示已關閉。", Priority.NOTICE)

    def _update_braille_monitor(self) -> None:
        """重送即時顯示的內容。

        每次都重送而不是只在文字變了才送：NVDA 把點字訊息當成暫時訊息，
        過幾秒就會換回焦點的內容，不重送就消失了。
        """
        if not self.braille_monitor or self._braille is None:
            return
        now = time.perf_counter()
        if now < self._braille_hold_until or now < self._braille_next_s:
            return
        self._braille_next_s = now + _BRAILLE_INTERVAL_S
        self._braille(self.session.braille_line())


    def pause_menu(self) -> None:
        """Esc：暫停選單。項目與視窗版一致（§25.5）。"""
        print("\n===== 暫停選單 =====", flush=True)
        print("1：繼續運轉", flush=True)
        print("2：快捷鍵說明", flush=True)
        print("3：列車狀態", flush=True)
        print("4：狀態查詢（單一項目）", flush=True)
        print(f"5：點字即時顯示（目前{'開啟' if self.braille_monitor else '關閉'}）", flush=True)
        print("6：離開遊戲", flush=True)
        print("請按 1 到 6。", flush=True)

        while True:
            key = read_key_nonblocking()
            if key is None:
                time.sleep(0.02)
                continue
            if key == "1":
                print("繼續運轉。\n", flush=True)
                return
            if key == "2":
                self.show_help()
            elif key == "3":
                print(self.session.status_text(), flush=True)
            elif key == "4":
                self.status_menu()
            elif key == "5":
                self.toggle_braille_monitor()
            elif key == "6":
                self.running = False
                print("離開遊戲。", flush=True)
                return
            else:
                print("請按 1 到 6。", flush=True)

    def status_menu(self) -> None:
        """狀態查詢選單。

        每一項也都有快捷鍵，可以直接在運轉中按；選單的用途是**不必先記住
        快捷鍵**（§2.1 所有快捷鍵必須可查詢）。項目與內容來自
        :class:`~railway_sim.roles.driver.DriverSession`，因此與視窗版完全
        相同（§25.5）。
        """
        items = self.session.status_items()
        print("\n----- 狀態查詢 -----", flush=True)
        for index, item in enumerate(items, start=1):
            keys = self._keys_text_for(STATUS_ITEM_ACTIONS.get(item.code, ""))
            print(f"{index}：{item.label}{f'（{keys}）' if keys else ''}", flush=True)
        print("0：返回", flush=True)

        while True:
            key = read_key_nonblocking()
            if key is None:
                time.sleep(0.02)
                continue
            if key == "0":
                print("返回暫停選單。", flush=True)
                return
            if key.isdigit() and 1 <= int(key) <= len(items):
                self.session.announce_status(items[int(key) - 1].code)
                self.announcer.flush()
                return
            print(f"請按 0 到 {len(items)}。", flush=True)

    def _keys_text_for(self, action: str) -> str:
        binding = self.keymap.binding_for(action) if action else None
        return binding.keys_text if binding is not None else ""

    # ------------------------------------------------------------------
    def announce_intro(self) -> None:
        print("===== 臺灣鐵路人員模擬器：司機員模式 =====", flush=True)
        for line in self.session.briefing_lines():
            print(line, flush=True)
        print("按 F1 查看快捷鍵說明，按 Esc 開啟暫停選單。", flush=True)
        print("=========================================\n", flush=True)

    def run(self) -> int:
        """主迴圈。"""
        self.announce_intro()
        last = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            self.session.advance(now - last)
            last = now
            self.announcer.flush()
            self._update_braille_monitor()

            key = read_key_nonblocking()
            if key is not None:
                result = self.dispatcher.dispatch(key)
                if not result.handled and result.reason == "unbound_key":
                    self._say("此按鍵未設定功能，按 F1 查看快捷鍵說明。")

            time.sleep(_LOOP_INTERVAL_S)

        self.announcer.flush()
        return 0
