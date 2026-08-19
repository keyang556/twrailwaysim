"""時刻表匯入指令。

臺鐵改點後，把新的 ``.ods`` 放進一個資料夾，執行：

```bash
python -m railway_sim.dataset --source <時刻表資料夾>
```

就會重新產生 ``data/stations.json``、``data/routes.json`` 與
``data/timetables.json``。加上 ``--dry-run`` 可以只看報告不寫檔。

若來源出現對照表沒有的站名，匯入會中止並列出站名，請先在
``data/station_registry.json`` 補上再重新執行——這是刻意的，避免把不認得
的車站悄悄漏掉。

捷運那一套走同一個指令，只是換一個 ``--system``：

```bash
python -m railway_sim.dataset --system mrt --source <維基百科條目資料夾>
```

來源是各線條目的網頁存檔，產生的是 ``data/mrt/`` 底下同名的三個檔案。
哪些車站已通車、跑哪些營運模式寫在 ``data/mrt/line_spec.json``（見
:mod:`railway_sim.dataset.mrt`）。

條目沒有時刻。北市府資料平台公布的逐班時刻表另外匯入：

```bash
python -m railway_sim.dataset --system mrt --timetables <CSV 資料夾>
```

只會改動班次的發車時刻與時刻摘要，車站、路網、停靠表都不動（見
:mod:`railway_sim.dataset.mrt_timetable`）。兩個來源可以一起給，此時先由
條目重建資料集，再把時刻補上去。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from railway_sim.data_loader import default_data_dir, load_game_data
from railway_sim.dataset.build import build_dataset, write_dataset
from railway_sim.dataset.mrt import MrtBuildError, build_mrt_dataset, write_mrt_dataset
from railway_sim.dataset.mrt_timetable import (
    MrtTimetableError,
    apply_mrt_timetables,
    build_mrt_timetables,
)
from railway_sim.dataset.ods import OdsReadError
from railway_sim.dataset.registry import UnknownStationError
from railway_sim.systems import SYSTEMS, system_data_dir


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m railway_sim.dataset",
        description="把公開的時刻表或條目資料匯入成遊戲資料檔。",
    )
    parser.add_argument(
        "--system",
        choices=tuple(SYSTEMS),
        default="tra",
        help="要匯入哪一個系統的資料。tra：臺鐵 .ods 時刻表；mrt：捷運維基百科條目。",
    )
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "來源資料夾。臺鐵為 .ods 時刻表資料夾，捷運為維基百科條目存檔資料夾。"
            "捷運只匯入時刻表時可以不給。"
        ),
    )
    parser.add_argument(
        "--timetables",
        default=None,
        metavar="資料夾",
        help=(
            "捷運專用：北市府資料平台的時刻表 CSV 資料夾。只會補上班次的發車"
            "時刻與時刻摘要，不動車站、路網與停靠表。"
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help="資料目錄，預設自動尋找專案的 data 目錄。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只顯示報告，不寫入任何檔案。",
    )
    parser.add_argument(
        "--show-skipped",
        action="store_true",
        help="列出所有被略過的班次與原因。",
    )
    return parser


def _run_mrt(args: argparse.Namespace, data_dir: Path) -> int:
    """匯入捷運資料。

    兩個來源各管各的：``--source`` 是維基百科條目（車站、路網、營運模式），
    ``--timetables`` 是北市府資料平台的逐班時刻。兩個都給時先重建資料集再
    補時刻——順序反過來的話，重建會把剛補上的時刻蓋掉。

    報告、``--dry-run`` 與寫入前的驗證流程與臺鐵那一路完全相同，因此使用者
    兩邊記同一組用法就夠。
    """
    if not args.source and not args.timetables:
        print("捷運匯入需要 --source（條目存檔）或 --timetables（時刻表 CSV）。",
              file=sys.stderr)
        return 2

    if args.source:
        code = _run_mrt_dataset(args, data_dir)
        if code != 0:
            return code

    if args.timetables:
        code = _run_mrt_timetables(args, data_dir)
        if code != 0:
            return code

    data = load_game_data(data_dir.parent, system="mrt")
    scheduled = sum(1 for s in data.services.values() if s.schedule is not None)
    print(
        f"\n資料驗證通過：車站 {len(data.stations)} 站、"
        f"路線 {len(data.routes)} 條、營運模式 {len(data.services)} 種"
        f"（其中 {scheduled} 種有公布時刻）。"
    )
    return 0


def _run_mrt_dataset(args: argparse.Namespace, data_dir: Path) -> int:
    """由維基百科條目重建捷運資料集。"""
    try:
        result = build_mrt_dataset(args.source, data_dir)
    except (FileNotFoundError, MrtBuildError) as exc:
        print(f"匯入失敗：{exc}", file=sys.stderr)
        return 2

    for line in result.report:
        print(line)
    for warning in result.warnings:
        print(f"注意：{warning}")

    if args.dry_run:
        print("\n--dry-run：未寫入任何檔案。")
        return 0

    try:
        written = write_mrt_dataset(result, data_dir)
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, OSError) as exc:
        print(f"\n寫入失敗：{exc}", file=sys.stderr)
        return 2

    print("\n已寫入：")
    for path in written:
        print(f"  {path}")
    return 0


def _run_mrt_timetables(args: argparse.Namespace, data_dir: Path) -> int:
    """把北市府資料平台的逐班時刻補進班次。"""
    try:
        result = build_mrt_timetables(args.timetables, data_dir)
    except (FileNotFoundError, MrtTimetableError, UnicodeDecodeError) as exc:
        print(f"時刻表匯入失敗：{exc}", file=sys.stderr)
        return 2

    for line in result.report:
        print(line)
    for warning in result.warnings:
        print(f"注意：{warning}")

    if args.dry_run:
        print("\n--dry-run：未寫入任何檔案。")
        return 0

    try:
        written = apply_mrt_timetables(result, data_dir)
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, OSError) as exc:
        print(f"\n寫入失敗：{exc}", file=sys.stderr)
        return 2

    print("\n已寫入：")
    for path in written:
        print(f"  {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    root = Path(args.out) if args.out else default_data_dir()
    data_dir = system_data_dir(root, args.system)
    if args.system == "mrt":
        return _run_mrt(args, data_dir)

    if args.timetables:
        print("--timetables 只有捷運有；臺鐵的時刻表用 --source 指定。", file=sys.stderr)
        return 2
    if not args.source:
        # --source 對捷運才是選用的（可以只補時刻表），臺鐵沒有它就無事可做。
        print("臺鐵匯入需要 --source（.ods 時刻表資料夾）。", file=sys.stderr)
        return 2

    try:
        result = build_dataset(args.source, data_dir)
    except (FileNotFoundError, UnknownStationError, OdsReadError) as exc:
        print(f"匯入失敗：{exc}", file=sys.stderr)
        return 2

    for line in result.report:
        print(line)

    if args.show_skipped and result.skipped:
        print("\n略過的班次：")
        for item in result.skipped:
            print(f"  - {item}")

    if args.dry_run:
        print("\n--dry-run：未寫入任何檔案。")
        return 0

    # write_dataset() 在暫存目錄內先驗證過完整資料集才會覆寫正式檔案，
    # 因此驗證失敗時正式目錄完全不受影響——這裡不需要再另外檢查一次。
    try:
        written = write_dataset(result, data_dir)
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, OSError) as exc:
        print(f"\n寫入失敗：{exc}", file=sys.stderr)
        return 2

    print("\n已寫入：")
    for path in written:
        print(f"  {path}")

    data = load_game_data(data_dir)
    print(
        f"\n資料驗證通過：車站 {len(data.stations)} 站、"
        f"路線 {len(data.routes)} 條、班次 {len(data.services)} 班。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
