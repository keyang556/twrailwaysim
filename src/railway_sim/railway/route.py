"""進路（路線）模型與路線規則驗證（規格 §10）。

``Route`` 把路網上的一串節點轉換成「沿線里程」座標系，讓列車位置可以用
單一個 ``position_m`` 表示，同時保留節點、分歧與聯絡線的拓撲資訊。

``validate_route`` 實作規格 §10 與 chat.md 中已確認的成功／追分／彰化
路線規則。這些規則屬於 §25 的禁止事項，任何違反都必須在測試中被攔下。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from railway_sim.railway.track import Network

__all__ = [
    "RegionRules",
    "Route",
    "RouteSegment",
    "RouteStop",
    "RouteViolation",
    "validate_route",
]


@dataclass(frozen=True)
class RouteStop:
    """路線上的一個車站點位。"""

    station_id: str
    node_id: str
    position_m: float


@dataclass(frozen=True)
class RouteSegment:
    """路線上的一段區間，帶有該區間的最高允許速度。"""

    from_node_id: str
    to_node_id: str
    start_m: float
    end_m: float
    max_speed_kmh: float
    line_id: str


@dataclass
class Route:
    """一條有方向的行駛路徑。

    Attributes:
        node_ids: 依序經過的節點。
        offsets: 每個節點在本路線上的里程（公尺，起點為 0）。
        segments: 相鄰節點之間的區間，含區間速限。
        stops: 路線上所有車站節點的點位（不代表一定停靠，停靠與否由班次
            的停靠表決定，見 §9.1）。
    """

    id: str
    line_id: str
    direction: str
    node_ids: tuple[str, ...]
    offsets: tuple[float, ...]
    segments: tuple[RouteSegment, ...]
    stops: tuple[RouteStop, ...]
    name_zh_tw: str = ""

    @property
    def length_m(self) -> float:
        return self.offsets[-1] if self.offsets else 0.0

    @property
    def line_ids(self) -> tuple[str, ...]:
        """本路線實際使用到的所有線別（依序，不重複）。"""
        seen: list[str] = []
        for segment in self.segments:
            if segment.line_id not in seen:
                seen.append(segment.line_id)
        return tuple(seen)

    @property
    def station_ids(self) -> tuple[str, ...]:
        return tuple(stop.station_id for stop in self.stops)

    def offset_of(self, node_id: str) -> float:
        index = self.node_ids.index(node_id)
        return self.offsets[index]

    def stop_for_station(self, station_id: str) -> RouteStop | None:
        for stop in self.stops:
            if stop.station_id == station_id:
                return stop
        return None

    def segment_at(self, position_m: float) -> RouteSegment:
        """取得某一里程所在的區間。超出範圍時回傳頭尾區間。"""
        if not self.segments:
            raise ValueError(f"路線 {self.id} 沒有區間資料")
        if position_m <= self.segments[0].start_m:
            return self.segments[0]
        for segment in self.segments:
            if segment.start_m <= position_m < segment.end_m:
                return segment
        return self.segments[-1]

    def line_speed_limit_kmh(self, position_m: float) -> float:
        return self.segment_at(position_m).max_speed_kmh

    def next_stop_ahead(self, position_m: float, *, strict: bool = False) -> RouteStop | None:
        """回傳前方最近的車站點位。

        Args:
            strict: ``False``（預設）時包含列車正好所在的車站；``True`` 時
                只取嚴格在前方的車站，用於「某站至某站間」這類位置描述，
                避免停在車站時報出「臺中至臺中間」。
        """
        threshold = position_m + 1e-6 if strict else position_m - 1e-6
        for stop in self.stops:
            if stop.position_m >= threshold:
                return stop
        return None

    def previous_stop(self, position_m: float) -> RouteStop | None:
        previous: RouteStop | None = None
        for stop in self.stops:
            if stop.position_m <= position_m + 1e-6:
                previous = stop
            else:
                break
        return previous

    # ------------------------------------------------------------------
    # 建構
    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        route_id: str,
        line_id: str,
        direction: str,
        node_ids: list[str],
        network: Network,
        *,
        name_zh_tw: str = "",
        speed_overrides: dict[str, float] | None = None,
    ) -> Route:
        """由路網與節點序列建立路線。

        Raises:
            ValueError: 節點序列在路網上不可通行（含倒插方向限制）。
        """
        ok, reason = network.is_traversable(node_ids)
        if not ok:
            raise ValueError(f"路線 {route_id} 不可通行：{reason}")

        overrides = speed_overrides or {}
        offsets: list[float] = [0.0]
        segments: list[RouteSegment] = []
        cursor = 0.0
        for index in range(len(node_ids) - 1):
            link = network.link(node_ids[index], node_ids[index + 1])
            assert link is not None  # is_traversable 已保證
            limit = overrides.get(link.id, link.max_speed_kmh)
            segments.append(
                RouteSegment(
                    from_node_id=link.from_node_id,
                    to_node_id=link.to_node_id,
                    start_m=cursor,
                    end_m=cursor + link.length_m,
                    max_speed_kmh=limit,
                    line_id=link.line_id,
                )
            )
            cursor += link.length_m
            offsets.append(cursor)

        stops: list[RouteStop] = []
        for node_id, offset in zip(node_ids, offsets, strict=True):
            node = network.node(node_id)
            if node.node_type == "station" and node.station_id:
                stops.append(
                    RouteStop(station_id=node.station_id, node_id=node_id, position_m=offset)
                )

        return cls(
            id=route_id,
            line_id=line_id,
            direction=direction,
            node_ids=tuple(node_ids),
            offsets=tuple(offsets),
            segments=tuple(segments),
            stops=tuple(stops),
            name_zh_tw=name_zh_tw,
        )


# ----------------------------------------------------------------------
# 路線規則驗證（規格 §10、§25.6–§25.9）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RouteViolation:
    """一項路線規則違反。"""

    rule: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 便利用途
        return f"[{self.rule}] {self.message}"


@dataclass(frozen=True)
class RegionRules:
    """臺中地區路線規則所需的車站與線別代碼。

    集中定義的目的是讓 §10 的規則以資料表達，而不是散落在程式中的字串。
    """

    chenggong_station_id: str = "CHENGGONG"
    zhuifen_station_id: str = "ZHUIFEN"
    changhua_station_id: str = "CHANGHUA"
    coast_line_id: str = "coast"
    mountain_line_id: str = "mountain"
    connector_line_id: str = "chengzhui"
    taichung_area_station_ids: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"TAICHUNG", "DAGING", "WURI", "XINWURI", "TAIYUAN", "TANZIH", "FONGYUAN"}
        )
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> RegionRules:
        if not raw:
            return cls()
        return cls(
            chenggong_station_id=raw.get("chenggong_station_id", "CHENGGONG"),
            zhuifen_station_id=raw.get("zhuifen_station_id", "ZHUIFEN"),
            changhua_station_id=raw.get("changhua_station_id", "CHANGHUA"),
            coast_line_id=raw.get("coast_line_id", "coast"),
            mountain_line_id=raw.get("mountain_line_id", "mountain"),
            connector_line_id=raw.get("connector_line_id", "chengzhui"),
            taichung_area_station_ids=frozenset(raw.get("taichung_area_station_ids", ())),
        )


def validate_route(
    route: Route,
    network: Network,
    rules: RegionRules | None = None,
) -> list[RouteViolation]:
    """驗證一條路線是否符合規格 §10 的成功／追分／彰化規則。

    Returns:
        違反項目清單。空清單表示通過。
    """
    rules = rules or RegionRules()
    violations: list[RouteViolation] = []

    # --- 基本拓撲 -----------------------------------------------------
    ok, reason = network.is_traversable(list(route.node_ids))
    if not ok:
        violations.append(RouteViolation("topology", f"路線 {route.id} 拓撲不合法：{reason}"))
        return violations

    stations = route.station_ids
    if not stations:
        return violations

    origin, destination = stations[0], stations[-1]
    used_lines = set(route.line_ids)
    uses_coast = rules.coast_line_id in used_lines
    uses_mountain = rules.mountain_line_id in used_lines
    has_chenggong = rules.chenggong_station_id in stations
    has_zhuifen = rules.zhuifen_station_id in stations
    has_changhua = rules.changhua_station_id in stations
    to_taichung_area = destination in rules.taichung_area_station_ids
    from_taichung_area = origin in rules.taichung_area_station_ids

    # --- §10.2 海線往彰化：不進成功 -----------------------------------
    if uses_coast and destination == rules.changhua_station_id and has_chenggong:
        violations.append(
            RouteViolation(
                "coast_to_changhua_via_chenggong",
                f"路線 {route.id} 為海線往彰化，不得經由成功站"
                "（規格 §10.2、§25.8）。正確為海線→追分→彰化。",
            )
        )

    # --- §10.3 海線往臺中：不經彰化 -----------------------------------
    if uses_coast and to_taichung_area and has_changhua:
        violations.append(
            RouteViolation(
                "coast_to_taichung_via_changhua",
                f"路線 {route.id} 為海線往臺中方向，不得經由彰化站"
                "（規格 §10.3、§25.9）。正確為海線→追分→成功方向→臺中地區。",
            )
        )

    # --- §10.4 臺中／豐原往海線：先成功再追分，不進彰化 ----------------
    if from_taichung_area and uses_coast:
        if has_changhua:
            violations.append(
                RouteViolation(
                    "taichung_to_coast_via_changhua",
                    f"路線 {route.id} 由臺中地區往海線，不得經由彰化站"
                    "（規格 §10.4）。正確為臺中→成功→追分→海線。",
                )
            )
        if not (has_chenggong and has_zhuifen):
            violations.append(
                RouteViolation(
                    "taichung_to_coast_missing_connector",
                    f"路線 {route.id} 由臺中地區往海線，必須經由成功站方向"
                    "轉往追分接入海線（規格 §10.4）。",
                )
            )
        elif stations.index(rules.chenggong_station_id) > stations.index(
            rules.zhuifen_station_id
        ):
            violations.append(
                RouteViolation(
                    "taichung_to_coast_wrong_order",
                    f"路線 {route.id} 由臺中地區往海線時，成功應在追分之前"
                    "（規格 §10.4）。",
                )
            )

    # --- chat.md：純山線列車不得經追分 --------------------------------
    if uses_mountain and not uses_coast and has_zhuifen:
        violations.append(
            RouteViolation(
                "mountain_via_zhuifen",
                f"路線 {route.id} 為山線列車，不得經由追分"
                "（chat.md 五：山線列車禁止經追分）。",
            )
        )

    return violations
