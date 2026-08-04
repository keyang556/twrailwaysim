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
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession

__all__ = ["ConsoleApp", "read_key_nonblocking"]

#: 主迴圈的實際更新間隔（秒）。
_LOOP_INTERVAL_S = 0.05

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

    # ------------------------------------------------------------------
    def _emit(self, announcement: Announcement) -> None:
        print(announcement.text, flush=True)
        if self.speak is not None:
            self.speak(announcement.text, announcement.priority >= Priority.SAFETY)

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

    def pause_menu(self) -> None:
        """Esc：暫停選單。"""
        print("\n===== 暫停選單 =====", flush=True)
        print("1：繼續運轉", flush=True)
        print("2：快捷鍵說明", flush=True)
        print("3：列車狀態", flush=True)
        print("4：離開遊戲", flush=True)
        print("請按 1 到 4。", flush=True)

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
                self.running = False
                print("離開遊戲。", flush=True)
                return
            else:
                print("請按 1 到 4。", flush=True)

    # ------------------------------------------------------------------
    def announce_intro(self) -> None:
        service = self.session.service
        class_name = self.session.data.service_class_name(service.train_type)
        print("===== 臺灣鐵路人員模擬器：司機員模式 =====", flush=True)
        print(f"車次：{class_name}{service.train_number}次", flush=True)
        print(f"路線：{self.session.route.name_zh_tw}", flush=True)
        print(f"路線長度：{self.session.route.length_m:.0f} 公尺", flush=True)
        stops = "、".join(
            p.name_zh_tw for p in self.session.stations if p.stop_kind == "stop"
        )
        passes = "、".join(
            p.name_zh_tw for p in self.session.stations if p.stop_kind != "stop"
        )
        print(f"停靠站：{stops or '無'}", flush=True)
        print(f"通過站：{passes or '無'}", flush=True)
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

            key = read_key_nonblocking()
            if key is not None:
                result = self.dispatcher.dispatch(key)
                if not result.handled and result.reason == "unbound_key":
                    self._say("此按鍵未設定功能，按 F1 查看快捷鍵說明。")

            time.sleep(_LOOP_INTERVAL_S)

        self.announcer.flush()
        return 0
