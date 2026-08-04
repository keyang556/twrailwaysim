"""應用程式進入點（規格 §24 M0「建立基本啟動入口」）。

啟動前一律先執行資料驗證與鍵位衝突檢查（§2.1、§7.1）。任何一項不通過就
拒絕啟動並印出原因，避免以錯誤資料開始遊戲。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from railway_sim import __version__
from railway_sim.accessibility.announcer import Announcer
from railway_sim.accessibility.speech import create_speech_sink
from railway_sim.data_loader import GameData, load_game_data
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession

__all__ = ["build_session", "main"]

#: 預設車次（區間車，停靠成功站）。
DEFAULT_SERVICE = "2701"


@dataclass(frozen=True)
class Scenario:
    """啟動情境。"""

    id: str
    name_zh_tw: str
    service_number: str
    obstruction_at_m: float | None = None
    description: str = ""


SCENARIOS: dict[str, Scenario] = {
    "local": Scenario(
        id="local",
        name_zh_tw="區間車 2701 次：臺中往彰化，各站停車",
        service_number="2701",
        description="包含成功站停靠（成功站僅停靠區間車）。",
    ),
    "express": Scenario(
        id="express",
        name_zh_tw="自強號 121 次：臺中往彰化，通過成功站",
        service_number="121",
        description="驗證自強號在成功站為通過站（規格 §25.7）。",
    ),
    "red_signal": Scenario(
        id="red_signal",
        name_zh_tw="區間車 2701 次＋前方列車故障",
        service_number="2701",
        obstruction_at_m=12500.0,
        description=(
            "特殊事件：成功站南方分歧前方有列車故障占用區間，"
            "號誌顯示停止，必須在號誌前停車"
            "（規格 §12.3：站外等待屬特殊事件，非日常狀態）。"
        ),
    ),
}


def build_session(
    data: GameData,
    scenario: Scenario,
    announcer: Announcer | None = None,
) -> DriverSession:
    """依情境建立司機員工作階段。"""
    session = DriverSession(
        data=data,
        service=data.service(scenario.service_number),
        announcer=announcer or Announcer(),
    )
    if scenario.obstruction_at_m is not None:
        session.add_obstruction("T_FAULT", scenario.obstruction_at_m)
    return session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="railway-sim",
        description="臺灣鐵路人員模擬器（司機員模式）",
    )
    parser.add_argument("--version", action="version", version=f"railway-sim {__version__}")
    parser.add_argument(
        "--ui",
        choices=("console", "wx"),
        default="console",
        help="介面種類。預設 console：純文字輸出，螢幕閱讀器可直接朗讀。",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        default="local",
        help="啟動情境。",
    )
    parser.add_argument("--data-dir", default=None, help="資料目錄，預設自動尋找。")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只執行資料驗證與鍵位衝突檢查後結束，不啟動遊戲。",
    )
    parser.add_argument(
        "--list-scenarios", action="store_true", help="列出可用情境後結束。"
    )
    parser.add_argument(
        "--check-gui",
        action="store_true",
        help="Check that the wx user interface can be imported without opening a window.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """程式進入點。回傳結束碼。"""
    args = _build_parser().parse_args(argv)

    if args.check_gui:
        try:
            import wx
            from railway_sim.ui.wx_app import run_wx
        except ImportError:
            print("wxPython is not available.", file=sys.stderr)
            return 2
        # Referencing the import keeps static analysers from treating it as a
        # disposable import while avoiding a window in automated smoke tests.
        _ = run_wx
        print(f"wxPython {wx.version()} is available.")
        return 0

    if args.list_scenarios:
        for scenario in SCENARIOS.values():
            print(f"{scenario.id}：{scenario.name_zh_tw}")
            if scenario.description:
                print(f"    {scenario.description}")
        return 0

    # --- 資料驗證 -----------------------------------------------------
    try:
        data = load_game_data(args.data_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"資料載入失敗：{exc}", file=sys.stderr)
        return 2

    if data.issues:
        print("資料驗證未通過：", file=sys.stderr)
        for issue in data.issues:
            print(f"- {issue}", file=sys.stderr)
        return 2

    # --- 鍵位衝突檢查（§2.1、§7.1）------------------------------------
    keymap = Keymap.from_dict(data.keymap_raw, "driver")
    conflicts = keymap.conflicts()
    if conflicts:
        print("快捷鍵衝突：", file=sys.stderr)
        for conflict in conflicts:
            print(f"- {conflict}", file=sys.stderr)
        return 2

    if args.check:
        print(f"資料驗證通過：車站 {len(data.stations)} 站、"
              f"路線 {len(data.routes)} 條、班次 {len(data.services)} 班。")
        print(f"鍵位檢查通過：{len(keymap.bindings)} 個動作，無衝突。")
        return 0

    # --- 啟動 ---------------------------------------------------------
    scenario = SCENARIOS[args.scenario]
    announcer = Announcer()
    session = build_session(data, scenario, announcer)
    speak = create_speech_sink()

    if args.ui == "wx":
        try:
            from railway_sim.ui.wx_app import run_wx
        except ImportError:
            print(
                "找不到 wxPython，請改用 --ui console 或安裝：pip install wxPython",
                file=sys.stderr,
            )
            return 2
        return run_wx(session, keymap, announcer, speak)

    from railway_sim.ui.console_app import ConsoleApp

    return ConsoleApp(session, keymap, announcer, speak).run()
