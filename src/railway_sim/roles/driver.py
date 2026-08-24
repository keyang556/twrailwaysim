"""司機員模式（規格 §4、§8、§9、§14）。

本模組把物理、閉塞、號誌、ATP、停靠判斷與無障礙播報接在一起，形成一個
可完整測試的運轉工作階段。介面層（wx 或主控台）只負責送入按鍵與輸出
文字，不含任何運轉邏輯，確保必要資訊不會只存在於視覺介面（§25.5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from railway_sim.accessibility import messages as msg
from railway_sim.accessibility.announcer import Announcer, Priority
from railway_sim.audio.broadcast import BroadcastSystem, RunState
from railway_sim.audio.mrt_broadcast import MrtBroadcastSystem
from railway_sim.audio.player import AudioPlayer
from railway_sim.data_loader import GameData
from railway_sim.events.event_bus import EventBus
from railway_sim.events.incidents import IncidentLog
from railway_sim.railway.block import BlockSystem
from railway_sim.railway.interlocking import Interlocking
from railway_sim.railway.route import Route, RouteStop
from railway_sim.railway.signal import SignalSystem
from railway_sim.simulation import braking, doors
from railway_sim.simulation.ato import DEFAULT_DOOR_SIDE, AtoController
from railway_sim.simulation.atp import AtpMonitor, AtpState, SpeedRestriction
from railway_sim.simulation.clock import SIMULATION_TICK_S, SimulationClock
from railway_sim.simulation.physics import step as physics_step
from railway_sim.simulation.position import describe_position
from railway_sim.simulation.train import Train, TrainType
from railway_sim.timetable.door_side import DoorSideRule
from railway_sim.timetable.service import Service
from railway_sim.timetable.stop_pattern import resolve_stop_kind

__all__ = [
    "STATUS_ITEM_ACTIONS",
    "STATUS_ITEM_LABELS",
    "STOP_WINDOW_M",
    "DriverSession",
    "StationProgress",
    "StatusItem",
]

#: 停車範圍（公尺）。車頭超過停車點加上此距離仍未停妥即為應停未停（§9.2）。
#: 真實月台長度無可靠公開來源（§27），此為第一版測試值。
STOP_WINDOW_M = 50.0

#: 開始播報「接近某站」的距離（公尺）。
APPROACH_ANNOUNCE_M = 800.0

#: 停車位置倒數的距離門檻（公尺，由遠而近）。
#:
#: 愈近愈密，是因為看不見月台標記的司機**只能靠這串數字**判斷還有多遠。
#: 遠處每次減速的效果很明顯，一百公尺報一次就夠；最後十公尺內列車已經在
#: 徐行，每一句之間仍有一兩秒，卻是決定停得準不準的關鍵。
#:
#: 門檻只是「什麼時候開口」，播報的是**當下的實際距離**而不是門檻值：
#: 一個步長內跨過好幾個門檻時（速度還很快）只會報一次，不會連珠炮。
STOP_COUNTDOWN_M: tuple[float, ...] = (
    200.0, 150.0, 100.0, 70.0, 50.0, 35.0, 25.0, 20.0, 15.0, 10.0, 7.0, 5.0, 3.0, 2.0, 1.0,
)

#: 點字即時顯示改用停車位置距離的門檻（公尺）。
#:
#: 再遠一點的時候，「離車站還有多遠」與「離停車位置還有多遠」是同一個數字；
#: 進到這個範圍才有分別，也才是需要細一點解析度的時候。
BRAILLE_FINE_RANGE_M = 200.0

#: 停妥後前進修正時，位置變動超過這個距離才重新播報（公尺）。
#:
#: 太小會在列車還在滑行的最後幾公分一直重播，太大則修正了也聽不出差別。
REALIGN_STEP_M = 0.5

#: 月台範圍的半長（公尺）：以停車位置為中心，前後各算這麼長。
#:
#: 真實月台長度無可靠公開來源（§27），與 :data:`STOP_WINDOW_M` 同樣是第一版
#: 的測試值。取「以停車位置為中心」而不是「停車位置往後算一個月台長」，是
#: 因為車頭停妥時車身還壓在月台上，通過的列車也一樣——整列車完全離開月台
#: 之前都還在月台範圍內。
PLATFORM_ZONE_M = 100.0

#: 狀態查詢項目的顯示名稱。
#:
#: 介面只負責呈現，項目與內容一律由本模組提供，兩個介面才會完全一致
#: （§25.5：必要資訊不得只存在於單一介面）。
STATUS_ITEM_LABELS: tuple[tuple[str, str], ...] = (
    ("speed", "速度"),
    ("position", "位置"),
    ("next_station", "下一站"),
    ("stop_point", "停車位置"),
    ("signal", "前方號誌"),
    ("train", "列車狀態"),
    ("doors", "車門狀態"),
    ("service", "車次資訊"),
    ("summary", "運轉摘要"),
)

#: 狀態項目對應的鍵位動作代碼，供介面在選單裡標出按鍵。
#:
#: 沒有列在這裡的項目（車次資訊、運轉摘要）沒有專屬快捷鍵，只從選單查詢；
#: 快捷鍵不必為了湊齊項目而占用一堆字母鍵。
STATUS_ITEM_ACTIONS: dict[str, str] = {
    "speed": "announce_speed",
    "position": "announce_position",
    "next_station": "announce_next_station",
    "stop_point": "announce_stop_point",
    "signal": "announce_signal",
    "train": "announce_train_status",
    "doors": "announce_doors",
}


def _braille_metres(metres: float) -> str:
    """點字用的距離寫法：短、固定、一摸就懂。

    刻意用阿拉伯數字與 ``m``／``km``，不用中文數字：點字的中文數字要好幾方，
    而距離是**一直在變**的欄位，長度浮動會讓摸讀的人每次都要重新找位置。
    """
    metres = max(0.0, float(metres))
    if metres >= 1000.0:
        return f"{metres / 1000.0:.1f}km"
    if metres >= 10.0:
        return f"{metres:.0f}m"
    return f"{metres:.1f}m"


@dataclass(frozen=True)
class StatusItem:
    """一項狀態查詢的結果。

    ``text`` 同時是螢幕上顯示的文字與播報出去的文字：兩者必須一致，否則
    看得到的和聽得到的會不一樣。
    """

    code: str
    label: str
    text: str


@dataclass
class StationProgress:
    """路線上一個車站的運轉進度。"""

    stop: RouteStop
    station_id: str
    name_zh_tw: str
    stop_kind: str
    approach_announced: bool = False
    served: bool = False
    passed: bool = False
    missed: bool = False
    stop_offset_m: float | None = None

    countdown_index: int = 0
    """已播報過幾個停車位置倒數門檻（見 :data:`STOP_COUNTDOWN_M`）。"""

    stop_point_announced: bool = False
    """車頭到達停車位置的那一句是否已播報過。"""

    @property
    def must_stop(self) -> bool:
        return self.stop_kind == "stop"

    @property
    def position_m(self) -> float:
        return self.stop.position_m


@dataclass
class DriverSession:
    """一次司機員運轉工作階段。"""

    data: GameData
    service: Service
    announcer: Announcer
    bus: EventBus = field(default_factory=EventBus)
    clock: SimulationClock = field(default_factory=SimulationClock)
    incidents: IncidentLog = field(default_factory=IncidentLog)
    player: AudioPlayer | None = None
    """播放廣播音檔的後端；``None`` 表示這台機器放不出聲音（§20.1）。"""

    route: Route = field(init=False)
    spec: TrainType = field(init=False)
    train: Train = field(init=False)
    blocks: BlockSystem = field(init=False)
    signals: SignalSystem = field(init=False)
    interlocking: Interlocking = field(init=False)
    atp: AtpMonitor = field(init=False)
    ato: AtoController = field(init=False)
    broadcast: BroadcastSystem = field(init=False)
    stations: list[StationProgress] = field(init=False, default_factory=list)

    finished: bool = field(default=False, init=False)
    last_state: AtpState | None = field(default=None, init=False)
    last_status: StatusItem | None = field(default=None, init=False)
    """最近一次狀態查詢的結果，供介面顯示該項目（不是整份狀態）。"""

    _previous_stop_id: str | None = field(default=None, init=False, repr=False)
    """最近停妥過的停靠站。廣播用它判斷「從哪裡來」與目前在哪一個區間。"""

    _unscheduled_stop_announced: bool = field(default=False, init=False, repr=False)
    """這一次站外停車的臨停廣播是否已播過。重新起動後歸零。"""

    _aligning_stop: StationProgress | None = field(default=None, init=False, repr=False)
    """剛停妥、還可以前進修正停車位置的那一站。離開停車範圍後歸零。"""

    _departed_aligning_stop: bool = field(default=False, init=False, repr=False)
    """``_aligning_stop`` 這一站是否已經開始離站。

    判斷準則不是「動了沒有」：前進修正本身就要靠動力向前推一點，那一瞬間
    列車一定不是靜止的。真正的準則是移動當下車頭有沒有到達或超過停車
    位置——未達停車位置時的移動是修正，到達後還繼續移動才是離站（見
    :meth:`_handle_realignment`）。離站後在停車範圍內若因號誌或緊急制軔
    等原因再次停下，``is_stopped`` 會重新變成真，但那不是回到同一次對位；
    這個旗標一旦設成真就不會再歸假，直到下一站重新停妥為止（見
    :meth:`stop_alignment_target`）。
    """

    # ------------------------------------------------------------------
    # 建立
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        self.route = self.data.route(self.service.route_id)
        self.spec = self.data.train_type(self.service.rolling_stock_id)

        self.train = Train(
            id=f"T{self.service.train_number}",
            train_type=self.spec.id,
            service_class=self.service.train_type,
            train_number=self.service.train_number,
            position_m=0.0,
            direction=self.route.direction,
            current_route_id=self.route.id,
            length_m=self.spec.length_m,
        )

        self.blocks = BlockSystem.from_route(self.route, self.data.network)
        self.signals = SignalSystem.from_blocks(
            self.route, self.blocks, station_names=self.data.station_names()
        )
        self.interlocking = Interlocking(
            network=self.data.network, blocks=self.blocks, signals=self.signals
        )
        self.atp = AtpMonitor(
            route=self.route,
            signals=self.signals,
            blocks=self.blocks,
            spec=self.spec,
            station_names=self.data.station_names(),
        )

        # 自動駕駛（§20.3）。臺鐵沒有 ATO，因此系統本身就不支援時直接關掉，
        # 不是「有按鍵但沒反應」。
        line = self.data.line(self.route.line_id)
        self.ato = AtoController(
            spec=self.spec,
            available=self.data.system.supports_ato and line.ato,
            driverless=line.driverless,
        )

        self.broadcast = self._build_broadcast()

        self._build_station_progress()
        self._build_platform_zones()
        self._update_occupancy()
        self._refresh_stop_target()

    def _build_broadcast(self) -> BroadcastSystem:
        """建立車上廣播（§20.2）。

        沒有廣播設備的車型（DR1000）連文字都不送出，因為那台車根本沒有播出
        任何東西。捷運與臺鐵的播放時機規則不同，因此用不同的實作；判斷依據是
        **這條線登記的廣播樣式**，不是「哪一個系統」——樣式寫在資料裡，日後
        多一種樣式不必改這裡。
        """
        common = {
            "library": self.data.broadcasts,
            "announcer": self.announcer,
            "player": self.player,
            "enabled": self.spec.has_broadcast,
            "line_id": self.route.line_id,
            "called_station_ids": tuple(self.service.stop_station_ids),
            "rolling_stock_id": self.spec.id,
            "boarding_notice": self.spec.boarding_notice,
            "door_sides": DoorSideRule.from_services(
                self.data.services.values()
            ).sides_for(self.service),
        }
        style = self.data.line(self.route.line_id).announcement_style
        if not style:
            return BroadcastSystem(**common)  # type: ignore[arg-type]

        final = self.service.stop_station_ids[-1] if self.service.stop_station_ids else ""
        return MrtBroadcastSystem(
            **common,  # type: ignore[arg-type]
            rules=self.data.broadcast_rules.for_line(self.route.line_id, style),
            terminus_name=self.data.stations[final].name_zh_tw if final else "",
        )

    def _build_station_progress(self) -> None:
        """依班次停靠表建立每站的停靠別（§9.1）。"""
        self.stations = []
        for stop in self.route.stops:
            station = self.data.stations[stop.station_id]
            kind = resolve_stop_kind(self.service, station)
            progress = StationProgress(
                stop=stop,
                station_id=station.id,
                name_zh_tw=station.name_zh_tw,
                stop_kind=kind,
            )
            # 起點站視為已服務，避免一啟動就判定應停未停。
            if stop.position_m <= 0.0:
                progress.served = True
                progress.approach_announced = True
            self.stations.append(progress)

    def _build_platform_zones(self) -> None:
        """建立「通過不停靠車站」的月台速限（§14.1 區間速限的一種）。

        只在本線登記了 ``platform_pass_limit_kmh`` 時才有東西可建：台北捷運
        各線站站停車，不會有通過的情形，因此沒有值是正確的，不是資料缺漏。

        停靠站不列入——停靠站本來就要停下來，再壓一個通過速限沒有意義，而且
        會讓停車前的允許速度多一個看不出理由的天花板。班次的停靠表在整趟
        運轉中不會變，因此只建一次。
        """
        limit = self.data.line(self.route.line_id).platform_pass_limit_kmh
        if limit is None:
            self.atp.zone_restrictions = ()
            return
        self.atp.zone_restrictions = tuple(
            SpeedRestriction(
                kind="platform_pass",
                limit_kmh=limit,
                position_m=progress.position_m - PLATFORM_ZONE_M,
                end_m=progress.position_m + PLATFORM_ZONE_M,
                label=f"{progress.name_zh_tw}站月台",
            )
            for progress in self.stations
            if not progress.must_stop
        )

    # ------------------------------------------------------------------
    # 特殊事件（§12.3）
    # ------------------------------------------------------------------
    def add_obstruction(
        self,
        train_id: str,
        position_m: float,
        length_m: float = 100.0,
        *,
        incident_kind: str = "train_failure",
        description: str = "前方列車故障，占用區間",
    ) -> None:
        """在前方放置一列占用閉塞的列車。

        這是規格 §12.3 定義的**特殊事件**，只能由外部明確觸發，系統不會
        自行產生（§12.2、chat.md「站外等待為特殊事件」）。
        """
        self.blocks.update_occupancy(train_id, position_m - length_m, position_m)
        self.incidents.record_incident(
            incident_kind, description, at_time_s=self.clock.elapsed_s
        )
        self.bus.publish("incident", kind=incident_kind, description=description)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def power_up(self) -> None:
        """增加電門（Z）。

        自動駕駛中，這個鍵就是**發車鍵**：ATO 已經在控制電門，再手動加一段
        沒有意義，而「關門之後啟動列車」正是駕駛在自動駕駛下唯一要做的操作
        （§20.3）。想手動加電門請先解除自動駕駛。
        """
        if self.ato.engaged:
            self.ato_depart()
            return
        result = braking.power_up(self.train, self.spec)
        if not result.accepted and result.reason == "emergency":
            self.announcer.announce(msg.power_blocked_by_emergency(), Priority.SAFETY)
            return
        if not result.accepted and result.reason == "doors_open":
            self.announcer.announce(msg.power_blocked_by_doors(), Priority.SAFETY)
            return
        if not result.accepted and result.reason == "max_power":
            self.announcer.announce(
                f"電門已在最高{msg.num_to_zh(self.spec.power_notches)}段。",
                Priority.ACTION,
                dedupe_key="power_max",
            )
            return
        self.announcer.announce(msg.power_notch(result.power_notch), Priority.ACTION)
        self.bus.publish("power_notch", notch=result.power_notch)

    def brake_up(self) -> None:
        """增加常用制軔（A）。"""
        result = braking.brake_up(self.train, self.spec)
        if not result.accepted and result.reason == "emergency":
            self.announcer.announce(msg.emergency_brake_applied(), Priority.SAFETY,
                                    dedupe_key="emergency_active")
            return
        if not result.accepted and result.reason == "max_brake":
            self.announcer.announce(
                f"制軔已在最高{msg.num_to_zh(self.spec.brake_notches)}段。",
                Priority.ACTION,
                dedupe_key="brake_max",
            )
            return
        self.announcer.announce(msg.brake_notch(result.brake_notch), Priority.ACTION)
        self.bus.publish("brake_notch", notch=result.brake_notch)

    def notch_down(self) -> None:
        """回到惰行或降低控制段位（S）。"""
        result = braking.notch_down(self.train, self.spec)
        if not result.accepted and result.reason == "emergency":
            self.announcer.announce(msg.emergency_brake_applied(), Priority.SAFETY,
                                    dedupe_key="emergency_active")
            return
        if not result.accepted:
            self.announcer.announce(msg.coasting(), Priority.ACTION, dedupe_key="coasting")
            return
        if result.power_notch == 0 and result.brake_notch == 0:
            self.announcer.announce(msg.coasting(), Priority.ACTION)
        elif result.power_notch > 0:
            self.announcer.announce(msg.power_notch(result.power_notch), Priority.ACTION)
        else:
            self.announcer.announce(msg.brake_notch(result.brake_notch), Priority.ACTION)
        self.bus.publish(
            "notch_down", power=result.power_notch, brake=result.brake_notch
        )

    def single_brake(self) -> None:
        """單手把往制軔方向移動一段（Q，OpenBVE 的 ``SINGLE_BRAKE``）。

        與 :meth:`notch_down` 方向相反：有電門時先減電門，電門為零之後改為
        加制軔，因此一路按下去就是 ``P5…P1 → 惰行 → B1…B7``。
        """
        result = braking.single_brake(self.train, self.spec)
        if not result.accepted and result.reason == "emergency":
            self.announcer.announce(
                msg.emergency_brake_applied(), Priority.SAFETY,
                dedupe_key="emergency_active",
            )
            return
        if not result.accepted and result.reason == "max_brake":
            self.announcer.announce(
                f"制軔已在最高{msg.num_to_zh(self.spec.brake_notches)}段。",
                Priority.ACTION,
                dedupe_key="brake_max",
            )
            return
        if result.power_notch > 0:
            self.announcer.announce(msg.power_notch(result.power_notch), Priority.ACTION)
        elif result.brake_notch == 0:
            self.announcer.announce(msg.coasting(), Priority.ACTION)
        else:
            self.announcer.announce(msg.brake_notch(result.brake_notch), Priority.ACTION)
        self.bus.publish(
            "single_brake", power=result.power_notch, brake=result.brake_notch
        )

    def release_brake(self) -> None:
        """鬆軔（R）。不得自動增加電門（§8.4）。"""
        result = braking.release_brake(self.train, self.spec)
        if not result.accepted and result.reason == "emergency":
            self.announcer.announce(msg.emergency_brake_blocked(), Priority.SAFETY)
            return
        # 無論是否原本就已緩解，都要播報目前制軔狀態（§8.4）。
        self.announcer.announce(msg.brake_notch(result.brake_notch), Priority.ACTION)
        self.bus.publish("release_brake", brake=result.brake_notch)

    def emergency_brake(self) -> None:
        """緊急制軔（Space）。"""
        braking.apply_emergency(self.train, self.spec)
        self.announcer.announce(
            msg.emergency_brake_applied(), Priority.EMERGENCY, dedupe_key="emergency_applied"
        )
        self.bus.publish("emergency_brake", applied=True)

    def release_emergency(self) -> None:
        """解除緊急制軔（E）。須列車停妥（§8.3）。"""
        result = braking.release_emergency(self.train, self.spec)
        if result.accepted:
            self.announcer.announce(msg.emergency_brake_released(), Priority.NOTICE)
            self.bus.publish("emergency_brake", applied=False)
            return
        if result.reason == "not_stopped":
            self.announcer.announce(msg.emergency_brake_blocked(), Priority.SAFETY)
        else:
            self.announcer.announce("目前未施加緊急制軔。", Priority.ACTION)

    def horn(self) -> None:
        """鳴笛（Enter）。"""
        self.announcer.announce(msg.horn(), Priority.ACTION)
        self.bus.publish("horn")

    # ------------------------------------------------------------------
    # 方向把手（F／V，取自 OpenBVE 的 REVERSER_FORWARD／REVERSER_BACKWARD）
    # ------------------------------------------------------------------
    def reverser_forward(self) -> None:
        """方向把手往前進方向移動一段（F）。"""
        self._move_reverser(1)

    def reverser_backward(self) -> None:
        """方向把手往後退方向移動一段（V）。

        停過頭時退回停車位置就是靠這個鍵：先停妥、把手退到後退位、加電門，
        列車就會往回走。
        """
        self._move_reverser(-1)

    def _move_reverser(self, step: int) -> None:
        result = braking.move_reverser(self.train, step)
        if not result.accepted and result.reason == "not_stopped":
            self.announcer.announce(
                msg.reverser_blocked_by_movement(), Priority.SAFETY
            )
            return
        if not result.accepted:
            self.announcer.announce(
                msg.reverser_at_end(self.train.reverser),
                Priority.ACTION,
                dedupe_key="reverser_end",
            )
            return
        self.announcer.announce(
            msg.reverser_position(self.train.reverser), Priority.ACTION
        )
        self.bus.publish("reverser", position=self.train.reverser)

    # ------------------------------------------------------------------
    # 自動駕駛（Alt+Shift+U，§20.3）
    # ------------------------------------------------------------------
    def toggle_ato(self) -> None:
        """啟動或解除自動駕駛（Alt＋Shift＋U）。

        臺鐵沒有 ATO，因此在臺鐵模式下會明確說「本系統沒有自動駕駛功能」，
        而不是靜靜地沒反應——按鍵一定要有回饋（§7.2）。
        """
        if self.ato.engaged:
            self.ato.disengage()
            self.announcer.announce(msg.ato_disengaged(), Priority.NOTICE)
            self.bus.publish("ato", engaged=False)
            return
        if not self.ato.engage():
            self.announcer.announce(msg.ato_unavailable(), Priority.ACTION)
            return
        # 接手時先把手動殘留的段位清掉，控制律下一個步長就會重新給值。
        self.train.power_notch = 0
        self.announcer.announce(
            msg.ato_engaged(driverless=self.ato.driverless), Priority.NOTICE
        )
        # 就地登記目前停在哪一站，否則玩家一啟動就按發車，會在下一個步長被
        # 「剛到站」的重設吃掉。
        self._register_stop(self.stopped_at())
        self.bus.publish("ato", engaged=True, driverless=self.ato.driverless)

    def _register_stop(self, here: StationProgress | None) -> None:
        """登記目前停妥的車站，換站時提醒駕駛該發車了。

        ATO 不會自己開走，駕駛不知道「現在輪到我」就會一直等下去；無人駕駛線
        不需要這一句，因為根本不用駕駛動手。
        """
        if not self.ato.arrive_at(here.station_id if here is not None else None):
            return
        if here is not None and not self.ato.driverless:
            self.announcer.announce(
                msg.ato_awaiting_departure(here.name_zh_tw), Priority.NOTICE
            )

    def ato_depart(self) -> None:
        """自動駕駛的發車鍵（F4）。

        關門之後按下，列車才會啟動；這是一般捷運線上駕駛唯一要做的操作。
        無人駕駛線不需要按——停站時間到了電腦自己會關門發車；在車門已經關上
        的空檔按下去只會讓它早一點開走。
        """
        if not self.ato.engaged:
            self.announcer.announce(msg.ato_not_engaged(), Priority.ACTION)
            return
        if self.train.any_door_open:
            self.announcer.announce(msg.power_blocked_by_doors(), Priority.SAFETY)
            return
        self.ato.authorise_departure()
        self.announcer.announce(msg.ato_departed(), Priority.ACTION)
        self.bus.publish("ato_depart")

    def _drive_with_ato(self, dt_s: float) -> None:
        """把 ATO 的決定寫進列車狀態。

        ATO 是電腦，直接給段位而不是一段一段推手把；但它不會覆蓋緊急制軔，
        也不會在車門開著時加電門（決策本身就已經排除這兩種情況）。
        """
        if not self.ato.engaged:
            return

        here = self.stopped_at()
        if self.train.is_stopped:
            self._register_stop(here)
        if self.ato.driverless:
            self._drive_doors(dt_s, here)

        holding = (
            here is not None
            and not self.ato.departure_authorised
            and self.train.is_stopped
        )
        target = self.next_scheduled_stop()
        state = self.last_state or self._evaluate_only()
        decision = self.ato.decide(
            self.train,
            permitted_kmh=state.permitted_kmh,
            distance_to_stop_m=(
                target.position_m - self.train.position_m if target is not None else None
            ),
            holding=holding,
        )
        if decision.reason == "emergency":
            return
        self.train.power_notch = decision.power_notch
        self.train.brake_notch = decision.brake_notch

    def _drive_doors(self, dt_s: float, here: StationProgress | None) -> None:
        """無人駕駛的車門與發車（文湖線、環狀線、三鶯線）。

        月台在哪一側不在來源資料裡（§22、§27），因此固定開
        :data:`~railway_sim.simulation.ato.DEFAULT_DOOR_SIDE` 那一側並在文件
        中說明，不假裝知道每一站的開門方向。
        """
        if here is None or not self.train.is_stopped:
            return
        if not self.train.any_door_open and self.ato.dwell_remaining_s > 0.0:
            self._toggle_doors(DEFAULT_DOOR_SIDE)
        if self.ato.tick_dwell(dt_s):
            if self.train.any_door_open:
                self._toggle_doors(DEFAULT_DOOR_SIDE)
            self.ato.authorise_departure()

    # ------------------------------------------------------------------
    # 車門（F5／F6，取自 OpenBVE 的 DOORS_LEFT／DOORS_RIGHT）
    # ------------------------------------------------------------------
    def toggle_left_doors(self) -> None:
        """開關左側車門（F5）。"""
        self._toggle_doors("left")

    def toggle_right_doors(self) -> None:
        """開關右側車門（F6）。"""
        self._toggle_doors("right")

    def _toggle_doors(self, side: str) -> None:
        """同一個鍵開也關，與 OpenBVE 的車門鍵一致。"""
        opening = not (
            self.train.left_doors_open if side == "left" else self.train.right_doors_open
        )
        result = doors.set_doors(self.train, side, opening=opening)

        if not result.accepted:
            if result.reason == "not_stopped":
                self.announcer.announce(
                    msg.door_blocked_by_movement(side), Priority.SAFETY
                )
            return

        text = msg.door_opened(side) if result.opened else msg.door_closed(side)
        self.announcer.announce(text, Priority.NOTICE)
        # 車門動作有對應的車上廣播（§20.2「車門聲」）。
        self.broadcast.announce_doors(side, opening=result.opened)
        self.bus.publish("doors", side=side, open=result.opened)

    # ------------------------------------------------------------------
    # 狀態查詢
    # ------------------------------------------------------------------
    # 每一項只在被查詢時才產生。這是 OpenBVE 無障礙模式的做法，也是本專案
    # 選擇的做法：常駐顯示整份狀態會讓螢幕閱讀器一直重讀沒有變動的內容，
    # 玩家反而要自己在一大段文字裡找需要的那一行。
    def status_item(self, code: str) -> StatusItem:
        """單一狀態項目。

        ``text`` 就是播報出去的那一句：顯示與朗讀必須是同一份文字。

        Raises:
            KeyError: 沒有這個項目代碼。
        """
        labels = dict(STATUS_ITEM_LABELS)
        if code not in labels:
            raise KeyError(f"沒有這個狀態項目：{code}")
        return StatusItem(code=code, label=labels[code], text=self._status_text(code))

    def status_items(self) -> list[StatusItem]:
        """所有狀態項目，供介面建立選單。"""
        return [self.status_item(code) for code, _ in STATUS_ITEM_LABELS]

    def announce_status(self, code: str) -> StatusItem:
        """查詢並播報一個狀態項目，同時記在 :attr:`last_status`。"""
        item = self.status_item(code)
        self.last_status = item
        self.announcer.announce(item.text, Priority.ACTION)
        self.bus.publish("status_query", code=item.code, text=item.text)
        return item

    def _status_text(self, code: str) -> str:
        state = self.last_state or self._evaluate_only()

        if code == "speed":
            return msg.speed_report(self.train.current_speed_kmh, state.permitted_kmh)

        if code == "position":
            report = describe_position(
                self.route,
                self.train.position_m,
                self.data.station_names(),
                self.data.line_names,
            )
            return msg.position_report(
                report.line_name,
                report.from_station_name,
                report.to_station_name,
                report.distance_to_next_m,
            )

        if code == "next_station":
            upcoming = self.next_station()
            if upcoming is None:
                return msg.no_station_ahead()
            return msg.next_station(
                upcoming.name_zh_tw,
                upcoming.position_m - self.train.position_m,
                upcoming.stop_kind,
            )

        if code == "stop_point":
            target = self.stop_alignment_target()
            if target is None:
                return msg.no_stop_point_ahead()
            return msg.stop_point_report(
                target.name_zh_tw, target.position_m - self.train.position_m
            )

        if code == "signal":
            if state.next_signal_aspect is None or state.next_signal_distance_m is None:
                return msg.no_signal_ahead()
            return msg.signal_report(
                str(state.next_signal_aspect),
                state.next_signal_distance_m,
                state.permitted_kmh,
            )

        if code == "train":
            return msg.train_status(
                self.service.train_number,
                self.data.service_class_name(self.service.train_type),
                self.train.current_speed_kmh,
                self.train.power_notch,
                self.train.brake_notch,
                self.train.emergency_brake,
                self.train.direction,
                self.train.reverser,
            )

        if code == "doors":
            return msg.door_status(
                self.train.left_doors_open, self.train.right_doors_open
            )

        if code == "service":
            return msg.service_report(
                self.service.train_number,
                self.data.service_class_name(self.service.train_type),
                self.spec.name_zh_tw,
                self.route.name_zh_tw,
            )

        # summary
        return msg.run_summary(self.clock.clock_text, self.incidents.violation_count)

    # -- 快捷鍵對應的查詢（鍵位表的動作代碼）---------------------------
    def announce_speed(self) -> None:
        """播報目前速度與允許速度（V／Ctrl+Shift+S）。"""
        self.announce_status("speed")

    def announce_position(self) -> None:
        """播報目前位置（P）。"""
        self.announce_status("position")

    def announce_next_station(self) -> None:
        """播報下一站與停靠別（N／Ctrl+Shift+T）。"""
        self.announce_status("next_station")

    def announce_stop_point(self) -> None:
        """播報距離停車位置多遠（D／Ctrl+D）。

        與「下一站」分開的理由是**用途不同**：下一站報的是還有多久到、要不要
        停；停車位置報的是車頭離月台標記還差幾公尺，是最後一百公尺裡唯一有
        用的數字。看不見月台標記的司機在對位時會反覆按這個鍵。
        """
        self.announce_status("stop_point")

    def announce_signal(self) -> None:
        """播報前方號誌（G／Ctrl+Shift+A，規格 §11.3）。"""
        self.announce_status("signal")

    def announce_train_status(self) -> None:
        """播報列車狀態（T）。"""
        self.announce_status("train")

    def announce_doors(self) -> None:
        """播報車門狀態（B）。"""
        self.announce_status("doors")

    # ------------------------------------------------------------------
    # 推進
    # ------------------------------------------------------------------
    def tick(self, dt_s: float = SIMULATION_TICK_S) -> None:
        """推進一個模擬步長。"""
        if self.finished:
            return

        self._drive_with_ato(dt_s)
        physics_step(self.train, self.spec, dt_s)
        self.clock.elapsed_s += dt_s

        self._update_occupancy()
        self._handle_stations()
        self._handle_realignment()
        self._handle_unscheduled_stop()
        self._handle_broadcast()
        self._refresh_stop_target()

        state, events = self.atp.evaluate(self.train, dt_s)
        self.last_state = state
        self._handle_atp_events(events)
        self._check_finished()

    def advance(self, real_dt_s: float) -> int:
        """依實際經過時間推進固定步長，回傳執行的 tick 數。"""
        ticks = self.clock.advance(real_dt_s)
        for _ in range(ticks):
            self.tick(self.clock.tick_s)
        return ticks

    # ------------------------------------------------------------------
    # 內部
    # ------------------------------------------------------------------
    def _evaluate_only(self) -> AtpState:
        state, _ = self.atp.evaluate(self.train, 0.0)
        return state

    def _update_occupancy(self) -> None:
        blocks = self.blocks.update_occupancy(
            self.train.id, self.train.rear_position_m, self.train.position_m
        )
        self.train.current_block_id = blocks[-1].id if blocks else None

    def next_station(self) -> StationProgress | None:
        """前方尚未處理完的第一個車站。"""
        for progress in self.stations:
            if progress.served or progress.passed or progress.missed:
                continue
            if progress.position_m >= self.train.position_m - STOP_WINDOW_M:
                return progress
        return None

    def next_scheduled_stop(self) -> StationProgress | None:
        """前方第一個必須停靠的車站。"""
        for progress in self.stations:
            if progress.served or progress.missed or not progress.must_stop:
                continue
            if progress.position_m > self.train.position_m - STOP_WINDOW_M:
                return progress
        return None

    def stop_alignment_target(self) -> StationProgress | None:
        """對準停車位置時要看的那一站。

        正在修正停車位置時就是**已經停妥的那一站**，其餘時候是前方第一個
        停靠站。少了前者，司機一停妥、車站被標記為已服務，查詢就會跳到下
        一站——正想微調位置的人反而問不到自己站在哪裡。

        但一旦離站就不會再回頭看它，即使列車在停車範圍內因號誌或緊急制軔
        等原因再次停下也一樣——``_departed_aligning_stop`` 是單向的旗標，
        不是看當下是否靜止（見該欄位的說明）。`_aligning_stop` 本身繼續
        保留給 :meth:`_handle_realignment`，讓還沒離站的前進修正仍能算出
        正確的誤差。
        """
        aligning = self.aligning_at()
        return aligning if aligning is not None else self.next_scheduled_stop()

    def aligning_at(self) -> StationProgress | None:
        """還在對準停車位置的那一站；車頭通過停車位置之後為 ``None``。

        「還在對位」與「已經離站」的界線是車頭有沒有通過停車位置，不是列車
        有沒有在動：停短了往前推一點也會動。廣播據此判斷該不該播「下一站」
        （issue #12）。
        """
        if self._aligning_stop is None or self._departed_aligning_stop:
            return None
        return self._aligning_stop

    def _refresh_stop_target(self) -> None:
        """把下一個停車站設為 ATP 的停車點（§14.1 前方停車點距離）。"""
        target = self.next_scheduled_stop()
        if target is None:
            self.atp.stop_target = None
            return
        self.atp.stop_target = SpeedRestriction(
            kind="station_stop",
            limit_kmh=0.0,
            position_m=target.position_m,
            label=f"{target.name_zh_tw}站停車位置",
        )

    def _handle_stations(self) -> None:
        position = self.train.position_m
        for progress in self.stations:
            if progress.served or progress.passed or progress.missed:
                continue

            distance = progress.position_m - position

            # 接近播報
            if (
                not progress.approach_announced
                and 0 < distance <= APPROACH_ANNOUNCE_M
            ):
                progress.approach_announced = True
                if progress.must_stop:
                    self.announcer.announce(
                        msg.station_approaching(progress.name_zh_tw, distance),
                        Priority.NOTICE,
                    )
                else:
                    self.announcer.announce(
                        msg.next_station(progress.name_zh_tw, distance, progress.stop_kind),
                        Priority.NOTICE,
                    )

            if progress.must_stop:
                self._handle_stop_countdown(progress, distance)
                self._handle_stop_station(progress, position)
            else:
                self._handle_pass_station(progress, position)

    def _handle_stop_countdown(self, progress: StationProgress, distance: float) -> None:
        """接近停車位置時逐段報出剩餘距離（§14.3 提前警告）。

        這是給看不見月台標記的司機用的：對位時他手上沒有任何連續的資訊，
        只能靠這串由疏而密的數字判斷還有多遠、該不該再加一段制軔。

        播報的是**當下的實際距離**而不是門檻值，而且一個步長內跨過幾個門檻
        也只報一次——列車還很快時連珠炮式地報距離，只會把後面真正需要的
        那幾句擠掉。
        """
        if distance <= 0.0:
            # 車頭到達停車位置：對位時最關鍵的一句，因為它不必自己從遞減的
            # 數字推算「就是現在」。
            if not progress.stop_point_announced:
                progress.stop_point_announced = True
                progress.countdown_index = len(STOP_COUNTDOWN_M)
                self.announcer.announce(msg.stop_point_reached(), Priority.NOTICE)
            return

        crossed = progress.countdown_index
        while crossed < len(STOP_COUNTDOWN_M) and distance <= STOP_COUNTDOWN_M[crossed]:
            crossed += 1
        if crossed == progress.countdown_index:
            return

        first = progress.countdown_index == 0
        progress.countdown_index = crossed
        text = (
            msg.stop_countdown_start(progress.name_zh_tw, distance)
            if first
            else msg.stop_countdown(distance)
        )
        self.announcer.announce(text, Priority.NOTICE)

    def _handle_realignment(self) -> None:
        """停妥之後前進修正停車位置。

        未達停車位置時列車還可以往前推一點，真實運轉上也是這樣處理；但看不見
        月台標記的司機每動一次都需要知道現在差多少，否則修正等於盲猜。

        修正**不會**改變已經判定的停靠結果（車站仍是已服務），只更新記錄下來
        的誤差並重新播報。判斷離站看的不是「動了沒有」——前進修正本身就要
        靠動力向前推一點，那一瞬間列車一定不是靜止的——而是移動當下車頭有
        沒有到達或超過停車位置：還沒到，仍然是「往前推一點」的修正；已經
        到了還繼續往前，才是真的離站，即使之後在停車範圍內因號誌或緊急
        制軔等原因又停下，也不會恢復成在對位（見
        :data:`_departed_aligning_stop`）。車頭真的離開停車範圍時才把整個
        狀態歸零。
        """
        progress = self._aligning_stop
        if progress is None:
            return

        offset = self.train.position_m - progress.position_m
        if abs(offset) > STOP_WINDOW_M:
            self._aligning_stop = None
            self._departed_aligning_stop = False
            return
        if not self.train.is_stopped:
            if offset >= 0.0:
                # 已經到達或超過停車位置了還在動，是離站不是修正；之後即使
                # 在範圍內又停下也不算回到這次對位（見
                # :data:`_departed_aligning_stop` 與 :meth:`stop_alignment_target`）。
                self._departed_aligning_stop = True
            return
        if self._departed_aligning_stop:
            # 已經離站後又在範圍內停下（號誌、緊急制軔……），不是回來對位，
            # 不該再報一次「這一站修正後」。
            return

        previous = progress.stop_offset_m
        if previous is not None and abs(offset - previous) < REALIGN_STEP_M:
            return
        progress.stop_offset_m = offset
        self.announcer.announce(
            msg.station_realigned(progress.name_zh_tw, offset), Priority.NOTICE
        )

    def _handle_stop_station(self, progress: StationProgress, position: float) -> None:
        offset = position - progress.position_m

        # 停妥判斷
        if self.train.is_stopped and abs(offset) <= STOP_WINDOW_M:
            progress.served = True
            progress.stop_offset_m = offset
            # 停妥不是對位的結束：還在停車範圍內就仍可前進修正（見
            # :meth:`_handle_realignment`）。
            self._aligning_stop = progress
            self._departed_aligning_stop = False
            self.announcer.announce(
                msg.station_arrival(progress.name_zh_tw, offset), Priority.NOTICE
            )
            self.bus.publish(
                "station_stopped", station_id=progress.station_id, offset_m=offset
            )
            return

        # 應停未停（§9.2）：車頭完全通過停車範圍。
        #
        # 這裡刻意**不**檢查列車是否仍在行進。單一步長內列車可能同時越過
        # 停車範圍並停妥（例如緊急制軔），若加上「仍在行進」的條件，該站
        # 會既未停妥也未判定應停未停，永遠停在未處理狀態而逃過違規紀錄。
        # 判定結果不應取決於列車剛好在越過停車範圍之前或之後停下。
        if offset > STOP_WINDOW_M:
            progress.missed = True
            self.incidents.record_violation(
                "missed_stop",
                f"{progress.name_zh_tw}站應停未停",
                at_position_m=position,
                at_time_s=self.clock.elapsed_s,
                station_id=progress.station_id,
            )
            self.announcer.announce(msg.missed_stop(progress.name_zh_tw), Priority.EMERGENCY)
            self.bus.publish("missed_stop", station_id=progress.station_id)

    # ------------------------------------------------------------------
    # 車上廣播（§20.2）
    # ------------------------------------------------------------------
    def final_stop(self) -> StationProgress | None:
        """本班次的終點站（停靠表的最後一站）。"""
        for progress in reversed(self.stations):
            if progress.must_stop:
                return progress
        return None

    def first_stop(self) -> StationProgress | None:
        """本班次的起站（停靠表的第一站）。"""
        for progress in self.stations:
            if progress.must_stop:
                return progress
        return None

    def stopped_at(self) -> StationProgress | None:
        """目前停妥於哪一個停靠站；行進中或停在站外時回傳 ``None``。"""
        if not self.train.is_stopped:
            return None
        for progress in self.stations:
            if not progress.must_stop:
                continue
            if abs(self.train.position_m - progress.position_m) <= STOP_WINDOW_M:
                return progress
        return None

    def run_state(self) -> RunState:
        """整理出廣播需要知道的運轉狀況。

        一律以**下一個停靠站**為準，不照路線上的車站順序推進：自強號、
        區間快會通過許多車站，照順序播就會播出根本不停的站。以停靠站為準
        的規則對區間車（站站停）同樣成立，因此不需要為車種分開處理。
        """
        target = self.next_scheduled_stop()
        here = self.stopped_at()
        final = self.final_stop()
        origin = self.first_stop()
        aligning = self.aligning_at()
        return RunState(
            speed_kmh=self.train.current_speed_kmh,
            at_station_id=here.station_id if here is not None else None,
            next_stop_id=target.station_id if target is not None else None,
            next_stop_name=target.name_zh_tw if target is not None else "",
            distance_to_next_stop_m=(
                target.position_m - self.train.position_m if target is not None else 0.0
            ),
            previous_stop_id=self._previous_stop_id,
            origin_id=origin.station_id if origin is not None else "",
            terminus_id=final.station_id if final is not None else "",
            service_class=self.service.train_type,
            aligning_at_id=aligning.station_id if aligning is not None else None,
        )

    def at_platform(self) -> bool:
        """車頭是否還在某一座月台的範圍內。

        停靠站與通過站都算：月台就是月台，列車停在通過站的月台邊仍然是停在
        月台，不是站外。範圍取 :data:`PLATFORM_ZONE_M`（以停車位置為中心）。
        """
        return any(
            abs(self.train.position_m - progress.position_m) <= PLATFORM_ZONE_M
            for progress in self.stations
        )

    def _handle_unscheduled_stop(self) -> None:
        """站外臨時停車的廣播（§20.2）。

        號誌、前方列車或事故讓列車停在月台以外的地方時，旅客只知道車忽然不
        動了，需要一句話說明這是臨時停車。停在月台範圍內不算——那是正常到站
        或正常通過時的停等，已經有到站廣播交代。

        旗標在列車重新起動時歸零，因此同一趟裡可以臨停很多次，但停著不動的
        每一個步長不會一直重播。
        """
        if not self.train.is_stopped:
            if self._unscheduled_stop_announced:
                self.broadcast.stop_unscheduled_stop()
            self._unscheduled_stop_announced = False
            return
        if self._unscheduled_stop_announced or self.at_platform():
            return
        self._unscheduled_stop_announced = True
        self.broadcast.announce_unscheduled_stop()
        self.bus.publish("unscheduled_stop", position_m=self.train.position_m)

    def _handle_broadcast(self) -> None:
        """把目前狀況交給廣播系統，由它決定要播什麼。

        運轉端刻意不碰「該播哪一則」：捷運的規則（往○○、宣導、終點變體）
        與臺鐵完全不同，全部收在廣播系統裡，這裡只描述事實。
        """
        here = self.stopped_at()
        if here is not None:
            self._previous_stop_id = here.station_id
        self.broadcast.update(self.run_state())

    def _handle_pass_station(self, progress: StationProgress, position: float) -> None:
        if position >= progress.position_m:
            progress.passed = True
            self.announcer.announce(msg.station_passed(progress.name_zh_tw), Priority.STATUS)
            self.bus.publish("station_passed", station_id=progress.station_id)

    def _handle_atp_events(self, events: list) -> None:
        for event in events:
            kind = event.kind
            detail = event.detail
            if kind == "overspeed":
                self.announcer.announce(
                    msg.overspeed(
                        float(detail["speed_kmh"]), float(detail["permitted_kmh"])
                    ),
                    Priority.SAFETY,
                    dedupe_key="overspeed",
                )
            elif kind == "overspeed_cleared":
                self.announcer.announce(msg.overspeed_cleared(), Priority.STATUS)
            elif kind == "overspeed_violation":
                self.incidents.record_violation(
                    "overspeed",
                    f"持續超速：{detail['speed_kmh']:.0f} / "
                    f"{detail['permitted_kmh']:.0f} 公里",
                    at_position_m=self.train.position_m,
                    at_time_s=self.clock.elapsed_s,
                )
                self.bus.publish("violation", kind="overspeed")
            elif kind == "emergency_overspeed":
                if not self.train.emergency_brake:
                    braking.apply_emergency(self.train, self.spec)
                    self.announcer.announce(
                        "嚴重超速，緊急制軔動作。", Priority.EMERGENCY,
                        dedupe_key="emergency_overspeed",
                    )
                    self.bus.publish("emergency_brake", applied=True, cause="overspeed")
            elif kind == "spad":
                if not self.train.emergency_brake:
                    braking.apply_emergency(self.train, self.spec)
                self.incidents.record_violation(
                    "spad",
                    f"冒進號誌：{detail['signal_name']}",
                    at_position_m=self.train.position_m,
                    at_time_s=self.clock.elapsed_s,
                )
                self.announcer.announce(
                    msg.signal_passed_at_danger(str(detail["signal_name"])),
                    Priority.EMERGENCY,
                )
                self.bus.publish("violation", kind="spad")
            elif kind == "brake_warning":
                distance = float(detail["distance_m"])
                if float(detail["limit_kmh"]) <= 0.0:
                    # 停車點（車站停車位置或停止號誌）：允許速度為零，
                    # 不可用「前方速限零公里」的說法。
                    text = msg.approaching_stop_point(str(detail["label"]), distance)
                elif detail.get("kind") == "platform_pass":
                    # 通過月台的速限要說得出是哪一站：司機聽到「前方速限七十」
                    # 會去找號誌牌，聽到「○○站月台」才知道是通過站的規定。
                    text = msg.approaching_platform_pass(
                        str(detail["label"]), float(detail["limit_kmh"]), distance
                    )
                else:
                    text = msg.approaching_speed_limit(
                        float(detail["limit_kmh"]), distance
                    )
                self.announcer.announce(
                    text, Priority.NOTICE, dedupe_key=f"brake_warning:{detail['label']}"
                )

    def _check_finished(self) -> None:
        final = self.stations[-1] if self.stations else None
        if final is None:
            return
        reached_end = self.train.position_m >= self.route.length_m - 1e-6
        if (final.served or final.missed or final.passed) and self.train.is_stopped:
            if not self.finished:
                self.finished = True
                self.announcer.announce(
                    msg.service_completed(
                        self.service.train_number, self.incidents.violation_count
                    ),
                    Priority.NOTICE,
                )
                self.bus.publish("service_completed")
        elif reached_end and self.train.is_stopped and not self.finished:
            self.finished = True
            self.announcer.announce(
                msg.service_completed(
                    self.service.train_number, self.incidents.violation_count
                ),
                Priority.NOTICE,
            )
            self.bus.publish("service_completed")

    # ------------------------------------------------------------------
    # 狀態文字（供介面顯示，內容與播報一致）
    # ------------------------------------------------------------------
    def briefing_lines(self) -> list[str]:
        """運轉開始前的行前提要。

        由本模組提供而非各自寫在介面裡，主控台與 wx 介面才會顯示完全相同
        的內容（§25.5：必要資訊不得只存在於單一介面）。
        """
        class_name = self.data.service_class_name(self.service.train_type)
        stops = "、".join(p.name_zh_tw for p in self.stations if p.stop_kind == "stop")
        passes = "、".join(p.name_zh_tw for p in self.stations if p.stop_kind != "stop")
        return [
            f"車次：{class_name}{self.service.train_number}次",
            f"車輛型式：{self.spec.name_zh_tw}",
            f"路線：{self.route.name_zh_tw}",
            f"路線長度：{self.route.length_m:.0f} 公尺",
            f"停靠站：{stops or '無'}",
            f"通過站：{passes or '無'}",
            f"時刻表：{self.schedule_text()}",
            f"車上廣播：{self.broadcast_status_text()}",
        ]

    def schedule_text(self) -> str:
        """本班次的時刻摘要，讓玩家知道自己開的是哪一班。

        分三種情形，因為「沒有時刻」的理由不只一種，講清楚才不會被當成資料
        壞掉：

        - 有時刻摘要（捷運，來自營運單位公布的逐班時刻表）：報營運日、首末
          班與班數。捷運的班次是營運模式而不是某一列特定的車，只報一個發車
          時刻沒有意義。
        - 只有發車時刻（臺鐵）：報本班次自起站發車的時刻。
        - 兩者都沒有：說明這條路線沒有公布逐班時刻，不是漏掉。
        """
        schedule = self.service.schedule
        if schedule is not None:
            parts = [
                schedule.service_days or "全日",
                f"首班 {schedule.first_departure}",
                f"末班 {schedule.last_departure}",
                f"每日 {schedule.departures_per_day} 班",
            ]
            if schedule.run_time_min is not None:
                parts.append(f"行車時間約 {schedule.run_time_min} 分")
            return "，".join(parts)

        origin = self.first_stop()
        departure = (
            self.service.departure_times.get(origin.station_id) if origin else None
        )
        if departure and origin is not None:
            return f"{departure} 自{msg.station_phrase(origin.name_zh_tw)}發車"
        return "本路線沒有公布逐班時刻"

    def broadcast_status_text(self) -> str:
        """車上廣播目前的狀態，讓玩家知道「沒有聲音」是哪一種原因。

        分成三種：本型車沒有設備、有設備但這條線還沒有音檔、正常。三種都是
        合法狀態，說清楚是哪一種才不會被誤認成故障。
        """
        if not self.spec.has_broadcast:
            return f"無（{self.spec.name_zh_tw}沒有車上廣播設備）"
        covered = sum(
            1
            for p in self.stations
            if p.must_stop and self.data.broadcasts.has_any(p.station_id)
        )
        total = sum(1 for p in self.stations if p.must_stop)
        if covered == 0:
            return "本路線尚無廣播音檔，僅提供文字"
        return f"停靠站 {covered} / {total} 站有廣播音檔"

    def status_lines(self) -> list[str]:
        """目前完整狀態的純文字，供介面顯示與螢幕閱讀器閱讀。"""
        state = self.last_state or self._evaluate_only()
        report = describe_position(
            self.route,
            self.train.position_m,
            self.data.station_names(),
            self.data.line_names,
        )
        upcoming = self.next_station()

        class_name = self.data.service_class_name(self.service.train_type)
        section = f"{report.from_station_name}至{report.to_station_name}間"

        lines = [
            f"車次：{class_name}{self.service.train_number}次",
            f"車輛型式：{self.spec.name_zh_tw}",
            f"目前速度：{self.train.current_speed_kmh:.0f} 公里／小時",
            f"允許速度：{state.permitted_kmh:.0f} 公里／小時",
            f"電門段位：{self.train.power_notch} / {self.spec.power_notches}",
            f"制軔段位：{self.train.brake_notch} / {self.spec.brake_notches}",
            f"緊急制軔：{'動作中' if self.train.emergency_brake else '未動作'}",
            msg.door_status(self.train.left_doors_open, self.train.right_doors_open),
            f"位置：{report.line_name} {section}",
            f"距離{report.to_station_name}：{report.distance_to_next_m:.0f} 公尺",
        ]

        if upcoming is not None:
            kind = msg.STOP_KIND_NAMES.get(upcoming.stop_kind, upcoming.stop_kind)
            lines.append(
                f"前方車站：{upcoming.name_zh_tw}（{kind}），"
                f"距離 {upcoming.position_m - self.train.position_m:.0f} 公尺"
            )
        else:
            lines.append("前方車站：無")

        if state.next_signal_aspect is not None and state.next_signal_distance_m is not None:
            aspect_zh = msg.SIGNAL_ASPECT_NAMES[str(state.next_signal_aspect)]
            lines.append(
                f"前方號誌：{aspect_zh}，距離 {state.next_signal_distance_m:.0f} 公尺"
            )
        else:
            lines.append("前方號誌：無")

        lines.append(f"所在閉塞：{self.train.current_block_id or '未在區間內'}")
        lines.append(f"行車違規：{self.incidents.violation_count} 件")
        lines.append(f"運轉時間：{self.clock.clock_text}")
        return lines

    def status_text(self) -> str:
        return "\n".join(self.status_lines())
    # ------------------------------------------------------------------
    # 點字即時顯示（Alt＋Shift＋T）
    # ------------------------------------------------------------------
    def braille_line(self) -> str:
        """點字顯示器用的一行即時狀態。

        點字顯示器一次只有二十到八十方，而且是用摸的——不能像螢幕那樣掃一眼
        就跳過不要的部分。因此這裡只放**開著車時會一直想知道**的那件事：
        下一站是哪一站、還有多遠。

        進到 :data:`BRAILLE_FINE_RANGE_M` 之內改報**停車位置**的距離並加上
        「停」字：最後兩百公尺裡，車站中心的距離已經沒有意義，需要的是車頭
        離月台標記還差幾公尺。停過頭時改說「過」，因為不可倒車，兩者要能一
        摸就分得出來。

        由本模組提供而非各介面自己組字串，視窗版與主控台顯示的內容才會一致
        （§25.5）。
        """
        target = self.stop_alignment_target()
        if target is not None:
            remaining = target.position_m - self.train.position_m
            if remaining <= BRAILLE_FINE_RANGE_M:
                if remaining < 0:
                    return f"{target.name_zh_tw} 過{_braille_metres(-remaining)}"
                return f"{target.name_zh_tw} 停 {_braille_metres(remaining)}"

        upcoming = self.next_station()
        if upcoming is None:
            return "路線終點"
        distance = upcoming.position_m - self.train.position_m
        return f"{upcoming.name_zh_tw} {_braille_metres(distance)}"



    # ------------------------------------------------------------------
    def action_handlers(self) -> dict[str, object]:
        """動作代碼對應的處理器，供 :class:`KeyDispatcher` 註冊。"""
        return {
            "power_up": self.power_up,
            "brake_up": self.brake_up,
            "notch_down": self.notch_down,
            "single_brake": self.single_brake,
            "release_brake": self.release_brake,
            "emergency_brake": self.emergency_brake,
            "release_emergency": self.release_emergency,
            "horn": self.horn,
            "reverser_forward": self.reverser_forward,
            "reverser_backward": self.reverser_backward,
            "doors_left": self.toggle_left_doors,
            "doors_right": self.toggle_right_doors,
            "toggle_ato": self.toggle_ato,
            "ato_depart": self.ato_depart,
            "announce_speed": self.announce_speed,
            "announce_position": self.announce_position,
            "announce_next_station": self.announce_next_station,
            "announce_stop_point": self.announce_stop_point,
            "announce_signal": self.announce_signal,
            "announce_train_status": self.announce_train_status,
            "announce_doors": self.announce_doors,
        }
