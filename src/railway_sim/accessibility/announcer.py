"""統一的無障礙播報介面（規格 §21）。

設計重點：

- 高優先級訊息可中斷尚未播出的低優先級訊息（§21.1）。
- 連續按鍵不得造成語音訊息塞車（§7.2）：相同 ``dedupe_key`` 在冷卻時間內
  只播報一次。
- 重要訊息需支援重複播報（§2.1）：``repeat_last()``。
- 播報內容一律為純文字，不依賴顏色、圖示或動畫（§2.1）。

``Announcer`` 本身不負責輸出，只負責排程與去重；實際輸出由 sink
（callable）處理，因此可在測試中完全離線驗證。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

__all__ = ["Announcement", "Announcer", "Priority"]


class Priority(IntEnum):
    """播報優先級（規格 §21.1）。"""

    STATUS = 0
    """一般狀態，例如定期速度更新。"""

    ACTION = 1
    """操作確認，例如電門段位變更。"""

    NOTICE = 2
    """重要提醒，例如接近停車站。"""

    SAFETY = 3
    """安全警告，例如超速。"""

    EMERGENCY = 4
    """緊急事件，例如緊急制軔、冒進號誌。"""


#: 優先級達到此值時，會清空尚未播出的較低優先級訊息。
INTERRUPT_THRESHOLD = Priority.SAFETY

#: 相同 dedupe_key 的預設冷卻秒數。
DEFAULT_DEDUPE_SECONDS = 3.0


@dataclass(frozen=True)
class Announcement:
    """一則已排入佇列的播報。"""

    text: str
    priority: Priority
    at: float
    dedupe_key: str | None = None

    def __str__(self) -> str:  # pragma: no cover - 便利用途
        return self.text


@dataclass
class Announcer:
    """播報排程器。

    Args:
        sink: 實際輸出訊息的 callable。預設為 ``None``，表示只保留在
            ``history`` 中（測試用）。
        clock: 取得目前時間的 callable，預設 ``time.monotonic``。注入時鐘可
            讓去重邏輯在測試中完全決定性。
        dedupe_seconds: 相同 ``dedupe_key`` 的冷卻秒數。
        history_limit: ``history`` 保留的訊息數量上限。
    """

    sink: Callable[[Announcement], None] | None = None
    clock: Callable[[], float] = time.monotonic
    dedupe_seconds: float = DEFAULT_DEDUPE_SECONDS
    history_limit: int = 200

    _pending: deque[Announcement] = field(default_factory=deque, init=False, repr=False)
    _history: list[Announcement] = field(default_factory=list, init=False, repr=False)
    _last_spoken_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------
    # 排程
    # ------------------------------------------------------------------
    def announce(
        self,
        message: str,
        priority: Priority | int = Priority.STATUS,
        *,
        dedupe_key: str | None = None,
    ) -> bool:
        """排入一則播報。

        Args:
            message: 播報文字（繁體中文）。
            priority: 優先級，見 :class:`Priority`。
            dedupe_key: 去重鍵。相同鍵在 ``dedupe_seconds`` 內只會播報一次，
                用於避免重複狀態播報過度冗長（§7.2）。傳入 ``None`` 表示
                不去重。

        Returns:
            ``True`` 表示訊息已排入佇列；``False`` 表示被去重規則抑制。
        """
        if not message:
            return False

        priority = Priority(priority)
        now = self.clock()

        if dedupe_key is not None:
            last = self._last_spoken_at.get(dedupe_key)
            if last is not None and now - last < self.dedupe_seconds:
                return False
            self._last_spoken_at[dedupe_key] = now

        item = Announcement(text=message, priority=priority, at=now, dedupe_key=dedupe_key)

        if priority >= INTERRUPT_THRESHOLD:
            # 安全警告與緊急事件會中斷尚未播出的一般訊息。
            self._pending = deque(a for a in self._pending if a.priority >= INTERRUPT_THRESHOLD)

        self._pending.append(item)
        return True

    # ------------------------------------------------------------------
    # 輸出
    # ------------------------------------------------------------------
    def flush(self) -> list[Announcement]:
        """把佇列中的訊息送到 sink，並回傳這批訊息。"""
        batch = list(self._pending)
        self._pending.clear()
        for item in batch:
            self._history.append(item)
            if self.sink is not None:
                self.sink(item)
        if len(self._history) > self.history_limit:
            del self._history[: len(self._history) - self.history_limit]
        return batch

    def repeat_last(self, count: int = 1) -> list[Announcement]:
        """重新播報最近 ``count`` 則已播出的訊息（§2.1 重複播報）。

        重播不受去重限制，也不會再次寫入歷史。
        """
        if count <= 0 or not self._history:
            return []
        batch = self._history[-count:]
        if self.sink is not None:
            for item in batch:
                self.sink(item)
        return batch

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    @property
    def pending(self) -> tuple[Announcement, ...]:
        """尚未送出的訊息。"""
        return tuple(self._pending)

    @property
    def history(self) -> tuple[Announcement, ...]:
        """已送出的訊息（最舊在前）。"""
        return tuple(self._history)

    def texts(self) -> list[str]:
        """已送出訊息的純文字，便於測試斷言。"""
        return [a.text for a in self._history]

    def clear_history(self) -> None:
        self._history.clear()
