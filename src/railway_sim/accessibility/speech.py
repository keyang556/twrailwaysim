"""選用的螢幕閱讀器輸出後端（語音與點字）。

**重要（規格 §2.3、§20.1）**：本模組屬於「加分」功能，不是必要資訊來源。
所有必要資訊一律以純文字輸出，即使沒有任何後端可用，遊戲仍可完全以螢幕
閱讀器操作。

目前支援的後端：

- NVDA Controller Client：需要 NVDA 官方提供的
  ``nvdaControllerClient.dll``（本專案不散布）。

NVDA Controller Client 的版本差異
---------------------------------

同一個 DLL 依 NVDA 版本提供不同的函式，用不到的那些會回傳
``RPC_S_UNKNOWN_IF``（1717）而不是崩潰。因此本模組**逐一探測**能力，缺哪
一項就少用哪一項，不做版本號比對——玩家電腦上的 NVDA 是哪一版我們不知道，
問 DLL 本人最準：

===================================  =========  ====================
函式                                  起始版本    本專案的用途
===================================  =========  ====================
``testIfRunning``／``speakText``       全部       基本語音
``cancelSpeech``                      全部       中斷語音
``brailleMessage``                    全部       點字訊息
``getProcessId``                      2024.1     確認 NVDA 換過人
``speakSsml``                         2024.1     **帶優先級**的語音
``isSpeaking``                        2026.3     查詢是否正在說話
===================================  =========  ====================

``speakSsml`` 是本模組優先使用的路徑（NVDA 2024.1 起，含 2026.2）。它比
「``cancelSpeech`` 之後再 ``speakText``」好的地方在於**優先級**：緊急訊息
用 ``SPEECH_PRIORITY_NOW`` 插播，NVDA 會在插播結束後把被打斷的內容接回去，
而不是像 ``cancelSpeech`` 那樣整個丟掉。對駕駛而言差別很實際——超速警告
不該讓正在唸的「下一站」整句消失。

安全性：DLL 載入
----------------

**絕不以裸檔名載入 DLL。** 把 ``"nvdaControllerClient.dll"`` 直接交給
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

找不到可信任的 DLL 時 :func:`create_screen_reader` 回傳 ``None``，呼叫端
改用文字輸出即可。

安全性：鎖定畫面
----------------

NVDA 官方文件提醒用戶端在送出資訊前確認 Windows 是否處於鎖定或安全桌面，
以免機密外洩。本專案不需要另做檢查：送出去的內容全部是模擬器自己產生的
運轉播報（速度、號誌、站名），本來就沒有任何使用者資料，而且遊戲視窗在
安全桌面上不會執行。
"""

from __future__ import annotations

import ctypes
import os
import sys
from enum import IntEnum
from pathlib import Path
from xml.sax.saxutils import escape

from railway_sim.accessibility.announcer import Priority

__all__ = [
    "DLL_ENV_VAR",
    "NvdaController",
    "ScreenReader",
    "SpeechPriority",
    "SymbolLevel",
    "create_screen_reader",
    "speech_priority_for",
    "trusted_dll_paths",
]

#: 可用此環境變數指定 NVDA Controller Client 的**絕對路徑**。
DLL_ENV_VAR = "RAILWAY_SIM_NVDA_DLL"

#: 內建 DLL 目錄中會嘗試的檔名，依優先順序排列。
#:
#: ``nvdaControllerClient.dll`` 是 NVDA 目前釋出的檔名（依架構放在不同資料夾
#: 裡，檔名本身不帶位元數）；帶 ``64``／``32`` 的是舊版的檔名，仍然留著，
#: 因為玩家手上可能是幾年前抓的那一份。
_DLL_NAMES = (
    "nvdaControllerClient.dll",
    "nvdaControllerClient64.dll",
    "nvdaControllerClient32.dll",
)

#: ``LoadLibraryExW`` 旗標：只在 System32 與該 DLL 自身所在目錄尋找相依項。
_LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_SAFE_WINMODE = _LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | _LOAD_LIBRARY_SEARCH_SYSTEM32

#: 所有函式成功時的回傳值（Windows 標準錯誤碼 ``ERROR_SUCCESS``）。
_OK = 0

#: 這一版 NVDA 沒有實作該介面時的回傳值（``RPC_S_UNKNOWN_IF``）。
#:
#: 這**不是**錯誤，而是「你的 NVDA 比較舊」。例如 ``isSpeaking`` 要到
#: NVDA 2026.3 才有，在 2026.2 上呼叫就會拿到這個值。
_RPC_UNKNOWN_INTERFACE = 1717


class SpeechPriority(IntEnum):
    """``speakSsml`` 的語音優先級（對應 NVDA 的 ``SpeechPriority``）。"""

    NORMAL = 0
    """一般語音，排在佇列後面。"""

    NEXT = 1
    """在目前這一句唸完之後就輪到它，不必等完整個佇列。"""

    NOW = 2
    """立刻插播，並中斷優先級較低的語音。

    與 ``cancelSpeech`` 的關鍵差別：插播結束後，被打斷的內容會由 NVDA
    自己接回去繼續唸，不是整段丟掉。
    """


class SymbolLevel(IntEnum):
    """``speakSsml`` 的標點符號詳細度（對應 NVDA 的 ``SymbolLevel``）。"""

    UNCHANGED = -1
    """沿用使用者自己的設定。播報文字沒有特殊符號，沒有理由蓋過玩家的偏好。"""

    NONE = 0
    SOME = 100
    MOST = 200
    ALL = 300
    CHAR = 1000


def speech_priority_for(priority: Priority | int) -> SpeechPriority:
    """把播報優先級對應到 NVDA 的語音優先級。

    對應關係刻意只有三級，因為 NVDA 也只有三級：

    - 安全警告與緊急事件 → :attr:`SpeechPriority.NOW`（立刻插播，唸完之後
      NVDA 會把被打斷的內容接回去）。
    - 重要提醒（接近車站、停車位置倒數）→ :attr:`SpeechPriority.NEXT`：
      唸完目前這一句就輪到它，不必排在一串電門段位確認後面。倒數的每一句
      只在一兩秒內有意義，排隊等於沒說。
    - 其餘 → :attr:`SpeechPriority.NORMAL`。

    這裡引用 :mod:`~railway_sim.accessibility.announcer` 的優先級而不是自己
    定一組數字：兩邊各定一組遲早會對不起來。反向不會發生（announcer 不認識
    語音後端），因此沒有循環相依。
    """
    if priority >= Priority.SAFETY:
        return SpeechPriority.NOW
    if priority >= Priority.NOTICE:
        return SpeechPriority.NEXT
    return SpeechPriority.NORMAL


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


def _to_ssml(text: str) -> str:
    """把純文字包成最小可用的 SSML。

    只做跳脫，不加任何韻律或發音標記：播報文字是給玩家聽懂的中文句子，
    自作主張調整語速或音高只會蓋掉玩家在 NVDA 裡調好的設定。
    """
    return f"<speak>{escape(text)}</speak>"


class NvdaController:
    """NVDA Controller Client 的極薄包裝。

    每一個方法對應一個 DLL 函式，能力探測在 :meth:`__init__` 完成，因此
    呼叫端不必自己判斷這一版 NVDA 有沒有某個功能。
    """

    def __init__(self) -> None:
        self._dll: ctypes.CDLL | None = None
        self.loaded_from: Path | None = None
        self.supports_ssml = False
        self.supports_process_id = False
        self.supports_is_speaking = False
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

        if self._dll is not None:
            self._declare_signatures()

    # ------------------------------------------------------------------
    def _declare_signatures(self) -> None:
        """宣告每個函式的參數與回傳型別。

        沒宣告的話 ctypes 只會照 Python 值猜，指標與 64 位元整數在 x64 上
        很容易猜錯而踩壞堆疊。舊版 NVDA 的 DLL 沒有新函式的匯出符號，取用
        時會擲出 ``AttributeError``——那正是我們用來判斷「這一版有沒有」的
        依據，比比對版本號可靠。
        """
        dll = self._dll
        assert dll is not None

        for name, argtypes in (
            ("nvdaController_testIfRunning", []),
            ("nvdaController_speakText", [ctypes.c_wchar_p]),
            ("nvdaController_cancelSpeech", []),
            ("nvdaController_brailleMessage", [ctypes.c_wchar_p]),
        ):
            function = getattr(dll, name)
            function.argtypes = argtypes
            function.restype = ctypes.c_ulong

        # --- NVDA 2024.1 起（含 2026.2）---------------------------------
        try:
            speak_ssml = dll.nvdaController_speakSsml
            get_pid = dll.nvdaController_getProcessId
        except AttributeError:
            pass
        else:
            # boolean 在 MIDL 中是 unsigned char，不是四位元組的 BOOL。
            speak_ssml.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_ubyte,
            ]
            speak_ssml.restype = ctypes.c_ulong
            get_pid.argtypes = [ctypes.POINTER(ctypes.c_ulong)]
            get_pid.restype = ctypes.c_ulong
            self.supports_ssml = True
            self.supports_process_id = True

        # --- NVDA 2026.3 起 ---------------------------------------------
        try:
            is_speaking = dll.nvdaController_isSpeaking
        except AttributeError:
            pass
        else:
            is_speaking.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
            is_speaking.restype = ctypes.c_ulong
            self.supports_is_speaking = True

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """NVDA 是否可用。DLL 已載入且 NVDA 正在執行才為 ``True``。"""
        if self._dll is None:
            return False
        try:
            return self._dll.nvdaController_testIfRunning() == _OK
        except OSError:
            return False

    def process_id(self) -> int | None:
        """NVDA 的行程識別碼；取不到時回傳 ``None``。

        用途是判斷 NVDA 是否**中途換過一個**（當掉重開、或使用者切換到
        安全桌面再回來）。換過之後先前送出的點字訊息當然不在了，需要重送。
        """
        if self._dll is None or not self.supports_process_id:
            return None
        pid = ctypes.c_ulong(0)
        try:
            if self._dll.nvdaController_getProcessId(ctypes.byref(pid)) != _OK:
                return None
        except OSError:
            return None
        return int(pid.value)

    def is_speaking(self) -> bool | None:
        """NVDA 目前是否正在說話。

        Returns:
            ``None`` 表示這一版 NVDA 沒有這個功能（2026.3 之前，包含
            2026.2），**不是**「沒有在說話」——兩者不可混為一談。
        """
        if self._dll is None or not self.supports_is_speaking:
            return None
        speaking = ctypes.c_ubyte(0)
        try:
            result = self._dll.nvdaController_isSpeaking(ctypes.byref(speaking))
        except OSError:
            return None
        if result == _RPC_UNKNOWN_INTERFACE:
            # DLL 是新的但 NVDA 是舊的：符號在，介面不在。
            self.supports_is_speaking = False
            return None
        if result != _OK:
            return None
        return bool(speaking.value)

    # ------------------------------------------------------------------
    def speak(
        self,
        text: str,
        *,
        interrupt: bool = False,
        priority: SpeechPriority = SpeechPriority.NORMAL,
    ) -> bool:
        """朗讀一段文字。回傳是否成功送出。

        有 ``speakSsml`` 就走它，把優先級交給 NVDA 排；沒有的話退回
        ``speakText``，並以 ``cancelSpeech`` 近似「立刻插播」。退路的效果
        比較粗糙（被打斷的內容不會接回去），但那是舊版 NVDA 能做到的極限。
        """
        if self._dll is None:
            return False

        if self.supports_ssml:
            try:
                result = self._dll.nvdaController_speakSsml(
                    _to_ssml(text),
                    int(SymbolLevel.UNCHANGED),
                    int(priority),
                    1,  # 非同步：模擬迴圈不能停下來等 NVDA 唸完。
                )
            except OSError:
                return False
            if result == _OK:
                return True
            if result != _RPC_UNKNOWN_INTERFACE:
                return False
            # DLL 有這個符號但 NVDA 太舊，改走 speakText 並且不再重試。
            self.supports_ssml = False

        try:
            if interrupt or priority is SpeechPriority.NOW:
                self._dll.nvdaController_cancelSpeech()
            # 參數型別已在 _declare_signatures 宣告，ctypes 會自己轉成
            # c_wchar_p，不必（也不該）在這裡再包一層。
            return self._dll.nvdaController_speakText(text) == _OK
        except OSError:
            return False

    def braille(self, text: str) -> bool:
        """在點字顯示器上顯示一段訊息。回傳是否成功送出。

        NVDA 把它當成**暫時訊息**：過一段時間（或使用者操作時）就會被目前
        焦點的內容蓋回去。需要它一直看得到的話，呼叫端要定期重送。
        """
        if self._dll is None or not text:
            return False
        try:
            return self._dll.nvdaController_brailleMessage(text) == _OK
        except OSError:
            return False

    def cancel(self) -> bool:
        """中斷目前的語音。"""
        if self._dll is None:
            return False
        try:
            return self._dll.nvdaController_cancelSpeech() == _OK
        except OSError:
            return False


class ScreenReader:
    """螢幕閱讀器輸出（語音＋點字）。

    介面層拿到的就是這個物件。它**可以直接當成 ``speak(text, interrupt)``
    呼叫**，因此原本只送語音的呼叫端不必改寫；要用點字的再多呼叫
    :meth:`braille`。
    """

    def __init__(self, controller: NvdaController) -> None:
        self.controller = controller

    # -- 當成 speak sink 使用 ------------------------------------------
    def __call__(self, text: str, interrupt: bool = False) -> bool:
        return self.speak(text, interrupt=interrupt)

    # ------------------------------------------------------------------
    def speak(
        self,
        text: str,
        *,
        interrupt: bool = False,
        priority: SpeechPriority | None = None,
    ) -> bool:
        """朗讀一段文字。

        ``priority`` 沒給時由 ``interrupt`` 決定：要插播就用
        :attr:`SpeechPriority.NOW`，否則 :attr:`SpeechPriority.NORMAL`。
        """
        if priority is None:
            priority = SpeechPriority.NOW if interrupt else SpeechPriority.NORMAL
        return self.controller.speak(text, interrupt=interrupt, priority=priority)

    def braille(self, text: str) -> bool:
        """在點字顯示器上顯示一段訊息。"""
        return self.controller.braille(text)

    def cancel(self) -> bool:
        return self.controller.cancel()

    # ------------------------------------------------------------------
    @property
    def supports_priority(self) -> bool:
        """能不能把優先級交給 NVDA 排（``speakSsml``，NVDA 2024.1 起）。"""
        return self.controller.supports_ssml

    def status_text(self) -> str:
        """一行說明目前接上了什麼，供行前提要顯示。

        「沒有聲音」有好幾種原因（沒裝 NVDA、沒放 DLL、NVDA 沒開），說清楚
        是哪一種，玩家才不會把正常狀態當成故障。
        """
        if not self.controller.available:
            return "未連接（改以文字輸出，功能不受影響）"
        features = ["語音", "點字"]
        if self.supports_priority:
            features.append("優先級插播")
        return f"NVDA 已連接：{'、'.join(features)}"


def create_screen_reader() -> ScreenReader | None:
    """建立螢幕閱讀器輸出；沒有可用後端時回傳 ``None``。

    Returns:
        :class:`ScreenReader`（可直接當成 ``speak(text, interrupt)`` 呼叫），
        或 ``None``。
    """
    controller = NvdaController()
    if not controller.available:
        return None
    return ScreenReader(controller)
