"""聯鎖系統（規格 §13）。

排列進路前必須檢查（§13.1）：

1. 前方閉塞是否空閒
2. 道岔是否可移動
3. 道岔是否已被占用
4. 是否存在衝突進路
5. 目標股道是否可用
6. 列車方向是否正確

安全規則（§13.2）：

- 列車占用道岔時不可轉換道岔
- 衝突進路不可同時開放
- 未鎖閉道岔不可開放號誌
- 進路建立後不得任意改變
- 列車完全通過後才能解除進路

衝突以「資源」表示：每個分歧節點屬於一個 ``conflict_group``（同一組道岔
或交叉），進路鎖定其行經的所有 conflict group 與閉塞區間。兩條進路只要
共用任一資源即為衝突，因此不需要人工列舉衝突配對。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from railway_sim.railway.block import BlockSystem
from railway_sim.railway.route import Route
from railway_sim.railway.signal import SignalSystem
from railway_sim.railway.track import Network

__all__ = ["Interlocking", "RouteLock", "RouteRequest", "RouteResult"]


@dataclass(frozen=True)
class RouteRequest:
    """一次進路申請。"""

    id: str
    train_id: str
    route: Route
    from_node_id: str
    to_node_id: str
    entry_signal_id: str | None = None

    def path_node_ids(self) -> tuple[str, ...]:
        """申請範圍內的節點序列。"""
        node_ids = self.route.node_ids
        start = node_ids.index(self.from_node_id)
        end = node_ids.index(self.to_node_id)
        if end < start:
            raise ValueError(
                f"進路 {self.id} 的方向不正確：{self.from_node_id} 在 {self.to_node_id} 之後"
            )
        return node_ids[start : end + 1]


@dataclass(frozen=True)
class RouteLock:
    """已建立且鎖閉的進路。"""

    request: RouteRequest
    resources: frozenset[str]
    start_m: float
    end_m: float

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def train_id(self) -> str:
        return self.request.train_id


@dataclass(frozen=True)
class RouteResult:
    """進路申請結果。"""

    granted: bool
    lock: RouteLock | None = None
    reason: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover - 便利用途
        return self.granted


@dataclass
class Interlocking:
    """單一路線範圍的聯鎖。"""

    network: Network
    blocks: BlockSystem
    signals: SignalSystem
    locks: dict[str, RouteLock] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 資源計算
    # ------------------------------------------------------------------
    def _resources_for(self, request: RouteRequest) -> frozenset[str]:
        """進路需要鎖定的資源：閉塞區間 + 道岔衝突群組。"""
        route = request.route
        node_ids = request.path_node_ids()
        start_m = route.offset_of(node_ids[0])
        end_m = route.offset_of(node_ids[-1])

        resources: set[str] = set()
        for block in self.blocks.blocks:
            if block.overlaps(start_m, end_m):
                resources.add(f"block:{block.id}")
        for node_id in node_ids:
            group = self.network.node(node_id).conflict_group
            if group:
                resources.add(f"switch:{group}")
        return frozenset(resources)

    def _switch_nodes(self, request: RouteRequest) -> list[str]:
        return [
            node_id
            for node_id in request.path_node_ids()
            if self.network.node(node_id).node_type == "junction"
        ]

    # ------------------------------------------------------------------
    # 申請與解除
    # ------------------------------------------------------------------
    def request_route(self, request: RouteRequest) -> RouteResult:
        """依 §13.1 檢查後建立進路。"""
        if request.id in self.locks:
            return RouteResult(False, reason=f"進路 {request.id} 已存在，建立後不得重複排列")

        # 6. 列車方向是否正確 / 路徑是否可通行（含倒插方向限制）
        try:
            node_ids = list(request.path_node_ids())
        except ValueError as exc:
            return RouteResult(False, reason=str(exc))

        ok, why = self.network.is_traversable(node_ids)
        if not ok:
            return RouteResult(False, reason=f"進路方向不正確：{why}")

        route = request.route
        start_m = route.offset_of(node_ids[0])
        end_m = route.offset_of(node_ids[-1])

        # 1. 前方閉塞是否空閒 / 5. 目標股道是否可用
        for block in self.blocks.blocks:
            if not block.overlaps(start_m, end_m):
                continue
            occupiers = block.occupiers_excluding(request.train_id)
            if occupiers:
                return RouteResult(
                    False,
                    reason=f"閉塞區間 {block.id} 已由列車 "
                    f"{'、'.join(occupiers)} 占用，不可排列進路",
                )

        # 2./3. 道岔是否可移動、是否已被占用
        for node_id in self._switch_nodes(request):
            occupier = self.switch_occupier(
                route, node_id, ignore_train_id=request.train_id
            )
            if occupier is not None:
                return RouteResult(
                    False,
                    reason=f"道岔 {node_id} 由列車 {occupier} 占用中，不可轉換",
                )

        # 4. 是否存在衝突進路
        resources = self._resources_for(request)
        for existing in self.locks.values():
            shared = resources & existing.resources
            if shared:
                return RouteResult(
                    False,
                    reason=f"與進路 {existing.id} 衝突，共用資源："
                    + "、".join(sorted(shared)),
                )

        lock = RouteLock(request=request, resources=resources, start_m=start_m, end_m=end_m)
        self.locks[lock.id] = lock

        # §13.2：道岔鎖閉後才可開放號誌
        if request.entry_signal_id:
            self.signals.signal(request.entry_signal_id).forced_stop = False
        return RouteResult(True, lock=lock)

    #: 判斷道岔是否被占用時，道岔前後視為同一區域的容許誤差（公尺）。
    SWITCH_MARGIN_M = 1.0

    def switch_occupier(
        self,
        route: Route,
        node_id: str,
        *,
        ignore_train_id: str | None = None,
    ) -> str | None:
        """回傳占用該道岔的列車代碼；沒有列車占用時為 ``None``。

        道岔位置通常正好落在閉塞分界上，因此前後各取 ``SWITCH_MARGIN_M``
        的範圍判斷，避免車尾仍壓在道岔上卻被判定為已淨空（§13.2）。
        """
        position = route.offset_of(node_id)
        low = position - self.SWITCH_MARGIN_M
        high = position + self.SWITCH_MARGIN_M
        for block in self.blocks.blocks:
            if not block.overlaps(low, high):
                continue
            occupiers = block.occupiers_excluding(ignore_train_id)
            if occupiers:
                return occupiers[0]
        return None

    def can_release(self, lock_id: str, train_rear_m: float) -> bool:
        """列車車尾完全通過進路終點後才可解除（§13.2）。"""
        lock = self.locks.get(lock_id)
        if lock is None:
            return False
        return train_rear_m >= lock.end_m

    def release_route(self, lock_id: str, *, train_rear_m: float | None = None) -> RouteResult:
        """解除進路。

        Args:
            lock_id: 進路代碼。
            train_rear_m: 列車車尾里程。提供時會強制檢查列車是否已完全通過。
        """
        lock = self.locks.get(lock_id)
        if lock is None:
            return RouteResult(False, reason=f"沒有進路 {lock_id}")
        if train_rear_m is not None and not self.can_release(lock_id, train_rear_m):
            return RouteResult(False, reason="列車尚未完全通過，不可解除進路")
        del self.locks[lock_id]
        if lock.request.entry_signal_id:
            self.signals.signal(lock.request.entry_signal_id).forced_stop = True
        return RouteResult(True, lock=lock)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    def active_route_ids(self) -> list[str]:
        return sorted(self.locks)

    def conflicts_with_active(self, request: RouteRequest) -> list[str]:
        """回傳與此申請衝突的既有進路代碼。"""
        resources = self._resources_for(request)
        return sorted(
            lock.id for lock in self.locks.values() if resources & lock.resources
        )
