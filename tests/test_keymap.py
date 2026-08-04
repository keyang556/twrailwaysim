"""鍵位設定測試（規格 §2.1、§7、chat.md 鍵位結論）。"""

from __future__ import annotations

import pytest

from railway_sim.data_loader import GameData
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap, normalise_key


@pytest.fixture
def driver_keymap(game_data: GameData) -> Keymap:
    return Keymap.from_dict(game_data.keymap_raw, "driver")


class TestNoConflicts:
    """快捷鍵不得互相衝突（規格 §2.1、§7.1）。"""

    def test_driver_profile_has_no_conflicts(self, driver_keymap: Keymap) -> None:
        conflicts = driver_keymap.conflicts()
        assert conflicts == [], [str(c) for c in conflicts]

    def test_conflict_detection_actually_works(self) -> None:
        """確認衝突檢查不是永遠回傳空清單。"""
        broken = Keymap.from_dict(
            {
                "profiles": {
                    "driver": {
                        "power_up": {"keys": ["D"], "label": "電門"},
                        "horn": {"keys": ["D"], "label": "鳴笛"},
                    }
                }
            },
            "driver",
        )
        conflicts = broken.conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].key == "D"
        assert set(conflicts[0].actions) == {"power_up", "horn"}

    def test_global_keys_are_included_in_conflict_check(self, game_data: GameData) -> None:
        """global 區段的鍵位必須一併納入檢查。"""
        raw = {
            "profiles": {
                "global": {"show_help": {"keys": ["F1"], "label": "說明"}},
                "driver": {"horn": {"keys": ["F1"], "label": "鳴笛"}},
            }
        }
        assert Keymap.from_dict(raw, "driver").conflicts() != []


class TestChatMdBindings:
    """chat.md 已確認的鍵位結論（衝突時以 chat.md 為準）。"""

    def test_d_is_power(self, driver_keymap: Keymap) -> None:
        assert driver_keymap.action_for("D") == "power_up"

    def test_a_is_brake(self, driver_keymap: Keymap) -> None:
        assert driver_keymap.action_for("A") == "brake_up"

    def test_emergency_brake_uses_a_dedicated_key(self, driver_keymap: Keymap) -> None:
        """chat.md：緊急制軔使用獨立按鍵。"""
        keys = driver_keymap.keys_for("emergency_brake")
        assert keys == ("SPACE",)
        assert driver_keymap.keys_for("brake_up") != keys
        assert driver_keymap.keys_for("power_up") != keys

    def test_binding_sources_are_recorded(self, driver_keymap: Keymap) -> None:
        """每個鍵位都要記錄依據，方便追溯衝突處理。"""
        for action in ("power_up", "brake_up", "emergency_brake"):
            binding = driver_keymap.binding_for(action)
            assert binding is not None
            assert "chat.md" in binding.source


class TestSpecBindings:
    """規格 §7.1 的鍵位表。"""

    @pytest.mark.parametrize(
        ("key", "action"),
        [
            ("D", "power_up"),
            ("A", "brake_up"),
            ("S", "notch_down"),
            ("SPACE", "emergency_brake"),
            ("R", "release_brake"),
            ("H", "horn"),
            ("V", "announce_speed"),
            ("P", "announce_position"),
            ("N", "announce_next_station"),
            ("G", "announce_signal"),
            ("T", "announce_train_status"),
            ("F1", "show_help"),
            ("ESC", "pause_menu"),
        ],
    )
    def test_default_bindings(self, driver_keymap: Keymap, key: str, action: str) -> None:
        assert driver_keymap.action_for(key) == action

    def test_emergency_release_has_a_key(self, driver_keymap: Keymap) -> None:
        """規格 §8.3：必須有明確的緊急制軔解除流程。"""
        assert driver_keymap.keys_for("release_emergency") == ("E",)


class TestKeyNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("d", "D"), ("D", "D"), (" ", "SPACE"), ("\x1b", "ESC"), ("f1", "F1")],
    )
    def test_normalise(self, raw: str, expected: str) -> None:
        assert normalise_key(raw) == expected

    def test_lowercase_key_still_dispatches(self, driver_keymap: Keymap) -> None:
        assert driver_keymap.action_for("d") == "power_up"

    def test_empty_key(self) -> None:
        assert normalise_key("") == ""


class TestHelpText:
    """所有快捷鍵必須可查詢（規格 §2.1）。"""

    def test_every_action_appears_in_help(self, driver_keymap: Keymap) -> None:
        text = driver_keymap.help_text()
        for binding in driver_keymap.bindings:
            assert binding.label in text

    def test_help_is_grouped_by_category(self, driver_keymap: Keymap) -> None:
        lines = driver_keymap.help_lines()
        assert "【駕駛操作】" in lines
        assert "【狀態查詢】" in lines
        assert "【系統】" in lines

    def test_space_is_displayed_readably(self, driver_keymap: Keymap) -> None:
        binding = driver_keymap.binding_for("emergency_brake")
        assert binding is not None
        assert binding.keys_text == "空白鍵"


class TestDispatcher:
    """按鍵派送（規格 §7.2）。"""

    def test_dispatch_calls_handler(self, driver_keymap: Keymap) -> None:
        calls: list[str] = []
        dispatcher = KeyDispatcher(driver_keymap)
        dispatcher.register("power_up", lambda: calls.append("power"))

        result = dispatcher.dispatch("D")
        assert result.handled
        assert result.action == "power_up"
        assert calls == ["power"]

    def test_unbound_key_reports_reason(self, driver_keymap: Keymap) -> None:
        """未綁定的按鍵也要有回饋，不可靜默（規格 §7.2）。"""
        result = KeyDispatcher(driver_keymap).dispatch("Z")
        assert not result.handled
        assert result.reason == "unbound_key"

    def test_registering_unknown_action_raises(self, driver_keymap: Keymap) -> None:
        with pytest.raises(KeyError):
            KeyDispatcher(driver_keymap).register("open_doors", lambda: None)

    def test_all_driver_actions_have_handlers(
        self, driver_keymap: Keymap, local_session
    ) -> None:
        """鍵位表中的每個動作都必須有對應實作，不能有空按鍵。"""
        dispatcher = KeyDispatcher(driver_keymap)
        dispatcher.register_all(local_session.action_handlers())
        dispatcher.register_all(
            {
                "show_help": lambda: None,
                "repeat_last": lambda: None,
                "pause_menu": lambda: None,
            }
        )
        assert dispatcher.unbound_actions() == []
