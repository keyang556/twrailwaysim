"""選用的語音輸出後端。

**重要（規格 §2.3、§20.1）**：本模組屬於「加分」功能，不是必要資訊來源。
所有必要資訊一律以純文字輸出，即使沒有任何語音後端可用，遊戲仍可完全
以螢幕閱讀器操作。

目前支援的後端：

- NVDA Controller Client：需要系統上存在 ``nvdaControllerClient64.dll``
  （NVDA 官方提供，本專案不散布）。找不到時自動停用。

未偵測到後端時 :func:`create_speech_sink` 回傳 ``None``，呼叫端應改用文字
輸出。
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable

__all__ = ["NvdaController", "create_speech_sink"]


class NvdaController:
    """NVDA Controller Client 的極薄包裝。"""

    DLL_NAMES = ("nvdaControllerClient64.dll", "nvdaControllerClient32.dll")

    def __init__(self) -> None:
        self._dll: ctypes.CDLL | None = None
        if sys.platform != "win32":
            return
        for name in self.DLL_NAMES:
            try:
                self._dll = ctypes.windll.LoadLibrary(name)  # type: ignore[attr-defined]
                break
            except OSError:
                continue

    @property
    def available(self) -> bool:
        """NVDA 是否可用。DLL 存在且 NVDA 正在執行才為 ``True``。"""
        if self._dll is None:
            return False
        try:
            return self._dll.nvdaController_testIfRunning() == 0
        except OSError:
            return False

    def speak(self, text: str, *, interrupt: bool = False) -> bool:
        """朗讀一段文字。回傳是否成功送出。"""
        if self._dll is None:
            return False
        try:
            if interrupt:
                self._dll.nvdaController_cancelSpeech()
            return self._dll.nvdaController_speakText(ctypes.c_wchar_p(text)) == 0
        except OSError:
            return False


def create_speech_sink() -> Callable[[str, bool], bool] | None:
    """建立語音輸出函式；沒有可用後端時回傳 ``None``。

    Returns:
        ``speak(text, interrupt) -> bool``，或 ``None``。
    """
    controller = NvdaController()
    if not controller.available:
        return None

    def speak(text: str, interrupt: bool = False) -> bool:
        return controller.speak(text, interrupt=interrupt)

    return speak
