"""號誌系統（規格 §11）。

第一版採用三顯示簡化號誌：

- ``stop``（停止）：禁止越過。
- ``caution``（注意）：必須準備減速或停車。
- ``clear``（平常）：可依路線速限行駛。

號誌顯示由前方閉塞占用狀況推導：防護區間被占用顯示停止，再前一個區間被
占用顯示注意，否則顯示平常。聯鎖系統可另行強制扣停號誌（§13.2：未鎖閉
道岔不可開放號誌）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from railway_sim.railway.block import BlockSystem
from railway_sim.railway.route import Route

__all__ = ["Signal", "SignalAspect", "SignalSystem"]


class SignalAspect(StrEnum):
    """號誌顯示（規格 §11.1）。"""

    STOP = "stop"
    CAUTION = "caution"
    CLEAR = "clear"


#: 各顯示對應的允許速度上限（公里／小時）。``None`` 表示依路線速限。
#: 此為第一版測試值，非臺鐵實際數值（規格 §27）。
ASPECT_SPEED_LIMIT_KMH: dict[SignalAspect, float | None] = {
    SignalAspect.STOP: 0.0,
    SignalAspect.CAUTION: 60.0,
    SignalAspect.CLEAR: None,
}


@dataclass
class Signal:
    """一架號誌機。

    Attributes:
        position_m: 號誌在路線上的里程。
        protected_block_id: 越過本號誌後隨即進入的閉塞區間。
        forced_stop: 由聯鎖或調度扣停。為 ``True`` 時一律顯示停止。
    """

    id: str
    name_zh_tw: str
    position_m: float
    protected_block_id: str
    forced_stop: bool = False

    def __str__(self) -> str:  # pragma: no cover - 便利用途
        return self.name_zh_tw or self.id


@dataclass
class SignalSystem:
    """一條路線上的號誌集合。"""

    signals: list[Signal] = field(default_factory=list)
    blocks: BlockSystem | None = None

    # ------------------------------------------------------------------
    # 建構
    # ------------------------------------------------------------------
    @classmethod
    def from_blocks(
        cls,
        route: Route,
        blocks: BlockSystem,
        *,
        station_names: dict[str, str] | None = None,
    ) -> SignalSystem:
        """在每個閉塞區間的起點設置一架號誌。

        車站節點處的號誌命名為「某站進站號誌」，其餘為區間號誌。
        """
        names = station_names or {}
        node_by_offset = {
            round(offset, 3): node_id
            for node_id, offset in zip(route.node_ids, route.offsets, strict=True)
        }
        station_by_node = {stop.node_id: stop.station_id for stop in route.stops}

        signals: list[Signal] = []
        for index, block in enumerate(blocks.blocks):
            node_id = node_by_offset.get(round(block.start_m, 3))
            station_id = station_by_node.get(node_id) if node_id else None
            if station_id:
                label = f"{names.get(station_id, station_id)}站出發號誌"
            else:
                label = f"第{index + 1}區間號誌"
            signals.append(
                Signal(
                    id=f"SIG_{block.id}",
                    name_zh_tw=label,
                    position_m=block.start_m,
                    protected_block_id=block.id,
                )
            )
        return cls(signals=signals, blocks=blocks)

    # ------------------------------------------------------------------
    # 顯示推導
    # ------------------------------------------------------------------
    def aspect_of(self, signal: Signal, *, ignore_train_id: str | None = None) -> SignalAspect:
        """推導號誌顯示。

        Args:
            signal: 目標號誌。
            ignore_train_id: 忽略此列車造成的占用（列車不會被自己擋住）。
        """
        if signal.forced_stop:
            return SignalAspect.STOP
        if self.blocks is None:
            return SignalAspect.CLEAR

        if not self.blocks.is_free(signal.protected_block_id, ignore_train_id=ignore_train_id):
            return SignalAspect.STOP

        following = self.blocks.block_after(signal.protected_block_id)
        if following is not None and not self.blocks.is_free(
            following.id, ignore_train_id=ignore_train_id
        ):
            return SignalAspect.CAUTION

        return SignalAspect.CLEAR

    def permitted_speed_kmh(
        self,
        signal: Signal,
        line_limit_kmh: float,
        *,
        ignore_train_id: str | None = None,
    ) -> float:
        """該號誌顯示下的允許速度（規格 §11.2）。"""
        aspect = self.aspect_of(signal, ignore_train_id=ignore_train_id)
        cap = ASPECT_SPEED_LIMIT_KMH[aspect]
        return line_limit_kmh if cap is None else min(cap, line_limit_kmh)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    def signal(self, signal_id: str) -> Signal:
        for item in self.signals:
            if item.id == signal_id:
                return item
        raise KeyError(f"沒有號誌：{signal_id}")

    def next_signal_ahead(self, position_m: float) -> Signal | None:
        """回傳列車前方最近的一架號誌。"""
        candidates = [s for s in self.signals if s.position_m > position_m + 1e-6]
        return min(candidates, key=lambda s: s.position_m) if candidates else None

    def governing_signal(self, position_m: float) -> Signal | None:
        """回傳目前正在管制列車的號誌（列車最後越過的那一架）。"""
        candidates = [s for s in self.signals if s.position_m <= position_m + 1e-6]
        return max(candidates, key=lambda s: s.position_m) if candidates else None
