"""把維基百科的捷運條目匯入成遊戲資料檔。

輸入
----

``data/mrt/line_spec.json``
    哪一條線、對應哪一頁條目、哪些車站已通車、跑哪些營運模式。條目的車站表
    同時列出已通車與規劃中的車站，也不會說哪一種列車停哪幾站，這兩件事只能
    由人判讀後寫進設定檔，因此每一條設定都附上依據。

條目存檔資料夾
    ``<線名> - 維基百科，自由的百科全書.htm``，由 :mod:`railway_sim.dataset.mrt_wiki`
    負責剖析。

輸出
----

``data/mrt/stations.json``、``routes.json``、``timetables.json``，格式與臺鐵那一套
完全相同——兩個系統共用同一組模型與載入器，差別只在資料目錄（見
:mod:`railway_sim.systems`）。

站距怎麼算
----------

一律用條目車站表的**累計里程相減**，不直接採用「與前一站站距」欄。理由是
未通車的車站也列在表上：機場捷運的 A14 機場第三航廈夾在 A13 與 A14a 之間，
照「與前一站站距」加總會把還沒通車的一段算進去，累計相減則自然得到
「目前實際營運的兩站之間有多遠」。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from railway_sim.data_loader import (
    IMPORT_STAGING_PREFIX,
    heal_interrupted_import,
    load_game_data,
)
from railway_sim.dataset.mrt_wiki import StationRow, parse_station_table, read_page

__all__ = [
    "LINE_SPEC_FILENAME",
    "LineSpec",
    "MrtBuildResult",
    "PatternSpec",
    "build_mrt_dataset",
    "write_mrt_dataset",
]

#: 匯入設定的檔名，放在捷運資料目錄底下。
LINE_SPEC_FILENAME = "line_spec.json"

#: 條目存檔的檔名格式。
_PAGE_TEMPLATE = "{title} - 維基百科，自由的百科全書.htm"

#: 資料來源說明，寫進每一筆資料的 ``source`` 欄位。
_SOURCE_TEMPLATE = "維基百科〈{title}〉車站表"


class MrtBuildError(RuntimeError):
    """匯入無法繼續（找不到條目、車站對不上、里程缺漏）。"""


@dataclass(frozen=True)
class PatternSpec:
    """一種營運模式。"""

    number: str
    name_zh_tw: str
    origin: str
    destination: str
    service_class: str
    rolling_stock_id: str
    stops: tuple[str, ...] | None = None
    """停靠站；``None`` 表示各站停車。"""

    both_directions: bool = True
    source: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PatternSpec:
        stops = raw.get("stops")
        return cls(
            number=raw["number"],
            name_zh_tw=raw["name_zh_tw"],
            origin=raw["from"],
            destination=raw["to"],
            service_class=raw["service_class"],
            rolling_stock_id=raw["rolling_stock_id"],
            stops=tuple(stops) if stops else None,
            both_directions=bool(raw.get("both_directions", True)),
            source=raw.get("source", ""),
        )


@dataclass(frozen=True)
class LineSpec:
    """一條線的匯入設定。"""

    id: str
    code: str
    name_zh_tw: str
    operator: str
    source_page: str
    max_speed_kmh: float
    speed_status: str
    ato: bool
    driverless: bool
    announcement_style: str
    chains: tuple[tuple[str, ...], ...]
    patterns: tuple[PatternSpec, ...]
    speed_source: str = ""
    driverless_note: str = ""
    platform_pass_limit_kmh: float | None = None
    """不停靠車站的月台通過速限；``None`` 表示本線沒有這項規定。"""

    platform_pass_status: str = ""
    platform_pass_source: str = ""

    @property
    def station_codes(self) -> tuple[str, ...]:
        """本線所有車站，依鏈結順序、不重複。"""
        seen: list[str] = []
        for chain in self.chains:
            for code in chain:
                if code not in seen:
                    seen.append(code)
        return tuple(seen)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LineSpec:
        return cls(
            id=raw["id"],
            code=raw["code"],
            name_zh_tw=raw["name_zh_tw"],
            operator=raw.get("operator", ""),
            source_page=raw["source_page"],
            max_speed_kmh=float(raw["max_speed_kmh"]),
            speed_status=raw.get("speed_status", "test_data"),
            speed_source=raw.get("speed_source", ""),
            ato=bool(raw.get("ato", False)),
            driverless=bool(raw.get("driverless", False)),
            driverless_note=raw.get("driverless_note", ""),
            announcement_style=raw.get("announcement_style", ""),
            chains=tuple(tuple(chain) for chain in raw.get("chains", ())),
            patterns=tuple(PatternSpec.from_dict(p) for p in raw.get("patterns", ())),
            platform_pass_limit_kmh=(
                None
                if raw.get("platform_pass_limit_kmh") is None
                else float(raw["platform_pass_limit_kmh"])
            ),
            platform_pass_status=raw.get("platform_pass_status", ""),
            platform_pass_source=raw.get("platform_pass_source", ""),
        )


@dataclass
class MrtBuildResult:
    """匯入結果。"""

    stations: dict[str, Any]
    routes: dict[str, Any]
    timetables: dict[str, Any]
    report: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 條目 → 車站表
# ----------------------------------------------------------------------
def _load_specs(spec_path: Path) -> list[LineSpec]:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    return [LineSpec.from_dict(line) for line in raw.get("lines", ())]


def _segments(rows: list[StationRow]) -> list[dict[str, float]]:
    """把車站表切成連續路段，每段是 ``車站代碼 -> 累計里程``。

    條目會把一條線的兩個路段放在同一張表裡（中和新蘆線的「南勢角—迴龍」與
    「大橋頭—蘆洲」），第二段的累計里程從零重新起算。因此累計相減之前必須
    先確定兩站在同一段裡，否則會算出負的或天文數字般的站距。

    路段的界線就是「沒有與前一站站距」的那一列——那是一段的起點。
    """
    segments: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for row in rows:
        if row.gap_km is None and current:
            segments.append(current)
            current = {}
        if row.cumulative_km is not None:
            current[row.code] = row.cumulative_km
    if current:
        segments.append(current)
    return segments


def _link_length_m(segments: list[dict[str, float]], first: str, second: str) -> float:
    for segment in segments:
        if first in segment and second in segment:
            return abs(segment[second] - segment[first]) * 1000.0
    raise MrtBuildError(
        f"車站 {first} 與 {second} 不在條目車站表的同一個路段裡，無法計算站距"
    )


# ----------------------------------------------------------------------
# 建立資料
# ----------------------------------------------------------------------
def build_mrt_dataset(source_dir: str | Path, data_dir: str | Path) -> MrtBuildResult:
    """由條目存檔資料夾建立捷運資料集。

    Args:
        source_dir: 存放維基百科條目存檔的資料夾。
        data_dir: 捷運資料目錄（``data/mrt``），設定檔與車輛資料都在這裡。

    Raises:
        MrtBuildError: 找不到條目、車站表缺站、或站距算不出來。
    """
    source = Path(source_dir)
    target = Path(data_dir)
    specs = _load_specs(target / LINE_SPEC_FILENAME)
    trains = json.loads((target / "trains.json").read_text(encoding="utf-8"))
    known_stock = {t["id"] for t in trains.get("train_types", ())}
    known_classes = {c["id"] for c in trains.get("service_classes", ())}

    existing_times = _existing_departure_times(target)

    stations: dict[str, dict[str, Any]] = {}
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    link_ids: set[str] = set()
    lines: dict[str, dict[str, Any]] = {}
    routes: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    report: list[str] = []
    warnings: list[str] = []

    for spec in specs:
        page_path = source / _PAGE_TEMPLATE.format(title=spec.source_page)
        if not page_path.is_file():
            raise MrtBuildError(f"找不到條目存檔：{page_path}")

        table = parse_station_table(read_page(page_path))
        warnings.extend(f"{spec.name_zh_tw}：{w}" for w in table.warnings)
        by_code = table.by_code()
        segments = _segments(table.rows)

        missing = [code for code in spec.station_codes if code not in by_code]
        if missing:
            raise MrtBuildError(
                f"{spec.name_zh_tw}的條目車站表沒有這些車站：{'、'.join(missing)}"
            )

        lines[spec.id] = {
            "name_zh_tw": spec.name_zh_tw,
            "code": spec.code,
            "operator": spec.operator,
            "ato": spec.ato,
            "driverless": spec.driverless,
            "announcement_style": spec.announcement_style,
            "max_speed_kmh": spec.max_speed_kmh,
            "verification_status": "official",
            "source": _SOURCE_TEMPLATE.format(title=spec.source_page),
        }
        if spec.driverless_note:
            lines[spec.id]["driverless_note"] = spec.driverless_note
        if spec.platform_pass_limit_kmh is not None:
            lines[spec.id]["platform_pass_limit_kmh"] = spec.platform_pass_limit_kmh
            lines[spec.id]["platform_pass_status"] = spec.platform_pass_status
            lines[spec.id]["platform_pass_source"] = spec.platform_pass_source

        # --- 車站與節點 ---------------------------------------------
        for code in spec.station_codes:
            row = by_code[code]
            entry = stations.setdefault(
                code,
                {
                    "id": code,
                    "name_zh_tw": row.name_zh_tw,
                    "name_en": row.name_en,
                    "line_ids": [],
                    "stop_rules": {},
                    "platforms": [],
                    "platform_count": None,
                    "is_operational_node": False,
                    "verification_status": "official",
                    "source": _SOURCE_TEMPLATE.format(title=spec.source_page),
                },
            )
            if spec.id not in entry["line_ids"]:
                entry["line_ids"].append(spec.id)

        # --- 連線 ---------------------------------------------------
        for chain in spec.chains:
            for first, second in pairwise(chain):
                link_id = f"STA_{first}->STA_{second}"
                if link_id in link_ids:
                    continue
                link_ids.add(link_id)
                links.append(
                    {
                        "from": f"STA_{first}",
                        "to": f"STA_{second}",
                        "length_m": round(_link_length_m(segments, first, second), 1),
                        "line_id": spec.id,
                        "max_speed_kmh": spec.max_speed_kmh,
                        "verification_status": (
                            "official"
                            if spec.speed_status == "official"
                            else "test_data"
                        ),
                    }
                )

        # --- 路線與班次 ---------------------------------------------
        adjacency = _adjacency(spec)
        for pattern in spec.patterns:
            if pattern.rolling_stock_id not in known_stock:
                raise MrtBuildError(
                    f"營運模式 {pattern.number} 參照到 trains.json 沒有的車輛型式："
                    f"{pattern.rolling_stock_id}"
                )
            if pattern.service_class not in known_classes:
                raise MrtBuildError(
                    f"營運模式 {pattern.number} 參照到 trains.json 沒有的車種："
                    f"{pattern.service_class}"
                )

            forward = _path(adjacency, pattern.origin, pattern.destination)
            if forward is None:
                raise MrtBuildError(
                    f"營運模式 {pattern.number}：{pattern.origin} 到 "
                    f"{pattern.destination} 之間在 {spec.name_zh_tw} 上不連通"
                )
            directions = [forward]
            if pattern.both_directions:
                directions.append(list(reversed(forward)))

            for offset, path in enumerate(directions):
                number = _service_number(pattern.number, offset)
                route_id = f"R_{number}"
                origin_name = stations[path[0]]["name_zh_tw"]
                destination_name = stations[path[-1]]["name_zh_tw"]
                routes.append(
                    {
                        "id": route_id,
                        "name_zh_tw": f"{origin_name}至{destination_name}",
                        "line_id": spec.id,
                        # 捷運不講南下北上，講「往哪一站」；播報時直接讀得通。
                        "direction": f"往{destination_name}",
                        "node_ids": [f"STA_{code}" for code in path],
                        "verification_status": "official",
                    }
                )

                stops = _stops_on_path(path, pattern.stops)
                passes = [code for code in path if code not in stops]
                for code in stops:
                    stations[code]["stop_rules"][pattern.service_class] = True
                service = {
                    "train_number": number,
                    "name_zh_tw": (
                        f"{spec.name_zh_tw}　{pattern.name_zh_tw}（往{destination_name}）"
                    ),
                    "train_type": pattern.service_class,
                    "rolling_stock_id": pattern.rolling_stock_id,
                    "route_id": route_id,
                    "stop_station_ids": stops,
                    "pass_station_ids": passes,
                    "departure_times": {},
                    "arrival_times": {},
                    "verification_status": "official",
                    "source": pattern.source,
                }
                service.update(existing_times.get((number, tuple(stops)), {}))
                services.append(service)

        report.append(
            f"{spec.name_zh_tw}：車站 {len(spec.station_codes)} 站、"
            f"營運模式 {len(spec.patterns)} 種"
        )

    # 分歧站＝軌道上多於兩個方向的車站：北投（往淡水／往新北投）、七張
    # （往新店／往小碧潭）、大橋頭（往迴龍／往蘆洲）。用實際的連線數判斷，
    # 不用「屬於幾條線」——大橋頭的兩個分支同屬中和新蘆線，照線別算會漏掉。
    neighbours: dict[str, set[str]] = {}
    for link in links:
        first = link["from"].removeprefix("STA_")
        second = link["to"].removeprefix("STA_")
        neighbours.setdefault(first, set()).add(second)
        neighbours.setdefault(second, set()).add(first)
    for code, entry in stations.items():
        entry["is_operational_node"] = len(neighbours.get(code, ())) > 2

    for code, entry in stations.items():
        nodes.append(
            {
                "id": f"STA_{code}",
                "node_type": "station",
                "station_id": code,
                "line_ids": list(entry["line_ids"]),
            }
        )

    report.insert(
        0,
        f"共 {len(lines)} 條線、{len(stations)} 站、{len(routes)} 條路線、"
        f"{len(services)} 個營運模式。",
    )

    return MrtBuildResult(
        stations=_stations_payload(stations),
        routes=_routes_payload(lines, nodes, links, routes),
        timetables=_timetables_payload(services),
        report=report,
        warnings=warnings,
    )


def _existing_departure_times(
    data_dir: Path,
) -> dict[tuple[str, tuple[str, ...]], dict[str, Any]]:
    """讀出現有 ``timetables.json`` 裡由時刻表匯入補上的欄位。

    條目匯入會把整份 ``timetables.json`` 重新產生。時刻不在條目裡（見
    :mod:`railway_sim.dataset.mrt_timetable`），因此不留一手的話，每次更新
    車站資料都會把辛苦匯入的時刻清成空的。

    比對鍵包含**停靠站**：停靠站變了就表示這個營運模式已經不是同一回事，
    舊的時刻對不上新的路線，那時寧可留白也不能沿用。
    """
    path = data_dir / "timetables.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    carried: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for service in payload.get("services", ()):
        if not service.get("departure_times"):
            continue
        key = (service["train_number"], tuple(service.get("stop_station_ids", ())))
        kept = {
            "departure_times": service["departure_times"],
            "arrival_times": service.get("arrival_times", {}),
        }
        if service.get("schedule"):
            kept["schedule"] = service["schedule"]
        carried[key] = kept
    return carried


def _adjacency(spec: LineSpec) -> dict[str, list[str]]:
    """由鏈結建立本線的雙向相鄰表。"""
    adjacency: dict[str, list[str]] = {}
    for chain in spec.chains:
        for first, second in pairwise(chain):
            adjacency.setdefault(first, []).append(second)
            adjacency.setdefault(second, []).append(first)
    return adjacency


def _path(adjacency: dict[str, list[str]], origin: str, destination: str) -> list[str] | None:
    """在單一路線上找出起訖之間的車站序列。

    捷運各線是簡單的線或樹，因此廣度優先就足夠，而且結果唯一——不會有
    「兩條路都到得了」的歧義。
    """
    if origin == destination:
        return None
    queue: deque[list[str]] = deque([[origin]])
    seen = {origin}
    while queue:
        path = queue.popleft()
        for neighbour in adjacency.get(path[-1], ()):
            if neighbour in seen:
                continue
            extended = [*path, neighbour]
            if neighbour == destination:
                return extended
            seen.add(neighbour)
            queue.append(extended)
    return None


def _stops_on_path(path: list[str], stops: tuple[str, ...] | None) -> list[str]:
    if stops is None:
        return list(path)
    wanted = set(stops)
    return [code for code in path if code in wanted]


def _service_number(base: str, offset: int) -> str:
    """同一種營運模式的兩個方向用相鄰的兩個號碼。

    設定檔一律給奇數，往終點方向用原號、回程用加一的偶數，與鐵路慣例
    （上下行分開編號）一致，也讓兩個方向一眼看得出是同一種模式。
    """
    if offset == 0:
        return base
    prefix = base.rstrip("0123456789")
    digits = base[len(prefix) :]
    return f"{prefix}{int(digits) + offset:0{len(digits)}d}"


# ----------------------------------------------------------------------
# 輸出
# ----------------------------------------------------------------------
def _stations_payload(stations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "meta": {
            "description": "捷運車站資料，依維基百科各線條目的車站表產生。",
            "id_policy": "車站代碼直接採用條目的「編號」欄（R22、A14a、LB02…）；廣播音檔檔名也用同一組代碼。",
            "stop_rules_policy": (
                "stop_rules 由營運模式反推：某一種車種只要有一種營運模式停靠本站，"
                "本站就辦理該車種停靠。台北捷運各線都是各站停車，因此只有機場捷運的"
                "直達車會真正篩掉車站。"
            ),
            "provenance": {"source": "維基百科各線條目", "generator": "railway_sim.dataset.mrt"},
        },
        "stations": [stations[code] for code in sorted(stations)],
    }


def _routes_payload(
    lines: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    routes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "meta": {
            "description": "捷運路網與行駛路徑，依維基百科各線條目的車站表產生。",
            "length_policy": (
                "站距一律由條目車站表的累計里程相減得出，因此還沒通車的車站"
                "（機場捷運 A14、三鶯線 LB07a）不會被算進營運中的區間。"
            ),
            "speed_policy": "區間速限採該線條目的營運速度；條目沒有列的（三鶯線）標記 test_data。",
            "platform_pass_policy": (
                "platform_pass_limit_kmh：本班車不停靠的車站，通過月台時的速限。"
                "來源見各線的 platform_pass_source。"
            ),
            "provenance": {"source": "維基百科各線條目", "generator": "railway_sim.dataset.mrt"},
        },
        "lines": lines,
        "nodes": nodes,
        "links": links,
        "routes": routes,
    }


def _timetables_payload(services: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "meta": {
            "description": "捷運營運模式。捷運不公布逐班時刻，因此這裡是「模式」而不是時刻表。",
            "number_policy": (
                "車次為本專案自訂：路線代號加四位數，奇數為去程、偶數為回程。"
                "捷運沒有對外公布的車次編號，因此不假裝有。"
            ),
            "timetable_policy": (
                "發車時刻不在條目裡，由 railway_sim.dataset.mrt_timetable 另外"
                "匯入；重建資料集時會保留已匯入的時刻（停靠站沒變的話）。"
            ),
            "provenance": {"source": "維基百科各線條目的列車營運模式章節", "generator": "railway_sim.dataset.mrt"},
        },
        "services": services,
    }


def write_mrt_dataset(result: MrtBuildResult, data_dir: str | Path) -> list[Path]:
    """把匯入結果寫入捷運資料目錄。

    與臺鐵的匯入採同一套安全流程：先寫進暫存目錄，用遊戲本身的載入器驗證
    過才逐檔取代正式檔案，因此驗證失敗時正式資料完全不受影響。詳見
    :func:`railway_sim.dataset.build.write_dataset` 的說明。

    Raises:
        ValueError: 匯入結果沒有通過驗證。
        FileNotFoundError: 缺少驗證所需、本次匯入不會改動的資料檔。
    """
    data_path = Path(data_dir)
    heal_interrupted_import(data_path)

    payloads = {
        "stations.json": result.stations,
        "routes.json": result.routes,
        "timetables.json": result.timetables,
    }

    staging = Path(tempfile.mkdtemp(prefix=IMPORT_STAGING_PREFIX, dir=data_path))
    try:
        for name, payload in payloads.items():
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            (staging / name).write_text(text, encoding="utf-8", newline="\n")

        # 驗證用的暫存目錄是「平的」：載入器只要在同一個目錄裡看得到五個
        # 資料檔就能驗證，因此把捷運自己的 trains.json 與共用的 keymap.json
        # 一起複製進去。
        shutil.copyfile(data_path / "trains.json", staging / "trains.json")
        keymap = data_path / "keymap.json"
        if not keymap.is_file():
            keymap = data_path.parent / "keymap.json"
        if not keymap.is_file():
            raise FileNotFoundError(f"找不到 keymap.json，無法驗證匯入結果：{data_path}")
        shutil.copyfile(keymap, staging / "keymap.json")

        validated = load_game_data(staging)
        if validated.issues:
            raise ValueError(
                "匯入結果未通過驗證，正式資料未被覆寫：\n"
                + "\n".join(f"- {issue}" for issue in validated.issues)
            )

        written: list[Path] = []
        for name in payloads:
            target = data_path / name
            if target.is_file():
                shutil.copyfile(target, staging / f"{name}.bak")
            os.replace(staging / name, target)
            written.append(target)
        return written
    finally:
        shutil.rmtree(staging, ignore_errors=True)
