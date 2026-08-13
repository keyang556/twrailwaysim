"""捷運車上廣播（規格 §20.2）。

與臺鐵的差別
------------

臺鐵只有「下一站」與「到站」兩種時機；捷運多了三件事，而且每一條線不一樣：

``往○○``
    整趟車重複播放的目的地廣播。台北捷運五條高運量線在**發車之前、列車還沒
    啟動時**播；文湖線在**發車之後**播；三鶯線與機場捷運沒有這種廣播。
    音檔以**終點站代碼**命名（``R28.destination`` 就是「往淡水」），因此同一
    條線的不同營運模式自動播到正確的那一則。

``下一站``
    只有三鶯線（每一站都有）與機場捷運（少數幾站）有。台北捷運各線沒有，
    因此連文字都不送出——沒播出來的東西不應該假裝有。

``終點／方向版本``
    同一站有好幾個版本：大安是終點時播 ``3-1``、正常時播 ``3``；奇岩在往北投
    的列車上播 ``19-1``；北投從新北投開來時播 ``20-2``。挑哪一個取決於這班車
    的**終點、起站與車種**，不是取決於車站，因此規則放在
    ``data/mrt/broadcast_rules.json``，程式不寫死任何站號。

``宣導``
    在指定的兩站之間、指定的方向上播一次。

規則怎麼比對
------------

每一條規則可以指定 ``when_terminus``、``when_origin``、``when_service_class``
三種條件（都可省略，省略即不限制），全部符合才算命中；由上而下取第一個命中
的。條件少而固定是刻意的：規則要看得懂、對得起使用者說明的那幾句話，而不是
一套什麼都能寫的小語言。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from railway_sim.accessibility import messages as msg
from railway_sim.accessibility.announcer import Priority
from railway_sim.audio.broadcast import BroadcastSystem, RunState

__all__ = [
    "BROADCAST_RULES_FILENAME",
    "MRT_ARRIVAL_DISTANCE_M",
    "NOTICE_STATION_ID",
    "LineBroadcastRules",
    "MrtBroadcastRules",
    "MrtBroadcastSystem",
    "NoticeRule",
    "VariantRule",
]

#: 規則檔的檔名，放在捷運資料目錄底下。
BROADCAST_RULES_FILENAME = "broadcast_rules.json"

#: 宣導廣播的「車站代碼」。宣導不屬於任何一站，但索引是以車站代碼為鍵，
#: 因此給它一個固定的保留字。
NOTICE_STATION_ID = "NOTICE"

#: 捷運的到站廣播起播距離（公尺）。
#:
#: 臺鐵用一千五百公尺，捷運不能照抄：最短的站距只有五百一十公尺（文湖線
#: 木柵至萬芳社區），照抄會在前一站還沒開走時就播下一站的到站廣播。四百
#: 公尺在時速七十公里下約有二十秒，足夠播完，也短於任何一段站距。
MRT_ARRIVAL_DISTANCE_M = 400.0


@dataclass(frozen=True)
class VariantRule:
    """某一站在特定條件下要改播哪一個版本。"""

    station_id: str
    variant: str
    when_terminus: tuple[str, ...] = ()
    when_origin: tuple[str, ...] = ()
    when_service_class: tuple[str, ...] = ()
    note: str = ""

    def matches(self, state: RunState, station_id: str) -> bool:
        if station_id != self.station_id:
            return False
        if self.when_terminus and state.terminus_id not in self.when_terminus:
            return False
        if self.when_origin and state.origin_id not in self.when_origin:
            return False
        return (
            not self.when_service_class
            or state.service_class in self.when_service_class
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VariantRule:
        return cls(
            station_id=str(raw["station_id"]),
            variant=str(raw.get("variant", "")),
            when_terminus=tuple(raw.get("when_terminus", ())),
            when_origin=tuple(raw.get("when_origin", ())),
            when_service_class=tuple(raw.get("when_service_class", ())),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class NoticeRule:
    """宣導廣播：在哪一段區間、哪一個方向播。"""

    from_station_id: str
    to_station_id: str
    variant: str = ""
    when_terminus: tuple[str, ...] = ()
    note: str = ""

    def matches(self, state: RunState) -> bool:
        if state.previous_stop_id != self.from_station_id:
            return False
        if state.next_stop_id != self.to_station_id:
            return False
        return not self.when_terminus or state.terminus_id in self.when_terminus

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NoticeRule:
        return cls(
            from_station_id=str(raw["from_station_id"]),
            to_station_id=str(raw["to_station_id"]),
            variant=str(raw.get("variant", "")),
            when_terminus=tuple(raw.get("when_terminus", ())),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class LineBroadcastRules:
    """一條線的廣播規則。"""

    destination_timing: str = "none"
    """``before_departure``、``after_departure`` 或 ``none``。"""

    next_stations: tuple[VariantRule, ...] = ()
    """有「下一站」廣播的車站。空的表示這條線沒有這種廣播。"""

    all_stations_have_next: bool = False
    """三鶯線是每一站都有，不必逐站列。"""

    arrival_variants: tuple[VariantRule, ...] = ()
    notices: tuple[NoticeRule, ...] = ()

    def next_variant(self, state: RunState, station_id: str) -> str | None:
        """該站的「下一站」廣播版本；``None`` 表示這一站不播。"""
        if self.all_stations_have_next:
            return ""
        for rule in self.next_stations:
            if rule.matches(state, station_id):
                return rule.variant
        return None

    def arrival_variant(self, state: RunState, station_id: str) -> str:
        for rule in self.arrival_variants:
            if rule.matches(state, station_id):
                return rule.variant
        return ""

    def notice_for(self, state: RunState) -> NoticeRule | None:
        for rule in self.notices:
            if rule.matches(state):
                return rule
        return None


@dataclass(frozen=True)
class MrtBroadcastRules:
    """整份規則檔。"""

    styles: dict[str, str] = field(default_factory=dict)
    """廣播樣式 → 目的地廣播的時機。"""

    lines: dict[str, LineBroadcastRules] = field(default_factory=dict)

    def for_line(self, line_id: str, style: str = "") -> LineBroadcastRules:
        """某一條線的規則。

        沒有登記的線別回傳「只有到站廣播」的預設值——那是最保守的行為：
        不會播出規則沒交代的東西。
        """
        rules = self.lines.get(line_id, LineBroadcastRules())
        timing = self.styles.get(style, "none")
        if rules.destination_timing == timing:
            return rules
        return LineBroadcastRules(
            destination_timing=timing,
            next_stations=rules.next_stations,
            all_stations_have_next=rules.all_stations_have_next,
            arrival_variants=rules.arrival_variants,
            notices=rules.notices,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MrtBroadcastRules:
        styles = {
            name: str(entry.get("destination_timing", "none"))
            for name, entry in (raw.get("styles") or {}).items()
            if isinstance(entry, dict)
        }
        lines: dict[str, LineBroadcastRules] = {}
        for line_id, entry in (raw.get("lines") or {}).items():
            if not isinstance(entry, dict):
                continue
            listed = entry.get("next_stations")
            lines[line_id] = LineBroadcastRules(
                next_stations=(
                    tuple(VariantRule.from_dict(r) for r in listed)
                    if isinstance(listed, list)
                    else ()
                ),
                all_stations_have_next=listed == "all",
                arrival_variants=tuple(
                    VariantRule.from_dict(r) for r in entry.get("arrival_variants", ())
                ),
                notices=tuple(NoticeRule.from_dict(r) for r in entry.get("notices", ())),
            )
        return cls(styles=styles, lines=lines)

    @classmethod
    def load(cls, path: str | Path) -> MrtBroadcastRules:
        """讀取規則檔。讀不到或格式不對都只代表「沒有規則」，不是致命錯誤。"""
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls()

    @classmethod
    def empty(cls) -> MrtBroadcastRules:
        return cls()


@dataclass
class MrtBroadcastSystem(BroadcastSystem):
    """捷運的車上廣播。

    只覆寫「什麼時候播什麼」，音檔查詢、文字輸出、播放後端全部沿用父類別。
    """

    #: 覆寫父類別的預設值（父類別的一千五百公尺是為臺鐵的長站距訂的）。
    arrival_distance_m: float = MRT_ARRIVAL_DISTANCE_M

    rules: LineBroadcastRules = field(default_factory=LineBroadcastRules)
    terminus_name: str = ""

    _destination_announced_at: str | None = field(default=None, init=False, repr=False)
    _notices_played: set[str] = field(default_factory=set, init=False, repr=False)

    # ------------------------------------------------------------------
    def update(self, state: RunState) -> None:
        if not self.enabled:
            return
        self._update_destination(state)
        if state.next_stop_id is None:
            return
        self._update_next_stop(state)
        self._update_notice(state)
        self._update_arrival(state)

    # ------------------------------------------------------------------
    def _update_destination(self, state: RunState) -> None:
        """往○○的廣播。

        兩種時機的差別只在「用哪一件事當觸發」：發車前看列車停在哪一站，
        發車後看列車剛離開哪一站。兩者都以車站為單位記錄，因此在站內前後
        移動或中途暫停都不會重播。
        """
        timing = self.rules.destination_timing
        if timing == "before_departure":
            here = state.at_station_id
        elif timing == "after_departure":
            here = state.previous_stop_id if state.moving else None
        else:
            return

        if here is None or here == self._destination_announced_at:
            return
        self._destination_announced_at = here
        if here == state.terminus_id:
            # 已經開到終點站，沒有「本列車開往○○」可言。
            return
        self._announce(
            state.terminus_id, "destination", msg.broadcast_destination(self.terminus_name)
        )

    def _update_next_stop(self, state: RunState) -> None:
        assert state.next_stop_id is not None
        if self._next_announced_for == state.next_stop_id or not state.moving:
            return
        variant = self.rules.next_variant(state, state.next_stop_id)
        # 記下來即使不播：這一站已經處理過，不必每個步長重新判斷一次。
        self._next_announced_for = state.next_stop_id
        if variant is None:
            return
        self._announce(
            state.next_stop_id,
            "next",
            msg.broadcast_next_stop(state.next_stop_name),
            variant=variant,
        )

    def _update_notice(self, state: RunState) -> None:
        rule = self.rules.notice_for(state)
        if rule is None or not state.moving:
            return
        key = f"{rule.from_station_id}->{rule.to_station_id}"
        if key in self._notices_played:
            return
        self._notices_played.add(key)
        self._announce(
            NOTICE_STATION_ID, "notice", msg.broadcast_notice(), variant=rule.variant
        )

    def _update_arrival(self, state: RunState) -> None:
        assert state.next_stop_id is not None
        if state.next_stop_id in self._arrival_announced:
            return
        if state.at_station_id is not None:
            return
        if state.distance_to_next_stop_m > self.arrival_distance_m:
            return

        self._arrival_announced.add(state.next_stop_id)
        variant = self.rules.arrival_variant(state, state.next_stop_id)
        is_terminus = state.next_stop_id == state.terminus_id
        text = (
            msg.broadcast_terminus(state.next_stop_name)
            if is_terminus
            else msg.broadcast_arriving(state.next_stop_name)
        )
        # 終點站優先找終點版本；這一站沒有終點版本（例如老街溪）就退回到站
        # 版本，而不是整則不播。
        if is_terminus and self._find_variant(state.next_stop_id, "terminus", variant):
            self._announce(state.next_stop_id, "terminus", text, variant=variant)
            return
        self._announce(state.next_stop_id, "arrive", text, variant=variant)

    # ------------------------------------------------------------------
    def _find_variant(self, station_id: str, kind: str, variant: str):
        return self.library.find(
            station_id, kind, line_id=self.line_id or None, variant=variant or None
        )

    def _announce(self, station_id: str, kind: str, text: str, *, variant: str = "") -> bool:
        if not self.enabled:
            return False
        self._play(self._find_variant(station_id, kind, variant))
        self.announcer.announce(text, Priority.STATUS)
        return True
