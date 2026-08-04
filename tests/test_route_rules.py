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
                "STA_DAGING",
                "BND_TAICHUNG_S",
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
                "BND_TAICHUNG_S",
                "STA_DAGING",
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
                "BND_TAICHUNG_S",
                "STA_DAGING",
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
    """規格 §10 的四條正確路徑都必須存在且合法。"""

    def test_coast_to_changhua_route(self, game_data: GameData) -> None:
        route = game_data.route("R_CO_SB_DAJIA_CHANGHUA")
        assert "CHENGGONG" not in route.station_ids
        assert "ZHUIFEN" in route.station_ids
        assert route.station_ids[-1] == "CHANGHUA"

    def test_coast_to_taichung_route(self, game_data: GameData) -> None:
        route = game_data.route("R_CO_TO_TAICHUNG")
        assert "CHANGHUA" not in route.station_ids
        assert route.station_ids.index("ZHUIFEN") < route.station_ids.index("CHENGGONG")
        assert route.station_ids[-1] == "TAICHUNG"

    def test_taichung_to_coast_route(self, game_data: GameData) -> None:
        route = game_data.route("R_TAICHUNG_TO_COAST")
        assert "CHANGHUA" not in route.station_ids
        assert route.station_ids.index("CHENGGONG") < route.station_ids.index("ZHUIFEN")

    def test_changhua_to_coast_route(self, game_data: GameData) -> None:
        route = game_data.route("R_CHANGHUA_TO_COAST")
        assert "CHENGGONG" not in route.station_ids
        assert route.station_ids[0] == "CHANGHUA"
        assert "ZHUIFEN" in route.station_ids

    def test_mountain_route_avoids_zhuifen(self, game_data: GameData) -> None:
        for route_id in ("R_MT_SB_TAICHUNG_CHANGHUA", "R_MT_NB_CHANGHUA_FONGYUAN"):
            route = game_data.route(route_id)
            assert "ZHUIFEN" not in route.station_ids
            assert "CHENGGONG" in route.station_ids


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
