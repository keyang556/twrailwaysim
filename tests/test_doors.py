"""車門操作與連鎖測試（規格 §16.2、§20.2）。

鍵位取自 OpenBVE：``DOORS_LEFT`` 為 F5、``DOORS_RIGHT`` 為 F6，同一個鍵
開也關。
"""

from __future__ import annotations

from railway_sim.accessibility.announcer import Announcer
from railway_sim.data_loader import GameData
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession
from railway_sim.simulation import braking, doors
from railway_sim.simulation.train import Train, TrainType


class TestDoorRules:
    def test_open_requires_the_train_to_be_stopped(self, train: Train) -> None:
        train.current_speed_kmh = 20.0
        result = doors.open_doors(train, "left")
        assert not result.accepted
        assert result.reason == "not_stopped"
        assert train.left_doors_open is False

    def test_open_when_stopped(self, train: Train) -> None:
        assert doors.open_doors(train, "left").accepted
        assert train.left_doors_open is True
        assert train.right_doors_open is False

    def test_close_is_always_allowed(self, train: Train) -> None:
        train.left_doors_open = True
        train.current_speed_kmh = 40.0
        result = doors.close_doors(train, "left")
        assert result.accepted
        assert train.left_doors_open is False

    def test_opening_an_already_open_side_changes_nothing(self, train: Train) -> None:
        doors.open_doors(train, "right")
        result = doors.open_doors(train, "right")
        assert not result.accepted
        assert result.reason == "already"
        assert train.right_doors_open is True

    def test_unknown_side_is_rejected(self, train: Train) -> None:
        result = doors.set_doors(train, "上面", opening=True)
        assert not result.accepted
        assert result.reason == "unknown_side"

    def test_any_door_open_reports_either_side(self, train: Train) -> None:
        assert train.any_door_open is False
        train.right_doors_open = True
        assert train.any_door_open is True


class TestDoorInterlock:
    """車門開啟中不得起動（§16.2：關門確認在出發之前）。"""

    def test_power_is_blocked_while_a_door_is_open(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.left_doors_open = True
        result = braking.power_up(train, train_spec)
        assert not result.accepted
        assert result.reason == "doors_open"
        assert train.power_notch == 0

    def test_blocked_power_does_not_release_the_brake(
        self, train: Train, train_spec: TrainType
    ) -> None:
        """被擋下的操作不可以順手把制軔放掉，否則停在站內的列車會溜逸。"""
        train.brake_notch = 3
        train.left_doors_open = True
        braking.power_up(train, train_spec)
        assert train.brake_notch == 3

    def test_power_works_again_after_closing(
        self, train: Train, train_spec: TrainType
    ) -> None:
        train.left_doors_open = True
        braking.power_up(train, train_spec)
        doors.close_doors(train, "left")
        assert braking.power_up(train, train_spec).accepted
        assert train.power_notch == 1


class TestDriverSessionDoors:
    """接到運轉工作階段之後的行為（含播報，§7.2）。"""

    def _session(self, game_data: GameData) -> tuple[DriverSession, Announcer]:
        announcer = Announcer(dedupe_seconds=0.0)
        session = DriverSession(
            data=game_data, service=game_data.service("2115"), announcer=announcer
        )
        return session, announcer

    def test_toggle_opens_then_closes(self, game_data: GameData) -> None:
        session, announcer = self._session(game_data)
        session.toggle_left_doors()
        assert session.train.left_doors_open is True
        session.toggle_left_doors()
        assert session.train.left_doors_open is False

        announcer.flush()
        assert "左側車門開啟。" in announcer.texts()
        assert "左側車門關閉。" in announcer.texts()

    def test_sides_are_independent(self, game_data: GameData) -> None:
        session, _ = self._session(game_data)
        session.toggle_right_doors()
        assert session.train.right_doors_open is True
        assert session.train.left_doors_open is False

    def test_opening_while_moving_is_refused_with_feedback(
        self, game_data: GameData
    ) -> None:
        session, announcer = self._session(game_data)
        session.train.current_speed_kmh = 60.0
        session.toggle_left_doors()

        announcer.flush()
        assert session.train.left_doors_open is False
        assert "列車尚未停妥，無法開啟左側車門。" in announcer.texts()

    def test_power_up_while_open_is_refused_with_feedback(
        self, game_data: GameData
    ) -> None:
        session, announcer = self._session(game_data)
        session.toggle_left_doors()
        session.power_up()

        announcer.flush()
        assert session.train.power_notch == 0
        assert "車門開啟中，無法加電門。請先關閉車門。" in announcer.texts()

    def test_door_status_is_queryable(self, game_data: GameData) -> None:
        session, _ = self._session(game_data)
        assert session.status_item("doors").text == "車門：全部關閉。"
        session.toggle_left_doors()
        session.toggle_right_doors()
        assert session.status_item("doors").text == "車門：左側、右側開啟中。"

    def test_door_state_is_in_the_full_status_text(self, game_data: GameData) -> None:
        session, _ = self._session(game_data)
        session.toggle_left_doors()
        assert "車門：左側開啟中。" in session.status_text()


class TestKeyBindings:
    """鍵位與 OpenBVE 相同（assets/Controls/Default.controls）。"""

    def test_doors_use_f5_and_f6(self, game_data: GameData) -> None:
        keymap = Keymap.from_dict(game_data.keymap_raw, "driver")
        assert keymap.action_for("F5") == "doors_left"
        assert keymap.action_for("F6") == "doors_right"

    def test_both_profiles_bind_the_door_keys(self, game_data: GameData) -> None:
        for profile in ("driver", "driver_legacy"):
            keymap = Keymap.from_dict(game_data.keymap_raw, profile)
            assert keymap.keys_for("doors_left") == ("F5",)
            assert keymap.keys_for("doors_right") == ("F6",)
