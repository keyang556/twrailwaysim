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
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from railway_sim.data_loader import default_data_dir, load_game_data
from railway_sim.dataset.build import build_dataset, write_dataset
from railway_sim.dataset.ods import OdsReadError
from railway_sim.dataset.registry import UnknownStationError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m railway_sim.dataset",
        description="把臺鐵公布的 .ods 時刻表匯入成遊戲資料檔。",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="存放臺鐵 .ods 時刻表的資料夾。",
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    data_dir = Path(args.out) if args.out else default_data_dir()
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
