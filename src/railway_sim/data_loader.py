"""資料檔載入與驗證（規格 §24 M0「建立資料讀取器」）。

載入時會一併執行資料驗證：

- 每條路線的拓撲是否可通行（含成追線倒插方向限制）
- 每條路線是否符合規格 §10 的成功／追分／彰化規則
- 每個班次的停靠表是否違反車站的車種停靠規則（§25.7）

驗證結果放在 :attr:`GameData.issues`，由 ``tests/test_route_rules.py`` 與
``tests/test_station_stop.py`` 強制為空。

臺鐵與捷運兩套資料
------------------

同一組模型可以載入兩個系統的資料，差別只在目錄：臺鐵在 ``data/``、捷運在
``data/mrt/``（見 :mod:`railway_sim.systems`）。鍵位表與廣播音檔兩邊共用，
因此在系統目錄裡找不到 ``keymap.json`` 時會退回上層的共用版本。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from railway_sim.audio.library import BroadcastLibrary, default_announcement_dir
from railway_sim.audio.mrt_broadcast import BROADCAST_RULES_FILENAME, MrtBroadcastRules
from railway_sim.railway.route import RegionRules, Route, validate_route
from railway_sim.railway.station import Station
from railway_sim.railway.track import Network
from railway_sim.simulation.train import TrainType
from railway_sim.systems import DEFAULT_SYSTEM, SYSTEMS, RailSystem, system_data_dir
from railway_sim.timetable.service import Service
from railway_sim.timetable.stop_pattern import validate_service

__all__ = [
    "IMPORT_STAGING_PREFIX",
    "GameData",
    "LineInfo",
    "default_data_dir",
    "heal_interrupted_import",
    "load_game_data",
]

#: 可用環境變數覆寫資料目錄，方便測試與封裝。
DATA_DIR_ENV = "RAILWAY_SIM_DATA_DIR"

#: 每個系統目錄都要有的資料檔。
#:
#: ``keymap.json`` 不在其中：鍵位兩邊共用，捷運目錄裡沒有自己的一份是正常的
#: （見 :func:`_read_keymap`）。
_REQUIRED_FILES = ("stations.json", "routes.json", "trains.json", "timetables.json")

#: ``data`` 目錄（而不是某個系統的子目錄）必須具備的資料檔。
#:
#: 比 :data:`_REQUIRED_FILES` 多一個共用的 ``keymap.json``：自動尋找資料目錄
#: 時要找的是**整套可以直接開起來的資料**，缺鍵位表的目錄不算。
ROOT_REQUIRED_FILES = (*_REQUIRED_FILES, "keymap.json")

#: ``railway_sim.dataset.build.write_dataset`` 匯入時使用的暫存目錄前綴。
#:
#: 兩邊必須用同一個常數：匯入過程逐檔以 ``os.replace`` 取代正式檔案時，
#: 如果整個行程被強制中止（斷電、kill -9），Python 的 ``try/except`` 完全
#: 攔不到——正式目錄可能停在「部分檔案已是新版、部分仍是舊版」的狀態。
#: :func:`heal_interrupted_import` 就是用這個前綴找出這類殘留，在下一次
#: 讀取或匯入時自動修復回一致的舊版本。
IMPORT_STAGING_PREFIX = ".import-staging-"


def _contains_required_data(directory: Path) -> bool:
    """Return whether *directory* has a complete automatic data set."""
    return all((directory / name).is_file() for name in ROOT_REQUIRED_FILES)


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


def heal_interrupted_import(directory: Path) -> None:
    """還原任何殘留的匯入暫存目錄，讓 ``directory`` 回到一致的狀態。

    ``write_dataset`` 逐檔取代正式資料前，會先把舊檔備份到暫存目錄裡的
    ``<檔名>.bak``，正常結束時會清掉暫存目錄。只有行程在「已取代部分
    檔案、尚未清理」的狹窄視窗中被強制中止（斷電、kill -9）才會殘留——
    此時 ``directory`` 可能一半是新版、一半是舊版，導致下次啟動遊戲時
    載入器回報一長串看似資料本身損壞的驗證問題，但其實只是匯入沒有
    跑完善後流程。

    找到殘留的暫存目錄時，把裡面的備份檔全部還原即可恢復一致：備份只
    在對應檔案被取代**之前**才會建立，因此還原永遠是安全、冪等的操作——
    無論當時取代動作實際上有沒有完成，複製備份回去的結果都一樣正確。
    每次載入資料或每次匯入開始前都會呼叫本函式，因此不管是下一次啟動
    遊戲、還是下一次重新執行匯入，都會自動修復。
    """
    if not directory.is_dir():
        return
    for staging in sorted(directory.glob(f"{IMPORT_STAGING_PREFIX}*")):
        if not staging.is_dir():
            continue
        for backup in staging.glob("*.bak"):
            target = directory / backup.name[: -len(".bak")]
            shutil.copyfile(backup, target)
        shutil.rmtree(staging, ignore_errors=True)


@dataclass(frozen=True)
class LineInfo:
    """一條路線（線別）的屬性。

    臺鐵的線別只有名稱，捷運另外帶著 ATO、無人駕駛與廣播樣式——這些是
    **整條線**的性質，不是某一班車的性質，因此放在線別而不是班次上。
    """

    id: str
    name_zh_tw: str
    ato: bool = False
    driverless: bool = False
    """全自動運轉（GoA 4）。車門與發車都由電腦控制，駕駛不需要任何動作。"""

    announcement_style: str = ""
    """車上廣播的規則樣式，見 :mod:`railway_sim.audio.mrt_broadcast`。"""

    operator: str = ""

    platform_pass_limit_kmh: float | None = None
    """本班車**不停靠**的車站，通過月台時的速限（公里／小時）。

    ``None`` 表示這條線沒有這項規定——台北捷運各線站站停車，根本不會通過
    月台，因此沒有值才是正確的，不是資料缺漏。機場捷運的直達車會通過許多
    車站，所以只有它有值。

    這是**線**的性質而不是某一段軌道的性質：同一段軌道對停靠的車與通過的
    車給的答案不同，因此不能寫成路網的區間速限。
    """

    @classmethod
    def from_dict(cls, line_id: str, raw: dict[str, Any]) -> LineInfo:
        limit = raw.get("platform_pass_limit_kmh")
        return cls(
            id=line_id,
            name_zh_tw=raw.get("name_zh_tw", line_id),
            ato=bool(raw.get("ato", False)),
            driverless=bool(raw.get("driverless", False)),
            announcement_style=raw.get("announcement_style", ""),
            operator=raw.get("operator", ""),
            platform_pass_limit_kmh=None if limit is None else float(limit),
        )


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
    system: RailSystem = SYSTEMS[DEFAULT_SYSTEM]
    """這份資料屬於哪一個鐵路系統（臺鐵或捷運）。"""

    lines: dict[str, LineInfo] = field(default_factory=dict)
    """線別屬性。``line_names`` 是它的名稱檢視，兩者一定一致。"""
    broadcasts: BroadcastLibrary = field(default_factory=BroadcastLibrary.empty)
    """車上廣播音檔索引（§20.2）。

    空的索引是合法狀態：還沒匯入任何廣播、或某條線暫時還沒有廣播，都只是
    「沒有聲音」，不是資料錯誤，因此**不會**進 :attr:`issues`。
    """

    broadcast_rules: MrtBroadcastRules = field(default_factory=MrtBroadcastRules.empty)
    """捷運廣播的播放規則。臺鐵沒有這個檔案，空的規則即為「照臺鐵那一套」。"""

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

    def line(self, line_id: str) -> LineInfo:
        """線別屬性。沒有這條線時回傳一個只有名稱的預設值。

        查不到不是錯誤：舊資料檔的 ``lines`` 區段可能只寫了名稱，甚至整條線
        沒有登記。這種情況下「沒有 ATO、沒有無人駕駛」正是正確的預設。
        """
        return self.lines.get(
            line_id, LineInfo(id=line_id, name_zh_tw=self.line_names.get(line_id, line_id))
        )

    def raise_on_issues(self) -> None:
        """有資料問題時擲出例外，供正式啟動時 fail fast。"""
        if self.issues:
            raise ValueError("資料驗證未通過：\n" + "\n".join(f"- {i}" for i in self.issues))


def _read_keymap(directory: Path, root: Path) -> dict[str, Any]:
    """讀取鍵位表。系統目錄沒有自己的一份時退回共用的那一份。

    鍵位是**玩家的**設定，不是某一個系統的資料：同一個人不會希望從臺鐵換到
    捷運就得重設一次按鍵。因此正常情況下只有 ``data/keymap.json`` 一份，
    系統目錄裡放一份只是為了讓匯入驗證用的暫存目錄能自成一體。
    """
    for candidate in (directory / "keymap.json", root / "keymap.json"):
        if candidate.is_file():
            return _read_json(candidate)
    raise FileNotFoundError(f"{directory} 與 {root} 都沒有 keymap.json")


def load_game_data(
    data_dir: str | Path | None = None, *, system: str = DEFAULT_SYSTEM
) -> GameData:
    """載入某個系統的所有資料檔並執行驗證。

    Args:
        data_dir: ``data`` 目錄（**不是**系統子目錄），預設自動尋找。
        system: 系統代碼，見 :data:`railway_sim.systems.SYSTEMS`。

    讀取前一律先呼叫 :func:`heal_interrupted_import`，修復任何殘留的
    中斷匯入，因此即使上一次 ``railway_sim.dataset`` 匯入在寫入正式檔案
    的過程中被強制中止，這裡仍然能載入到一致的（回復成匯入前的）資料。

    Raises:
        KeyError: 沒有這個系統代碼。
        FileNotFoundError: 系統目錄缺少資料檔。
    """
    root = Path(data_dir) if data_dir is not None else default_data_dir()
    rail_system = SYSTEMS[system]
    directory = system_data_dir(root, system)
    heal_interrupted_import(directory)
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
    lines = {
        line_id: LineInfo.from_dict(line_id, info)
        for line_id, info in routes_raw.get("lines", {}).items()
    }
    line_names = {line_id: info.name_zh_tw for line_id, info in lines.items()}

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
    keymap_raw = _read_keymap(directory, root)

    # --- 車上廣播（§20.2）----------------------------------------------
    # 掃描資料夾而不是讀清單檔：廣播會改版、新站會通車，把檔案放進資料夾
    # 就該生效；缺檔一律視為「這一站暫時沒有廣播」，不是資料錯誤。
    #
    # 音檔放在共用的 data/audio/announcements（依 line_id 分類），兩個系統
    # 的線別代碼不會相撞，因此不需要各自一份。系統目錄裡有自己的一份時
    # 優先採用，讓匯入驗證用的暫存目錄能自成一體。
    announcements = default_announcement_dir(directory)
    if not announcements.is_dir():
        announcements = default_announcement_dir(root)
    broadcasts = BroadcastLibrary.load(announcements)
    broadcast_rules = MrtBroadcastRules.load(directory / BROADCAST_RULES_FILENAME)

    return GameData(
        data_dir=directory,
        system=rail_system,
        lines=lines,
        stations=stations,
        network=network,
        routes=routes,
        region_rules=region_rules,
        train_types=train_types,
        service_classes=service_classes,
        services=services,
        line_names=line_names,
        keymap_raw=keymap_raw,
        broadcasts=broadcasts,
        broadcast_rules=broadcast_rules,
        issues=issues,
    )
