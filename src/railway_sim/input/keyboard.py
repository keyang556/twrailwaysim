"""按鍵派送（規格 §7.2）。

規格要求：

- 按鍵操作不得要求長時間按住 → 每個動作都是一次按鍵完成的離散操作。
- 每次按鍵都應提供文字回饋 → 未綁定的按鍵也會回覆說明，不會靜默。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from railway_sim.input.keymap import Keymap, normalise_key

__all__ = ["DispatchResult", "KeyDispatcher"]


@dataclass(frozen=True)
class DispatchResult:
    """一次按鍵派送的結果。"""

    handled: bool
    action: str | None = None
    reason: str | None = None


@dataclass
class KeyDispatcher:
    """把按鍵轉成動作呼叫。"""

    keymap: Keymap
    handlers: dict[str, Callable[[], None]] = field(default_factory=dict)

    def register(self, action: str, handler: Callable[[], None]) -> None:
        """註冊一個動作的處理器。"""
        if self.keymap.binding_for(action) is None:
            raise KeyError(f"鍵位表中沒有動作：{action}")
        self.handlers[action] = handler

    def register_all(self, handlers: dict[str, Callable[[], None]]) -> None:
        for action, handler in handlers.items():
            self.register(action, handler)

    def unbound_actions(self) -> list[str]:
        """鍵位表中尚未註冊處理器的動作。"""
        return [a for a in self.keymap.actions if a not in self.handlers]

    def dispatch(self, key: str) -> DispatchResult:
        """派送一次按鍵。

        Returns:
            :class:`DispatchResult`。``handled`` 為 ``False`` 時，``reason``
            會說明原因，呼叫端應把它播報出來（§7.2 每次按鍵都有回饋）。
        """
        normalised = normalise_key(key)
        if not normalised:
            return DispatchResult(False, reason="no_key")

        action = self.keymap.action_for(normalised)
        if action is None:
            return DispatchResult(False, reason="unbound_key")

        handler = self.handlers.get(action)
        if handler is None:
            return DispatchResult(False, action=action, reason="no_handler")

        handler()
        return DispatchResult(True, action=action)
