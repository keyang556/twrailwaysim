"""聯鎖與衝突進路測試（規格 §13、§23.1）。"""

from __future__ import annotations

import pytest

from railway_sim.railway.interlocking import Interlocking, RouteRequest
from railway_sim.roles.driver import DriverSession


@pytest.fixture
def interlocking(local_session: DriverSession) -> Interlocking:
    return local_session.interlocking


def make_request(
    session: DriverSession,
    request_id: str,
    train_id: str,
    from_node: str,
    to_node: str,
) -> RouteRequest:
    return RouteRequest(
        id=request_id,
        train_id=train_id,
        route=session.route,
        from_node_id=from_node,
        to_node_id=to_node,
    )


class TestRouteGranting:
    """排列進路前的檢查（規格 §13.1）。"""

    def test_route_granted_when_everything_is_clear(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHANGHUA_N"
        )
        result = interlocking.request_route(request)
        assert result.granted
        assert interlocking.active_route_ids() == ["RT1"]

    def test_route_rejected_when_block_occupied(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        local_session.add_obstruction("T_OTHER", 12500.0)
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHANGHUA_N"
        )
        result = interlocking.request_route(request)
        assert not result.granted
        assert "占用" in str(result.reason)

    def test_route_rejected_when_direction_is_wrong(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        """列車方向必須正確（規格 §13.1 第 6 項）。"""
        request = make_request(
            local_session, "RT1", "T2701", "JCT_CHANGHUA_N", "STA_CHENGGONG"
        )
        result = interlocking.request_route(request)
        assert not result.granted
        assert "方向" in str(result.reason)

    def test_duplicate_route_id_rejected(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        """進路建立後不得任意改變（規格 §13.2）。"""
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        assert interlocking.request_route(request).granted
        second = interlocking.request_route(request)
        assert not second.granted
        assert "已存在" in str(second.reason)


class TestConflictingRoutes:
    """衝突進路不可同時開放（規格 §13.2、§23.1）。"""

    def test_two_routes_sharing_a_switch_conflict(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(
            local_session, "RT1", "T_A", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        assert interlocking.request_route(first).granted

        second = make_request(
            local_session, "RT2", "T_B", "STA_XINWURI", "JCT_CHENGZHUI"
        )
        result = interlocking.request_route(second)
        assert not result.granted
        assert "衝突" in str(result.reason)

    def test_conflict_names_the_shared_switch(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(
            local_session, "RT1", "T_A", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        interlocking.request_route(first)
        second = make_request(
            local_session, "RT2", "T_B", "STA_XINWURI", "JCT_CHENGZHUI"
        )
        assert "SW_CHENGZHUI" in str(interlocking.request_route(second).reason)

    def test_non_overlapping_routes_do_not_conflict(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(local_session, "RT1", "T_A", "STA_TAICHUNG", "STA_DAGING")
        second = make_request(
            local_session, "RT2", "T_B", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        assert interlocking.request_route(first).granted
        assert interlocking.request_route(second).granted

    def test_conflicts_with_active_lists_the_blocking_route(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(local_session, "RT1", "T_A", "STA_TAICHUNG", "STA_DAGING")
        interlocking.request_route(first)
        overlapping = make_request(
            local_session, "RT2", "T_B", "BND_TAICHUNG_S", "STA_WURI"
        )
        assert interlocking.conflicts_with_active(overlapping) == ["RT1"]


class TestSwitchProtection:
    """列車占用道岔時不可轉換道岔（規格 §13.2）。"""

    def test_switch_occupied_by_train_blocks_route(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        # 讓另一列車橫跨成追線分歧
        local_session.blocks.update_occupancy("T_OTHER", 11650.0, 11750.0)
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHANGHUA_N"
        )
        result = interlocking.request_route(request)
        assert not result.granted
        assert "占用" in str(result.reason)

    def test_switch_occupier_detected_across_boundary(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        local_session.blocks.update_occupancy("T_OTHER", 11600.0, 11699.0)
        occupier = interlocking.switch_occupier(
            local_session.route, "JCT_CHENGZHUI", ignore_train_id="T2701"
        )
        assert occupier == "T_OTHER"

    def test_free_switch_has_no_occupier(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        assert (
            interlocking.switch_occupier(
                local_session.route, "JCT_CHENGZHUI", ignore_train_id="T2701"
            )
            is None
        )


class TestRouteRelease:
    """列車完全通過後才能解除進路（規格 §13.2）。"""

    def test_cannot_release_before_train_clears(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHANGHUA_N"
        )
        interlocking.request_route(request)

        result = interlocking.release_route("RT1", train_rear_m=12000.0)
        assert not result.granted
        assert "尚未完全通過" in str(result.reason)
        assert interlocking.active_route_ids() == ["RT1"]

    def test_release_after_train_clears(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHANGHUA_N"
        )
        interlocking.request_route(request)

        assert interlocking.release_route("RT1", train_rear_m=15800.0).granted
        assert interlocking.active_route_ids() == []

    def test_released_route_frees_the_switch_for_the_other_direction(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(
            local_session, "RT1", "T_A", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        interlocking.request_route(first)
        interlocking.release_route("RT1", train_rear_m=12000.0)

        second = make_request(
            local_session, "RT2", "T_B", "STA_XINWURI", "JCT_CHENGZHUI"
        )
        assert interlocking.request_route(second).granted

    def test_release_unknown_route(self, interlocking: Interlocking) -> None:
        assert not interlocking.release_route("NOPE").granted
