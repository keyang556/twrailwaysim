"""極簡事件匯流排。

用途是讓模擬核心與介面層解耦：核心只發佈事件，wx 介面、主控台介面與測試
各自訂閱。介面層因此無法成為唯一的資訊來源（規格 §25.5）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Event", "EventBus"]


@dataclass(frozen=True)
class Event:
    """一則事件。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Handler = Callable[[Event], None]


@dataclass
class EventBus:
    """同步事件匯流排。"""

    _handlers: dict[str, list[Handler]] = field(
        default_factory=lambda: defaultdict(list), init=False, repr=False
    )
    _any_handlers: list[Handler] = field(default_factory=list, init=False, repr=False)
    history: list[Event] = field(default_factory=list, init=False)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """訂閱特定事件型別。"""
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        """訂閱所有事件。"""
        self._any_handlers.append(handler)

    def publish(self, event_type: str, **payload: Any) -> Event:
        """發佈事件並同步呼叫所有處理器。"""
        event = Event(type=event_type, payload=payload)
        self.history.append(event)
        for handler in list(self._handlers.get(event_type, ())):
            handler(event)
        for handler in list(self._any_handlers):
            handler(event)
        return event

    def events_of(self, event_type: str) -> list[Event]:
        """歷史中特定型別的事件，供測試使用。"""
        return [e for e in self.history if e.type == event_type]

    def clear(self) -> None:
        self.history.clear()
