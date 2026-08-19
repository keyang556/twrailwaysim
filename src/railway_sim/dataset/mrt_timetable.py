"""把臺北捷運公布的時刻表 CSV 匯入成班次的實際發車時刻。

為什麼需要這一步
----------------

``railway_sim.dataset.mrt`` 由維基百科條目建出車站、路網與**營運模式**，但
條目不含時刻，因此每個班次的 ``departure_times`` 一直是空的。北市府資料平台
公布的「北市平台版」時刻表補得起這一塊：它是**逐班**的實際發車時刻，不是
班距或首末班的摘要。

來源格式
--------

CSV 為 Big5 編碼，每一列是「某一站的某一班發車」：

===========================  ================================================
欄位                          內容
===========================  ================================================
``RouteID``                   營運路線，例如 ``BL-1``（頂埔—南港展覽館）
``StationID``                 車站代碼，與本專案的代碼**完全相同**（BL01…）
``Direction``                 ``0``／``1``，方向
``DestinationStaionID``       這一班開往哪一站（原檔即拼作 Staion）
``DepartureTimes``            ``{序號,,,HH:MM,}``
``ServiceDays``               ``{'平日',1,1,1,1,1,0,0,0}``
===========================  ================================================

**序號是「本站的第幾班」，不是車次**。同一班車在下一站的序號不一樣，因此
不能靠它把一班車串起來。串接改用時間：捷運的班距（三到八分）明顯大於站距
的行車時間（一到三分），所以「本站發車之後，下一站最早的那一班」必然是同
一列車。:func:`chain_trip` 就是這樣走的。

沒有到達時刻
------------

來源只公布**發車**時刻，終點站因此沒有任何時刻——它只有到達。這裡不補、也
不推估：規格 §2.3 禁止把推測當成事實。``arrival_times`` 一律留空，終點站
不出現在 ``departure_times`` 裡。

跨午夜
------

捷運的營運日到凌晨一點多才結束，``00:20`` 是**當天的末班車**而不是隔天的
首班車。因此小於 :data:`_DAY_BREAK_H` 的時刻一律視為隔日，換算成分鐘時加上
一天，時間順序才排得對。
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from railway_sim.data_loader import IMPORT_STAGING_PREFIX, heal_interrupted_import

__all__ = [
    "MrtTimetableError",
    "RouteTimetable",
    "ScheduleMatch",
    "TimetableFile",
    "TimetableImportResult",
    "apply_mrt_timetables",
    "build_mrt_timetables",
    "chain_trip",
    "read_timetable_file",
]

#: 來源檔案的編碼。北市平台版一律是 Big5。
_ENCODING = "big5"

#: 小於這個小時數的時刻視為「隔日凌晨」，屬於前一個營運日。
_DAY_BREAK_H = 4

#: 串接一班車時，相鄰兩站之間可接受的最長時間（分鐘）。
#:
#: 捷運最長的站距也跑不到這麼久。超過就表示串錯了——通常是該站在這個時段
#: 沒有這條營運路線的車，硬串下去會得到一份看起來合理、實際上不存在的時刻。
_MAX_SEGMENT_MIN = 8

#: 優先採用的營運日。同一條線通常有平日與假日兩份，遊戲用平日那一份。
_PREFERRED_SERVICE_DAYS = ("平日", "週六", "週日", "假日")


class MrtTimetableError(RuntimeError):
    """時刻表匯入無法繼續。"""


# ----------------------------------------------------------------------
# 讀取
# ----------------------------------------------------------------------
def _to_minutes(hhmm: str) -> int:
    """``HH:MM`` 轉成「營運日內的分鐘數」，凌晨的時刻算隔日。"""
    hour, minute = hhmm.split(":")
    hour_value = int(hour)
    if hour_value < _DAY_BREAK_H:
        hour_value += 24
    return hour_value * 60 + int(minute)


def _to_clock(minutes: int) -> str:
    """分鐘數轉回 ``HH:MM``，隔日的時刻寫回 24 小時制。"""
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _parse_departure(value: str) -> str:
    """從 ``{序號,,,HH:MM,}`` 取出時刻。"""
    parts = value.strip().strip("{}").split(",")
    if len(parts) < 4 or ":" not in parts[3]:
        raise MrtTimetableError(f"看不懂的發車時刻欄位：{value!r}")
    return parts[3].strip()


def _parse_service_days(value: str) -> str:
    """從 ``{'平日',1,1,1,1,1,0,0,0}`` 取出營運日名稱。"""
    return value.strip().strip("{}").split(",")[0].strip().strip("'")


@dataclass(frozen=True)
class RouteTimetable:
    """一份 CSV 裡的一組營運路線（同一個路線代號、方向與終點）。"""

    route_id: str
    direction: str
    destination: str
    departures: dict[str, tuple[int, ...]]
    """車站代碼 -> 該站的所有發車時刻（營運日分鐘數，已排序）。"""

    @property
    def station_ids(self) -> frozenset[str]:
        """本組涵蓋的車站：有發車的站，加上只到達不發車的終點站。"""
        return frozenset(self.departures) | {self.destination}

    def first_departure(self, station_id: str) -> int | None:
        times = self.departures.get(station_id)
        return times[0] if times else None

    def last_departure(self, station_id: str) -> int | None:
        times = self.departures.get(station_id)
        return times[-1] if times else None


@dataclass(frozen=True)
class TimetableFile:
    """一個 CSV 檔的內容。"""

    path: Path
    service_days: str
    effective_date: str
    routes: tuple[RouteTimetable, ...]

    @property
    def title(self) -> str:
        """給 ``source`` 欄位用的來源名稱（檔名去掉副檔名）。"""
        return self.path.stem


def read_timetable_file(path: str | Path) -> TimetableFile:
    """讀取一個「北市平台版」時刻表 CSV。

    Raises:
        MrtTimetableError: 欄位缺漏或格式看不懂。
    """
    file_path = Path(path)
    text = file_path.read_bytes().decode(_ENCODING, errors="strict")
    reader = csv.DictReader(io.StringIO(text))

    required = {"RouteID", "StationID", "Direction", "DestinationStaionID", "DepartureTimes"}
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise MrtTimetableError(
            f"{file_path.name} 缺少欄位：{'、'.join(sorted(missing))}"
        )

    grouped: dict[tuple[str, str, str], dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    service_days: set[str] = set()
    effective: set[str] = set()

    for row in reader:
        key = (row["RouteID"], row["Direction"], row["DestinationStaionID"])
        minutes = _to_minutes(_parse_departure(row["DepartureTimes"]))
        grouped[key][row["StationID"]].append(minutes)
        service_days.add(_parse_service_days(row.get("ServiceDays", "")))
        effective.add(row.get("EffectiveDate", "").strip())

    if not grouped:
        raise MrtTimetableError(f"{file_path.name} 沒有任何發車紀錄")

    routes = tuple(
        RouteTimetable(
            route_id=route_id,
            direction=direction,
            destination=destination,
            departures={
                station: tuple(sorted(times)) for station, times in sorted(stations.items())
            },
        )
        for (route_id, direction, destination), stations in sorted(grouped.items())
    )
    return TimetableFile(
        path=file_path,
        # 一個檔案只有一種營運日；真的混了就把它們全列出來，讓人看得見異常。
        service_days="／".join(sorted(service_days)),
        # 同一份檔案的啟用日期一定一樣；取最大值只是為了對付萬一的雜訊。
        effective_date=max(effective) if effective else "",
        routes=routes,
    )


def read_timetable_dir(source_dir: str | Path) -> list[TimetableFile]:
    """讀取資料夾裡所有的時刻表 CSV。"""
    directory = Path(source_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"找不到時刻表資料夾：{directory}")
    paths = sorted(
        p for p in directory.iterdir() if p.suffix.lower() == ".csv" and p.is_file()
    )
    if not paths:
        raise MrtTimetableError(f"{directory} 裡沒有 CSV 檔")
    return [read_timetable_file(path) for path in paths]


# ----------------------------------------------------------------------
# 串接一班車
# ----------------------------------------------------------------------
def chain_trip(
    route: RouteTimetable, station_ids: list[str], start_minutes: int
) -> dict[str, int] | None:
    """把一班車沿著 ``station_ids`` 串起來，回傳每一站的發車時刻。

    序號是「本站的第幾班」而不是車次，所以只能用時間串：本站發車之後，
    下一站最早的那一班就是同一列車——捷運的班距（三到八分）明顯大於站距
    的行車時間（一到三分），不會串到後面那一班。

    終點站不會出現在結果裡：來源只公布發車時刻，終點站沒有發車。

    Returns:
        ``車站代碼 -> 營運日分鐘數``；串不起來時回傳 ``None``（該時段這條
        營運路線在某一站沒有車，硬串會得到一份不存在的時刻表）。
    """
    if not station_ids:
        return None

    origin, *rest = station_ids
    if start_minutes not in route.departures.get(origin, ()):
        return None

    times = {origin: start_minutes}
    previous = start_minutes
    for station_id in rest:
        departures = route.departures.get(station_id)
        if departures is None:
            # 終點站沒有發車時刻，這是正常的，而且它一定是最後一站。
            if station_id == station_ids[-1]:
                break
            return None
        following = [t for t in departures if t >= previous]
        if not following or following[0] - previous > _MAX_SEGMENT_MIN:
            return None
        times[station_id] = following[0]
        previous = following[0]
    return times


# ----------------------------------------------------------------------
# 對應班次
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleMatch:
    """一個班次對應到的來源時刻表。"""

    train_number: str
    file: TimetableFile
    route: RouteTimetable
    departures: dict[str, str]
    """車站代碼 -> ``HH:MM``。"""

    first_departure: str
    last_departure: str
    departures_per_day: int
    run_time_min: int | None
    """起站發車到**最後一個有公布發車時刻的車站**之間的分鐘數。

    不是完整的旅行時間：終點站只有到達、沒有發車，來源不公布到達時刻，
    因此最後一段的行車時間不在裡面。只有兩站的支線（新北投、小碧潭）只有
    起站有時刻，這個值沒有意義，為 ``None``。
    """

    exact: bool
    """來源的營運路線與本班次的停靠站**完全相同**。

    ``False`` 表示本班次是來源那一組的一部分（例如「大安—北投」的車與
    「象山—北投」的車在來源裡同屬一組）。時刻仍然是真的，只是那一組裡也
    有別的營運模式，因此標記為 ``derived_from_timetable`` 而不是 official。
    """

    @property
    def verification_status(self) -> str:
        return "official" if self.exact else "derived_from_timetable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_days": self.file.service_days,
            "effective_date": self.file.effective_date,
            "first_departure": self.first_departure,
            "last_departure": self.last_departure,
            "departures_per_day": self.departures_per_day,
            "run_time_min": self.run_time_min,
            "verification_status": self.verification_status,
            "source": f"臺北市政府資料平台〈{self.file.title}〉",
        }


def _service_day_rank(service_days: str) -> int:
    try:
        return _PREFERRED_SERVICE_DAYS.index(service_days)
    except ValueError:
        return len(_PREFERRED_SERVICE_DAYS)


def match_service(
    station_ids: list[str], files: list[TimetableFile]
) -> tuple[TimetableFile, RouteTimetable, bool] | None:
    """找出哪一份來源的哪一組營運路線對得上這個班次。

    條件有三個，缺一不可：終點站相同、本班次的每一站都在那一組裡、起站在
    那一組裡有發車。只比終點站會把「象山—北投」與「大安—北投」混為一談，
    只比車站集合則會漏掉方向。

    完全相同（``exact``）的優先於只是子集的，平日的優先於假日的：遊戲要的
    是最貼近日常的那一份。
    """
    wanted = set(station_ids)
    candidates: list[tuple[int, int, TimetableFile, RouteTimetable, bool]] = []

    for file in files:
        for route in file.routes:
            if route.destination != station_ids[-1]:
                continue
            if not wanted <= route.station_ids:
                continue
            if route.first_departure(station_ids[0]) is None:
                continue
            exact = wanted == route.station_ids
            candidates.append(
                (0 if exact else 1, _service_day_rank(file.service_days), file, route, exact)
            )

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2].path.name))
    _, _, file, route, exact = candidates[0]
    return file, route, exact


def build_match(
    train_number: str, station_ids: list[str], files: list[TimetableFile]
) -> ScheduleMatch | None:
    """為一個班次建立時刻資料；對不上或串不起來時回傳 ``None``。

    取的是**當天第一班**：首班車是唯一每天都存在、而且不受尖峰加班影響的
    一班，拿它當代表最不會誤導。串不起來時往後試下一班，因為第一班偶爾會
    是只跑一小段的區間車。
    """
    matched = match_service(station_ids, files)
    if matched is None:
        return None
    file, route, exact = matched

    origin_departures = route.departures[station_ids[0]]
    trip: dict[str, int] | None = None
    for start in origin_departures:
        trip = chain_trip(route, station_ids, start)
        if trip is not None:
            break
    if trip is None:
        return None

    ordered = [sid for sid in station_ids if sid in trip]
    return ScheduleMatch(
        train_number=train_number,
        file=file,
        route=route,
        departures={sid: _to_clock(trip[sid]) for sid in ordered},
        first_departure=_to_clock(origin_departures[0]),
        last_departure=_to_clock(origin_departures[-1]),
        departures_per_day=len(origin_departures),
        run_time_min=(
            trip[ordered[-1]] - trip[ordered[0]] if len(ordered) > 1 else None
        ),
        exact=exact,
    )


# ----------------------------------------------------------------------
# 匯入
# ----------------------------------------------------------------------
@dataclass
class TimetableImportResult:
    """匯入結果。"""

    timetables: dict[str, Any]
    matched: list[ScheduleMatch] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_mrt_timetables(
    source_dir: str | Path,
    data_dir: str | Path,
    *,
    existing_payload: dict[str, Any] | None = None,
) -> TimetableImportResult:
    """把時刻表 CSV 的內容併進現有的 ``timetables.json``。

    只動 ``departure_times`` 與 ``schedule``：車站、停靠表、車輛型式全部
    來自維基百科那一路的匯入，時刻表沒有資格改它們。

    對不上的班次**保持原樣**並列在報告裡。捷運公布的時刻表只涵蓋部分路線
    （文湖線、環狀線、三鶯線、機場捷運都沒有），把它們補成空白或推估值只會
    讓人以為那是真的（§2.3）。

    ``existing_payload`` 給定時直接拿它比對，不讀磁碟上的 ``timetables.json``
    ——與 ``--source`` 合併執行且 ``--dry-run`` 時磁碟還是重建前的舊資料，
    這時要比對的是這次重建出、還沒寫入的候選資料，讀舊檔只會得到不準確的
    預覽。
    """
    files = read_timetable_dir(source_dir)
    if existing_payload is not None:
        payload = existing_payload
    else:
        target = Path(data_dir)
        payload = json.loads((target / "timetables.json").read_text(encoding="utf-8"))

    matched: list[ScheduleMatch] = []
    unmatched: list[str] = []

    for service in payload.get("services", ()):
        station_ids = list(service.get("stop_station_ids", ()))
        match = build_match(service["train_number"], station_ids, files) if station_ids else None
        if match is None:
            unmatched.append(service["train_number"])
            continue
        service["departure_times"] = dict(match.departures)
        # 來源只公布發車時刻，沒有到達時刻，因此不填也不推估（§2.3）。
        service["arrival_times"] = {}
        service["schedule"] = match.as_dict()
        matched.append(match)

    payload["meta"] = _meta(files, payload.get("meta", {}))

    report = [
        f"讀取 {len(files)} 份時刻表：",
        *(f"  {f.path.name}（{f.service_days}，{f.effective_date} 啟用）" for f in files),
        f"對上時刻的營運模式 {len(matched)} 個，沒有對應來源的 {len(unmatched)} 個。",
    ]
    for match in matched:
        report.append(
            f"  {match.train_number}：{match.first_departure} 首班、"
            f"{match.last_departure} 末班，每日 {match.departures_per_day} 班"
            + (
                f"，{match.run_time_min} 分"
                if match.run_time_min is not None
                else ""
            )
            + f"（{match.verification_status}）"
        )
    warnings = []
    if unmatched:
        warnings.append(
            "以下營運模式在來源時刻表中沒有對應，發車時刻保持空白："
            + "、".join(unmatched)
        )
    return TimetableImportResult(
        timetables=payload,
        matched=matched,
        unmatched=unmatched,
        report=report,
        warnings=warnings,
    )


def _meta(files: list[TimetableFile], previous: dict[str, Any]) -> dict[str, Any]:
    """更新 ``timetables.json`` 的說明。

    原本寫著「捷運不公布逐班時刻」——對台北捷運這四條線而言已經不成立了，
    留著會誤導後面的人以為沒有資料可用。
    """
    meta = dict(previous)
    meta["description"] = (
        "捷運營運模式。有公布逐班時刻的路線（北市府資料平台的板南線、"
        "中和新蘆線、松山新店線、淡水信義線）帶有實際發車時刻，其餘路線"
        "只有營運模式。"
    )
    meta["timetable_policy"] = (
        "departure_times 取自來源時刻表的**當天第一班**，逐站以時間串接："
        "來源的序號是「本站的第幾班」而不是車次，串不起來的班次寧可留白。"
        "來源只公布發車時刻，終點站因此不出現，arrival_times 一律留空——"
        "推估到達時刻等於把猜測當成事實（§2.3）。"
    )
    meta["timetable_sources"] = [
        {
            "file": file.path.name,
            "service_days": file.service_days,
            "effective_date": file.effective_date,
        }
        for file in files
    ]
    provenance = dict(meta.get("provenance", {}))
    provenance["timetable_source"] = "臺北市政府資料平台　捷運各線時刻表（北市平台版）"
    provenance["timetable_generator"] = "railway_sim.dataset.mrt_timetable"
    meta["provenance"] = provenance
    return meta


def apply_mrt_timetables(
    result: TimetableImportResult, data_dir: str | Path
) -> list[Path]:
    """把匯入結果寫回 ``timetables.json``。

    與其他匯入採同一套安全流程：先寫進暫存目錄，用遊戲本身的載入器驗證過
    才取代正式檔案，驗證失敗時正式資料完全不受影響。

    Raises:
        ValueError: 匯入結果沒有通過驗證。
    """
    from railway_sim.data_loader import load_game_data

    data_path = Path(data_dir)
    heal_interrupted_import(data_path)

    staging = Path(tempfile.mkdtemp(prefix=IMPORT_STAGING_PREFIX, dir=data_path))
    try:
        text = json.dumps(result.timetables, ensure_ascii=False, indent=2) + "\n"
        (staging / "timetables.json").write_text(text, encoding="utf-8", newline="\n")

        # 驗證用的暫存目錄要自成一體：把本次不改動的資料檔一起複製進去。
        for name in ("stations.json", "routes.json", "trains.json"):
            shutil.copyfile(data_path / name, staging / name)
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

        target = data_path / "timetables.json"
        if target.is_file():
            shutil.copyfile(target, staging / "timetables.json.bak")
        os.replace(staging / "timetables.json", target)
        return [target]
    finally:
        shutil.rmtree(staging, ignore_errors=True)
