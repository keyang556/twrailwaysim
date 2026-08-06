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
DEFAULT_SERVICE = "2115"

# Keep Chinese CLI output usable when a Windows process inherits a western code
# page (for example, a frozen executable run by GitHub Actions).
_UNICODE_OUTPUT_PROBE = "臺灣鐵路人員模擬器（司機員模式）：區間車 2701 次＋§"


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
        name_zh_tw="區間車 2115 次：豐原往彰化，各站停車",
        service_number="2115",
        description="包含成功站停靠（成功站僅停靠區間車）。",
    ),
    "express": Scenario(
        id="express",
        name_zh_tw="區間快 2021 次：豐原往彰化，通過成功站",
        service_number="2021",
        description="與 2115 次同一條路線，可直接比較停靠與通過的差別。",
    ),
    "tze_chiang": Scenario(
        id="tze_chiang",
        name_zh_tw="自強號 101 次：臺中往潮州",
        service_number="101",
        description="長途對號列車，通過成功站（規格 §25.7）。",
    ),
    "chengzhui": Scenario(
        id="chengzhui",
        name_zh_tw="區間車 2600 次：臺中往大甲，經成追線",
        service_number="2600",
        description=(
            "由臺中經成功站轉成追線接入海線，不經彰化（規格 §10.4）。"
            "時刻表以車次後綴「追」標示經由成追線。"
        ),
    ),
    "red_signal": Scenario(
        id="red_signal",
        name_zh_tw="區間車 2115 次＋前方列車故障",
        service_number="2115",
        obstruction_at_m=28500.0,
        description=(
            "特殊事件：新烏日與成功之間前方有列車故障占用區間，"
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


def _configure_text_stream(stream: object | None) -> None:
    """Use UTF-8 when *stream* cannot encode the application's CLI text.

    A Windows console using a Chinese code page can retain its native encoding,
    while redirected streams with a western code page are switched to UTF-8.
    This prevents status and diagnostic output from terminating the process.
    """
    if stream is None:
        return

    encoding = getattr(stream, "encoding", None)
    if encoding:
        try:
            _UNICODE_OUTPUT_PROBE.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            pass
        else:
            return

    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return

    try:
        reconfigure(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        # Some IDE and test-capture streams intentionally do not support
        # reconfiguration. Preserve their existing behaviour in that case.
        return


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
        "--keymap-profile",
        default="driver",
        help=(
            "鍵位配置。預設 driver：與 OpenBVE 相同的鍵位。"
            "driver_legacy 為先前的 D 電門／A 制軔配置。"
        ),
    )
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
    _configure_text_stream(sys.stdout)
    _configure_text_stream(sys.stderr)
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
    try:
        keymap = Keymap.from_dict(data.keymap_raw, args.keymap_profile)
    except KeyError as exc:
        print(f"鍵位配置載入失敗：{exc}", file=sys.stderr)
        return 2
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
