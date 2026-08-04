"""路網拓撲（規格 §6.3、§10.5）。

規格 §10.5 明確禁止把路網簡化成 ``station_a -> station_b -> station_c``
的線性清單，必須建立車站節點、分歧節點、聯絡線節點、方向性連線與衝突
進路。本模組即為該要求的實作。

關鍵設計：**分歧節點的可行轉向**
----------------------------------

成功站南方的成追線接續是「倒插」（chat.md：「海線倒插在成功南、彰化北」），
也就是聯絡線朝北接入山線。因此列車由追分方向進入該分歧後，只能繼續往
成功方向，不能直接轉向彰化。這個限制無法用「有連線就能通行」表達，必須
在分歧節點上以 ``allowed_transitions`` 明確列出可行的轉向組合。

有了這層限制，下列規則就成為拓撲本身的性質，而不是額外的特例判斷：

- 海線往彰化：追分 → 彰化（不進成功）
- 海線往臺中：追分 → 成功方向 → 臺中（不經彰化）
- 臺中往海線：成功 → 追分 → 海線（不進彰化）
- 彰化往海線：彰化 → 追分（不進成功）
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any

__all__ = ["NODE_TYPES", "Link", "Network", "Node"]

#: ``node_type`` 的允許值（規格 §6.3）。
NODE_TYPES = frozenset(
    {"station", "junction", "signal", "block_boundary", "operational_point"}
)


@dataclass(frozen=True)
class Node:
    """路網節點（規格 §6.3 RouteNode）。

    Attributes:
        allowed_transitions: 分歧節點的可行轉向，元素為
            ``(進入來源節點, 離開目的節點)``。空集合表示不限制。
        conflict_group: 佔用同一組道岔／交叉的節點群組名稱。同一群組的
            進路不得同時開放（§13.2）。
    """

    id: str
    node_type: str
    station_id: str | None = None
    line_ids: tuple[str, ...] = ()
    allowed_transitions: frozenset[tuple[str, str]] = frozenset()
    conflict_group: str | None = None

    def __post_init__(self) -> None:
        if self.node_type not in NODE_TYPES:
            raise ValueError(f"節點 {self.id} 的 node_type 無效：{self.node_type}")

    def permits(self, from_node_id: str, to_node_id: str) -> bool:
        """由 ``from_node_id`` 進入本節點後，是否可前往 ``to_node_id``。"""
        if not self.allowed_transitions:
            return True
        return (from_node_id, to_node_id) in self.allowed_transitions


@dataclass(frozen=True)
class Link:
    """兩節點之間的方向性連線。

    ``Network`` 只儲存單向連線；雙向區間在載入時展開成兩條 ``Link``，
    使方向性成為模型的一級概念（§10.5）。
    """

    from_node_id: str
    to_node_id: str
    length_m: float
    line_id: str
    max_speed_kmh: float
    track_id: str | None = None
    verification_status: str = "test_data"

    @property
    def id(self) -> str:
        return f"{self.from_node_id}->{self.to_node_id}"


@dataclass
class Network:
    """有向路網圖。"""

    nodes: dict[str, Node] = field(default_factory=dict)
    links: dict[str, Link] = field(default_factory=dict)
    _outgoing: dict[str, list[Link]] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------
    # 建構
    # ------------------------------------------------------------------
    def add_node(self, node: Node) -> None:
        if node.id in self.nodes:
            raise ValueError(f"節點重複：{node.id}")
        self.nodes[node.id] = node
        self._outgoing.setdefault(node.id, [])

    def add_link(self, link: Link) -> None:
        for node_id in (link.from_node_id, link.to_node_id):
            if node_id not in self.nodes:
                raise ValueError(f"連線 {link.id} 參照到不存在的節點：{node_id}")
        if link.id in self.links:
            raise ValueError(f"連線重複：{link.id}")
        self.links[link.id] = link
        self._outgoing[link.from_node_id].append(link)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise KeyError(f"路網中沒有節點：{node_id}") from None

    def link(self, from_node_id: str, to_node_id: str) -> Link | None:
        return self.links.get(f"{from_node_id}->{to_node_id}")

    def outgoing(self, node_id: str) -> list[Link]:
        return list(self._outgoing.get(node_id, ()))

    def station_node_ids(self) -> list[str]:
        return [n.id for n in self.nodes.values() if n.node_type == "station"]

    def node_for_station(self, station_id: str) -> Node | None:
        for node in self.nodes.values():
            if node.node_type == "station" and node.station_id == station_id:
                return node
        return None

    # ------------------------------------------------------------------
    # 通行判斷
    # ------------------------------------------------------------------
    def is_traversable(self, node_ids: list[str]) -> tuple[bool, str | None]:
        """檢查一串節點是否構成合法的行駛路徑。

        除了每一段都必須有方向性連線之外，還要檢查中間節點的
        ``allowed_transitions``（倒插方向限制）。

        Returns:
            ``(是否合法, 錯誤說明)``。合法時錯誤說明為 ``None``。
        """
        if len(node_ids) < 2:
            return False, "路徑至少需要兩個節點"

        for node_id in node_ids:
            if node_id not in self.nodes:
                return False, f"路徑包含不存在的節點：{node_id}"

        for index in range(len(node_ids) - 1):
            current, nxt = node_ids[index], node_ids[index + 1]
            if self.link(current, nxt) is None:
                return False, f"{current} 至 {nxt} 之間沒有方向性連線"

        for index in range(1, len(node_ids) - 1):
            prev, current, nxt = node_ids[index - 1], node_ids[index], node_ids[index + 1]
            if not self.node(current).permits(prev, nxt):
                return False, f"節點 {current} 不允許由 {prev} 轉向 {nxt}"

        return True, None

    def path_length_m(self, node_ids: list[str]) -> float:
        total = 0.0
        for index in range(len(node_ids) - 1):
            link = self.link(node_ids[index], node_ids[index + 1])
            if link is None:
                raise ValueError(f"{node_ids[index]} 至 {node_ids[index + 1]} 沒有連線")
            total += link.length_m
        return total

    def shortest_path(self, start_id: str, goal_id: str) -> list[str] | None:
        """在遵守轉向限制的前提下尋找最短路徑。

        狀態為 ``(前一節點, 目前節點)``，才能正確套用 ``allowed_transitions``。
        主要供測試與資料驗證使用；實際運轉的進路由 ``routes.json`` 明確定義，
        不依賴自動尋路（§15.2：AI 不得任意變更山線或海線）。
        """
        self.node(start_id)
        self.node(goal_id)
        if start_id == goal_id:
            return [start_id]

        queue: list[tuple[float, int, str | None, str, tuple[str, ...]]] = [
            (0.0, 0, None, start_id, (start_id,))
        ]
        best: dict[tuple[str | None, str], float] = {(None, start_id): 0.0}
        counter = 1

        while queue:
            cost, _, prev_id, current_id, path = heapq.heappop(queue)
            if current_id == goal_id:
                return list(path)
            if cost > best.get((prev_id, current_id), float("inf")):
                continue
            for link in self.outgoing(current_id):
                if prev_id is not None and not self.node(current_id).permits(
                    prev_id, link.to_node_id
                ):
                    continue
                if link.to_node_id in path:  # 不繞回頭
                    continue
                new_cost = cost + link.length_m
                key = (current_id, link.to_node_id)
                if new_cost < best.get(key, float("inf")):
                    best[key] = new_cost
                    heapq.heappush(
                        queue,
                        (new_cost, counter, current_id, link.to_node_id, path + (link.to_node_id,)),
                    )
                    counter += 1
        return None

    # ------------------------------------------------------------------
    # 載入
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Network:
        network = cls()
        for node_raw in raw.get("nodes", ()):
            transitions = frozenset(
                (t["from"], t["to"]) for t in node_raw.get("allowed_transitions", ())
            )
            network.add_node(
                Node(
                    id=node_raw["id"],
                    node_type=node_raw["node_type"],
                    station_id=node_raw.get("station_id"),
                    line_ids=tuple(node_raw.get("line_ids", ())),
                    allowed_transitions=transitions,
                    conflict_group=node_raw.get("conflict_group"),
                )
            )

        for link_raw in raw.get("links", ()):
            forward = Link(
                from_node_id=link_raw["from"],
                to_node_id=link_raw["to"],
                length_m=float(link_raw["length_m"]),
                line_id=link_raw["line_id"],
                max_speed_kmh=float(link_raw["max_speed_kmh"]),
                track_id=link_raw.get("track_id"),
                verification_status=link_raw.get("verification_status", "test_data"),
            )
            network.add_link(forward)
            if link_raw.get("bidirectional", True):
                network.add_link(
                    Link(
                        from_node_id=link_raw["to"],
                        to_node_id=link_raw["from"],
                        length_m=forward.length_m,
                        line_id=forward.line_id,
                        max_speed_kmh=forward.max_speed_kmh,
                        track_id=forward.track_id,
                        verification_status=forward.verification_status,
                    )
                )
        return network
