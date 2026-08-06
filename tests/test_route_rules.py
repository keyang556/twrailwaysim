"""成功／追分／彰化路線規則測試（規格 §10、§25.6–§25.9、chat.md v1.5-14）。

這是整份規格中最容易做錯、也最明確禁止的部分，因此測試同時涵蓋兩層：

1. **拓撲層**：成追線倒插方向限制讓錯誤路徑根本無法建立。
2. **規則層**：即使拓撲上繞得出來，``validate_route`` 也必須攔下。
"""

from __future__ import annotations

import pytest

from railway_sim.data_loader import GameData
from railway_sim.railway.route import Route, validate_route


class TestShippedDataIsClean:
    """正式資料本身必須完全通過驗證。"""

    def test_no_data_issues(self, game_data: GameData) -> None:
        assert game_data.issues == []

    def test_all_routes_pass_region_rules(self, game_data: GameData) -> None:
        for route in game_data.routes.values():
            violations = validate_route(route, game_data.network, game_data.region_rules)
            assert violations == [], f"{route.id}：{[str(v) for v in violations]}"


class TestTopologyBlocksIllegalPaths:
    """倒插方向限制（chat.md：海線倒插在成功南、彰化北）。"""

    def test_zhuifen_cannot_reach_changhua_through_the_junction(
        self, game_data: GameData
    ) -> None:
        """追分進入成追線分歧後只能往成功，不能直接轉向彰化。"""
        ok, reason = game_data.network.is_traversable(
            ["STA_ZHUIFEN", "JCT_CHENGZHUI", "JCT_CHANGHUA_N", "STA_CHANGHUA"]
        )
        assert not ok
        assert "JCT_CHENGZHUI" in str(reason)

    def test_changhua_cannot_reach_zhuifen_through_the_junction(
        self, game_data: GameData
    ) -> None:
        ok, _ = game_data.network.is_traversable(
            ["STA_CHANGHUA", "JCT_CHANGHUA_N", "JCT_CHENGZHUI", "STA_ZHUIFEN"]
        )
        assert not ok

    def test_building_illegal_route_raises(self, game_data: GameData) -> None:
        with pytest.raises(ValueError, match="不可通行"):
            Route.build(
                route_id="R_ILLEGAL",
                line_id="coast",
                direction="southbound",
                node_ids=[
                    "STA_DADU",
                    "STA_ZHUIFEN",
                    "JCT_CHENGZHUI",
                    "JCT_CHANGHUA_N",
                    "STA_CHANGHUA",
                ],
                network=game_data.network,
            )

    def test_legal_connector_directions_are_allowed(self, game_data: GameData) -> None:
        # 海線往臺中：追分 → 分歧 → 成功
        ok, _ = game_data.network.is_traversable(
            ["STA_ZHUIFEN", "JCT_CHENGZHUI", "STA_CHENGGONG"]
        )
        assert ok
        # 臺中往海線：成功 → 分歧 → 追分
        ok, _ = game_data.network.is_traversable(
            ["STA_CHENGGONG", "JCT_CHENGZHUI", "STA_ZHUIFEN"]
        )
        assert ok
        # 山線：成功 → 分歧 → 彰化方向
        ok, _ = game_data.network.is_traversable(
            ["STA_CHENGGONG", "JCT_CHENGZHUI", "JCT_CHANGHUA_N"]
        )
        assert ok


class TestRegionRuleViolations:
    """規格 §10 的四條規則，各以一條刻意違規的路線驗證。"""

    def test_coast_to_changhua_must_not_use_chenggong(self, game_data: GameData) -> None:
        """§10.2、§25.8：海線往彰化不得進成功站。"""
        route = Route.build(
            route_id="R_BAD_COAST_CHANGHUA",
            line_id="coast",
            direction="southbound",
            node_ids=[
                "STA_DADU",
                "STA_ZHUIFEN",
                "JCT_CHENGZHUI",
                "STA_CHENGGONG",
                "JCT_CHENGZHUI",
                "JCT_CHANGHUA_N",
                "STA_CHANGHUA",
            ],
            network=game_data.network,
        )
        rules = [
            v.rule for v in validate_route(route, game_data.network, game_data.region_rules)
        ]
        assert "coast_to_changhua_via_chenggong" in rules

    def test_coast_to_taichung_must_not_use_changhua(self, game_data: GameData) -> None:
        """§10.3、§25.9：海線往臺中不得經彰化。"""
        route = Route.build(
            route_id="R_BAD_COAST_TAICHUNG",
            line_id="coast",
            direction="southbound",
            node_ids=[
                "STA_DADU",
                "STA_ZHUIFEN",
                "JCT_CHANGHUA_N",
                "STA_CHANGHUA",
                "JCT_CHANGHUA_N",
                "JCT_CHENGZHUI",
                "STA_CHENGGONG",
                "STA_XINWURI",
                "STA_WURI",
                "STA_DAQING",
                "STA_WUQUAN",
                "STA_TAICHUNG",
            ],
            network=game_data.network,
        )
        rules = [
            v.rule for v in validate_route(route, game_data.network, game_data.region_rules)
        ]
        assert "coast_to_taichung_via_changhua" in rules

    def test_taichung_to_coast_must_not_use_changhua(self, game_data: GameData) -> None:
        """§10.4：臺中往海線先經成功再轉追分，不進彰化。"""
        route = Route.build(
            route_id="R_BAD_TAICHUNG_COAST",
            line_id="mountain",
            direction="southbound",
            node_ids=[
                "STA_TAICHUNG",
                "STA_WUQUAN",
                "STA_DAQING",
                "STA_WURI",
                "STA_XINWURI",
                "STA_CHENGGONG",
                "JCT_CHENGZHUI",
                "JCT_CHANGHUA_N",
                "STA_CHANGHUA",
                "JCT_CHANGHUA_N",
                "STA_ZHUIFEN",
                "STA_DADU",
            ],
            network=game_data.network,
        )
        rules = [
            v.rule for v in validate_route(route, game_data.network, game_data.region_rules)
        ]
        assert "taichung_to_coast_via_changhua" in rules

    def test_mountain_service_must_not_use_zhuifen(self, game_data: GameData) -> None:
        """chat.md 五：山線列車禁止經追分。

        路徑刻意在成追線上折返，因此拓撲上可通行，必須由規則層攔下。
        """
        route = Route.build(
            route_id="R_BAD_MOUNTAIN_ZHUIFEN",
            line_id="mountain",
            direction="southbound",
            node_ids=[
                "STA_TAICHUNG",
                "STA_WUQUAN",
                "STA_DAQING",
                "STA_WURI",
                "STA_XINWURI",
                "STA_CHENGGONG",
                "JCT_CHENGZHUI",
                "STA_ZHUIFEN",
                "JCT_CHENGZHUI",
                "STA_CHENGGONG",
                "JCT_CHENGZHUI",
                "JCT_CHANGHUA_N",
                "STA_CHANGHUA",
            ],
            network=game_data.network,
        )
        rules = [
            v.rule for v in validate_route(route, game_data.network, game_data.region_rules)
        ]
        assert "mountain_via_zhuifen" in rules


class TestCorrectRoutesInShippedData:
    """規格 §10 的正確路徑必須存在於實際時刻表產生的資料中。

    這些不再是為了測試而寫的路線，而是由臺鐵時刻表匯入後實際存在的班次；
    因此它們同時驗證了規則與匯入結果。
    """

    def _routes_of(self, game_data: GameData, predicate) -> list:
        seen = {}
        for service in game_data.services.values():
            route = game_data.routes.get(service.route_id)
            if route is not None and predicate(service, route):
                seen[route.id] = route
        return list(seen.values())

    def test_chengzhui_services_exist(self, game_data: GameData) -> None:
        """時刻表以車次後綴「追」標示經由成追線，這些班次必須匯入成功。"""
        routes = self._routes_of(
            game_data,
            lambda s, r: "ZHUIFEN" in r.station_ids and "CHENGGONG" in r.station_ids,
        )
        assert routes, "找不到任何經由成追線的班次"

    def test_taichung_to_coast_goes_chenggong_then_zhuifen(
        self, game_data: GameData
    ) -> None:
        """§10.4：臺中往海線先經成功再轉追分，且不進彰化。"""
        routes = self._routes_of(
            game_data,
            lambda s, r: (
                r.station_ids[0] in game_data.region_rules.taichung_area_station_ids
                and "ZHUIFEN" in r.station_ids
            ),
        )
        assert routes, "找不到由臺中地區開往海線的班次"
        for route in routes:
            ids = route.station_ids
            assert "CHANGHUA" not in ids, route.id
            assert ids.index("CHENGGONG") < ids.index("ZHUIFEN"), route.id

    def test_coast_to_taichung_avoids_changhua(self, game_data: GameData) -> None:
        """§10.3、§25.9：海線往臺中不得經彰化。"""
        routes = self._routes_of(
            game_data,
            lambda s, r: (
                r.station_ids[-1] in game_data.region_rules.taichung_area_station_ids
                and "ZHUIFEN" in r.station_ids
            ),
        )
        assert routes, "找不到由海線開往臺中地區的班次"
        for route in routes:
            ids = route.station_ids
            assert "CHANGHUA" not in ids, route.id
            assert ids.index("ZHUIFEN") < ids.index("CHENGGONG"), route.id

    def test_coast_to_changhua_avoids_chenggong(self, game_data: GameData) -> None:
        """§10.2、§25.8：海線往彰化不得進成功站。"""
        routes = self._routes_of(
            game_data,
            lambda s, r: (
                r.station_ids[-1] == "CHANGHUA" and "ZHUIFEN" in r.station_ids
            ),
        )
        assert routes, "找不到由海線開往彰化的班次"
        for route in routes:
            assert "CHENGGONG" not in route.station_ids, route.id

    def test_mountain_routes_avoid_zhuifen(self, game_data: GameData) -> None:
        """chat.md 五：純山線列車不得經追分。"""
        checked = 0
        for route in game_data.routes.values():
            if "coast" in route.line_ids or "chengzhui" in route.line_ids:
                continue
            if "CHENGGONG" not in route.station_ids:
                continue
            checked += 1
            assert "ZHUIFEN" not in route.station_ids, route.id
        assert checked, "找不到任何純山線班次"


class TestStopRulesMatchTheTimetable:
    """成功站的停靠規則必須來自實際時刻表，而不是人工推測。"""

    def test_chenggong_is_local_only(self, game_data: GameData) -> None:
        """規格 §25.7、chat.md：成功站僅辦理區間車停靠。"""
        station = game_data.stations["CHENGGONG"]
        assert station.allows_train_type("local")
        assert not station.allows_train_type("tze_chiang")

    def test_no_service_stops_a_tze_chiang_at_chenggong(
        self, game_data: GameData
    ) -> None:
        offenders = [
            s.train_number
            for s in game_data.services.values()
            if s.train_type == "tze_chiang" and "CHENGGONG" in s.stop_station_ids
        ]
        assert offenders == []

    def test_express_services_do_pass_through_chenggong(
        self, game_data: GameData
    ) -> None:
        """通過與不經過是兩回事：對號列車確實行經成功站，只是不停。"""
        passing = [
            s.train_number
            for s in game_data.services.values()
            if s.train_type == "tze_chiang"
            and "CHENGGONG" in game_data.routes[s.route_id].station_ids
        ]
        assert passing, "沒有任何對號列車行經成功站，山線經由判斷可能有誤"


class TestNetworkModelling:
    """規格 §10.5：必須建立節點模型，不得只用線性車站清單。"""

    def test_junction_nodes_exist(self, game_data: GameData) -> None:
        node_types = {n.id: n.node_type for n in game_data.network.nodes.values()}
        assert node_types["JCT_CHENGZHUI"] == "junction"
        assert node_types["JCT_CHANGHUA_N"] == "junction"

    def test_junctions_declare_conflict_groups(self, game_data: GameData) -> None:
        """衝突進路需要道岔群組（§13.2）。"""
        assert game_data.network.node("JCT_CHENGZHUI").conflict_group == "SW_CHENGZHUI"
        assert game_data.network.node("JCT_CHANGHUA_N").conflict_group == "SW_CHANGHUA_N"

    def test_links_are_directional(self, game_data: GameData) -> None:
        forward = game_data.network.link("STA_CHENGGONG", "JCT_CHENGZHUI")
        backward = game_data.network.link("JCT_CHENGZHUI", "STA_CHENGGONG")
        assert forward is not None and backward is not None
        assert forward.length_m == backward.length_m

    def test_shortest_path_respects_transitions(self, game_data: GameData) -> None:
        """自動尋路也不能走出倒插禁止的方向。"""
        path = game_data.network.shortest_path("STA_DADU", "STA_CHANGHUA")
        assert path is not None
        assert "STA_CHENGGONG" not in path

    def test_shortest_path_taichung_to_coast_avoids_changhua(
        self, game_data: GameData
    ) -> None:
        path = game_data.network.shortest_path("STA_TAICHUNG", "STA_DADU")
        assert path is not None
        assert "STA_CHANGHUA" not in path
        assert "STA_CHENGGONG" in path
