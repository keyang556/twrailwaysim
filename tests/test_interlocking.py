"""聯鎖與衝突進路測試（規格 §13、§23.1）。"""

from __future__ import annotations

import pytest
from conftest import block_midpoint

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
    entry_signal_id: str | None = None,
) -> RouteRequest:
    return RouteRequest(
        id=request_id,
        train_id=train_id,
        route=session.route,
        from_node_id=from_node,
        to_node_id=to_node,
        entry_signal_id=entry_signal_id,
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
        local_session.add_obstruction(
            "T_OTHER",
            block_midpoint(local_session, "BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"),
        )
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
        first = make_request(local_session, "RT1", "T_A", "STA_TAICHUNG", "STA_DAQING")
        second = make_request(
            local_session, "RT2", "T_B", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        assert interlocking.request_route(first).granted
        assert interlocking.request_route(second).granted

    def test_conflicts_with_active_lists_the_blocking_route(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(local_session, "RT1", "T_A", "STA_TAICHUNG", "STA_DAQING")
        interlocking.request_route(first)
        overlapping = make_request(
            local_session, "RT2", "T_B", "STA_WUQUAN", "STA_WURI"
        )
        assert interlocking.conflicts_with_active(overlapping) == ["RT1"]


class TestEntrySignal:
    """未鎖閉道岔不可開放號誌（規格 §13.2）。"""

    #: 成功站南方分歧至彰化北方咽喉的閉塞，其防護號誌位於成功至分歧進路之外。
    UNRELATED_SIGNAL = "SIG_BLK_JCT_CHENGZHUI_JCT_CHANGHUA_N"

    def test_entry_signal_cleared_when_it_protects_the_route(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        signal_id = "SIG_BLK_STA_CHENGGONG_JCT_CHENGZHUI"
        interlocking.signals.signal(signal_id).forced_stop = True
        request = make_request(
            local_session,
            "RT1",
            "T2701",
            "STA_CHENGGONG",
            "JCT_CHENGZHUI",
            entry_signal_id=signal_id,
        )
        assert interlocking.request_route(request).granted
        assert interlocking.signals.signal(signal_id).forced_stop is False

    def test_unrelated_entry_signal_is_rejected(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        """號誌防護的閉塞不在進路鎖定範圍內時不得開放。"""
        interlocking.signals.signal(self.UNRELATED_SIGNAL).forced_stop = True
        request = make_request(
            local_session,
            "RT1",
            "T2701",
            "STA_CHENGGONG",
            "JCT_CHENGZHUI",
            entry_signal_id=self.UNRELATED_SIGNAL,
        )
        result = interlocking.request_route(request)

        assert not result.granted
        assert "不在進路" in str(result.reason)
        assert interlocking.signals.signal(self.UNRELATED_SIGNAL).forced_stop is True
        assert interlocking.active_route_ids() == []

    def test_missing_entry_signal_is_rejected_without_leaving_a_lock(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        """不存在的號誌必須回傳失敗，且不可留下已建立的鎖。"""
        request = make_request(
            local_session,
            "RT1",
            "T2701",
            "STA_CHENGGONG",
            "JCT_CHENGZHUI",
            entry_signal_id="SIG_DOES_NOT_EXIST",
        )
        result = interlocking.request_route(request)

        assert not result.granted
        assert "不存在" in str(result.reason)
        assert interlocking.active_route_ids() == []

    def test_rejected_request_leaves_the_switch_available(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        """被拒絕的申請不可占住資源，後續合法申請仍應成功。"""
        interlocking.request_route(
            make_request(
                local_session,
                "RT1",
                "T2701",
                "STA_CHENGGONG",
                "JCT_CHENGZHUI",
                entry_signal_id="SIG_DOES_NOT_EXIST",
            )
        )
        assert interlocking.request_route(
            make_request(local_session, "RT2", "T_B", "STA_CHENGGONG", "JCT_CHENGZHUI")
        ).granted

    def test_release_restores_the_entry_signal_to_stop(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        signal_id = "SIG_BLK_STA_CHENGGONG_JCT_CHENGZHUI"
        request = make_request(
            local_session,
            "RT1",
            "T2701",
            "STA_CHENGGONG",
            "JCT_CHENGZHUI",
            entry_signal_id=signal_id,
        )
        interlocking.request_route(request)
        rear_m = local_session.route.offset_of("JCT_CHENGZHUI") + 100.0
        interlocking.release_route("RT1", train_rear_m=rear_m)
        assert interlocking.signals.signal(signal_id).forced_stop is True


class TestSwitchProtection:
    """列車占用道岔時不可轉換道岔（規格 §13.2）。"""

    def test_switch_occupied_by_train_blocks_route(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        # 讓另一列車橫跨成追線分歧
        junction_m = local_session.route.offset_of("JCT_CHENGZHUI")
        local_session.blocks.update_occupancy(
            "T_OTHER", junction_m - 50.0, junction_m + 50.0
        )
        request = make_request(
            local_session, "RT1", "T2701", "STA_CHENGGONG", "JCT_CHANGHUA_N"
        )
        result = interlocking.request_route(request)
        assert not result.granted
        assert "占用" in str(result.reason)

    def test_switch_occupier_detected_across_boundary(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        junction_m = local_session.route.offset_of("JCT_CHENGZHUI")
        local_session.blocks.update_occupancy(
            "T_OTHER", junction_m - 100.0, junction_m - 1.0
        )
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

        rear_m = local_session.route.offset_of("JCT_CHANGHUA_N") - 100.0
        result = interlocking.release_route("RT1", train_rear_m=rear_m)
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

        rear_m = local_session.route.offset_of("JCT_CHANGHUA_N") + 100.0
        assert interlocking.release_route("RT1", train_rear_m=rear_m).granted
        assert interlocking.active_route_ids() == []

    def test_released_route_frees_the_switch_for_the_other_direction(
        self, local_session: DriverSession, interlocking: Interlocking
    ) -> None:
        first = make_request(
            local_session, "RT1", "T_A", "STA_CHENGGONG", "JCT_CHENGZHUI"
        )
        interlocking.request_route(first)
        rear_m = local_session.route.offset_of("JCT_CHENGZHUI") + 100.0
        interlocking.release_route("RT1", train_rear_m=rear_m)

        second = make_request(
            local_session, "RT2", "T_B", "STA_XINWURI", "JCT_CHENGZHUI"
        )
        assert interlocking.request_route(second).granted

    def test_release_unknown_route(self, interlocking: Interlocking) -> None:
        assert not interlocking.release_route("NOPE").granted
