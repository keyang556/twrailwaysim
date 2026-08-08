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

__all__ = [
    "build_session",
    "main",
    "resolve_scenario",
    "scenario_for_service",
    "service_menu_lines",
    "start_choice_key",
    "start_choices",
    "start_session",
]

#: 預設車次（區間車，停靠成功站）。
DEFAULT_SERVICE = "2115"

#: 未指定情境也未指定車次時，主控台介面使用的情境。
DEFAULT_SCENARIO = "local"

#: :func:`start_session` 的識別字串前綴。
SCENARIO_KEY_PREFIX = "scenario:"
SERVICE_KEY_PREFIX = "service:"

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


def scenario_for_service(data: GameData, train_number: str) -> Scenario:
    """把任一個車次包成情境，讓 ``--service`` 不限於預設情境。

    Raises:
        KeyError: 時刻表中沒有這個車次。
    """
    service = data.service(train_number)
    class_name = data.service_class_name(service.train_type)
    route = data.routes.get(service.route_id)
    stock = data.train_types.get(service.rolling_stock_id)
    return Scenario(
        id=f"{SERVICE_KEY_PREFIX}{train_number}",
        name_zh_tw=f"{class_name}{train_number}次：{route.name_zh_tw if route else ''}",
        service_number=train_number,
        description=f"車輛型式：{stock.name_zh_tw if stock else service.rolling_stock_id}",
    )


def resolve_scenario(
    data: GameData, scenario_id: str | None, train_number: str | None
) -> Scenario | None:
    """把命令列參數解析成情境。

    Returns:
        兩者都沒指定時回傳 ``None``，代表「由介面自己問玩家要開哪一班」。

    Raises:
        KeyError: 指定的車次不存在。
    """
    if train_number:
        return scenario_for_service(data, train_number)
    if scenario_id:
        return SCENARIOS[scenario_id]
    return None


def start_choice_key(scenario: Scenario) -> str:
    """情境對應到 :func:`start_session` 的識別字串。"""
    if scenario.id.startswith(SERVICE_KEY_PREFIX):
        return scenario.id
    return f"{SCENARIO_KEY_PREFIX}{scenario.id}"


def start_session(data: GameData, key: str) -> tuple[DriverSession, Announcer]:
    """由識別字串建立工作階段，供視窗介面在遊戲中換車次。"""
    if key.startswith(SCENARIO_KEY_PREFIX):
        scenario = SCENARIOS[key[len(SCENARIO_KEY_PREFIX) :]]
    elif key.startswith(SERVICE_KEY_PREFIX):
        scenario = scenario_for_service(data, key[len(SERVICE_KEY_PREFIX) :])
    else:
        scenario = scenario_for_service(data, key)
    announcer = Announcer()
    return build_session(data, scenario, announcer), announcer


def start_choices(data: GameData) -> list:
    """車次選擇視窗的完整選項：五個預設情境，加上時刻表裡的每一個車次。"""
    from railway_sim.ui.wx_app import StartChoice

    choices = [
        StartChoice(
            key=f"{SCENARIO_KEY_PREFIX}{scenario.id}",
            label=f"【情境】{scenario.name_zh_tw}",
            detail=scenario.description,
        )
        for scenario in SCENARIOS.values()
    ]

    names = data.station_names()
    for number in sorted(data.services, key=lambda n: (len(n), n)):
        service = data.services[number]
        class_name = data.service_class_name(service.train_type)
        route = data.routes.get(service.route_id)
        stock = data.train_types.get(service.rolling_stock_id)
        stops = service.stop_station_ids
        origin = names.get(stops[0], "") if stops else ""
        destination = names.get(stops[-1], "") if stops else ""
        along = "、".join(
            names.get(sid, "")
            for sid in (*service.stop_station_ids, *service.pass_station_ids)
        )
        choices.append(
            StartChoice(
                key=f"{SERVICE_KEY_PREFIX}{number}",
                label=(
                    f"{class_name}{number}次　{origin}－{destination}　"
                    f"{stock.name_zh_tw if stock else service.rolling_stock_id}"
                ),
                detail=(
                    f"路線：{route.name_zh_tw if route else ''}　"
                    f"停靠 {len(service.stop_station_ids)} 站"
                ),
                search_text=along,
            )
        )
    return choices


def service_menu_lines(data: GameData, keyword: str = "") -> list[str]:
    """可駕駛車次的一行摘要，依車次排序。

    Args:
        keyword: 過濾用關鍵字。除了摘要本身，也會比對該班次**沿途每一站**的
            站名，因此「集集」找得到停靠集集站但起訖不是集集的班次。
    """
    keyword = keyword.strip()
    names = data.station_names()
    lines: list[str] = []
    for number in sorted(data.services, key=lambda n: (len(n), n)):
        service = data.services[number]
        class_name = data.service_class_name(service.train_type)
        route = data.routes.get(service.route_id)
        stops = service.stop_station_ids
        origin = names.get(stops[0], "") if stops else ""
        destination = names.get(stops[-1], "") if stops else ""
        line = (
            f"{number}\t{class_name}\t{service.rolling_stock_id}\t"
            f"{origin}－{destination}\t{route.name_zh_tw if route else ''}"
        )
        if keyword:
            along = [
                names.get(sid, "")
                for sid in (*service.stop_station_ids, *service.pass_station_ids)
            ]
            if keyword not in line and keyword not in "".join(along):
                continue
        lines.append(line)
    return lines


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
        default=None,
        help=(
            "啟動情境。主控台介面未指定時使用 local；"
            "視窗介面未指定時會先開啟車次選擇視窗。"
        ),
    )
    parser.add_argument(
        "--service",
        default=None,
        metavar="車次",
        help=(
            "直接指定車次（例如 --service 1801）。時刻表裡的任何一個車次都可以，"
            "指定後會蓋過 --scenario。用 --list-services 查詢。"
        ),
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
        "--list-services",
        nargs="?",
        const="",
        default=None,
        metavar="關鍵字",
        help=(
            "列出可駕駛的車次後結束。可加關鍵字過濾車次、車種、車輛型式、"
            "起訖站或路線名稱，例如 --list-services 集集。"
        ),
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

    if args.list_services is not None:
        lines = service_menu_lines(data, args.list_services)
        print("車次\t車種\t車輛型式\t起訖\t路線")
        for line in lines:
            print(line)
        print(f"共 {len(lines)} 個車次。用 --service <車次> 直接駕駛。")
        return 0

    # --- 啟動 ---------------------------------------------------------
    try:
        scenario = resolve_scenario(data, args.scenario, args.service)
    except KeyError as exc:
        print(f"{exc.args[0]}。用 --list-services 查詢可用車次。", file=sys.stderr)
        return 2
    speak = create_speech_sink()

    if args.ui == "wx":
        try:
            # wx_app 只在函式內部匯入 wx，因此這裡必須自己確認 wxPython 裝好了，
            # 否則缺少 wxPython 時玩家看到的是例外堆疊而不是這行說明。
            import wx

            from railway_sim.ui.wx_app import run_wx
        except ImportError:
            print(
                "找不到 wxPython，請改用 --ui console 或安裝：pip install wxPython",
                file=sys.stderr,
            )
            return 2
        # 沒有指定車次時先開車次選擇視窗：視窗版沒有命令列可以下 --service，
        # 少了這一步，安裝版就永遠只能開同一個預設車次。
        initial_key = None if scenario is None else start_choice_key(scenario)
        return run_wx(
            start_choices(data),
            lambda key: start_session(data, key),
            keymap,
            speak,
            initial_key=initial_key,
        )

    from railway_sim.ui.console_app import ConsoleApp

    announcer = Announcer()
    session = build_session(data, scenario or SCENARIOS[DEFAULT_SCENARIO], announcer)
    return ConsoleApp(session, keymap, announcer, speak).run()
