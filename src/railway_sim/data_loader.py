"""資料檔載入與驗證（規格 §24 M0「建立資料讀取器」）。

載入時會一併執行資料驗證：

- 每條路線的拓撲是否可通行（含成追線倒插方向限制）
- 每條路線是否符合規格 §10 的成功／追分／彰化規則
- 每個班次的停靠表是否違反車站的車種停靠規則（§25.7）

驗證結果放在 :attr:`GameData.issues`，由 ``tests/test_route_rules.py`` 與
``tests/test_station_stop.py`` 強制為空。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from railway_sim.railway.route import RegionRules, Route, validate_route
from railway_sim.railway.station import Station
from railway_sim.railway.track import Network
from railway_sim.simulation.train import TrainType
from railway_sim.timetable.service import Service
from railway_sim.timetable.stop_pattern import validate_service

__all__ = ["GameData", "default_data_dir", "load_game_data"]

#: 可用環境變數覆寫資料目錄，方便測試與封裝。
DATA_DIR_ENV = "RAILWAY_SIM_DATA_DIR"

_REQUIRED_FILES = ("stations.json", "routes.json", "trains.json", "timetables.json", "keymap.json")


def _contains_required_data(directory: Path) -> bool:
    """Return whether *directory* has a complete automatic data set."""
    return all((directory / name).is_file() for name in _REQUIRED_FILES)


def default_data_dir() -> Path:
    """尋找 ``data`` 目錄。

    順序：環境變數 → 由本檔向上尋找含 ``stations.json`` 的 ``data`` 目錄。
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)

    # Frozen builds may carry a complete data directory beside the executable.
    # This supports portable deployments without masking bundled release data.
    if getattr(sys, "frozen", False):
        executable_data = Path(sys.executable).resolve().parent / "data"
        if _contains_required_data(executable_data):
            return executable_data

    # PyInstaller exposes its resource directory through _MEIPASS for both
    # one-file and one-folder builds. This fallback keeps portable builds
    # self-contained when no user data directory exists yet.
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidate = Path(bundle_root) / "data"
        if _contains_required_data(candidate):
            return candidate

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data"
        if _contains_required_data(candidate):
            return candidate
    raise FileNotFoundError("找不到 data 目錄，請設定環境變數 " + DATA_DIR_ENV)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class GameData:
    """載入後的完整遊戲資料。"""

    data_dir: Path
    stations: dict[str, Station]
    network: Network
    routes: dict[str, Route]
    region_rules: RegionRules
    train_types: dict[str, TrainType]
    service_classes: dict[str, str]
    services: dict[str, Service]
    line_names: dict[str, str]
    keymap_raw: dict[str, Any]
    issues: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def station_names(self) -> dict[str, str]:
        return {sid: s.name_zh_tw for sid, s in self.stations.items()}

    def service(self, train_number: str) -> Service:
        try:
            return self.services[train_number]
        except KeyError:
            raise KeyError(f"沒有車次：{train_number}") from None

    def route(self, route_id: str) -> Route:
        try:
            return self.routes[route_id]
        except KeyError:
            raise KeyError(f"沒有路線：{route_id}") from None

    def train_type(self, type_id: str) -> TrainType:
        try:
            return self.train_types[type_id]
        except KeyError:
            raise KeyError(f"沒有車輛型式：{type_id}") from None

    def service_class_name(self, class_id: str) -> str:
        return self.service_classes.get(class_id, class_id)

    def raise_on_issues(self) -> None:
        """有資料問題時擲出例外，供正式啟動時 fail fast。"""
        if self.issues:
            raise ValueError("資料驗證未通過：\n" + "\n".join(f"- {i}" for i in self.issues))


def load_game_data(data_dir: str | Path | None = None) -> GameData:
    """載入 ``data`` 目錄下的所有資料檔並執行驗證。"""
    directory = Path(data_dir) if data_dir is not None else default_data_dir()
    missing = [name for name in _REQUIRED_FILES if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{directory} 缺少資料檔：{'、'.join(missing)}")

    issues: list[str] = []

    # --- 車站 ---------------------------------------------------------
    stations_raw = _read_json(directory / "stations.json")
    stations = {
        raw["id"]: Station.from_dict(raw) for raw in stations_raw.get("stations", ())
    }

    # --- 路網與路線 ---------------------------------------------------
    routes_raw = _read_json(directory / "routes.json")
    network = Network.from_dict(routes_raw)
    region_rules = RegionRules.from_dict(routes_raw.get("region_rules"))
    line_names = {
        line_id: info.get("name_zh_tw", line_id)
        for line_id, info in routes_raw.get("lines", {}).items()
    }

    routes: dict[str, Route] = {}
    for raw in routes_raw.get("routes", ()):
        try:
            route = Route.build(
                route_id=raw["id"],
                line_id=raw["line_id"],
                direction=raw["direction"],
                node_ids=list(raw["node_ids"]),
                network=network,
                name_zh_tw=raw.get("name_zh_tw", ""),
            )
        except ValueError as exc:
            issues.append(str(exc))
            continue
        routes[route.id] = route
        for violation in validate_route(route, network, region_rules):
            issues.append(str(violation))

    # 路網中每個車站節點都必須對應到 stations.json 的車站
    for node in network.nodes.values():
        if node.node_type == "station" and node.station_id not in stations:
            issues.append(f"路網節點 {node.id} 參照到不存在的車站：{node.station_id}")

    # --- 車輛型式與車種 -----------------------------------------------
    trains_raw = _read_json(directory / "trains.json")
    train_types = {
        raw["id"]: TrainType.from_dict(raw) for raw in trains_raw.get("train_types", ())
    }
    service_classes = {
        raw["id"]: raw["name_zh_tw"] for raw in trains_raw.get("service_classes", ())
    }

    # --- 班次 ---------------------------------------------------------
    timetables_raw = _read_json(directory / "timetables.json")
    services: dict[str, Service] = {}
    for raw in timetables_raw.get("services", ()):
        service = Service.from_dict(raw)
        services[service.train_number] = service
        issues.extend(validate_service(service, stations))
        if service.route_id not in routes:
            issues.append(f"班次 {service.train_number} 參照到不存在的路線：{service.route_id}")
        if service.rolling_stock_id and service.rolling_stock_id not in train_types:
            issues.append(
                f"班次 {service.train_number} 參照到不存在的車輛型式："
                f"{service.rolling_stock_id}"
            )
        if service.train_type not in service_classes:
            issues.append(
                f"班次 {service.train_number} 參照到不存在的車種：{service.train_type}"
            )
        route = routes.get(service.route_id)
        if route is not None:
            unknown = [
                sid
                for sid in (*service.stop_station_ids, *service.pass_station_ids)
                if sid not in route.station_ids
            ]
            if unknown:
                issues.append(
                    f"班次 {service.train_number} 的停靠表包含不在路線上的車站："
                    + "、".join(unknown)
                )

    # --- 鍵位 ---------------------------------------------------------
    keymap_raw = _read_json(directory / "keymap.json")

    return GameData(
        data_dir=directory,
        stations=stations,
        network=network,
        routes=routes,
        region_rules=region_rules,
        train_types=train_types,
        service_classes=service_classes,
        services=services,
        line_names=line_names,
        keymap_raw=keymap_raw,
        issues=issues,
    )
