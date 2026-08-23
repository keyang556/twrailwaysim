"""音檔播放後端（規格 §20）。

**播放能力是選用的（§20.1、§2.3）。** 廣播內容一律同時以文字送出，因此
一個後端都沒有時遊戲照常可玩，只是不出聲；本模組的任何一個函式都不會
因為缺少後端、缺少檔案或播放失敗而拋出例外。

支援的後端
----------

``pygame``
    有安裝 ``pygame`` 時優先使用。它內建 SDL_mixer，可直接播放 Ogg
    Vorbis，也不需要外部程式。以 ``pip install railway-sim[audio]`` 安裝。

``ffplay``
    ``ffmpeg`` 隨附的播放器，在 ``PATH`` 上就能用。臺鐵廣播是 Ogg Vorbis，
    而 Windows 內建的播放元件（wxPython 用的 Media Foundation）並不解
    Vorbis，因此這個後端是沒有裝 ``pygame`` 時的實際主力。

一次只播一則
------------

廣播動輒數十秒（臺北的「下一站」廣播就有四十六秒），站間距離卻可能更短。
若直接疊著播，兩則廣播會同時出聲，什麼都聽不清楚。因此播放採單一佇列，
由背景執行緒依序播放；:meth:`AudioPlayer.stop` 會清空佇列並中止目前這則，
供「到站廣播必須蓋過還沒播完的下一站廣播」這類情況使用。

循環播放
--------

「請勿上車」是**在車門開著的整段時間裡持續提醒**的廣播（提醒沒有買這班車
的旅客不要上車），因此 :meth:`AudioPlayer.play` 收 ``loop=True``：播完自己
再排一次，直到 :meth:`AudioPlayer.stop` 為止。同一時間只會有一則循環廣播。
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AudioPlayer",
    "FfplayBackend",
    "PlayerCreation",
    "PygameBackend",
    "create_player",
    "create_player_with_diagnostics",
]

#: 背景執行緒輪詢「這一則播完了沒」的間隔（秒）。
_POLL_INTERVAL_S = 0.05

#: 佇列最多保留幾則尚未播出的廣播。
#:
#: 廣播很長，堆積太多只會讓內容嚴重落後於列車實際位置，不如丟掉。
_QUEUE_LIMIT = 4


class _Backend:
    """播放後端介面。"""

    name = "none"

    def start(self, path: Path) -> bool:
        """開始播放，立即回傳。回傳是否成功送出。"""
        raise NotImplementedError

    def is_busy(self) -> bool:
        """目前是否還在播。"""
        raise NotImplementedError

    def stop(self) -> None:
        """中止目前這一則。"""
        raise NotImplementedError

    def close(self) -> None:
        self.stop()

    def can_load(self, path: Path) -> bool:
        """確認後端能否讀取音檔，不開始播放。"""
        return path.is_file()


class PygameBackend(_Backend):
    """以 ``pygame.mixer`` 播放。"""

    name = "pygame"

    def __init__(self) -> None:
        import pygame

        self._pygame = pygame
        pygame.mixer.init()

    def start(self, path: Path) -> bool:
        try:
            self._pygame.mixer.music.load(str(path))
            self._pygame.mixer.music.play()
        except Exception:  # noqa: BLE001 - 播放失敗不得影響運轉
            return False
        return True

    def can_load(self, path: Path) -> bool:
        try:
            self._pygame.mixer.music.load(str(path))
        except Exception:  # noqa: BLE001 - 診斷不可讓遊戲失敗
            return False
        return True

    def is_busy(self) -> bool:
        try:
            return bool(self._pygame.mixer.music.get_busy())
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        # 停止播放失敗沒有任何補救動作，也不該打斷運轉；記錄下來只會在
        # 主控台介面污染畫面，反而蓋掉真正要看的播報（§7.2）。
        try:
            self._pygame.mixer.music.stop()
        except Exception:  # noqa: BLE001, S110
            pass

    def close(self) -> None:
        self.stop()
        try:
            self._pygame.mixer.quit()
        except Exception:  # noqa: BLE001, S110
            pass


class FfplayBackend(_Backend):
    """以 ``ffplay`` 子行程播放。"""

    name = "ffplay"

    def __init__(self, executable: str) -> None:
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, path: Path) -> bool:
        self.stop()
        # Windows 上若不指定旗標，每播一則就會閃出一個主控台視窗。
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                [
                    self._executable,
                    "-nodisp",
                    "-autoexit",
                    "-loglevel",
                    "quiet",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError:
            self._process = None
            return False
        return True

    def is_busy(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:  # pragma: no cover - 行程已結束
            return
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 極少發生
            process.kill()


class AudioPlayer:
    """依序播放音檔的播放器。

    以背景執行緒服務佇列，因此 :meth:`play` 立即回傳，模擬迴圈不會被
    數十秒的廣播卡住。
    """

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._stopping = threading.Event()
        self._closed = False
        self._loop_path: Path | None = None
        self._worker = threading.Thread(
            target=self._run, name="railway-sim-audio", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------
    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def available(self) -> bool:
        return not self._closed

    def can_load(self, path: str | Path) -> bool:
        """確認目前後端可讀取音檔，但不播放它。"""
        if self._closed:
            return False
        target = Path(path)
        return target.is_file() and self._backend.can_load(target)

    # ------------------------------------------------------------------
    def play(
        self, path: str | Path, *, interrupt: bool = False, loop: bool = False
    ) -> bool:
        """排入一則音檔。

        Args:
            interrupt: ``True`` 時先清掉佇列並中止目前這一則，讓新的立刻播。
            loop: ``True`` 時反覆播放同一則，直到 :meth:`stop` 為止。用於
                「請勿上車」這種**在車門開著的整段時間裡持續提醒**的廣播。
                同一時間只會有一則循環廣播，新的會取代舊的。

        Returns:
            是否已排入。檔案不存在或播放器已關閉時回傳 ``False``——這是
            正常狀態（暫時沒有這一站的廣播），呼叫端照樣送出文字即可。
        """
        if self._closed:
            return False
        target = Path(path)
        if not target.is_file():
            return False
        if interrupt:
            self.stop()
        if self._queue.qsize() >= _QUEUE_LIMIT:
            return False
        if loop:
            self._loop_path = target
        self._queue.put(target)
        return True

    def stop(self) -> None:
        """清空佇列、中止目前播放的音檔，並取消循環播放。

        取消循環也在這裡，是因為呼叫 :meth:`stop` 的情境（關門、換一則廣播、
        結束工作階段）沒有一種是「循環那一則應該繼續」。
        """
        self._loop_path = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
        self._stopping.set()
        self._backend.stop()

    @property
    def looping(self) -> bool:
        """目前是否有循環播放中的音檔。"""
        return self._loop_path is not None

    def close(self) -> None:
        """關閉播放器並結束背景執行緒。可重複呼叫。"""
        if self._closed:
            return
        self._closed = True
        self.stop()
        self._queue.put(None)
        self._worker.join(timeout=2.0)
        self._backend.close()

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._stopping.clear()
                if not self._backend.start(item):
                    continue
                while self._backend.is_busy() and not self._stopping.is_set():
                    time.sleep(_POLL_INTERVAL_S)
                if self._stopping.is_set():
                    self._backend.stop()
                elif self._loop_path == item:
                    # 循環播放：播完再排一次自己。停止是由 stop() 清掉
                    # _loop_path 達成的，因此不需要另一個旗標。
                    self._queue.put(item)
            except Exception:  # noqa: BLE001, S112 - 背景執行緒不得讓遊戲掛掉
                continue
            finally:
                self._queue.task_done()


def _find_ffplay() -> str | None:
    return shutil.which("ffplay")


@dataclass(frozen=True)
class PlayerCreation:
    """建立播放後端的結果，以及可供診斷使用的退回原因。"""

    player: AudioPlayer | None
    diagnostics: tuple[str, ...]


def _exception_diagnostic(backend: str, error: Exception) -> str:
    detail = str(error).strip()
    suffix = f": {detail}" if detail else ""
    return f"{backend} unavailable ({type(error).__name__}){suffix}"


def create_player_with_diagnostics() -> PlayerCreation:
    """建立播放器並保留後端選擇過程的診斷資訊。

    回傳 ``None`` **不是錯誤**：廣播內容仍會以文字送出（§20.1）。
    """
    diagnostics: list[str] = []
    # 沒裝 pygame、或有裝但這台機器沒有音效裝置：兩者都只代表「用下一個
    # 後端；一般遊戲不會拋出例外，但保留原因供啟動提示和 --check-audio 使用。
    try:
        return PlayerCreation(AudioPlayer(PygameBackend()), tuple(diagnostics))
    except Exception as error:  # noqa: BLE001 - 音效裝置錯誤屬正常 fallback
        diagnostics.append(_exception_diagnostic("pygame", error))

    executable = _find_ffplay()
    if executable is not None:
        diagnostics.append(f"using ffplay fallback: {executable}")
        return PlayerCreation(
            AudioPlayer(FfplayBackend(executable)), tuple(diagnostics)
        )
    diagnostics.append("ffplay not found on PATH")
    return PlayerCreation(None, tuple(diagnostics))


def create_player() -> AudioPlayer | None:
    """建立播放器；沒有任何可用後端時回傳 ``None``。

    新程式若要顯示或記錄退回原因，請改用
    :func:`create_player_with_diagnostics`。保留此函式以相容既有呼叫端。
    """
    return create_player_with_diagnostics().player
