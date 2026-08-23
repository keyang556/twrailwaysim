"""開門側的判斷（``railway_sim.timetable.door_side``）。

規則來自使用者說明：標準情況左側；這班車在某一站比同車種的其他班次待得
明顯久，表示它在該站待避，改為右側。
"""

from __future__ import annotations

from railway_sim.data_loader import GameData
from railway_sim.timetable.door_side import (
    DEFAULT_DOOR_SIDE,
    OVERTAKE_MARGIN_MIN,
    DoorSideRule,
    section_minutes,
)
from railway_sim.timetable.service import Service


def _service(number: str, class_id: str, times: dict[str, str]) -> Service:
    return Service(
        train_number=number,
        train_type=class_id,
        route_id="R",
        stop_station_ids=tuple(times),
        departure_times=dict(times),
    )


class TestSectionMinutes:
    def test_it_measures_departure_to_departure(self) -> None:
        service = _service("1", "local", {"A": "08:00", "B": "08:12"})
        assert section_minutes(service, "A", "B") == 12

    def test_it_unwraps_midnight(self) -> None:
        service = _service("1", "local", {"A": "23:50", "B": "00:05"})
        assert section_minutes(service, "A", "B") == 15

    def test_missing_times_are_not_guessed(self) -> None:
        service = _service("1", "local", {"A": "08:00"})
        assert section_minutes(service, "A", "B") is None


class TestOvertakeRule:
    """待避的判定。"""

    def _rule(self, *services: Service) -> DoorSideRule:
        return DoorSideRule.from_services(services)

    def test_a_long_hold_is_an_overtake(self) -> None:
        fast = _service("2", "local", {"A": "09:00", "B": "09:10"})
        held = _service("1", "local", {"A": "08:00", "B": "08:20"})
        assert self._rule(fast, held).sides_for(held) == {"B": "right"}

    def test_a_normal_run_stays_on_the_default_side(self) -> None:
        fast = _service("2", "local", {"A": "09:00", "B": "09:10"})
        normal = _service("1", "local", {"A": "08:00", "B": "08:11"})
        assert self._rule(fast, normal).sides_for(normal) == {}
        assert DEFAULT_DOOR_SIDE == "left"

    def test_class_speed_differences_are_not_overtakes(self) -> None:
        """自強號跑同一段本來就比區間車快，那不是區間車在待避。"""
        express = _service("1", "tze_chiang", {"A": "08:00", "B": "08:10"})
        local = _service("2", "local", {"A": "09:00", "B": "09:20"})
        assert self._rule(express, local).sides_for(local) == {}

    def test_a_long_section_needs_a_proportionally_longer_hold(self) -> None:
        """一段本來就要跑一小時的長區間，多四分鐘只是排點餘裕。"""
        fast = _service("2", "local", {"A": "09:00", "B": "10:00"})
        held = _service("1", "local", {"A": "08:00", "B": "09:05"})
        assert held.departure_times  # 差值正好是最少額外分鐘數
        assert section_minutes(held, "A", "B") - 60 == OVERTAKE_MARGIN_MIN + 1
        assert self._rule(fast, held).sides_for(held) == {}

    def test_the_only_service_of_its_class_is_never_flagged(self) -> None:
        """沒有可比的對象時不猜，維持預設側。"""
        alone = _service("1", "local", {"A": "08:00", "B": "09:30"})
        assert self._rule(alone).sides_for(alone) == {}


class TestShippedTimetable:
    def test_the_rule_runs_over_the_shipped_services(
        self, game_data: GameData
    ) -> None:
        rule = DoorSideRule.from_services(game_data.services.values())
        flagged = {
            station
            for service in game_data.services.values()
            for station in rule.sides_for(service)
        }
        # 有判出來，但不是每一站都判成待避——兩種極端都表示規則沒作用。
        assert flagged
        assert len(flagged) < len(game_data.stations)

    def test_every_side_is_a_known_door_side(self, game_data: GameData) -> None:
        rule = DoorSideRule.from_services(game_data.services.values())
        for service in game_data.services.values():
            for station_id, side in rule.sides_for(service).items():
                assert side in ("left", "right")
                assert station_id in service.stop_station_ids
