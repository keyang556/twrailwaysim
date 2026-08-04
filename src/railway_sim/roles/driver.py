"""司機員模式（規格 §4、§8、§9、§14）。

本模組把物理、閉塞、號誌、ATP、停靠判斷與無障礙播報接在一起，形成一個
可完整測試的運轉工作階段。介面層（wx 或主控台）只負責送入按鍵與輸出
文字，不含任何運轉邏輯，確保必要資訊不會只存在於視覺介面（§25.5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from railway_sim.accessibility import messages as msg
from railway_sim.accessibility.announcer import Announcer, Priority
from railway_sim.data_loader import GameData
from railway_sim.events.event_bus import EventBus
from railway_sim.events.incidents import IncidentLog
from railway_sim.railway.block import BlockSystem
from railway_sim.railway.interlocking import Interlocking
from railway_sim.railway.route import Route, RouteStop
from railway_sim.railway.signal import SignalSystem
from railway_sim.simulation import braking
from railway_sim.simulation.atp import AtpMonitor, AtpState, SpeedRestriction
from railway_sim.simulation.clock import SIMULATION_TICK_S, SimulationClock
from railway_sim.simulation.physics import step as physics_step
from railway_sim.simulation.position import describe_position
from railway_sim.simulation.train import Train, TrainType
from railway_sim.timetable.service import Service
from railway_sim.timetable.stop_pattern import resolve_stop_kind

__all__ = ["STOP_WINDOW_M", "DriverSession", "StationProgress"]

#: 停車範圍（公尺）。車頭超過停車點加上此距離仍未停妥即為應停未停（§9.2）。
#: 真實月台長度無可靠公開來源（§27），此為第一版測試值。
STOP_WINDOW_M = 50.0

#: 開始播報「接近某站」的距離（公尺）。
APPROACH_ANNOUNCE_M = 800.0


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

    route: Route = field(init=False)
    spec: TrainType = field(init=False)
    train: Train = field(init=False)
    blocks: BlockSystem = field(init=False)
    signals: SignalSystem = field(init=False)
    interlocking: Interlocking = field(init=False)
    atp: AtpMonitor = field(init=False)
    stations: list[StationProgress] = field(init=False, default_factory=list)

    finished: bool = field(default=False, init=False)
    last_state: AtpState | None = field(default=None, init=False)

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

        self._build_station_progress()
        self._update_occupancy()
        self._refresh_stop_target()

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
        """增加電門（D）。"""
        result = braking.power_up(self.train, self.spec)
        if not result.accepted and result.reason == "emergency":
            self.announcer.announce(msg.power_blocked_by_emergency(), Priority.SAFETY)
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
        """鳴笛（H）。"""
        self.announcer.announce(msg.horn(), Priority.ACTION)
        self.bus.publish("horn")

    # ------------------------------------------------------------------
    # 查詢播報
    # ------------------------------------------------------------------
    def announce_speed(self) -> None:
        """播報目前速度與允許速度（V）。"""
        state = self.last_state or self._evaluate_only()
        self.announcer.announce(
            msg.speed_report(self.train.current_speed_kmh, state.permitted_kmh),
            Priority.ACTION,
        )

    def announce_position(self) -> None:
        """播報目前位置（P）。"""
        report = describe_position(
            self.route,
            self.train.position_m,
            self.data.station_names(),
            self.data.line_names,
        )
        self.announcer.announce(
            msg.position_report(
                report.line_name,
                report.from_station_name,
                report.to_station_name,
                report.distance_to_next_m,
            ),
            Priority.ACTION,
        )

    def announce_next_station(self) -> None:
        """播報下一站與停靠別（N）。"""
        upcoming = self.next_station()
        if upcoming is None:
            self.announcer.announce("前方無車站，已至路線終點。", Priority.ACTION)
            return
        self.announcer.announce(
            msg.next_station(
                upcoming.name_zh_tw,
                upcoming.position_m - self.train.position_m,
                upcoming.stop_kind,
            ),
            Priority.ACTION,
        )

    def announce_signal(self) -> None:
        """播報前方號誌（G，規格 §11.3）。"""
        state = self.last_state or self._evaluate_only()
        if state.next_signal_aspect is None or state.next_signal_distance_m is None:
            self.announcer.announce(msg.no_signal_ahead(), Priority.ACTION)
            return
        self.announcer.announce(
            msg.signal_report(
                str(state.next_signal_aspect),
                state.next_signal_distance_m,
                state.permitted_kmh,
            ),
            Priority.ACTION,
        )

    def announce_train_status(self) -> None:
        """播報列車狀態（T）。"""
        self.announcer.announce(
            msg.train_status(
                self.service.train_number,
                self.data.service_class_name(self.service.train_type),
                self.train.current_speed_kmh,
                self.train.power_notch,
                self.train.brake_notch,
                self.train.emergency_brake,
                self.train.direction,
            ),
            Priority.ACTION,
        )

    # ------------------------------------------------------------------
    # 推進
    # ------------------------------------------------------------------
    def tick(self, dt_s: float = SIMULATION_TICK_S) -> None:
        """推進一個模擬步長。"""
        if self.finished:
            return

        physics_step(self.train, self.spec, dt_s)
        self.clock.elapsed_s += dt_s

        self._update_occupancy()
        self._handle_stations()
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
                self._handle_stop_station(progress, position)
            else:
                self._handle_pass_station(progress, position)

    def _handle_stop_station(self, progress: StationProgress, position: float) -> None:
        offset = position - progress.position_m

        # 停妥判斷
        if self.train.is_stopped and abs(offset) <= STOP_WINDOW_M:
            progress.served = True
            progress.stop_offset_m = offset
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
            f"目前速度：{self.train.current_speed_kmh:.0f} 公里／小時",
            f"允許速度：{state.permitted_kmh:.0f} 公里／小時",
            f"電門段位：{self.train.power_notch} / {self.spec.power_notches}",
            f"制軔段位：{self.train.brake_notch} / {self.spec.brake_notches}",
            f"緊急制軔：{'動作中' if self.train.emergency_brake else '未動作'}",
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
    def action_handlers(self) -> dict[str, object]:
        """動作代碼對應的處理器，供 :class:`KeyDispatcher` 註冊。"""
        return {
            "power_up": self.power_up,
            "brake_up": self.brake_up,
            "notch_down": self.notch_down,
            "release_brake": self.release_brake,
            "emergency_brake": self.emergency_brake,
            "release_emergency": self.release_emergency,
            "horn": self.horn,
            "announce_speed": self.announce_speed,
            "announce_position": self.announce_position,
            "announce_next_station": self.announce_next_station,
            "announce_signal": self.announce_signal,
            "announce_train_status": self.announce_train_status,
        }
