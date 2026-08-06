"""由臺鐵時刻表產生 ``data/`` 底下的遊戲資料檔。

流程
----

1. 解析所有 ``.ods``（:mod:`~railway_sim.dataset.tra_parser`）。
2. 以「同一個表格區塊中相鄰的兩站」建立候選連線，再刪掉被其他區塊證明
   中間還有車站的連線（見 :func:`_prune_spanned`）。對號列車時刻表只列
   主要車站，這一步可以避免產生「汐止直接接七堵」這種假的相鄰關係。
3. 併入 ``topology_overrides.json`` 的分歧節點、成追線與線別歸屬。
4. 每個班次以停靠站為路標逐段尋路，得到實際行經的節點序列；山／海／追
   標記用來在山線與海線之間做選擇。
5. 輸出 ``stations.json``、``routes.json``、``timetables.json``。

資料可信度（規格 §22、§27）
---------------------------

- **站名、站序、停靠與時刻**：來自臺鐵公布的時刻表，標記 ``official``。
- **停靠規則**（哪些車種在哪一站辦客）：由實際時刻表統計而得，標記
  ``derived_from_timetable``，不是臺鐵另行公布的規則。
- **區間長度**：時刻表沒有里程，改由「該區間最短運轉時間 × 線別標稱
  速度」推估，標記 ``estimated_from_timetable``，**不是實際里程**。
- **區間速限、月台、股道**：仍無公開來源，維持測試值或 ``null``。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from railway_sim.dataset.registry import StationRegistry, UnknownStationError
from railway_sim.dataset.tra_parser import (
    ParsedService,
    ParsedStop,
    SourceBlock,
    parse_file,
)

__all__ = ["BuildResult", "build_dataset", "write_dataset"]

#: 車種名稱 -> 遊戲內的車種代碼。
TRAIN_CLASS_IDS = {
    "區間車": "local",
    "區間快": "local_express",
    "自強": "tze_chiang",
    "自強3000": "tze_chiang",
    "普悠瑪": "tze_chiang",
    "太魯閣": "tze_chiang",
    "莒光": "chu_kuang",
    "復興": "fu_hsing",
    "普快車": "ordinary",
    "普快": "ordinary",
}

#: 車種代碼 -> 建議車輛型式（``trains.json`` 的 ``train_types``）。
ROLLING_STOCK_BY_CLASS = {
    "區間車": "EMU900",
    "區間快": "EMU900",
    "自強": "EMU3000",
    "自強3000": "EMU3000",
    "普悠瑪": "EMU3000",
    "太魯閣": "EMU3000",
    "莒光": "PP",
}

#: 推估區間長度時採用的標稱速度（公尺／秒）。
#:
#: 相鄰兩站的最短排點時間包含起步與煞車，因此標稱速度取得比線路速限低
#: 得多。這個常數只影響推估里程的整體比例，不代表任何實際速度。
NOMINAL_SPEED_MPS = 55_000 / 3600

#: 推估里程的上下限（公尺），避免異常排點造成極端值。
_MIN_LINK_M = 400.0
_MAX_LINK_M = 30_000.0

_DATE_IN_NAME = re.compile(r"(20\d{2})(\d{2})(\d{2})")


# ----------------------------------------------------------------------
@dataclass
class BuildResult:
    """一次匯入的產出與報告。"""

    stations: dict
    routes: dict
    timetables: dict
    report: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def station_count(self) -> int:
        return len(self.stations["stations"])

    @property
    def service_count(self) -> int:
        return len(self.timetables["services"])

    @property
    def route_count(self) -> int:
        return len(self.routes["routes"])


# ----------------------------------------------------------------------
# 來源掃描
# ----------------------------------------------------------------------
def _source_files(source_dir: Path) -> list[Path]:
    return sorted(p for p in source_dir.glob("*.ods") if not p.name.startswith("~"))


def _effective_date(paths: list[Path]) -> str:
    """由檔名中的日期推出實施日期，取最多數的那一個。"""
    counts: dict[str, int] = defaultdict(int)
    for path in paths:
        match = _DATE_IN_NAME.search(path.name)
        if match:
            counts[f"{match.group(1)}-{match.group(2)}-{match.group(3)}"] += 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# 站序與連線
# ----------------------------------------------------------------------
def _line_for_block(block: SourceBlock, rules: list[dict]) -> str | None:
    for rule in rules:
        if rule["match"] in block.title:
            return rule["line_id"]
    return None


def _prune_spanned(
    edges: dict[tuple[str, str], str], sequences: list[list[str]]
) -> dict[tuple[str, str], str]:
    """刪掉「中間其實還有車站」的候選連線。

    對號列車時刻表只列主要車站，直接相鄰不代表實際相鄰。若任何一份站序
    中，同樣兩站之間還夾著別的車站，就表示這組相鄰是跳過中間站的結果。
    """
    kept: dict[tuple[str, str], str] = {}
    for (left, right), line_id in edges.items():
        spanned = False
        for sequence in sequences:
            if left in sequence and right in sequence:
                gap = abs(sequence.index(left) - sequence.index(right))
                if gap > 1:
                    spanned = True
                    break
        if not spanned:
            kept[(left, right)] = line_id
    return kept


def _collect_sequences(
    blocks: list[SourceBlock], overrides: dict
) -> tuple[list[tuple[list[str], str]], list[list[str]]]:
    """回傳 ``(可用於建連線的站序, 全部站序)``。

    只有排版 A（區間車時刻表）列出區間內每一站，因此預設只有它可以用來
    建連線。南迴線沒有區間車時刻表，改由 ``adjacency_from_column_layout``
    指定可信任的區段。
    """
    usable: list[tuple[list[str], str]] = []
    everything: list[list[str]] = []
    title_rules = overrides.get("line_from_title", [])

    for block in blocks:
        line_id = _line_for_block(block, title_rules)
        for sequence in block.sequences:
            names = list(dict.fromkeys(sequence))
            if len(names) < 2:
                continue
            everything.append(names)
            if block.layout == "row_per_train" and line_id:
                usable.append((names, line_id))

    for allow in overrides.get("adjacency_from_column_layout", ()):
        start, end = allow["between"]
        for block in blocks:
            if block.source_file != allow["source_file"]:
                continue
            for sequence in block.sequences:
                names = list(dict.fromkeys(sequence))
                if start not in names or end not in names:
                    continue
                first, last = sorted((names.index(start), names.index(end)))
                usable.append((names[first : last + 1], allow["line_id"]))
    return usable, everything


def _build_edges(
    blocks: list[SourceBlock], overrides: dict
) -> dict[tuple[str, str], str]:
    """建立無向的相鄰關係（中文站名），值為線別代碼。"""
    priority = {
        line: index for index, line in enumerate(overrides.get("line_priority", []))
    }
    usable, everything = _collect_sequences(blocks, overrides)

    candidates: dict[tuple[str, str], str] = {}
    for names, line_id in usable:
        for left, right in pairwise(names):
            if left == right:
                continue
            key = (left, right) if left < right else (right, left)
            current = candidates.get(key)
            if current is None or priority.get(line_id, 99) < priority.get(current, 99):
                candidates[key] = line_id

    edges = _prune_spanned(candidates, everything)

    for override in overrides.get("line_overrides", ()):
        stations = override["stations"]
        for left, right in pairwise(stations):
            key = (left, right) if left < right else (right, left)
            if key in edges:
                edges[key] = override["line_id"]
    return edges


# ----------------------------------------------------------------------
# 區間長度推估
# ----------------------------------------------------------------------
def _running_times(services: list[ParsedService]) -> dict[tuple[str, str], int]:
    """每組相鄰停靠站的最短排點時間（秒）。"""
    fastest: dict[tuple[str, str], int] = {}
    for service in services:
        calling = service.calling_stops
        for left, right in pairwise(calling):
            start, end = left.minutes, right.minutes
            if start is None or end is None:
                continue
            delta = end - start
            if delta <= 0:
                delta += 24 * 60
            if delta <= 0 or delta > 120:
                continue
            key = (
                (left.station_name, right.station_name)
                if left.station_name < right.station_name
                else (right.station_name, left.station_name)
            )
            seconds = delta * 60
            if seconds < fastest.get(key, 10**9):
                fastest[key] = seconds
    return fastest


def _estimate_length_m(key: tuple[str, str], runs: dict[tuple[str, str], int]) -> float:
    seconds = runs.get(key)
    if seconds is None:
        # 沒有任何班次在這兩站間連續停靠（例如只有直達車經過的區間）。
        return 3000.0
    metres = seconds * NOMINAL_SPEED_MPS
    return float(round(min(max(metres, _MIN_LINK_M), _MAX_LINK_M), -1))


# ----------------------------------------------------------------------
# 路網組裝
# ----------------------------------------------------------------------
def _node_id(station_id: str) -> str:
    return f"STA_{station_id}"


@dataclass
class _Graph:
    """匯入期間使用的簡單無向圖，值為 ``(長度, 線別, 速限)``。"""

    neighbours: dict[str, dict[str, tuple[float, str, float]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    transitions: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    """分歧節點的可行轉向；未列出的節點不限制。"""

    def connect(
        self, left: str, right: str, length_m: float, line_id: str, speed: float
    ) -> None:
        self.neighbours[left][right] = (length_m, line_id, speed)
        self.neighbours[right][left] = (length_m, line_id, speed)

    def connect_if_absent(
        self, left: str, right: str, length_m: float, line_id: str, speed: float
    ) -> bool:
        """只在尚未連通時建立區間，回傳是否真的建立。

        彰化站北方咽喉由山線與海線共用，兩條插入規則都會碰到它；若直接覆寫，
        後寫入的線別會把先前的蓋掉，讓純山線路線被誤判成有經過海線。
        """
        if right in self.neighbours.get(left, {}):
            return False
        self.connect(left, right, length_m, line_id, speed)
        return True

    def disconnect(self, left: str, right: str) -> None:
        self.neighbours.get(left, {}).pop(right, None)
        self.neighbours.get(right, {}).pop(left, None)

    def permits(self, previous: str | None, current: str, following: str) -> bool:
        allowed = self.transitions.get(current)
        if not allowed or previous is None:
            return True
        return (previous, following) in allowed

    def path(
        self, start: str, goal: str, preferred_line: str | None = None
    ) -> list[str] | None:
        """以長度為成本尋路；``preferred_line`` 會讓該線別的區間成本降低。

        兩項限制必須同時滿足：

        - **分歧轉向**：狀態帶著前一個節點，才能套用成追線倒插這類限制。
          少了它，尋路會走出「追分→分歧→彰化」這種實際上不存在的捷徑。
        - **經由偏好**：山線與海線在竹南與彰化之間平行，單看長度無法決定
          走哪一條，因此用時刻表上的「山」「海」「追」標記作為偏好。
        """
        import heapq

        if start == goal:
            return [start]
        queue: list[tuple[float, int, str | None, str, tuple[str, ...]]] = [
            (0.0, 0, None, start, (start,))
        ]
        best: dict[tuple[str | None, str], float] = {(None, start): 0.0}
        counter = 1
        while queue:
            cost, _, previous, current, path = heapq.heappop(queue)
            if current == goal:
                return list(path)
            if cost > best.get((previous, current), float("inf")):
                continue
            for neighbour, (length, line_id, _) in self.neighbours[current].items():
                if neighbour in path:
                    continue
                if not self.permits(previous, current, neighbour):
                    continue
                weight = length
                if preferred_line and line_id != preferred_line:
                    weight = length * _VIA_PREFERENCE_FACTOR
                new_cost = cost + weight
                key = (current, neighbour)
                if new_cost < best.get(key, float("inf")):
                    best[key] = new_cost
                    heapq.heappush(
                        queue,
                        (new_cost, counter, current, neighbour, path + (neighbour,)),
                    )
                    counter += 1
        return None


#: 「經由」標記對應的偏好線別。
_VIA_LINES = {"山": "mountain", "海": "coast", "追": "chengzhui"}

#: 非偏好線別的成本倍率。
#:
#: 山線與海線在竹南至彰化之間長度接近，這個倍率足以決定走哪一條；但它刻意
#: 不大，才不會讓「經由山線」的標記把一段本來就在海線上的行程拉去繞遠路。
_VIA_PREFERENCE_FACTOR = 1.6


# ----------------------------------------------------------------------
def _resolve_parallel_names(
    blocks: list[SourceBlock], station_lines: dict[str, set[str]]
) -> int:
    """解出對號列車時刻表中山線／海線並排欄位的實際站名。

    竹南至彰化之間，對號列車時刻表把海線與山線的站名並排成左右兩欄共用
    一個時刻欄，例如同一列左邊是「後龍」右邊是「苗栗」。哪一邊才算數，由
    該班次的「山」／「海」標記決定。線別歸屬則來自區間車時刻表建立的路網，
    因此這一步必須在 :func:`_build_edges` 之後執行。

    Returns:
        無法判定而沿用左欄站名的班次數。
    """
    unresolved = 0
    for block in blocks:
        for index, service in enumerate(block.services):
            if not any(stop.alternatives for stop in service.stops):
                continue
            wanted = _VIA_LINES.get(service.via_note)
            if wanted is None:
                unresolved += 1
                continue
            resolved = []
            for stop in service.stops:
                name = stop.station_name
                if stop.alternatives:
                    for candidate in (stop.station_name, *stop.alternatives):
                        if wanted in station_lines.get(candidate, set()):
                            name = candidate
                            break
                resolved.append(
                    ParsedStop(name, stop.time_text, stop.stops)
                )
            block.services[index] = replace(service, stops=tuple(resolved))
    return unresolved


def _merge_services(blocks: list[SourceBlock]) -> dict[str, ParsedService]:
    """同一車次在多份檔案出現時合併成一筆。

    例如樹林開往臺東的自強號同時出現在東部幹線與南迴線時刻表，各自只列出
    自己那一段，合併後才是完整行程。
    """
    grouped: dict[str, list[ParsedService]] = defaultdict(list)
    for block in blocks:
        for service in block.services:
            grouped[service.train_number].append(service)

    merged: dict[str, ParsedService] = {}
    for number, group in grouped.items():
        stops: dict[str, tuple[int, bool, str]] = {}
        for service in group:
            for stop in service.stops:
                minutes = stop.minutes
                previous = stops.get(stop.station_name)
                if previous is None or (minutes is not None and previous[0] < 0):
                    stops[stop.station_name] = (
                        minutes if minutes is not None else -1,
                        stop.stops,
                        stop.time_text,
                    )

        # 依時刻排序，跨日的班次把凌晨時段往後推一天。
        timed = [(name, data) for name, data in stops.items() if data[0] >= 0]
        timed.sort(key=lambda item: item[1][0])
        if timed and timed[-1][1][0] - timed[0][1][0] > 12 * 60:
            timed.sort(
                key=lambda item: item[1][0] + (24 * 60 if item[1][0] < 4 * 60 else 0)
            )

        primary = max(group, key=lambda s: len(s.stops))
        order = [name for name, _ in timed]
        # 沒有時刻的通過站，插回它在原始表中的相對位置。
        for service in group:
            names = [s.station_name for s in service.stops]
            for index, name in enumerate(names):
                if name in order or stops[name][0] >= 0:
                    continue
                anchor = next((n for n in names[index + 1 :] if n in order), None)
                position = order.index(anchor) if anchor else len(order)
                order.insert(position, name)

        merged[number] = ParsedService(
            train_number=number,
            train_class=primary.train_class,
            stops=tuple(
                ParsedStop(name, stops[name][2], stops[name][1]) for name in order
            ),
            origin_name=order[0] if order else "",
            destination_name=order[-1] if order else "",
            via_note=next((s.via_note for s in group if s.via_note), ""),
            operating_mark=next((s.operating_mark for s in group if s.operating_mark), ""),
            source_file="、".join(sorted({s.source_file for s in group})),
            source_sheet=primary.source_sheet,
            source_title=primary.source_title,
        )
    return merged


# ----------------------------------------------------------------------
def build_dataset(source_dir: str | Path, data_dir: str | Path) -> BuildResult:
    """讀取來源時刻表，回傳要寫入 ``data/`` 的三個資料檔內容。"""
    source_path = Path(source_dir)
    data_path = Path(data_dir)

    registry = StationRegistry.load(data_path / "station_registry.json")
    overrides = json.loads(
        (data_path / "topology_overrides.json").read_text(encoding="utf-8")
    )

    files = _source_files(source_path)
    if not files:
        raise FileNotFoundError(f"{source_path} 裡沒有 .ods 時刻表檔案")

    report: list[str] = []
    skipped: list[str] = []

    blocks: list[SourceBlock] = []
    for path in files:
        blocks.extend(parse_file(path))
    report.append(f"讀取 {len(files)} 個檔案、{len(blocks)} 個表格區塊。")

    # --- 站名檢查 -----------------------------------------------------
    names_seen: set[str] = set()
    for block in blocks:
        names_seen.update(block.station_names)
    unknown = sorted(n for n in names_seen if not registry.contains(n))
    if unknown:
        raise UnknownStationError(
            "來源時刻表出現對照表沒有的站名："
            + "、".join(unknown)
            + "。請在 data/station_registry.json 補上後重新匯入。"
        )

    # --- 連線 ---------------------------------------------------------
    # 先建連線才知道每一站屬於哪條線，對號時刻表山線／海線並排的欄位才解得開。
    edges = _build_edges(blocks, overrides)
    station_lines: dict[str, set[str]] = defaultdict(set)
    for (left, right), line_id in edges.items():
        station_lines[left].add(line_id)
        station_lines[right].add(line_id)

    unresolved = _resolve_parallel_names(blocks, station_lines)
    if unresolved:
        report.append(
            f"警告：{unresolved} 個班次在山線／海線並排欄位沒有經由標記，"
            "沿用左欄（海線）站名。"
        )

    services = _merge_services(blocks)
    report.append(f"合併後共 {len(services)} 個車次。")

    runs = _running_times([s for b in blocks for s in b.services])
    speeds = overrides.get("default_speeds_kmh", {})

    graph = _Graph()
    for (left, right), line_id in edges.items():
        graph.connect(
            _node_id(registry.id_for(left)),
            _node_id(registry.id_for(right)),
            _estimate_length_m((left, right), runs),
            line_id,
            float(speeds.get(line_id, 100)),
        )
    report.append(f"由站序建立 {len(edges)} 個區間。")

    # --- 分歧節點 -----------------------------------------------------
    junction_nodes = overrides.get("junction_nodes", [])
    for node in junction_nodes:
        allowed = {
            (t["from"], t["to"]) for t in node.get("allowed_transitions", ())
        }
        if allowed:
            graph.transitions[node["id"]] = allowed
    for insert in overrides.get("insert_junctions", ()):
        left_name, right_name = insert["between"]
        left = _node_id(registry.id_for(left_name))
        right = _node_id(registry.id_for(right_name))
        original = graph.neighbours.get(left, {}).get(right)
        if original is None:
            report.append(f"警告：{left_name}－{right_name} 沒有可插入分歧的區間。")
            continue
        total_length, _, speed = original
        graph.disconnect(left, right)
        chain = [left, *insert["via"], right]
        line_ids = insert.get("line_ids", [])
        share = total_length / (len(chain) - 1)
        for index, (a, b) in enumerate(pairwise(chain)):
            line_id = line_ids[index] if index < len(line_ids) else "mountain"
            graph.connect_if_absent(
                a, b, round(share, -1), line_id, float(speeds.get(line_id, 100))
            )

    for extra in overrides.get("extra_links", ()):
        graph.connect(
            extra["from"],
            extra["to"],
            float(extra["length_m"]),
            extra["line_id"],
            float(extra["max_speed_kmh"]),
        )

    # --- 車站停靠規則（由實際時刻表統計）------------------------------
    calls: dict[str, set[str]] = defaultdict(set)
    observed_classes: set[str] = set()
    for service in services.values():
        class_id = TRAIN_CLASS_IDS.get(service.train_class)
        if not class_id:
            continue
        observed_classes.add(class_id)
        for stop in service.calling_stops:
            calls[stop.station_name].add(class_id)

    used_names = sorted(names_seen)
    station_entries = []
    for name in used_names:
        entry = registry.entry_for(name)
        served = calls.get(name, set())
        station_entries.append(
            {
                "id": entry.id,
                "name_zh_tw": name,
                "name_en": entry.official_name_en,
                "name_en_verification": (
                    "official" if entry.official_name_en else "unknown"
                ),
                "line_ids": sorted(station_lines.get(name, set())),
                "stop_rules": {
                    class_id: (class_id in served)
                    for class_id in sorted(observed_classes)
                },
                "platforms": [],
                "platform_count": None,
                "is_operational_node": False,
                "verification_status": "official",
                "source": "臺鐵公布之列車時刻表（見 timetables.json 的 meta.sources）",
            }
        )

    # --- 路線 ---------------------------------------------------------
    node_ids = {name for name in graph.neighbours}
    routes_by_path: dict[tuple[str, ...], str] = {}
    route_entries: list[dict] = []
    service_entries: list[dict] = []

    for number in sorted(services, key=lambda n: (len(n), n)):
        service = services[number]
        class_id = TRAIN_CLASS_IDS.get(service.train_class)
        if not class_id:
            skipped.append(f"{number}：未知車種「{service.train_class}」")
            continue

        # 只有印出時刻的停靠站才是可靠的路標。對號列車時刻表在竹南與彰化
        # 之間只印海線車站，山線列車在那些車站也印「↓」；若把「↓」也當成
        # 必經點，山線列車會被硬拉到海線上。通過站改由算出的路徑回推。
        waypoints = [
            _node_id(registry.id_for(stop.station_name))
            for stop in service.calling_stops
        ]
        waypoints = [w for w in waypoints if w in node_ids]
        if len(waypoints) < 2:
            skipped.append(f"{number}：可用車站不足兩站")
            continue

        preferred = _VIA_LINES.get(service.via_note)
        path: list[str] = [waypoints[0]]
        broken = ""
        for left, right in pairwise(waypoints):
            leg = graph.path(left, right, preferred)
            if leg is None:
                broken = f"{left} 至 {right} 無法連通"
                break
            path.extend(leg[1:])
        if broken:
            skipped.append(f"{number}：{broken}")
            continue

        key = tuple(path)
        route_id = routes_by_path.get(key)
        if route_id is None:
            route_id = f"R_AUTO_{len(routes_by_path) + 1:04d}"
            routes_by_path[key] = route_id
            first = _station_name_of(path[0], registry)
            last = _station_name_of(path[-1], registry)
            line_id = _dominant_line(graph, path)
            route_entries.append(
                {
                    "id": route_id,
                    "name_zh_tw": f"{first}至{last}",
                    "line_id": line_id,
                    "direction": _direction_of(service),
                    "node_ids": list(path),
                    "verification_status": "derived_from_timetable",
                }
            )

        on_path = {n for n in path}
        stop_ids = []
        pass_ids = []
        departure = {}
        arrival = {}
        for stop in service.stops:
            station_id = registry.id_for(stop.station_name)
            node = _node_id(station_id)
            if node not in node_ids:
                continue
            if stop.stops:
                stop_ids.append(station_id)
                if stop.time_text:
                    departure[station_id] = stop.time_text
                    arrival[station_id] = stop.time_text
            elif node in on_path:
                # 來源標為通過、且確實在本班次路徑上的車站才列入通過站。
                pass_ids.append(station_id)

        service_entries.append(
            {
                "train_number": number,
                "name_zh_tw": f"{service.train_class}{number}次",
                "train_type": class_id,
                "train_class_zh_tw": service.train_class,
                "rolling_stock_id": ROLLING_STOCK_BY_CLASS.get(service.train_class, ""),
                "route_id": route_id,
                "stop_station_ids": stop_ids,
                "pass_station_ids": pass_ids,
                "departure_times": departure,
                "arrival_times": arrival,
                "via_note": service.via_note,
                "operating_mark": service.operating_mark,
                "verification_status": "official",
                "source_file": service.source_file,
            }
        )

    # 只有被班次用到的車站才需要標成運轉節點。
    junction_station_ids = {
        _station_id_of(t["from"], registry) or _station_id_of(t["to"], registry)
        for node in junction_nodes
        for t in node.get("allowed_transitions", ())
    }
    for entry in station_entries:
        if entry["id"] in junction_station_ids:
            entry["is_operational_node"] = True

    report.append(f"產生 {len(route_entries)} 條路線、{len(service_entries)} 個班次。")
    if skipped:
        report.append(f"略過 {len(skipped)} 個班次（詳見報告）。")

    sources = [
        {
            "file": path.name,
            "sha256": _checksum(path),
            "bytes": path.stat().st_size,
        }
        for path in files
    ]
    effective = _effective_date(files)
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    provenance = {
        "effective_date": effective,
        "generated_at": generated,
        "generator": "python -m railway_sim.dataset",
        "sources": sources,
    }

    stations_doc = {
        "meta": {
            "description": f"臺鐵全線車站，依 {effective} 實施之時刻表產生。",
            "data_policy": (
                "站名與所屬線別取自臺鐵公布的列車時刻表，標記 official；"
                "stop_rules 由時刻表中實際辦客的班次統計而得，屬推導結果；"
                "月台數、股道配置、月台長度仍無公開來源，一律為 null 並標記 unknown"
                "（規格 §22、§27）。"
            ),
            "stop_rules_policy": (
                "stop_rules 表示「該車種在本站至少有一班列車辦理停靠」，"
                "不是臺鐵另行公布的停靠規則。某一班次停或不停一律以該班次的"
                "停靠表為準（規格 §9.1）。"
            ),
            "provenance": provenance,
        },
        "stations": station_entries,
    }

    link_entries = []
    seen_links: set[tuple[str, str]] = set()
    for left, neighbours in sorted(graph.neighbours.items()):
        for right, (length, line_id, speed) in sorted(neighbours.items()):
            key = (left, right) if left < right else (right, left)
            if key in seen_links:
                continue
            seen_links.add(key)
            link_entries.append(
                {
                    "from": key[0],
                    "to": key[1],
                    "length_m": length,
                    "line_id": line_id,
                    "max_speed_kmh": speed,
                    "verification_status": "estimated_from_timetable",
                }
            )

    node_entries = []
    for entry in station_entries:
        node_entries.append(
            {
                "id": _node_id(entry["id"]),
                "node_type": "station",
                "station_id": entry["id"],
                "line_ids": entry["line_ids"],
            }
        )
    for node in junction_nodes:
        node_entries.append(
            {
                "id": node["id"],
                "node_type": node.get("node_type", "junction"),
                "station_id": None,
                "line_ids": node.get("line_ids", []),
                "conflict_group": node.get("conflict_group"),
                "note": node.get("note", ""),
                "allowed_transitions": [
                    {"from": t["from"], "to": t["to"], "note": t.get("note", "")}
                    for t in node.get("allowed_transitions", ())
                ],
            }
        )
    node_entries = [n for n in node_entries if n["id"] in node_ids]

    routes_doc = {
        "meta": {
            "description": f"臺鐵全線路網與行駛路徑，依 {effective} 實施之時刻表產生。",
            "data_policy": (
                "節點與相鄰關係取自時刻表站序（official）；"
                "length_m 由排點時間推估並標記 estimated_from_timetable，"
                "**不是實際里程**；max_speed_kmh 無公開來源，為依線別給定的測試值"
                "（規格 §27）。"
            ),
            "topology_source": (
                "分歧節點、倒插方向限制與線別歸屬取自 data/topology_overrides.json，"
                "該檔為人工維護，內容依開發規格 §10 與 chat.md 的成功／追分／彰化結論。"
            ),
            "provenance": provenance,
        },
        "lines": overrides.get("lines", {}),
        "region_rules": {
            k: v for k, v in overrides.get("region_rules", {}).items() if k != "note"
        },
        "nodes": node_entries,
        "links": link_entries,
        "routes": route_entries,
    }

    timetables_doc = {
        "meta": {
            "description": f"臺鐵 {effective} 實施之列車時刻表。",
            "data_policy": (
                "車次、車種、停靠站、通過站與時刻全部取自臺鐵公布的時刻表，"
                "標記 official。operating_mark 為來源檔案中的行駛日期符號，"
                "其意義見來源檔案的「註：」，本專案未實作按日期篩選。"
            ),
            "stop_rule_note": (
                "停靠與通過一律以本班次的停靠表為準，"
                "不得由「列車行駛於這條路線」推論（規格 §9.1）。"
            ),
            "provenance": provenance,
        },
        "services": service_entries,
    }

    return BuildResult(
        stations=stations_doc,
        routes=routes_doc,
        timetables=timetables_doc,
        report=report,
        skipped=skipped,
    )


def _station_name_of(node_id: str, registry: StationRegistry) -> str:
    station_id = _station_id_of(node_id, registry)
    if not station_id:
        return node_id
    for entry in registry.entries:
        if entry.id == station_id:
            return entry.name_zh_tw
    return station_id


def _station_id_of(node_id: str, registry: StationRegistry) -> str | None:
    if not node_id.startswith("STA_"):
        return None
    return node_id[4:]


def _dominant_line(graph: _Graph, path: list[str]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for left, right in pairwise(path):
        edge = graph.neighbours.get(left, {}).get(right)
        if edge:
            counts[edge[1]] += 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _direction_of(service: ParsedService) -> str:
    """臺鐵車次尾數為奇數者為南下／逆行，偶數為北上／順行。"""
    try:
        return "southbound" if int(service.train_number) % 2 else "northbound"
    except ValueError:
        return "southbound"


# ----------------------------------------------------------------------
def write_dataset(result: BuildResult, data_dir: str | Path) -> list[Path]:
    """把匯入結果寫入 ``data/``，回傳實際寫出的檔案。"""
    data_path = Path(data_dir)
    written: list[Path] = []
    for name, payload in (
        ("stations.json", result.stations),
        ("routes.json", result.routes),
        ("timetables.json", result.timetables),
    ):
        target = data_path / name
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written
