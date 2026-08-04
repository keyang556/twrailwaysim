"""列車位置描述（規格 §4.1、§21.2）。

規格 §2.1 要求「避免需要玩家依賴視覺地圖判斷列車位置」，因此位置一律以
「某站至某站間，距離某站多少」的方式描述，不使用座標或地圖。
"""

from __future__ import annotations

from dataclasses import dataclass

from railway_sim.railway.route import Route

__all__ = ["PositionReport", "describe_position"]


@dataclass(frozen=True)
class PositionReport:
    """可直接轉成播報文字的位置資訊。"""

    line_name: str
    from_station_id: str | None
    from_station_name: str
    to_station_id: str | None
    to_station_name: str
    distance_to_next_m: float
    distance_from_previous_m: float
    position_m: float


def describe_position(
    route: Route,
    position_m: float,
    station_names: dict[str, str],
    line_names: dict[str, str] | None = None,
) -> PositionReport:
    """描述列車在路線上的位置。

    Args:
        route: 目前路線。
        position_m: 車頭里程。
        station_names: ``{車站代碼: 中文站名}``。
        line_names: ``{線別代碼: 中文線名}``。
    """
    lines = line_names or {}
    segment = route.segment_at(position_m)
    line_name = lines.get(segment.line_id, route.name_zh_tw or route.line_id)

    previous = route.previous_stop(position_m)
    following = route.next_stop_ahead(position_m, strict=True)

    if previous is None:
        from_id, from_name, from_pos = None, "起點", 0.0
    else:
        from_id = previous.station_id
        from_name = station_names.get(previous.station_id, previous.station_id)
        from_pos = previous.position_m

    if following is None:
        to_id, to_name, to_pos = None, "終點", route.length_m
    else:
        to_id = following.station_id
        to_name = station_names.get(following.station_id, following.station_id)
        to_pos = following.position_m

    return PositionReport(
        line_name=line_name,
        from_station_id=from_id,
        from_station_name=from_name,
        to_station_id=to_id,
        to_station_name=to_name,
        distance_to_next_m=max(0.0, to_pos - position_m),
        distance_from_previous_m=max(0.0, position_m - from_pos),
        position_m=position_m,
    )
