"""廣播音檔匯入與檢查的命令列工具。

用法::

    # 看看會怎麼改名，不動任何檔案
    python -m railway_sim.audio import --dry-run "…/台鐵廣播/1縱貫北" "…/台鐵廣播/2山線"

    # 實際匯入（廣播改版時重跑同一行即可，內容沒變的檔案不會重寫）
    python -m railway_sim.audio import "…/台鐵廣播/1縱貫北" "…/台鐵廣播/2山線"

    # 檢查目前索引到哪些廣播、哪些停靠站還沒有
    python -m railway_sim.audio check

捷運的廣播用同一個指令，只是換一個 ``--system``（來源檔名的對照規則放在
``data/mrt/audio/source_map.json``）::

    python -m railway_sim.audio --system mrt import "…/mrt/廣播/淡水信義線"

音檔一律匯入共用的 ``data/audio/announcements``，依 ``line_id`` 分資料夾；
兩個系統的線別代碼不會相撞，因此不需要各自一份。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from railway_sim.audio.importer import (
    SOURCE_MAP_FILENAME,
    SourceMap,
    apply_plan,
    plan_import,
)
from railway_sim.audio.library import BroadcastLibrary, default_announcement_dir
from railway_sim.data_loader import default_data_dir, load_game_data
from railway_sim.systems import SYSTEMS, system_data_dir


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m railway_sim.audio",
        description="車上廣播音檔的匯入與檢查工具",
    )
    parser.add_argument("--data-dir", default=None, help="資料目錄，預設自動尋找。")
    parser.add_argument(
        "--system",
        choices=tuple(SYSTEMS),
        default="tra",
        help="要處理哪一個系統的廣播。預設 tra（臺鐵）。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    importer = sub.add_parser("import", help="把來源資料夾的廣播匯入 data/audio/announcements")
    importer.add_argument("sources", nargs="+", help="來源資料夾（例如 1縱貫北、2山線）")
    importer.add_argument(
        "--dry-run", action="store_true", help="只顯示會怎麼改名，不實際複製。"
    )

    sub.add_parser("check", help="顯示目前索引到的廣播，並列出還沒有廣播的停靠站")
    return parser


def _run_import(args: argparse.Namespace, data_dir: Path) -> int:
    source_map_path = system_data_dir(data_dir, args.system) / SOURCE_MAP_FILENAME
    try:
        source_map = SourceMap.load(source_map_path)
    except (OSError, ValueError) as exc:
        print(f"讀取 {source_map_path} 失敗：{exc}", file=sys.stderr)
        return 2

    data = load_game_data(data_dir, system=args.system)
    # 目的地一律是共用的 data/audio/announcements，因此這裡傳的是 data 目錄
    # 本身而不是系統子目錄。
    plan = plan_import(list(args.sources), data_dir, data.station_names(), source_map)

    for line in plan.report_lines():
        print(line)

    if args.dry_run:
        print("（--dry-run：未實際複製任何檔案）")
        return 0

    counts = apply_plan(plan)
    print(
        f"完成：新增 {counts['added']} 個、更新 {counts['updated']} 個、"
        f"內容未變 {counts['unchanged']} 個。"
    )
    return 0


def _run_check(data_dir: Path, system: str) -> int:
    data = load_game_data(data_dir, system=system)
    library = BroadcastLibrary.load(default_announcement_dir(data_dir))

    print(f"廣播資料夾：{library.root}")
    print(f"已索引音檔：{len(library)} 個，涵蓋 {len(library.station_ids())} 站。")
    for warning in library.warnings:
        print(f"注意：{warning}")

    # 只列出「已經有部分廣播」的路線缺哪幾站。整條線都還沒錄的（大部分路線
    # 目前如此）列出來只是雜訊，新站通車時要找的是前者。
    by_line: dict[str, list[tuple[str, str, bool]]] = {}
    for station in data.stations.values():
        covered = library.has_any(station.id)
        for line_id in station.line_ids:
            by_line.setdefault(line_id, []).append((station.id, station.name_zh_tw, covered))

    print("各路線覆蓋率（該站有沒有任何一則廣播）：")
    for line_id in sorted(by_line):
        stations = by_line[line_id]
        covered = [s for s in stations if s[2]]
        line_name = data.line_names.get(line_id, line_id)
        print(f"  {line_id}（{line_name}）：{len(covered)} / {len(stations)} 站")
        if not covered:
            continue
        for station_id, name, is_covered in sorted(stations):
            if not is_covered:
                print(f"      缺少：{station_id}\t{name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    if args.command == "import":
        return _run_import(args, data_dir)
    return _run_check(data_dir, args.system)


if __name__ == "__main__":  # pragma: no cover - 命令列進入點
    raise SystemExit(main())
