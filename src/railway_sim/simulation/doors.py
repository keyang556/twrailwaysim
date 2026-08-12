"""車門操作與連鎖（規格 §16.2 出發流程、§20.2 車門聲）。

司機員模式提供的是**整側**車門的開關（OpenBVE 的 ``DOORS_LEFT`` 與
``DOORS_RIGHT``，預設鍵 F5／F6）。單門開啟、最後一門確認屬於列車長模式
（§16.1），第二階段之後才實作。

連鎖規則
--------

1. 列車未停妥不得開門。
2. 車門開啟中不得加電門（§16.2：關門確認在出發之前）。

**沒有**實作「開門側必須是月台側」：月台配置與月台側別沒有可靠的公開
來源（§27），與其猜一個假資料再據以判違規，不如由玩家自行選邊，把可以
確定的規則做對。
"""

from __future__ import annotations

from dataclasses import dataclass

from railway_sim.simulation.train import Train

__all__ = ["DOOR_SIDES", "DoorChange", "close_doors", "open_doors", "set_doors"]

#: 可操作的車門側別。
DOOR_SIDES = ("left", "right")


@dataclass(frozen=True)
class DoorChange:
    """一次車門操作的結果。

    Attributes:
        accepted: 操作是否被接受；被連鎖擋下時為 ``False``。
        side: 操作的側別。
        opened: 操作後該側是否為開啟狀態。
        reason: 被擋下的原因（``not_stopped``／``unknown_side``），
            或已處於目標狀態時的 ``already``。
    """

    accepted: bool
    side: str
    opened: bool
    reason: str | None = None


def set_doors(train: Train, side: str, *, opening: bool) -> DoorChange:
    """把某一側車門設為開或關。"""
    if side not in DOOR_SIDES:
        return DoorChange(False, side, False, reason="unknown_side")

    attribute = "left_doors_open" if side == "left" else "right_doors_open"
    current: bool = getattr(train, attribute)

    if opening and not train.is_stopped:
        return DoorChange(False, side, current, reason="not_stopped")
    if current == opening:
        return DoorChange(False, side, current, reason="already")

    setattr(train, attribute, opening)
    return DoorChange(True, side, opening)


def open_doors(train: Train, side: str) -> DoorChange:
    """開啟某一側車門。列車未停妥時被擋下。"""
    return set_doors(train, side, opening=True)


def close_doors(train: Train, side: str) -> DoorChange:
    """關閉某一側車門。任何時候都可以關。"""
    return set_doors(train, side, opening=False)
