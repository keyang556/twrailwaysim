"""ATP 與速度監控（規格 §14）。

第一版實作簡化速度監控，監控內容（§14.1）：

- 目前速度
- 區間速限
- 號誌限制
- 前方停車點距離

超速處理（§14.2）：

1. 超速時警告
2. 持續超速時記錄違規
3. 嚴重超速或闖紅燈（冒進號誌）時觸發緊急制軔

警告原則（§14.3）：提前警告，不在已無法制動時才警告；訊息簡潔；重複警告
限制頻率（頻率限制由 :class:`~railway_sim.accessibility.announcer.Announcer`
的去重機制負責）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from railway_sim.railway.block import BlockSystem
from railway_sim.railway.route import Route
from railway_sim.railway.signal import SignalAspect, SignalSystem
from railway_sim.simulation.braking import braking_distance_m
from railway_sim.simulation.train import Train, TrainType

__all__ = ["AtpEvent", "AtpMonitor", "AtpState", "SpeedRestriction"]

#: 超過允許速度多少公里／小時開始警告。
OVERSPEED_WARN_KMH = 1.0

#: 持續超速多久（秒）記錄一次違規。
OVERSPEED_VIOLATION_S = 5.0

#: 超過允許速度多少公里／小時視為嚴重超速，觸發緊急制軔。
OVERSPEED_EMERGENCY_KMH = 15.0

#: 提前警告的反應時間餘裕（秒）。
WARNING_REACTION_S = 3.0

#: 尋找前方速限變化的最大距離（公尺）。
RESTRICTION_LOOKAHEAD_M = 5000.0


@dataclass(frozen=True)
class SpeedRestriction:
    """一項速度限制。"""

    kind: str
    """``line`` 區間速限、``signal`` 號誌限制、``station_stop`` 停車點。"""

    limit_kmh: float
    position_m: float
    label: str

    @property
    def distance_from(self) -> float:  # pragma: no cover - 便利用途
        return self.position_m


@dataclass(frozen=True)
class AtpEvent:
    """ATP 產生的事件。"""

    kind: str
    """``overspeed`` / ``overspeed_cleared`` / ``overspeed_violation``
    / ``emergency_overspeed`` / ``spad`` / ``brake_warning``。"""

    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AtpState:
    """一次評估的結果快照。

    ``permitted_kmh`` 與 ``advisory_kmh`` 的差別很重要：

    - ``permitted_kmh`` 是**強制**允許速度，只由區間速限與號誌決定，超過
      會累積違規並可能觸發緊急制軔（§14.2）。
    - ``advisory_kmh`` 另外把「下一個停車站」納入計算，僅用於提前提醒司機
      減速（§14.3）。營業停靠不是保安功能，ATP 不會為了讓列車停在月台而
      強制介入；停不下來屬於司機的行車違規（應停未停，§9.2）。
    """

    speed_kmh: float
    permitted_kmh: float
    advisory_kmh: float
    line_limit_kmh: float
    signal_limit_kmh: float
    next_signal_id: str | None
    next_signal_name: str
    next_signal_aspect: SignalAspect | None
    next_signal_distance_m: float | None
    target: SpeedRestriction | None
    target_distance_m: float | None
    overspeed_kmh: float
    braking_required: bool

    @property
    def is_overspeed(self) -> bool:
        return self.overspeed_kmh >= OVERSPEED_WARN_KMH


@dataclass
class AtpMonitor:
    """速度監控器。

    以 ``evaluate`` 取得目前狀態並回傳事件。事件不含播報文字，播報由
    角色模式負責，確保監控邏輯可獨立測試。
    """

    route: Route
    signals: SignalSystem
    blocks: BlockSystem
    spec: TrainType
    station_names: dict[str, str] = field(default_factory=dict)

    #: 目前必須停車的停車點（由角色模式依班次停靠表設定）。
    stop_target: SpeedRestriction | None = None

    _overspeed_elapsed_s: float = field(default=0.0, init=False, repr=False)
    _was_overspeed: bool = field(default=False, init=False, repr=False)
    _passed_stop_signals: set[str] = field(default_factory=set, init=False, repr=False)

    violation_count: int = field(default=0, init=False)

    # ------------------------------------------------------------------
    # 速限查詢
    # ------------------------------------------------------------------
    def line_limit_kmh(self, position_m: float) -> float:
        return min(self.route.line_speed_limit_kmh(position_m), self.spec.max_speed_kmh)

    def next_line_restriction(self, position_m: float) -> SpeedRestriction | None:
        """前方第一個比目前速限更低的區間速限。"""
        current = self.route.line_speed_limit_kmh(position_m)
        horizon = position_m + RESTRICTION_LOOKAHEAD_M
        for segment in self.route.segments:
            if segment.start_m <= position_m or segment.start_m > horizon:
                continue
            if segment.max_speed_kmh < current:
                return SpeedRestriction(
                    kind="line",
                    limit_kmh=segment.max_speed_kmh,
                    position_m=segment.start_m,
                    label="區間速限",
                )
        return None

    # ------------------------------------------------------------------
    # 評估
    # ------------------------------------------------------------------
    def evaluate(self, train: Train, dt_s: float = 0.0) -> tuple[AtpState, list[AtpEvent]]:
        """評估目前狀態並回傳 ``(狀態, 事件清單)``。"""
        events: list[AtpEvent] = []
        position = train.position_m
        line_limit = self.line_limit_kmh(position)

        # --- 號誌 -----------------------------------------------------
        signal = self.signals.next_signal_ahead(position)
        aspect: SignalAspect | None = None
        signal_distance: float | None = None
        signal_limit = line_limit
        if signal is not None:
            aspect = self.signals.aspect_of(signal, ignore_train_id=train.id)
            signal_distance = signal.position_m - position
            signal_limit = self.signals.permitted_speed_kmh(
                signal, line_limit, ignore_train_id=train.id
            )

        # --- 冒進號誌（§14.2 第 3 點）---------------------------------
        events.extend(self._check_spad(train))

        # --- 前方目標（停車點或降速點）--------------------------------
        enforced_target = self._select_target(position, signal, aspect, include_stop=False)
        target = self._select_target(position, signal, aspect, include_stop=True)
        target_distance = None if target is None else max(0.0, target.position_m - position)

        # --- 強制允許速度：只看區間速限與號誌 --------------------------
        permitted = line_limit
        if aspect is SignalAspect.CAUTION:
            permitted = min(permitted, signal_limit)
        elif aspect is SignalAspect.STOP and signal_distance is not None:
            # 接近停止號誌時，允許速度依制動曲線遞減。
            permitted = min(permitted, self._curve_speed_kmh(signal_distance))

        if enforced_target is not None:
            distance = max(0.0, enforced_target.position_m - position)
            permitted = min(
                permitted, self._curve_speed_kmh(distance, enforced_target.limit_kmh)
            )

        permitted = max(0.0, permitted)

        # --- 建議速度：另計入下一個停車站，僅供提醒 --------------------
        advisory = permitted
        if self.stop_target is not None and self.stop_target.position_m > position:
            advisory = min(
                advisory,
                self._curve_speed_kmh(
                    self.stop_target.position_m - position, self.stop_target.limit_kmh
                ),
            )

        overspeed = max(0.0, train.current_speed_kmh - permitted)

        # --- 制動警告（§14.3 提前警告）--------------------------------
        braking_required = False
        if target is not None and target_distance is not None:
            needed = braking_distance_m(
                train.current_speed_kmh,
                target.limit_kmh,
                self.spec.max_service_brake_ms2 * 0.7,
                reaction_time_s=WARNING_REACTION_S,
            )
            if needed >= target_distance and train.current_speed_kmh > target.limit_kmh:
                braking_required = True
                events.append(
                    AtpEvent(
                        "brake_warning",
                        {
                            "kind": target.kind,
                            "label": target.label,
                            "limit_kmh": target.limit_kmh,
                            "distance_m": target_distance,
                        },
                    )
                )

        # --- 超速處理（§14.2）-----------------------------------------
        events.extend(self._check_overspeed(train, permitted, overspeed, dt_s))

        state = AtpState(
            speed_kmh=train.current_speed_kmh,
            permitted_kmh=permitted,
            advisory_kmh=advisory,
            line_limit_kmh=line_limit,
            signal_limit_kmh=signal_limit,
            next_signal_id=None if signal is None else signal.id,
            next_signal_name="" if signal is None else signal.name_zh_tw,
            next_signal_aspect=aspect,
            next_signal_distance_m=signal_distance,
            target=target,
            target_distance_m=target_distance,
            overspeed_kmh=overspeed,
            braking_required=braking_required,
        )
        return state, events

    # ------------------------------------------------------------------
    # 內部
    # ------------------------------------------------------------------
    def _select_target(
        self,
        position_m: float,
        signal: object,
        aspect: SignalAspect | None,
        *,
        include_stop: bool,
    ) -> SpeedRestriction | None:
        """選出前方最先需要處理的限制。

        Args:
            include_stop: 是否納入營業停車點。強制允許速度不納入，提醒用的
                目標才納入。
        """
        candidates: list[SpeedRestriction] = []

        if signal is not None and aspect is SignalAspect.STOP:
            candidates.append(
                SpeedRestriction(
                    kind="signal",
                    limit_kmh=0.0,
                    position_m=signal.position_m,  # type: ignore[attr-defined]
                    label=signal.name_zh_tw or "前方號誌",  # type: ignore[attr-defined]
                )
            )

        line_restriction = self.next_line_restriction(position_m)
        if line_restriction is not None:
            candidates.append(line_restriction)

        if (
            include_stop
            and self.stop_target is not None
            and self.stop_target.position_m > position_m
        ):
            candidates.append(self.stop_target)

        if not candidates:
            return None
        return min(candidates, key=lambda r: r.position_m)

    def _curve_speed_kmh(self, distance_m: float, target_kmh: float = 0.0) -> float:
        """依剩餘距離推算的允許速度（簡化制動曲線）。

        使用常用制軔的七成作為監控曲線，讓司機仍有操作餘裕。
        """
        decel = self.spec.max_service_brake_ms2 * 0.7
        if distance_m <= 0:
            return target_kmh
        v_target_ms = target_kmh / 3.6
        v_ms = (v_target_ms**2 + 2.0 * decel * distance_m) ** 0.5
        return v_ms * 3.6

    def _check_spad(self, train: Train) -> list[AtpEvent]:
        """檢查是否冒進停止號誌。"""
        events: list[AtpEvent] = []
        governing = self.signals.governing_signal(train.position_m)
        if governing is None or governing.id in self._passed_stop_signals:
            return events
        # 只在剛越過的瞬間判斷：號誌位置在車頭之後、車尾之前。
        if not (train.rear_position_m <= governing.position_m <= train.position_m):
            return events
        aspect = self.signals.aspect_of(governing, ignore_train_id=train.id)
        if aspect is SignalAspect.STOP:
            self._passed_stop_signals.add(governing.id)
            self.violation_count += 1
            events.append(
                AtpEvent("spad", {"signal_name": governing.name_zh_tw, "signal_id": governing.id})
            )
        return events

    def _check_overspeed(
        self,
        train: Train,
        permitted_kmh: float,
        overspeed_kmh: float,
        dt_s: float,
    ) -> list[AtpEvent]:
        events: list[AtpEvent] = []
        is_over = overspeed_kmh >= OVERSPEED_WARN_KMH

        if is_over:
            if not self._was_overspeed:
                events.append(
                    AtpEvent(
                        "overspeed",
                        {"speed_kmh": train.current_speed_kmh, "permitted_kmh": permitted_kmh},
                    )
                )
            self._overspeed_elapsed_s += dt_s
            if self._overspeed_elapsed_s >= OVERSPEED_VIOLATION_S:
                self._overspeed_elapsed_s = 0.0
                self.violation_count += 1
                events.append(
                    AtpEvent(
                        "overspeed_violation",
                        {"speed_kmh": train.current_speed_kmh, "permitted_kmh": permitted_kmh},
                    )
                )
            if overspeed_kmh >= OVERSPEED_EMERGENCY_KMH:
                events.append(
                    AtpEvent(
                        "emergency_overspeed",
                        {"speed_kmh": train.current_speed_kmh, "permitted_kmh": permitted_kmh},
                    )
                )
        else:
            if self._was_overspeed:
                events.append(AtpEvent("overspeed_cleared", {}))
            self._overspeed_elapsed_s = 0.0

        self._was_overspeed = is_over
        return events
