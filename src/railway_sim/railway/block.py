"""閉塞系統（規格 §12）。

基本規則：每一閉塞區間同一時間原則上只能有一列列車（§12.1）。

本模組把閉塞區間建立在路線的里程座標上。閉塞分界取自路網中的車站節點、
分歧節點與 ``block_boundary`` 節點，因此閉塞長度來自實際路網資料，而不是
任意切割。

規格 §12.2 與 §12.3 要求：正常時刻表不應頻繁出現站外等待。本模組只提供
占用判斷；站外等待是否發生，由 AI 與事件系統依「異常事件」決定，
不得當作日常高頻事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from railway_sim.railway.route import Route
from railway_sim.railway.track import Network

__all__ = ["Block", "BlockSystem"]

#: 會成為閉塞分界的節點型別。
_BOUNDARY_NODE_TYPES = frozenset({"station", "junction", "block_boundary"})


@dataclass
class Block:
    """閉塞區間（規格 §12.1）。

    正常運轉下同一區間只會有一列列車，但模型必須能表達「兩列列車同時占用
    同一區間」這個異常狀態，否則後進入的列車會覆蓋前一列的占用紀錄，使
    號誌誤判為空閒。因此內部以清單保存所有占用者，
    ``occupied_by_train_id`` 維持規格 §12.1 的欄位語意，回傳最先進入者。
    """

    id: str
    start_m: float
    end_m: float
    direction: str | None = None
    occupying_train_ids: list[str] = field(default_factory=list)

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m

    @property
    def occupied_by_train_id(self) -> str | None:
        """主要占用列車（規格 §12.1 欄位）。"""
        return self.occupying_train_ids[0] if self.occupying_train_ids else None

    @property
    def is_occupied(self) -> bool:
        return bool(self.occupying_train_ids)

    @property
    def has_conflict(self) -> bool:
        """是否有一列以上的列車同時占用（異常狀態）。"""
        return len(self.occupying_train_ids) > 1

    def occupiers_excluding(self, train_id: str | None) -> list[str]:
        return [t for t in self.occupying_train_ids if t != train_id]

    def contains(self, position_m: float) -> bool:
        return self.start_m <= position_m < self.end_m

    def overlaps(self, rear_m: float, front_m: float) -> bool:
        return front_m > self.start_m and rear_m < self.end_m


@dataclass
class BlockSystem:
    """一條路線上的閉塞區間集合。"""

    blocks: list[Block] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 建構
    # ------------------------------------------------------------------
    @classmethod
    def from_route(cls, route: Route, network: Network) -> BlockSystem:
        """依路網節點型別在路線上切出閉塞區間。"""
        boundaries: list[tuple[str, float]] = []
        for node_id, offset in zip(route.node_ids, route.offsets, strict=True):
            if network.node(node_id).node_type in _BOUNDARY_NODE_TYPES:
                boundaries.append((node_id, offset))

        blocks: list[Block] = []
        for index in range(len(boundaries) - 1):
            (from_id, start), (to_id, end) = boundaries[index], boundaries[index + 1]
            if end - start <= 0:
                continue
            blocks.append(
                Block(
                    id=f"BLK_{from_id}_{to_id}",
                    start_m=start,
                    end_m=end,
                    direction=route.direction,
                )
            )
        return cls(blocks=blocks)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    def block(self, block_id: str) -> Block:
        for block in self.blocks:
            if block.id == block_id:
                return block
        raise KeyError(f"沒有閉塞區間：{block_id}")

    def block_at(self, position_m: float) -> Block | None:
        for block in self.blocks:
            if block.contains(position_m):
                return block
        return None

    def index_at(self, position_m: float) -> int | None:
        for index, block in enumerate(self.blocks):
            if block.contains(position_m):
                return index
        return None

    def block_after(self, block_id: str) -> Block | None:
        for index, block in enumerate(self.blocks):
            if block.id == block_id:
                return self.blocks[index + 1] if index + 1 < len(self.blocks) else None
        return None

    def blocks_for_train(self, train_id: str) -> list[Block]:
        return [b for b in self.blocks if train_id in b.occupying_train_ids]

    def is_free(self, block_id: str, *, ignore_train_id: str | None = None) -> bool:
        """該區間是否空閒。``ignore_train_id`` 用於忽略自己造成的占用。"""
        return not self.block(block_id).occupiers_excluding(ignore_train_id)

    # ------------------------------------------------------------------
    # 占用
    # ------------------------------------------------------------------
    def update_occupancy(self, train_id: str, rear_m: float, front_m: float) -> list[Block]:
        """更新某列車的閉塞占用。

        列車進入時標記占用，離開時解除（chat.md v1.6-1）。列車跨越分界時會
        同時占用兩個區間，直到車尾完全通過為止。

        Args:
            train_id: 列車代碼。
            rear_m: 車尾里程。
            front_m: 車頭里程。

        Returns:
            目前被此列車占用的區間。
        """
        if front_m < rear_m:
            rear_m, front_m = front_m, rear_m

        for block in self.blocks:
            occupied = train_id in block.occupying_train_ids
            if block.overlaps(rear_m, front_m):
                if not occupied:
                    block.occupying_train_ids.append(train_id)
            elif occupied:
                block.occupying_train_ids.remove(train_id)
        return self.blocks_for_train(train_id)

    def release(self, train_id: str) -> None:
        """列車離開路線時解除其所有占用。"""
        for block in self.blocks:
            if train_id in block.occupying_train_ids:
                block.occupying_train_ids.remove(train_id)

    def occupied_summary(self) -> dict[str, str]:
        """回傳 ``{區間代碼: 主要占用列車}``，供調度員介面與測試使用。"""
        return {
            b.id: b.occupied_by_train_id
            for b in self.blocks
            if b.occupied_by_train_id is not None
        }

    def conflicting_blocks(self) -> list[Block]:
        """回傳同時被一列以上列車占用的區間（異常狀態）。"""
        return [b for b in self.blocks if b.has_conflict]
