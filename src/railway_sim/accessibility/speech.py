"""選用的語音輸出後端。

**重要（規格 §2.3、§20.1）**：本模組屬於「加分」功能，不是必要資訊來源。
所有必要資訊一律以純文字輸出，即使沒有任何語音後端可用，遊戲仍可完全
以螢幕閱讀器操作。

目前支援的後端：

- NVDA Controller Client：需要 NVDA 官方提供的
  ``nvdaControllerClient64.dll``（本專案不散布）。

DLL 載入的安全性
----------------

**絕不以裸檔名載入 DLL。** 把 ``"nvdaControllerClient64.dll"`` 直接交給
``LoadLibrary`` 會沿用 Windows 的預設搜尋順序，其中包含應用程式目錄等
可能可被寫入的位置；攻擊者只要在搜尋路徑上放一個同名 DLL，就會被載入
遊戲程序並取得同等權限（DLL 綁架）。

因此本模組：

1. 只從**明確且可信任的絕對路徑**載入：套件內建的 ``lib`` 目錄，或使用者
   以環境變數 :data:`DLL_ENV_VAR` 指定的絕對路徑。
2. 載入前先確認該路徑確實是檔案。
3. 以 ``LOAD_LIBRARY_SEARCH_SYSTEM32`` 與
   ``LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`` 限制相依 DLL 的搜尋範圍，
   不使用預設搜尋順序。

找不到可信任的 DLL 時 :func:`create_speech_sink` 回傳 ``None``，呼叫端
改用文字輸出即可。
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from pathlib import Path

__all__ = ["DLL_ENV_VAR", "NvdaController", "create_speech_sink", "trusted_dll_paths"]

#: 可用此環境變數指定 NVDA Controller Client 的**絕對路徑**。
DLL_ENV_VAR = "RAILWAY_SIM_NVDA_DLL"

#: 內建 DLL 目錄中會嘗試的檔名。
_DLL_NAMES = ("nvdaControllerClient64.dll", "nvdaControllerClient32.dll")

#: ``LoadLibraryExW`` 旗標：只在 System32 與該 DLL 自身所在目錄尋找相依項。
_LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_SAFE_WINMODE = _LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | _LOAD_LIBRARY_SEARCH_SYSTEM32


def _bundled_dll_dir() -> Path:
    """套件內建的 DLL 目錄（隨程式碼安裝，視為可信任）。"""
    return Path(__file__).resolve().parent.parent / "lib"


def trusted_dll_paths() -> list[Path]:
    """回傳允許載入的候選絕對路徑，依優先順序排列。

    環境變數指定的路徑必須是絕對路徑；相對路徑會被忽略，因為它會隨行程的
    工作目錄改變，等同重新引入不受控的搜尋行為。
    """
    override = os.environ.get(DLL_ENV_VAR)
    if override:
        candidate = Path(override)
        return [candidate] if candidate.is_absolute() else []

    bundled = _bundled_dll_dir()
    return [bundled / name for name in _DLL_NAMES]


class NvdaController:
    """NVDA Controller Client 的極薄包裝。"""

    def __init__(self) -> None:
        self._dll: ctypes.CDLL | None = None
        self.loaded_from: Path | None = None
        if sys.platform != "win32":
            return

        for path in trusted_dll_paths():
            if not path.is_file():
                continue
            try:
                # 以絕對路徑載入並限制相依 DLL 的搜尋範圍，
                # 不使用 Windows 預設搜尋順序。
                self._dll = ctypes.WinDLL(str(path), winmode=_SAFE_WINMODE)  # type: ignore[attr-defined]
                self.loaded_from = path
                break
            except OSError:
                continue

    @property
    def available(self) -> bool:
        """NVDA 是否可用。DLL 已載入且 NVDA 正在執行才為 ``True``。"""
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
