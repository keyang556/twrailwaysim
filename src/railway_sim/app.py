"""應用程式進入點（規格 §24 M0「建立基本啟動入口」）。

啟動前一律先執行資料驗證與鍵位衝突檢查（§2.1、§7.1）。任何一項不通過就
拒絕啟動並印出原因，避免以錯誤資料開始遊戲。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from railway_sim import __version__
from railway_sim.accessibility.announcer import Announcer
from railway_sim.accessibility.speech import NvdaController, create_screen_reader
from railway_sim.audio.player import (
    AudioPlayer,
    PlayerCreation,
    create_player_with_diagnostics,
)
from railway_sim.data_loader import GameData, default_data_dir, load_game_data
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession
from railway_sim.systems import DEFAULT_SYSTEM, SYSTEMS

__all__ = [
    "build_session",
    "main",
    "resolve_scenario",
    "scenario_for_service",
    "service_menu_lines",
    "start_choice_key",
    "start_choices",
    "start_session",
    "system_choices",
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

# Keep this path fixed: a frozen-build smoke test must fail if this particular
# Ogg asset is omitted, rather than passing because some unrelated data survived.
_AUDIO_SMOKE_CLIP = Path("audio/announcements/xinbeitou/R22A.arrive.ogg")


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


def start_session(
    data: GameData, key: str, player: AudioPlayer | None = None
) -> tuple[DriverSession, Announcer]:
    """由識別字串建立工作階段，供視窗介面在遊戲中換車次。"""
    if key.startswith(SCENARIO_KEY_PREFIX):
        scenario = SCENARIOS[key[len(SCENARIO_KEY_PREFIX) :]]
    elif key.startswith(SERVICE_KEY_PREFIX):
        scenario = scenario_for_service(data, key[len(SERVICE_KEY_PREFIX) :])
    else:
        scenario = scenario_for_service(data, key)
    announcer = Announcer()
    return build_session(data, scenario, announcer, player), announcer


def system_choices() -> list:
    """開場的系統選擇項目（臺鐵／捷運）。

    兩個介面共用同一份，因此主控台與視窗版看到的選項一定一致（§25.5）。
    """
    from railway_sim.ui.wx_app import StartChoice

    return [
        StartChoice(
            key=system.id,
            label=f"{system.name_zh_tw}（{system.id}）",
            detail=system.description,
        )
        for system in SYSTEMS.values()
    ]


def start_choices(data: GameData) -> list:
    """車次選擇視窗的完整選項。

    情境只屬於臺鐵（成追線、山線海線那幾個），因此捷運模式下只列營運模式，
    不會出現開不起來的情境。
    """
    from railway_sim.ui.wx_app import StartChoice

    choices = [
        StartChoice(
            key=f"{SCENARIO_KEY_PREFIX}{scenario.id}",
            label=f"【情境】{scenario.name_zh_tw}",
            detail=scenario.description,
        )
        for scenario in SCENARIOS.values()
        if data.system.id == DEFAULT_SYSTEM
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
        stock_name = stock.name_zh_tw if stock else service.rolling_stock_id
        # 捷運沒有對外公布的車次，玩家認的是「哪一條線的哪一種營運模式」，
        # 因此直接用班次名稱；臺鐵維持「車種＋車次」的既有寫法。
        label = (
            f"{service.name_zh_tw}　{stock_name}"
            if data.system.id != DEFAULT_SYSTEM
            else f"{class_name}{number}次　{origin}－{destination}　{stock_name}"
        )
        detail = (
            f"路線：{route.name_zh_tw if route else ''}　"
            f"停靠 {len(service.stop_station_ids)} 站"
        )
        # 有公布時刻的營運模式把首末班一起列出來：選車次時最想知道的就是
        # 「這一班什麼時候跑」，翻到行前提要才看得到等於晚了一步。
        if service.schedule is not None:
            schedule = service.schedule
            detail += (
                f"　{schedule.service_days}首班 {schedule.first_departure}、"
                f"末班 {schedule.last_departure}"
            )
        choices.append(
            StartChoice(
                key=f"{SERVICE_KEY_PREFIX}{number}",
                label=label,
                detail=detail,
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
        # 線別也列出來，「三鶯線」「山線」這種以線為單位的關鍵字才找得到——
        # 路線名稱只寫起訖站（「頂埔至鶯桃福德」），不含線名。
        line_name = data.line_names.get(route.line_id, "") if route else ""
        line = (
            f"{number}\t{class_name}\t{service.rolling_stock_id}\t"
            f"{origin}－{destination}\t{route.name_zh_tw if route else ''}\t{line_name}"
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
    player: AudioPlayer | None = None,
) -> DriverSession:
    """依情境建立司機員工作階段。"""
    session = DriverSession(
        data=data,
        service=data.service(scenario.service_number),
        announcer=announcer or Announcer(),
        player=player,
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


def _ask_system() -> str:
    """主控台的系統選擇。

    用 ``input()`` 而不是即時按鍵：這一步在遊戲開始之前，畫面上有完整的選項
    文字，螢幕閱讀器讀得到；輸入錯了重問一次即可，不需要任何特殊鍵盤處理。
    """
    options = list(SYSTEMS.values())
    print("===== 選擇鐵路系統 =====", flush=True)
    for index, system in enumerate(options, start=1):
        print(f"{index}：{system.name_zh_tw}", flush=True)
        if system.description:
            print(f"    {system.description}", flush=True)
    prompt = f"請輸入 1 到 {len(options)}（直接按 Enter 使用{options[0].name_zh_tw}）："

    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            # 非互動式執行（管線、測試）沒有輸入可讀，用預設值繼續。
            return DEFAULT_SYSTEM
        if not answer:
            return options[0].id
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1].id
        if answer in SYSTEMS:
            return answer
        print(f"請輸入 1 到 {len(options)}。", flush=True)


def _ask_service(data: GameData) -> str | None:
    """主控台的車次選擇。回傳車次；玩家放棄時回傳 ``None``。

    只有沒有預設情境的系統（捷運）會用到：臺鐵的預設情境本來就開得起來，
    多問一次只是擋路。
    """
    numbers = sorted(data.services, key=lambda n: (len(n), n))
    print(f"===== 選擇{data.system.name_zh_tw}營運模式 =====", flush=True)
    for index, number in enumerate(numbers, start=1):
        service = data.services[number]
        stock = data.train_types.get(service.rolling_stock_id)
        print(
            f"{index}：{number}　{service.name_zh_tw}　"
            f"{stock.name_zh_tw if stock else service.rolling_stock_id}",
            flush=True,
        )
    prompt = f"請輸入 1 到 {len(numbers)}，或直接輸入車次（按 Enter 離開）："

    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(numbers):
            return numbers[int(answer) - 1]
        if answer in data.services:
            return answer
        print(f"請輸入 1 到 {len(numbers)}，或一個存在的車次。", flush=True)


def _audio_unavailable_message(creation: PlayerCreation) -> str:
    details = "；".join(creation.diagnostics)
    return "無法使用音訊播放後端，廣播仍會以文字顯示。" f"診斷：{details}"


def _check_audio(data_dir: str | None) -> int:
    """驗證目前程序可用 pygame 載入一個隨附的廣播音檔。"""
    creation = create_player_with_diagnostics()
    player = creation.player
    if player is None:
        print("Audio check failed: no audio backend is available.", file=sys.stderr)
        for detail in creation.diagnostics:
            print(f"- {detail}", file=sys.stderr)
        return 2

    try:
        if player.backend_name != "pygame":
            print(
                "Audio check failed: expected the bundled pygame backend, "
                f"but selected {player.backend_name}.",
                file=sys.stderr,
            )
            for detail in creation.diagnostics:
                print(f"- {detail}", file=sys.stderr)
            return 2

        root = Path(data_dir) if data_dir is not None else default_data_dir()
        clip = root / _AUDIO_SMOKE_CLIP
        if not clip.is_file():
            print(
                f"Audio check failed: bundled clip is missing: {clip}",
                file=sys.stderr,
            )
            return 2
        if not player.can_load(clip):
            print(
                f"Audio check failed: pygame could not load bundled clip: {clip}",
                file=sys.stderr,
            )
            return 2
        print(f"pygame audio backend loaded bundled clip: {_AUDIO_SMOKE_CLIP}")
        return 0
    finally:
        player.close()


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
        "--system",
        choices=tuple(SYSTEMS),
        default=None,
        help=(
            "要駕駛哪一個鐵路系統。tra：臺鐵；mrt：捷運（含 ATO 與自動駕駛）。"
            "未指定時會先詢問。"
        ),
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
        "--no-audio",
        action="store_true",
        help=(
            "不播放車上廣播音檔。廣播內容仍會以文字送出（規格 §20.1），"
            "因此關掉聲音不會少掉任何資訊。"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只執行資料驗證與鍵位衝突檢查後結束，不啟動遊戲。",
    )
    parser.add_argument(
        "--check-audio",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--check-nvda-controller",
        action="store_true",
        help=argparse.SUPPRESS,
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

    if args.check_nvda_controller:
        controller = NvdaController()
        if not controller.client_loaded:
            print("NVDA Controller Client could not be loaded.", file=sys.stderr)
            return 2
        # A release build must be able to load its DLL even when NVDA is not
        # running in CI. Connection state stays dynamic at game runtime.
        connection = "connected" if controller.available else "not connected"
        print(f"NVDA Controller Client loaded ({connection}).")
        return 0

    if args.check_audio:
        return _check_audio(args.data_dir)

    if args.list_scenarios:
        for scenario in SCENARIOS.values():
            print(f"{scenario.id}：{scenario.name_zh_tw}")
            if scenario.description:
                print(f"    {scenario.description}")
        return 0

    # --- 系統（臺鐵／捷運）--------------------------------------------
    # 只有主控台會在這裡問；視窗版留到 run_wx 才問，因為它得先開視窗才有
    # 地方顯示選項，而且要能在遊戲中換系統。
    system = args.system
    if system is None and args.ui != "wx":
        # --check 與 --list-services 不是遊戲流程，問玩家反而擋住自動化用途。
        inspecting = args.check or args.list_services is not None
        system = DEFAULT_SYSTEM if inspecting else _ask_system()

    # --- 資料驗證 -----------------------------------------------------
    try:
        data = load_game_data(args.data_dir, system=system or DEFAULT_SYSTEM)
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
        print("車次\t車種\t車輛型式\t起訖\t路線\t線別")
        for line in lines:
            print(line)
        print(f"共 {len(lines)} 個車次。用 --service <車次> 直接駕駛。")
        return 0

    # --- 啟動 ---------------------------------------------------------
    if args.scenario and data.system.id != DEFAULT_SYSTEM:
        print(
            f"--scenario 只有臺鐵有；{data.system.name_zh_tw}請改用 --service 指定營運模式，"
            "或用 --list-services 查詢。",
            file=sys.stderr,
        )
        return 2
    try:
        scenario = resolve_scenario(data, args.scenario, args.service)
    except KeyError as exc:
        print(f"{exc.args[0]}。用 --list-services 查詢可用車次。", file=sys.stderr)
        return 2
    # 螢幕閱讀器輸出（語音＋點字）。可以直接當成 speak(text, interrupt) 用，
    # 因此只送語音的主控台介面不必知道點字的存在。
    speak = create_screen_reader()
    # 播放後端是選用的：放不出聲音時廣播仍以文字送出（§20.1）。
    creation = None if args.no_audio else create_player_with_diagnostics()
    player = None if creation is None else creation.player
    audio_warning = None
    if creation is not None and player is None:
        audio_warning = _audio_unavailable_message(creation)

    if args.ui == "wx":
        try:
            # wx_app 只在函式內部匯入 wx，因此這裡必須自己確認 wxPython 裝好了，
            # 否則缺少 wxPython 時玩家看到的是例外堆疊而不是這行說明。
            import wx

            from railway_sim.ui.wx_app import run_wx
        except ImportError:
            if player is not None:
                player.close()
            print(
                "找不到 wxPython，請改用 --ui console 或安裝：pip install wxPython",
                file=sys.stderr,
            )
            return 2
        # 沒有指定車次時先開車次選擇視窗：視窗版沒有命令列可以下 --service，
        # 少了這一步，安裝版就永遠只能開同一個預設車次。
        initial_key = None if scenario is None else start_choice_key(scenario)
        loaded: dict[str, GameData] = {data.system.id: data}

        def open_system(system_id: str):
            """視窗版換系統時才載入那一套資料，並記住已載入的。

            兩套資料一起載入要多花一次完整驗證的時間，只玩其中一邊的人不必付
            這個代價；換過去之後留在快取裡，來回切換就不會重複載入。
            """
            if system_id not in loaded:
                loaded[system_id] = load_game_data(args.data_dir, system=system_id)
            game_data = loaded[system_id]
            return start_choices(game_data), (
                lambda key: start_session(game_data, key, player)
            )

        try:
            return run_wx(
                open_system,
                keymap,
                speak,
                systems=system_choices(),
                initial_system=system,
                initial_key=initial_key,
                startup_message=audio_warning,
            )
        finally:
            if player is not None:
                player.close()

    from railway_sim.ui.console_app import ConsoleApp

    if audio_warning is not None:
        print(f"警告：{audio_warning}", file=sys.stderr)

    if scenario is None:
        if data.system.id == DEFAULT_SYSTEM:
            scenario = SCENARIOS[DEFAULT_SCENARIO]
        else:
            # 捷運沒有預設情境（那幾個情境都是臺鐵的山線海線題目），因此改問
            # 要開哪一種營運模式——視窗版本來就會問，主控台不該少一步。
            number = _ask_service(data)
            if number is None:
                return 0
            scenario = scenario_for_service(data, number)

    announcer = Announcer()
    session = build_session(data, scenario, announcer, player)
    try:
        return ConsoleApp(session, keymap, announcer, speak).run()
    finally:
        if player is not None:
            player.close()
