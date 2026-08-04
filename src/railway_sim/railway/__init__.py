"""路網領域模型：車站、路網、進路、號誌、閉塞、聯鎖。"""

from railway_sim.railway.block import Block, BlockSystem
from railway_sim.railway.interlocking import (
    Interlocking,
    RouteLock,
    RouteRequest,
    RouteResult,
)
from railway_sim.railway.route import Route, RouteStop, RouteViolation, validate_route
from railway_sim.railway.signal import Signal, SignalAspect, SignalSystem
from railway_sim.railway.station import Platform, Station
from railway_sim.railway.track import Link, Network, Node

__all__ = [
    "Block",
    "BlockSystem",
    "Interlocking",
    "Link",
    "Network",
    "Node",
    "Platform",
    "Route",
    "RouteLock",
    "RouteRequest",
    "RouteResult",
    "RouteStop",
    "RouteViolation",
    "Signal",
    "SignalAspect",
    "SignalSystem",
    "Station",
    "validate_route",
]
